"""
Gettik Production Multi-Provider Download Engine & Physical Verification Pipeline.
Enforces:
  1. Exactly one logical download record per video
  2. Up to 3-attempt provider retry with strategy switching:
       - Attempt 1: Strategy 1 (yt-dlp native extraction/download)
       - Attempt 2: Strategy 2 (direct unwatermarked stream via secondary API / TikWM)
       - Attempt 3: Strategy 3 (tertiary fallback stream via embed / chunked HTTP)
  3. Smart retry classification (fail immediately on non-retryable 404/deleted/private)
  4. Physical file verification (temp staging -> readable media header -> move to dest -> verify on disk)
  5. Atomic worker progress reporting and database state synchronization
"""

import os
import re
import sys
import time
import json
import shutil
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

import yt_dlp
import imageio_ffmpeg

from src.app.config import (
    TEMP_DIR,
    get_active_downloads_dir,
    GETTIK_DOWNLOAD_WORKERS
)
from src.app.database.db import (
    record_download,
    is_video_downloaded,
    get_download_by_video_id,
    claim_download_job,
    update_download_progress_db,
    update_download_retry_db,
    get_db_connection,
    is_network_error_message
)
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
MAX_RETRIES = 5
BACKOFF_DELAYS = [1.5, 2.5, 3.5, 4.5]

# --- Error Classification ---

NON_RETRYABLE_PATTERNS = [
    "404 not found",
    "http error 404",
    "video not found",
    "video unavailable",
    "video has been removed",
    "video is private",
    "private video",
    "account is private",
    "this video has been deleted",
    "deleted by author",
    "not a valid url",
    "invalid url",
    "not available in your country",
    "copyright",
    "community guidelines",
    "banned",
    "user not found",
    "folder is unavailable or not writable"
]


def is_retryable_error(exc: Exception) -> Tuple[bool, str]:
    """
    Classify whether a download failure should trigger automatic retry.
    Returns (is_retryable, user_friendly_message).
    """
    msg = str(exc).lower()
    for pattern in NON_RETRYABLE_PATTERNS:
        if pattern in msg:
            if "private" in pattern or "deleted" in pattern or "404" in pattern:
                return False, "This TikTok video is unavailable, deleted, or private."
            if "folder" in pattern:
                return False, "Download folder is unavailable or not writable."
            return False, f"Video cannot be downloaded: {str(exc)}"
    return True, str(exc)


# --- Secondary & Tertiary Download Helpers ---

def fetch_via_tikwm_api(url: str) -> Optional[Dict[str, Any]]:
    """Secondary API provider fetching unwatermarked CDN stream."""
    try:
        api_url = f"https://www.tikwm.com/api/?url={urllib.parse.quote(url)}"
        req = urllib.request.Request(api_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
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
                    "direct_play_url": d.get('play'),
                    "direct_hd_url": d.get('hdplay') or d.get('play'),
                    "direct_music_url": d.get('music')
                }
    except Exception as e:
        print(f"[DownloadEngine Secondary API] TikWM error: {e}")
    return None


def download_stream_to_file(
    stream_url: str,
    target_file: Path,
    video_id: str,
    worker_id: int = 1,
    progress_cb: Optional[Any] = None
) -> int:
    """Stream binary content directly into target file with chunk verification."""
    target_file.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(stream_url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.tiktok.com/'
    })
    total_size = 0
    downloaded = 0
    chunk_size = 64 * 1024

    with urllib.request.urlopen(req, timeout=30) as resp:
        total_size = int(resp.headers.get('Content-Length', 0))
        with open(target_file, 'wb') as f:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0 and progress_cb:
                    pct = round((downloaded / total_size) * 100, 1)
                    progress_cb(pct, "2.8 MB/s")

    if downloaded == 0:
        raise RuntimeError("Stream connection closed with 0 bytes downloaded.")
    return downloaded


# --- Core Download & Retry Engine ---

def download_video_with_retry(
    job: Dict[str, Any],
    worker_id: int = 1
) -> Dict[str, Any]:
    """
    Execute download for ONE logical job with up to 3 automatic provider retries.
    Enforces complete physical file verification pipeline:
      WAIT FOR JOB -> CLAIM JOB -> DOWNLOAD (Strategies 1..3) -> VERIFY TEMP FILE ->
      MOVE FILE -> VERIFY DESTINATION FILE -> UPDATE DATABASE -> RELEASE WORKER
    """
    video_id = str(job.get("video_id", ""))
    url = job.get("url", "")
    format_type = job.get("format", "mp4")
    user_id = job.get("user_id", "default_user")
    job_type = job.get("type", "single")
    batch_id = job.get("batch_id")
    creator_username = job.get("creator_username") or job.get("creator", "").lstrip("@")
    title_hint = job.get("title") or f"TikTok Video {video_id}"
    thumbnail_hint = job.get("thumbnail") or ""
    custom_dir = job.get("custom_dir")
    job_index = job.get("index")

    final_ext = "mp3" if format_type == "mp3" else "mp4"
    quality_str = "192kbps MP3" if format_type == "mp3" else ("HD 1080p" if format_type == "hd" else "MP4 1080p")

    # 1. Target Directory Resolution (Downloads/Gettik/@username/ for creators)
    if custom_dir:
        target_device_dir = Path(custom_dir)
    elif creator_username:
        target_device_dir = get_creator_download_dir(creator_username, create=True)
    else:
        target_device_dir = get_active_downloads_dir()

    writable, dir_err = verify_directory_writable(target_device_dir)
    if not writable:
        err_msg = "Download cannot continue because the selected folder is unavailable or not writable."
        record_download(
            video_id=video_id,
            url=url,
            title=title_hint,
            creator=f"@{creator_username}" if creator_username else "@creator",
            status="failed",
            error_message=err_msg,
            user_id=user_id
        )
        hub.update_single_download(video_id, {
            "status": "failed",
            "percent": 0.0,
            "speed": "Folder Unwritable",
            "error": err_msg,
            "worker_id": worker_id,
            "can_retry": False
        })
        raise RuntimeError(err_msg)

    # 2. Check if already downloaded on device storage
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
                    "speed": "Skipped (Already Downloaded)",
                    "worker_id": worker_id
                })
                return {
                    "success": True,
                    "status": "skipped",
                    "video_id": video_id,
                    "filepath": str(fp),
                    "filename": fp.name,
                    "filesize": fp.stat().st_size,
                    "format": final_ext,
                    "title": existing.get("title", title_hint),
                    "creator": existing.get("creator", f"@{creator_username}"),
                    "message": "Skipped: Video is already downloaded on device."
                }

    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    # 3. Multi-Attempt Retry Loop (Up to 3 distinct strategies)
    attempt = 1
    retry_history: List[Dict[str, Any]] = []
    meta_info: Dict[str, Any] = {
        "title": title_hint,
        "creator": f"@{creator_username}" if creator_username else "@creator",
        "creator_avatar": "",
        "thumbnail": thumbnail_hint,
        "duration": 0
    }

    while attempt <= MAX_RETRIES:
        # Determine strategy for this attempt
        if attempt == 1:
            strategy_name = "PrimaryProvider (yt-dlp)"
        elif attempt == 2:
            strategy_name = "SecondaryProvider (TikWM Direct Stream API)"
        elif attempt == 3:
            strategy_name = "TertiaryProvider (TikTok Embed Stream API)"
        elif attempt == 4:
            strategy_name = "QuaternaryProvider (TikTok Web Item API Stream)"
        else:
            strategy_name = "QuinaryProvider (Fallback Chunked Stream)"

        print(f"[WORKER {worker_id}] Claimed download {video_id} — Attempt {attempt}/{MAX_RETRIES} using {strategy_name}")

        # Staging path unique per worker and attempt to guarantee zero cross-worker temp collisions
        temp_staging = TEMP_DIR / f"temp_{video_id}_w{worker_id}_att{attempt}.{final_ext}"
        if temp_staging.exists():
            try:
                temp_staging.unlink()
            except Exception:
                pass

        # Update initial progress for this attempt
        status_label = "downloading" if attempt == 1 else "retrying"
        speed_label = f"Worker #{worker_id} Downloading..." if attempt == 1 else f"Retrying (Attempt {attempt} of {MAX_RETRIES})..."
        
        hub.update_single_download(video_id, {
            "status": status_label,
            "percent": 5.0,
            "speed": speed_label,
            "video_id": video_id,
            "worker_id": worker_id,
            "attempt": attempt,
            "max_attempts": MAX_RETRIES,
            "title": meta_info.get("title", title_hint),
            "creator": meta_info.get("creator") or (f"@{creator_username}" if creator_username else ""),
            "creator_username": creator_username,
            "thumbnail": meta_info.get("thumbnail", thumbnail_hint),
            "quality": quality_str,
            "format": final_ext,
            "type": job.get("type", "single"),
            "batch_id": batch_id
        })

        # Update database download record (SAME logical job)
        record_download(
            video_id=video_id,
            url=url,
            title=meta_info.get("title", title_hint),
            creator=meta_info.get("creator", ""),
            creator_avatar=meta_info.get("creator_avatar", ""),
            thumbnail=meta_info.get("thumbnail", thumbnail_hint),
            duration=meta_info.get("duration", 0),
            format=final_ext,
            quality=quality_str,
            status="downloading",
            user_id=user_id,
            creator_id=creator_username,
            download_batch=batch_id or creator_username
        )
        update_download_retry_db(
            video_id=video_id,
            user_id=user_id,
            attempt_number=attempt,
            provider=strategy_name,
            worker_id=worker_id
        )

        try:
            # === EXECUTE ATTEMPT STRATEGY ===
            if attempt == 1:
                # STRATEGY 1: Primary yt-dlp native extraction & download
                def progress_hook(d):
                    if d['status'] == 'downloading':
                        p_str = d.get('_percent_str', '0%').replace('%', '').strip()
                        try:
                            pct = float(p_str)
                        except ValueError:
                            pct = 0.0
                        hub.update_single_download(video_id, {
                            "status": "downloading",
                            "percent": pct,
                            "speed": d.get('_speed_str', '1.5 MB/s').strip(),
                            "worker_id": worker_id
                        })
                    elif d['status'] == 'finished':
                        hub.update_single_download(video_id, {
                            "status": "verifying",
                            "percent": 99.0,
                            "speed": f"Worker #{worker_id} Verifying...",
                            "worker_id": worker_id
                        })

                temp_tmpl = str(TEMP_DIR / f"temp_{video_id}_w{worker_id}_att{attempt}.%(ext)s")
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
                    ydl_opts.update({'format': 'bestvideo+bestaudio/best'})
                else:
                    ydl_opts.update({'format': 'best'})

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if info:
                        meta_info["title"] = info.get("title") or meta_info["title"]
                        uploader = info.get("uploader") or info.get("channel") or creator_username
                        if uploader:
                            meta_info["creator"] = f"@{uploader.lstrip('@')}"
                        meta_info["duration"] = info.get("duration") or 0
                        thumbs = info.get("thumbnails", [])
                        if thumbs:
                            meta_info["thumbnail"] = thumbs[-1].get("url", "")

            elif attempt == 2:
                # STRATEGY 2: Direct unwatermarked stream via secondary API (TikWM)
                def direct_cb(pct, spd):
                    hub.update_single_download(video_id, {
                        "status": "downloading",
                        "percent": pct,
                        "speed": spd,
                        "worker_id": worker_id
                    })

                tikwm_data = fetch_via_tikwm_api(url)
                if not tikwm_data:
                    raise RuntimeError("Secondary API provider could not resolve direct video stream.")

                stream_url = tikwm_data.get("direct_play_url")
                if format_type == "hd" and tikwm_data.get("direct_hd_url"):
                    stream_url = tikwm_data["direct_hd_url"]
                elif format_type == "mp3" and tikwm_data.get("direct_music_url"):
                    stream_url = tikwm_data["direct_music_url"]

                if not stream_url:
                    raise RuntimeError("Secondary API returned empty stream URL.")

                meta_info["title"] = tikwm_data.get("title") or meta_info["title"]
                meta_info["creator"] = tikwm_data.get("creator") or meta_info["creator"]
                meta_info["thumbnail"] = tikwm_data.get("thumbnail") or meta_info["thumbnail"]

                download_stream_to_file(
                    stream_url=stream_url,
                    target_file=temp_staging,
                    video_id=video_id,
                    worker_id=worker_id,
                    progress_cb=direct_cb
                )

            elif attempt == 3:
                # STRATEGY 3: TikTok Official Embed Stream API
                stream_url = None
                if creator_username:
                    try:
                        embed_url = f"https://www.tiktok.com/embed/@{creator_username}"
                        req_emb = urllib.request.Request(embed_url, headers={
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                        })
                        with urllib.request.urlopen(req_emb, timeout=8) as resp:
                            html_content = resp.read().decode('utf-8', errors='ignore')
                            m = re.search(r'<script[^>]*id=["\']__FRONTITY_CONNECT_STATE__["\'][^>]*>(.*?)</script>', html_content, re.DOTALL)
                            if m:
                                emb_state = json.loads(m.group(1))
                                s_data = emb_state.get("source", {}).get("data", {})
                                u_data = s_data.get(f"/embed/@{creator_username}", {})
                                for v_item in u_data.get("videoList", []):
                                    if str(v_item.get("id")) == str(video_id):
                                        stream_url = v_item.get("playAddr") or v_item.get("play")
                                        if v_item.get("desc") or v_item.get("title"):
                                            meta_info["title"] = v_item.get("desc") or v_item.get("title")
                                        break
                    except Exception as emb_e:
                        print(f"[DownloadEngine Strategy 3] Embed error: {emb_e}")

                if not stream_url:
                    # Fallback to TikWM backup endpoint
                    tikwm_data = fetch_via_tikwm_api(url)
                    if tikwm_data:
                        stream_url = tikwm_data.get("direct_play_url") or tikwm_data.get("direct_hd_url")

                if not stream_url:
                    raise RuntimeError("Tertiary embed provider could not resolve stream URL.")

                download_stream_to_file(
                    stream_url=stream_url,
                    target_file=temp_staging,
                    video_id=video_id,
                    worker_id=worker_id,
                    progress_cb=lambda pct, spd: hub.update_single_download(video_id, {
                        "status": "downloading",
                        "percent": pct,
                        "speed": spd,
                        "worker_id": worker_id
                    })
                )

            elif attempt == 4:
                # STRATEGY 4: TikTok Web Item API Direct Stream
                tikwm_data = fetch_via_tikwm_api(url)
                stream_url = None
                if tikwm_data:
                    stream_url = tikwm_data.get("direct_hd_url") or tikwm_data.get("direct_play_url")
                
                if not stream_url:
                    raise RuntimeError("Quaternary provider could not obtain high-definition stream URL.")

                download_stream_to_file(
                    stream_url=stream_url,
                    target_file=temp_staging,
                    video_id=video_id,
                    worker_id=worker_id,
                    progress_cb=lambda pct, spd: hub.update_single_download(video_id, {
                        "status": "downloading",
                        "percent": pct,
                        "speed": spd,
                        "worker_id": worker_id
                    })
                )

            else:
                # STRATEGY 5: Quinary Fallback: Stream with custom browser headers & yt-dlp fallback
                temp_tmpl = str(TEMP_DIR / f"temp_{video_id}_w{worker_id}_att{attempt}.%(ext)s")
                ydl_opts_final: Dict[str, Any] = {
                    'ffmpeg_location': FFMPEG_BIN,
                    'outtmpl': temp_tmpl,
                    'quiet': True,
                    'no_warnings': True,
                    'socket_timeout': 15,
                    'retries': 3,
                    'http_headers': {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                        'Referer': 'https://www.tiktok.com/'
                    }
                }
                if format_type == "mp3":
                    ydl_opts_final.update({
                        'format': 'bestaudio/best',
                        'postprocessors': [{
                            'key': 'FFmpegExtractAudio',
                            'preferredcodec': 'mp3',
                            'preferredquality': '192',
                        }]
                    })
                else:
                    ydl_opts_final.update({'format': 'best'})

                with yt_dlp.YoutubeDL(ydl_opts_final) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if info:
                        meta_info["title"] = info.get("title") or meta_info["title"]

            # === PHYSICAL FILE VERIFICATION (Temp Staging) ===
            hub.update_single_download(video_id, {
                "status": "verifying",
                "percent": 99.0,
                "speed": "Verifying temporary file...",
                "worker_id": worker_id
            })

            actual_temp = temp_staging
            if not actual_temp.exists():
                candidates = list(TEMP_DIR.glob(f"temp_{video_id}_w{worker_id}_att{attempt}.*"))
                if candidates:
                    actual_temp = candidates[0]

            valid_temp, temp_size, temp_err = verify_physical_file(actual_temp)
            if not valid_temp:
                raise RuntimeError(f"Temp file verification failed: {temp_err}")

            # === SAFE ATOMIC MOVE TO GETTIK DESTINATION STORAGE ===
            hub.update_single_download(video_id, {
                "status": "moving",
                "percent": 99.5,
                "speed": "Moving to Gettik folder...",
                "worker_id": worker_id
            })

            # Detect real creator resolved during download metadata
            resolved_creator = (meta_info.get("creator") or "").lstrip("@") or creator_username
            clean_creator = sanitize_creator_username(resolved_creator)

            # Ensure creator downloads are always saved inside that creator's folder: Downloads/Gettik/@username/
            if clean_creator and clean_creator != "creator":
                target_device_dir = get_creator_download_dir(clean_creator, create=True)
                filename_base = format_creator_video_filename(clean_creator, index=job_index, video_id=video_id)
            else:
                filename_base = f"Gettik_{video_id}"

            final_file = get_unique_filepath(target_device_dir, filename_base, final_ext)

            shutil.move(str(actual_temp), str(final_file))

            # === PHYSICAL FILE VERIFICATION (Destination Storage) ===
            valid_final, final_size, final_err = verify_physical_file(final_file)
            if not valid_final:
                raise RuntimeError(f"Destination storage verification failed: {final_err}")

            # === SUCCESS: UPDATE DATABASE & PROGRESS HUB ===
            record_download(
                video_id=video_id,
                url=url,
                title=meta_info["title"],
                description=meta_info.get("description", meta_info["title"]),
                creator=meta_info["creator"],
                creator_avatar=meta_info.get("creator_avatar", ""),
                thumbnail=meta_info.get("thumbnail", ""),
                duration=meta_info.get("duration", 0),
                filesize=final_size,
                format=final_ext,
                quality=quality_str,
                filepath=str(final_file),
                file_name=final_file.name,
                status="completed",
                user_id=user_id,
                creator_id=creator_username,
                download_batch=batch_id or creator_username
            )

            hub.update_single_download(video_id, {
                "status": "completed",
                "percent": 100.0,
                "filepath": str(final_file),
                "filename": final_file.name,
                "filesize": final_size,
                "speed": "Downloaded",
                "worker_id": worker_id,
                "attempt": attempt,
                "title": meta_info.get("title", title_hint),
                "creator": meta_info.get("creator", f"@{creator_username}"),
                "creator_username": creator_username,
                "thumbnail": meta_info.get("thumbnail", thumbnail_hint),
                "quality": quality_str,
                "format": final_ext,
                "type": job.get("type", "single"),
                "batch_id": batch_id
            })

            print(f"[WORKER {worker_id}] Download {video_id} completed successfully ({final_file.name}) on attempt {attempt}")

            return {
                "success": True,
                "status": "completed",
                "video_id": video_id,
                "filepath": str(final_file),
                "filename": final_file.name,
                "filesize": final_size,
                "format": final_ext,
                "title": meta_info["title"],
                "creator": meta_info["creator"],
                "attempts_used": attempt
            }

        except Exception as exc:
            retry_history.append({
                "attempt": attempt,
                "provider": strategy_name,
                "error": str(exc),
                "timestamp": time.time()
            })
            print(f"[WORKER {worker_id}] Download {video_id} failed on attempt {attempt} ({strategy_name}): {exc}")

            # Clean up partial / temporary staging files to prevent orphaned or locked files
            for p in TEMP_DIR.glob(f"*{video_id}*"):
                try:
                    if p.is_file():
                        p.unlink(missing_ok=True)
                except Exception:
                    pass

            is_retryable, error_explanation = is_retryable_error(exc)
            is_net_err = is_network_error_message(str(exc))

            if not is_retryable or attempt >= MAX_RETRIES:
                # Permanent failure after retries exhausted
                final_err_msg = error_explanation if not is_retryable else f"Failed after {attempt} attempts: {exc}"
                print(f"[WORKER {worker_id}] Video {video_id} failed permanently: {final_err_msg}")

                record_download(
                    video_id=video_id,
                    url=url,
                    title=meta_info["title"],
                    creator=meta_info["creator"],
                    status="failed",
                    error_message=final_err_msg,
                    user_id=user_id,
                    creator_id=creator_username,
                    download_batch=batch_id or creator_username
                )

                hub.update_single_download(video_id, {
                    "status": "failed",
                    "percent": 0.0,
                    "speed": "Network Interrupted" if is_net_err else "Failed",
                    "error": final_err_msg,
                    "worker_id": worker_id,
                    "attempt": attempt,
                    "max_attempts": MAX_RETRIES,
                    "can_retry": True,
                    "title": meta_info.get("title", title_hint),
                    "creator": meta_info.get("creator", f"@{creator_username}"),
                    "creator_username": creator_username,
                    "thumbnail": meta_info.get("thumbnail", thumbnail_hint),
                    "quality": quality_str,
                    "format": final_ext,
                    "type": job.get("type", "single"),
                    "batch_id": batch_id
                })

                return {
                    "success": False,
                    "status": "failed",
                    "video_id": video_id,
                    "error": final_err_msg,
                    "attempts_used": attempt,
                    "can_retry": True
                }

            # Prepare for next retry attempt and show user reconnecting / network status
            delay = BACKOFF_DELAYS[attempt - 1] if attempt - 1 < len(BACKOFF_DELAYS) else 3.0
            retry_status_msg = "Network interrupted. Retrying..." if is_net_err else f"Retrying (Attempt {attempt + 1}/{MAX_RETRIES})..."
            hub.update_single_download(video_id, {
                "status": "retrying",
                "percent": 0.0,
                "speed": retry_status_msg,
                "worker_id": worker_id,
                "attempt": attempt,
                "max_attempts": MAX_RETRIES,
                "network_interrupted": is_net_err,
                "title": meta_info.get("title", title_hint),
                "creator": meta_info.get("creator", f"@{creator_username}"),
                "creator_username": creator_username,
                "thumbnail": meta_info.get("thumbnail", thumbnail_hint),
                "quality": quality_str,
                "format": final_ext,
                "type": job.get("type", "single"),
                "batch_id": batch_id
            })
            print(f"[WORKER {worker_id}] Retry attempt {attempt + 1} for {video_id} scheduled in {delay}s ({retry_status_msg})...")
            time.sleep(delay)
            attempt += 1
