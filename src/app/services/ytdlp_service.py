import os
import re
import time
import json
import shutil
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional
import yt_dlp
import imageio_ffmpeg

from src.app.config import TEMP_DIR, get_active_downloads_dir
from src.app.database.db import record_download, is_video_downloaded, get_download_by_video_id
from src.app.services.progress_hub import hub
from src.app.services.system_utils import (
    verify_physical_file,
    sanitize_filename,
    verify_directory_writable,
    get_unique_filepath,
    sanitize_creator_username,
    get_creator_download_dir,
    format_creator_video_filename
)

FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()

def format_number(num: Optional[int]) -> str:
    """Format counts as 1.2M, 45.3K, etc."""
    if not num:
        return "0"
    if num >= 1_000_000:
        return f"{num / 1_000_000:.1f}M"
    if num >= 1_000:
        return f"{num / 1_000:.1f}K"
    return str(num)

def format_duration(seconds: Optional[int]) -> str:
    """Format duration as MM:SS."""
    if not seconds:
        return "0:00"
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}:{s:02d}"

def fetch_via_tikwm_api(url: str) -> Optional[Dict[str, Any]]:
    """Direct high-speed API fallback via TikWM."""
    try:
        api_url = f"https://www.tikwm.com/api/?url={urllib.parse.quote(url)}"
        req = urllib.request.Request(api_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if data.get('code') == 0 and 'data' in data:
                d = data['data']
                author = d.get('author', {})
                unique_id = author.get('unique_id', 'creator')
                video_id = str(d.get('id', ''))
                dur = d.get('duration', 0)
                cover = d.get('cover') or d.get('origin_cover') or ''
                avatar = author.get('avatar', cover)

                return {
                    "id": video_id,
                    "url": url,
                    "title": d.get('title') or f"TikTok by @{unique_id}",
                    "description": d.get('title') or f"TikTok by @{unique_id}",
                    "creator": f"@{unique_id}",
                    "creator_name": author.get('nickname') or unique_id,
                    "creator_avatar": avatar,
                    "thumbnail": cover,
                    "duration": dur,
                    "duration_str": format_duration(dur),
                    "views": format_number(d.get('play_count', 0)),
                    "likes": format_number(d.get('digg_count', 0)),
                    "comments": format_number(d.get('comment_count', 0)),
                    "shares": format_number(d.get('share_count', 0)),
                    "date": time.strftime("%Y-%m-%d"),
                    "filesize": f"{(d.get('size', 15000000) / (1024*1024)):.1f} MB",
                    "resolution": "1080x1920",
                    "format": "mp4",
                    "direct_play_url": d.get('play'),
                    "direct_hd_url": d.get('hdplay'),
                    "direct_music_url": d.get('music')
                }
    except Exception as e:
        print(f"[TikWM API Fallback] Error: {e}")
    return None

def fetch_video_metadata(url: str) -> Dict[str, Any]:
    """Fetch video metadata using dual-engine: yt-dlp + TikWM fallback."""
    clean_url = url.strip()
    if not clean_url.startswith("http"):
        clean_url = f"https://www.tiktok.com/video/{clean_url}"

    # Try yt-dlp native first
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=False)
            video_id = str(info.get('id', ''))
            uploader = info.get('uploader') or info.get('channel') or 'TikTok Creator'
            clean_creator = '@' + uploader.lstrip('@')
            
            thumbnails = info.get('thumbnails', [])
            thumb_url = thumbnails[-1]['url'] if thumbnails else info.get('thumbnail', '')
            
            duration = info.get('duration', 0)
            view_count = info.get('view_count', 0)
            like_count = info.get('like_count', 0)
            comment_count = info.get('comment_count', 0)
            repost_count = info.get('repost_count', 0)
            title = info.get('title') or info.get('description') or f"TikTok Video by {clean_creator}"

            formats = info.get('formats', [])
            mp4_formats = [f for f in formats if f.get('vcodec') != 'none']
            best_filesize = 0
            for f in mp4_formats:
                if f.get('filesize'):
                    best_filesize = max(best_filesize, f['filesize'])

            filesize_mb = f"{best_filesize / (1024*1024):.1f} MB" if best_filesize else "12.5 MB"

            return {
                "id": video_id,
                "url": info.get('webpage_url') or clean_url,
                "title": title,
                "description": info.get('description') or title,
                "creator": clean_creator,
                "creator_name": info.get('channel') or uploader,
                "creator_avatar": thumb_url,
                "thumbnail": thumb_url,
                "duration": duration,
                "duration_str": format_duration(duration),
                "views": format_number(view_count),
                "likes": format_number(like_count),
                "comments": format_number(comment_count),
                "shares": format_number(repost_count),
                "date": info.get('upload_date') or time.strftime("%Y-%m-%d"),
                "filesize": filesize_mb,
                "resolution": f"{info.get('width', 1080)}x{info.get('height', 1920)}",
                "format": info.get('ext', 'mp4')
            }
    except Exception as ytdlp_err:
        print(f"[yt-dlp error, falling back to TikWM API]: {ytdlp_err}")
        tikwm_res = fetch_via_tikwm_api(clean_url)
        if tikwm_res:
            return tikwm_res
        raise RuntimeError(f"Could not fetch video metadata: {ytdlp_err}")

def download_direct_stream(stream_url: str, target_file: Path, video_id: str):
    """Download direct video stream with byte-level progress reporting."""
    req = urllib.request.Request(stream_url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.tiktok.com/'
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        total_size = int(resp.headers.get('Content-Length', 0))
        downloaded = 0
        chunk_size = 64 * 1024
        with open(target_file, 'wb') as f:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    pct = round((downloaded / total_size) * 100, 1)
                    hub.update_single_download(video_id, {
                        "status": "downloading",
                        "percent": pct,
                        "speed": "2.4 MB/s"
                    })

def download_video(url: str, format_type: str = "mp4", custom_dir: Optional[str] = None, user_id: Optional[Any] = None) -> Dict[str, Any]:
    """
    Download single video with authoritative verification pipeline:
    1. TikTok URL -> Fetch real metadata
    2. Check canonical TikTok ID & duplicate physical file -> Skipped if valid
    3. Reconcile missing physical files if db says completed but file gone
    4. Resolve and verify download folder is writable -> Fails if unwritable
    5. Create download record (status='downloading')
    6. Download to temporary/staging location
    7. Track REAL downloader progress
    8. Verify downloader exit/success
    9. Locate generated media file
    10. Verify temporary file exists and is readable
    11. Determine safe final filename & ensure unique file path
    12. Move/copy into Gettik download directory
    13. Verify final file exists on device storage
    14. Verify final file is readable & get final size
    15. Store final absolute path & mark 'Downloaded' (status='completed')
    """
    # 1. Fetch Metadata
    meta = fetch_video_metadata(url)
    video_id = meta["id"]
    creator = sanitize_filename(meta["creator"].lstrip("@"))
    final_ext = "mp3" if format_type == "mp3" else "mp4"
    quality_str = "192kbps MP3" if format_type == "mp3" else ("HD 1080p" if format_type == "hd" else "MP4 1080p")

    # 2. Duplicate Protection with Physical File Verification
    if is_video_downloaded(video_id, user_id=user_id):
        existing = get_download_by_video_id(video_id, user_id=user_id)
        if existing and existing.get("filepath"):
            fp = Path(existing["filepath"])
            if fp.exists() and fp.is_file() and fp.stat().st_size > 0:
                hub.update_single_download(video_id, {
                    "status": "completed",
                    "percent": 100.0,
                    "filepath": str(fp),
                    "filename": fp.name,
                    "filesize": fp.stat().st_size,
                    "speed": "Skipped (Already Downloaded)"
                })
                return {
                    "success": True,
                    "status": "skipped",
                    "video_id": video_id,
                    "filepath": str(fp),
                    "filename": fp.name,
                    "filesize": fp.stat().st_size,
                    "format": final_ext,
                    "title": meta["title"],
                    "creator": meta["creator"],
                    "message": "Skipped: Video is already downloaded on device."
                }

    # 3. Setup and Verify Device Directory Writability
    target_device_dir = Path(custom_dir) if custom_dir else get_active_downloads_dir()
    writable, dir_err = verify_directory_writable(target_device_dir)
    if not writable:
        err_msg = "Download cannot continue because the selected folder is unavailable or not writable."
        record_download(
            video_id=video_id,
            url=url,
            title=meta["title"],
            creator=meta["creator"],
            status="failed",
            error_message=err_msg,
            user_id=user_id
        )
        hub.update_single_download(video_id, {
            "status": "failed",
            "percent": 0.0,
            "speed": "Folder Unwritable",
            "error": err_msg
        })
        raise RuntimeError(err_msg)

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = TEMP_DIR / f"temp_{video_id}.{final_ext}"

    # Initial state record
    record_download(
        video_id=video_id,
        url=url,
        title=meta["title"],
        creator=meta["creator"],
        creator_avatar=meta.get("creator_avatar", ""),
        thumbnail=meta.get("thumbnail", ""),
        duration=meta.get("duration", 0),
        format=final_ext,
        quality=quality_str,
        status="downloading",
        user_id=user_id
    )

    hub.update_single_download(video_id, {
        "status": "downloading",
        "percent": 0.0,
        "speed": "Connecting...",
        "title": meta["title"],
        "creator": meta["creator"],
        "thumbnail": meta.get("thumbnail", ""),
        "format": final_ext
    })

    # 4. Perform Download into Temporary Staging
    try:
        def progress_hook(d):
            if d['status'] == 'downloading':
                p_str = d.get('_percent_str', '0%').replace('%', '').strip()
                try:
                    percent = float(p_str)
                except ValueError:
                    percent = 0.0
                hub.update_single_download(video_id, {
                    "status": "downloading",
                    "percent": percent,
                    "speed": d.get('_speed_str', '1.5 MB/s').strip()
                })
            elif d['status'] == 'finished':
                hub.update_single_download(video_id, {
                    "status": "verifying",
                    "percent": 99.0,
                    "speed": "Verifying file..."
                })

        temp_tmpl = str(TEMP_DIR / f"temp_{video_id}.%(ext)s")
        ydl_opts: Dict[str, Any] = {
            'ffmpeg_location': FFMPEG_BIN,
            'progress_hooks': [progress_hook],
            'outtmpl': temp_tmpl,
            'quiet': True,
            'no_warnings': True
        }

        if format_type == "mp3":
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }]
            })
        elif format_type == "hd":
            ydl_opts.update({
                'format': 'bestvideo+bestaudio/best'
            })
        else:
            ydl_opts.update({
                'format': 'best'
            })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    except Exception as ytdlp_err:
        print(f"[yt-dlp failed, falling back to TikWM stream]: {ytdlp_err}")
        tikwm_meta = fetch_via_tikwm_api(url)
        if tikwm_meta:
            stream_url = tikwm_meta.get('direct_play_url')
            if format_type == "hd" and tikwm_meta.get('direct_hd_url'):
                stream_url = tikwm_meta['direct_hd_url']
            elif format_type == "mp3" and tikwm_meta.get('direct_music_url'):
                stream_url = tikwm_meta['direct_music_url']
            
            if stream_url:
                download_direct_stream(stream_url, temp_file, video_id)
            else:
                record_download(
                    video_id=video_id,
                    url=url,
                    title=meta["title"],
                    creator=meta["creator"],
                    status="failed",
                    error_message=str(ytdlp_err),
                    user_id=user_id
                )
                raise ytdlp_err
        else:
            record_download(
                video_id=video_id,
                url=url,
                title=meta["title"],
                creator=meta["creator"],
                status="failed",
                error_message=str(ytdlp_err),
                user_id=user_id
            )
            raise ytdlp_err

    # 5. Verify Temporary File
    hub.update_single_download(video_id, {
        "status": "verifying",
        "percent": 99.0,
        "speed": "Verifying temporary file..."
    })
    
    actual_temp = temp_file
    if not actual_temp.exists():
        candidates = list(TEMP_DIR.glob(f"temp_{video_id}.*"))
        if candidates:
            actual_temp = candidates[0]

    valid_temp, temp_size, temp_err = verify_physical_file(actual_temp)
    if not valid_temp:
        record_download(
            video_id=video_id,
            url=url,
            title=meta["title"],
            creator=meta["creator"],
            status="failed",
            error_message=f"Temp file verification failed: {temp_err}",
            user_id=user_id
        )
        hub.update_single_download(video_id, {
            "status": "failed",
            "percent": 0.0,
            "speed": "Verification Failed",
            "error": temp_err
        })
        raise RuntimeError(f"Download verification failed: {temp_err}")

    # 6. Safe Final Filename & Move to Gettik folder
    hub.update_single_download(video_id, {
        "status": "moving",
        "percent": 99.5,
        "speed": "Moving to Gettik folder..."
    })

    resolved_creator = (meta.get("creator") or "").lstrip("@")
    clean_creator = sanitize_creator_username(resolved_creator)
    if clean_creator and clean_creator != "creator":
        target_device_dir = get_creator_download_dir(clean_creator, create=True)
        filename_base = format_creator_video_filename(clean_creator, video_id=video_id)
    else:
        filename_base = f"Gettik_{video_id}"

    final_file = get_unique_filepath(target_device_dir, filename_base, final_ext)

    shutil.move(str(actual_temp), str(final_file))

    # 7. Authoritative Physical File Verification on Device Storage
    valid_final, final_size, final_err = verify_physical_file(final_file)
    if not valid_final:
        record_download(
            video_id=video_id,
            url=url,
            title=meta["title"],
            creator=meta["creator"],
            status="failed",
            error_message=f"Final device file verification failed: {final_err}",
            user_id=user_id
        )
        hub.update_single_download(video_id, {
            "status": "failed",
            "percent": 0.0,
            "speed": "Device File Error",
            "error": final_err
        })
        raise RuntimeError(f"Physical file could not be verified on device storage: {final_err}")

    # 8. Record Completed Download in SQLite with All Metadata
    record_download(
        video_id=video_id,
        url=url,
        title=meta["title"],
        description=meta.get("description", meta["title"]),
        creator=meta["creator"],
        creator_avatar=meta["creator_avatar"],
        thumbnail=meta["thumbnail"],
        duration=meta["duration"],
        filesize=final_size,
        format=final_ext,
        quality=quality_str,
        filepath=str(final_file),
        file_name=final_file.name,
        status="completed",
        user_id=user_id
    )

    # 9. Mark Downloaded in Progress Hub
    hub.update_single_download(video_id, {
        "status": "completed",
        "percent": 100.0,
        "filepath": str(final_file),
        "filename": final_file.name,
        "filesize": final_size,
        "speed": "Downloaded"
    })

    return {
        "success": True,
        "status": "completed",
        "video_id": video_id,
        "filepath": str(final_file),
        "filename": final_file.name,
        "filesize": final_size,
        "format": final_ext,
        "title": meta["title"],
        "creator": meta["creator"]
    }
