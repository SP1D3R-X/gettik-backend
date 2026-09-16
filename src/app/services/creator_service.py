import os
import shutil
import threading
import time
import asyncio
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import urllib.request
import urllib.error
import json
import yt_dlp
import imageio_ffmpeg

from src.app.config import get_active_downloads_dir, TEMP_DIR, GETTIK_DOWNLOAD_WORKERS
from src.app.services.system_utils import (
    get_creator_download_dir,
    sanitize_creator_username,
    format_creator_video_filename
)
from src.app.database.db import (
    record_download,
    is_video_downloaded,
    get_downloaded_video_ids,
    save_creator,
    save_creator_videos,
    get_cached_creator_videos,
    update_creator_progress,
    get_creator_progress,
    add_creator_activity,
    get_creator_activities,
    create_download_batch,
    update_download_batch,
    upsert_batch_item
)
from src.app.services.creator_providers import creator_orchestrator, validate_creator_response
from src.app.services.progress_hub import hub
from src.app.services.system_utils import (
    verify_physical_file,
    sanitize_filename,
    verify_directory_writable,
    get_unique_filepath
)
from src.app.services.ytdlp_service import FFMPEG_BIN, format_duration

def fetch_real_tiktok_avatar(clean_name: str, info: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """
    Extract the creator's real profile picture and nickname.
    Never uses a video thumbnail, video frame, or generic icon.
    """
    avatar_url = ""
    nickname = clean_name

    # 1. Check yt-dlp channel/uploader metadata
    if info:
        channel_name = info.get('channel') or info.get('uploader')
        if channel_name:
            nickname = channel_name
        c_thumbs = info.get('thumbnails', [])
        for t in c_thumbs:
            u = t.get('url', '')
            if 'avatar' in u.lower() or 'user' in u.lower():
                avatar_url = u
                break

    # 2. Directly scrape TikTok web profile page for real profile picture
    try:
        req_url = f"https://www.tiktok.com/@{clean_name}"
        req = urllib.request.Request(
            req_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9"
            }
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            # 2a. Check __UNIVERSAL_DATA_FOR_REHYDRATION__
            if "__UNIVERSAL_DATA_FOR_REHYDRATION__" in html:
                m = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html)
                if m:
                    try:
                        data = json.loads(m.group(1))
                        default_scope = data.get("__DEFAULT_SCOPE__", {})
                        user_detail = default_scope.get("webapp.user-detail", {})
                        user_info = user_detail.get("userInfo", {}).get("user", {})
                        real_avatar = user_info.get("avatarLarger") or user_info.get("avatarMedium") or user_info.get("avatarThumb")
                        if real_avatar:
                            avatar_url = real_avatar
                        real_nick = user_info.get("nickname")
                        if real_nick:
                            nickname = real_nick
                    except Exception:
                        pass
            
            # 2b. Check OpenGraph image meta tag
            if not avatar_url:
                og = re.search(r'<meta\s+(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html)
                if og:
                    cand = og.group(1)
                    if "tiktok" in cand.lower() or "byteoversea" in cand.lower() or "tiktokcdn" in cand.lower():
                        avatar_url = cand
    except Exception:
        pass

    return avatar_url, nickname

class CreatorDownloader:
    def __init__(self):
        self.active_tasks: Dict[str, threading.Thread] = {}
        self.pause_flags: Dict[str, threading.Event] = {}
        self.stop_flags: Dict[str, threading.Event] = {}
        self.cached_playlists: Dict[str, List[Dict[str, Any]]] = {}
        self.task_users: Dict[str, str] = {}
        self.batch_states: Dict[str, Dict[str, Any]] = {}

    def fetch_creator_info(self, username_or_url: str, user_id: str = "default_user", refresh: bool = False, limit: int = 50) -> Dict[str, Any]:
        """Fetch creator profile information and video list using multi-provider orchestrator with fallback."""
        raw_user = username_or_url.strip()
        if "tiktok.com" in raw_user:
            match = re.search(r'@([a-zA-Z0-9_\.]+)', raw_user)
            clean_name = match.group(1) if match else raw_user.split("/")[-1].lstrip("@")
        else:
            clean_name = raw_user.lstrip("@").split("?")[0].split("/")[0].strip()
        url = f"https://www.tiktok.com/@{clean_name}"
        normalized = None
        import asyncio
        import concurrent.futures

        # 1. Primary: Run multi-provider orchestrator
        try:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        normalized = executor.submit(
                            asyncio.run,
                            creator_orchestrator.fetch_creator(clean_name, limit=limit, refresh=refresh, user_id=user_id)
                        ).result(timeout=15.0)
                else:
                    normalized = loop.run_until_complete(
                        creator_orchestrator.fetch_creator(clean_name, limit=limit, refresh=refresh, user_id=user_id)
                    )
            except RuntimeError:
                normalized = asyncio.run(
                    creator_orchestrator.fetch_creator(clean_name, limit=limit, refresh=refresh, user_id=user_id)
                )
        except Exception as orch_err:
            print(f"[CreatorService] Multi-provider orchestrator notice for @{clean_name}: {orch_err}")

        # 2. Fallback to direct yt-dlp + fetch_real_tiktok_avatar if orchestrator could not resolve
        if not normalized:
            ydl_opts = {
                'extract_flat': True,
                'quiet': True,
                'no_warnings': True
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    creator_name = info.get('title') or clean_name
                    entries = info.get('entries', [])
                    total_videos = len(entries)
                    avatar_url, nickname = fetch_real_tiktok_avatar(clean_name, info)
                    if entries and len(entries) > 0 and (not nickname or nickname == clean_name):
                        channel = entries[0].get('channel')
                        if channel:
                            nickname = channel

                    if avatar_url:
                        self.cached_playlists[clean_name] = entries
                        save_creator_videos(clean_name, entries)
                        effective_limit = limit if (limit and limit > 0) else total_videos
                        save_creator(
                            username=clean_name,
                            nickname=nickname,
                            avatar=avatar_url,
                            avatar_url=avatar_url,
                            bio=f"TikTok Creator @{clean_name}",
                            video_count=total_videos,
                            download_limit=effective_limit if total_videos > 0 else 50,
                            user_id=user_id
                        )
                        creator_dir = get_creator_download_dir(clean_name, create=True)
                        return {
                            "username": f"@{clean_name}",
                            "clean_username": clean_name,
                            "nickname": nickname,
                            "avatar": avatar_url,
                            "avatar_url": avatar_url,
                            "total_videos": total_videos,
                            "download_dir": str(creator_dir),
                            "videos": entries[:limit] if (limit and limit > 0) else entries
                        }
            except Exception as ydl_err:
                print(f"[CreatorService] yt-dlp fallback error: {ydl_err}")

        if not normalized:
            raise RuntimeError("Unable to fetch creator data. Please verify the username and try again.")

        entries = [v.to_dict() for v in normalized.videos]
        self.cached_playlists[clean_name] = entries
        save_creator_videos(clean_name, entries)

        total_videos = normalized.video_count if normalized.video_count > 0 else len(entries)
        effective_limit = limit if (limit and limit > 0) else total_videos
        save_creator(
            username=clean_name,
            nickname=normalized.nickname,
            avatar=normalized.avatar_url,
            avatar_url=normalized.avatar_url,
            bio=normalized.bio or f"TikTok Creator @{clean_name}",
            video_count=total_videos,
            download_limit=effective_limit if total_videos > 0 else 50,
            user_id=user_id
        )

        creator_dir = get_creator_download_dir(clean_name, create=True)
        return {
            "username": f"@{clean_name}",
            "clean_username": clean_name,
            "nickname": normalized.nickname,
            "avatar": normalized.avatar_url,
            "avatar_url": normalized.avatar_url,
            "total_videos": total_videos,
            "download_dir": str(creator_dir),
            "videos": entries[:limit] if (limit and limit > 0) else entries
        }

    async def async_fetch_creator_info(self, username_or_url: str, user_id: str = "default_user", refresh: bool = False, limit: int = 50) -> Dict[str, Any]:
        """Asynchronously fetch creator information using the multi-provider orchestrator."""
        raw_user = username_or_url.strip()
        if "tiktok.com" in raw_user:
            match = re.search(r'@([a-zA-Z0-9_\.]+)', raw_user)
            clean_name = match.group(1) if match else raw_user.split("/")[-1].lstrip("@")
        else:
            clean_name = raw_user.lstrip("@").split("?")[0].split("/")[0].strip()

        try:
            normalized = await creator_orchestrator.fetch_creator(clean_name, limit=limit, refresh=refresh, user_id=user_id)
            entries = [v.to_dict() for v in normalized.videos]
            self.cached_playlists[clean_name] = entries
            save_creator_videos(clean_name, entries)

            total_videos = normalized.video_count if normalized.video_count > 0 else len(entries)
            effective_limit = limit if (limit and limit > 0) else total_videos
            save_creator(
                username=clean_name,
                nickname=normalized.nickname,
                avatar=normalized.avatar_url,
                avatar_url=normalized.avatar_url,
                bio=normalized.bio or f"TikTok Creator @{clean_name}",
                video_count=total_videos,
                download_limit=effective_limit if total_videos > 0 else 50,
                user_id=user_id
            )

            creator_dir = get_creator_download_dir(clean_name, create=True)
            creator_dict = normalized.to_dict()
            creator_dict["download_dir"] = str(creator_dir)
            creator_dict["videos"] = entries[:limit] if (limit and limit > 0) else entries
            return creator_dict
        except Exception:
            # Fall back to synchronous pipeline in executor if async providers exhausted
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self.fetch_creator_info, username_or_url, user_id, refresh, limit)

    def is_stopped(self, username: str, user_id: str = "default_user") -> bool:
        """Check if creator download is marked stopped."""
        clean_user = username.lstrip("@").strip()
        task_key = f"{user_id}:{clean_user}"
        if task_key in self.stop_flags and self.stop_flags[task_key].is_set():
            return True
        if clean_user in self.stop_flags and self.stop_flags[clean_user].is_set():
            return True
        return False

    def is_paused(self, username: str, user_id: str = "default_user") -> bool:
        """Check if creator download is marked paused."""
        clean_user = username.lstrip("@").strip()
        task_key = f"{user_id}:{clean_user}"
        if task_key in self.pause_flags and not self.pause_flags[task_key].is_set():
            return True
        if clean_user in self.pause_flags and not self.pause_flags[clean_user].is_set():
            return True
        return False

    def wait_if_paused(self, username: str, user_id: str = "default_user"):
        """Block if creator download is currently paused until resumed."""
        clean_user = username.lstrip("@").strip()
        task_key = f"{user_id}:{clean_user}"
        if task_key in self.pause_flags:
            self.pause_flags[task_key].wait()
        elif clean_user in self.pause_flags:
            self.pause_flags[clean_user].wait()

    def stop_all_for_user(self, user_id: str):
        """Cancel and stop all active creator downloads belonging to a specific user."""
        for task_key, uid in list(self.task_users.items()):
            if uid == user_id:
                clean_user = task_key.split(":", 1)[1] if ":" in task_key else task_key
                self.stop(clean_user, user_id=user_id)

    def pause(self, username: str, user_id: str = "default_user"):
        clean_user = username.lstrip("@").strip()
        task_key = f"{user_id}:{clean_user}"
        if task_key in self.pause_flags:
            self.pause_flags[task_key].clear() # Cleared = paused
        elif clean_user in self.pause_flags:
            self.pause_flags[clean_user].clear()
        hub.update_creator(clean_user, {"status": "paused"}, user_id=user_id)
        progress = get_creator_progress(clean_user, user_id=user_id) or {}
        update_creator_progress(
            creator_username=clean_user,
            status="paused",
            total_found=progress.get("total_found", 0),
            download_limit=progress.get("download_limit", 3),
            completed_count=progress.get("completed_count", 0),
            remaining_count=progress.get("remaining_count", 0),
            failed_count=progress.get("failed_count", 0),
            skipped_count=progress.get("skipped_count", 0),
            last_video_index=progress.get("last_video_index", 0),
            user_id=user_id
        )

    def resume(self, username: str, user_id: str = "default_user"):
        clean_user = username.lstrip("@").strip()
        task_key = f"{user_id}:{clean_user}"
        if task_key in self.pause_flags and not self.pause_flags[task_key].is_set():
            self.pause_flags[task_key].set()
            hub.update_creator(clean_user, {"status": "downloading"}, user_id=user_id)
        elif clean_user in self.pause_flags and not self.pause_flags[clean_user].is_set():
            self.pause_flags[clean_user].set()
            hub.update_creator(clean_user, {"status": "downloading"}, user_id=user_id)
        else:
            self.start_batch_download(clean_user, resume=True, user_id=user_id)

    def stop(self, username: str, user_id: str = "default_user"):
        clean_user = username.lstrip("@").strip()
        task_key = f"{user_id}:{clean_user}"
        if task_key in self.stop_flags:
            self.stop_flags[task_key].set()
        elif clean_user in self.stop_flags:
            self.stop_flags[clean_user].set()
        if task_key in self.pause_flags:
            self.pause_flags[task_key].set() # Unblock if paused so thread can exit
        elif clean_user in self.pause_flags:
            self.pause_flags[clean_user].set()
        hub.update_creator(clean_user, {"status": "stopped"}, user_id=user_id)
        progress = get_creator_progress(clean_user, user_id=user_id) or {}
        update_creator_progress(
            creator_username=clean_user,
            status="stopped",
            total_found=progress.get("total_found", 0),
            download_limit=progress.get("download_limit", 3),
            completed_count=progress.get("completed_count", 0),
            remaining_count=progress.get("remaining_count", 0),
            failed_count=progress.get("failed_count", 0),
            skipped_count=progress.get("skipped_count", 0),
            last_video_index=progress.get("last_video_index", 0),
            user_id=user_id
        )

    def queue_creator_batch(
        self,
        username: str,
        limit: int = 3,
        user_id: str = "default_user",
        format_type: str = "mp4"
    ) -> Dict[str, Any]:
        """
        Fast batch enqueueing into the central 5-worker download queue:
        1. Fast lookup of playlist / cached videos
        2. Filters out already downloaded videos for this user
        3. Creates download_batch record in database
        4. Enqueues all N jobs into download_queue with 'queued' state
        5. Returns immediately (target <= 2s) with batch status and worker counts.
        """
        from src.app.workers.redis_queue import download_queue
        from src.app.workers.download_worker import download_worker_pool

        clean_user = sanitize_creator_username(username)
        task_key = f"{user_id}:{clean_user}"
        self.task_users[task_key] = user_id

        # Setup creator storage dir: Downloads/Gettik/@username/
        creator_device_dir = get_creator_download_dir(clean_user, create=True)

        # Retrieve cached playlist or quick-fetch
        entries = self.cached_playlists.get(clean_user)
        if not entries:
            db_vids = get_cached_creator_videos(clean_user, limit=500)
            if db_vids:
                entries = db_vids
                self.cached_playlists[clean_user] = entries
            else:
                try:
                    url = f"https://www.tiktok.com/@{clean_user}"
                    ydl_opts = {
                        'extract_flat': True,
                        'quiet': True,
                        'no_warnings': True,
                        'socket_timeout': 0.3,
                        'retries': 0,
                        'extractor_retries': 0
                    }
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                        entries = info.get('entries', [])
                        self.cached_playlists[clean_user] = entries
                        save_creator_videos(clean_user, entries)
                except Exception as e:
                    print(f"[CreatorDownloader] Error quick-fetching entries for {clean_user}: {e}")
                    entries = []

        total_found = len(entries)
        all_vids = [str(item.get('id', '')) for item in entries if item.get('id')]
        downloaded_ids = get_downloaded_video_ids(all_vids, user_id=user_id)
        eligible_entries = [
            item for item in entries
            if str(item.get('id', '')) and str(item.get('id', '')) not in downloaded_ids
        ]

        if not eligible_entries:
            hub.update_creator(clean_user, {
                "status": "completed",
                "speed": "Downloaded",
                "total": total_found,
                "limit": limit,
                "completed": total_found,
                "remaining": 0,
                "percent": 100.0,
                "current_video": "All available videos already downloaded"
            }, user_id=user_id)
            return {
                "success": True,
                "batch_id": f"batch_{clean_user}_done",
                "total_jobs": 0,
                "queued": 0,
                "active": 0,
                "message": "All eligible videos are already downloaded."
            }

        batch_entries = eligible_entries[:limit]
        target_limit = len(batch_entries)
        batch_id = f"batch_{clean_user}_{int(time.time())}"
        create_download_batch(batch_id, clean_user, target_limit, user_id=user_id)

        # Track batch in memory
        self.batch_states[batch_id] = {
            "creator": clean_user,
            "user_id": user_id,
            "total": target_limit,
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "lock": threading.Lock()
        }

        # Reset flags
        self.pause_flags[task_key] = threading.Event()
        self.pause_flags[task_key].set()
        self.stop_flags[task_key] = threading.Event()

        # Update initial progress in Hub and DB
        hub.update_creator(clean_user, {
            "status": "downloading",
            "total": total_found,
            "limit": target_limit,
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "remaining": target_limit,
            "percent": 0.0,
            "speed": f"Queued {target_limit} videos across {GETTIK_DOWNLOAD_WORKERS} workers"
        }, user_id=user_id)
        update_creator_progress(
            creator_username=clean_user,
            status="downloading",
            total_found=total_found,
            download_limit=target_limit,
            completed_count=0,
            remaining_count=target_limit,
            failed_count=0,
            skipped_count=0,
            last_video_index=0,
            user_id=user_id
        )

        # Ensure 5 worker threads are running
        download_worker_pool.start_worker_pool()

        # Immediately create 'queued' records in DB and enqueue jobs into central queue
        for idx, item in enumerate(batch_entries):
            vid = str(item.get('id', ''))
            video_url = item.get('url') or f"https://www.tiktok.com/@{clean_user}/video/{vid}"
            video_title = item.get('title') or item.get('description') or f"TikTok Video {vid}"
            video_desc = item.get('description') or video_title
            thumbnail = item.get('thumbnail') or item.get('thumbnail_url') or item.get('cover') or ""

            upsert_batch_item(batch_id, vid, video_title, status="Queued")

            record_download(
                video_id=vid,
                url=video_url,
                title=video_title,
                description=video_desc,
                creator=f"@{clean_user}",
                creator_avatar="",
                thumbnail=thumbnail,
                duration=int(item.get('duration') or 0),
                format=format_type,
                quality="1080p",
                status="queued",
                download_batch=batch_id,
                user_id=user_id,
                creator_id=clean_user
            )

            # Register video in ProgressHub so it is immediately visible in BOTH Top Header and Creator Progress page
            hub.update_single_download(vid, {
                "video_id": vid,
                "job_id": vid,
                "batch_id": batch_id,
                "type": "creator",
                "user_id": user_id,
                "title": video_title,
                "creator": f"@{clean_user}",
                "creator_username": clean_user,
                "thumbnail": thumbnail,
                "quality": "1080p",
                "format": format_type,
                "status": "queued",
                "percent": 0.0,
                "speed": "Queued",
                "worker_id": None,
                "attempt": 1,
                "max_attempts": 3
            })

            job = {
                "type": "creator",
                "batch_id": batch_id,
                "creator_username": clean_user,
                "creator": f"@{clean_user}",
                "video_id": vid,
                "url": video_url,
                "title": video_title,
                "description": video_desc,
                "thumbnail": thumbnail,
                "quality": "1080p",
                "format": format_type,
                "user_id": user_id,
                "custom_dir": str(creator_device_dir),
                "index": idx,
                "total_batch_jobs": target_limit
            }
            download_queue.enqueue_download(job)

        active_workers = min(target_limit, GETTIK_DOWNLOAD_WORKERS)
        queued_count = target_limit

        print(f"[CreatorDownloader] Queued batch {batch_id}: {target_limit} videos across {GETTIK_DOWNLOAD_WORKERS} worker slots.")
        return {
            "success": True,
            "batch_id": batch_id,
            "total_jobs": target_limit,
            "queued": queued_count,
            "active": active_workers,
            "message": f"Successfully queued {target_limit} videos across {GETTIK_DOWNLOAD_WORKERS} concurrent worker slots."
        }

    def start_batch_download(self, username: str, limit: int = 3, resume: bool = False, user_id: str = "default_user"):
        """Start batch download via central 5-worker queue."""
        return self.queue_creator_batch(username, limit=limit, user_id=user_id)

    def _run_download_loop(self, clean_user: str, limit: int, resume: bool = False, user_id: str = "default_user"):
        """Synchronous loop for sequential batch execution with duplicate skipping and physical verification."""
        task_key = f"{user_id}:{clean_user}"
        try:
            hub.update_creator(clean_user, {
                "status": "fetching",
                "speed": "Connecting..."
            }, user_id=user_id)

            creator_device_dir = get_active_downloads_dir() / f"@{clean_user}"
            writable, dir_err = verify_directory_writable(creator_device_dir)
            if not writable:
                err_msg = "Download cannot continue because the selected folder is unavailable or not writable."
                hub.update_creator(clean_user, {
                    "status": "error",
                    "speed": "Folder Unwritable",
                    "error": err_msg
                }, user_id=user_id)
                raise RuntimeError(err_msg)

            TEMP_DIR.mkdir(parents=True, exist_ok=True)

            saved_prog = get_creator_progress(clean_user, user_id=user_id) if resume else None
            completed_count = saved_prog.get("completed_count", 0) if saved_prog else 0
            skipped_count = saved_prog.get("skipped_count", 0) if saved_prog else 0
            failed_count = saved_prog.get("failed_count", 0) if saved_prog else 0

            entries = self.cached_playlists.get(clean_user)
            if not entries:
                url = f"https://www.tiktok.com/@{clean_user}"
                ydl_opts = {'extract_flat': True, 'quiet': True, 'no_warnings': True}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    entries = info.get('entries', [])
                    self.cached_playlists[clean_user] = entries

            total_found = len(entries)
            undownloaded = [e for e in entries if not is_video_downloaded(str(e.get('id', '')), user_id=user_id)]
            if not undownloaded:
                hub.update_creator(clean_user, {
                    "status": "completed",
                    "current_video": "All available videos already downloaded",
                    "speed": "Downloaded",
                    "percent": 100.0,
                    "completed": total_found,
                    "remaining": 0
                }, user_id=user_id)
                update_creator_progress(
                    creator_username=clean_user,
                    status="completed",
                    total_found=total_found,
                    download_limit=limit,
                    completed_count=total_found,
                    remaining_count=0,
                    failed_count=0,
                    skipped_count=0,
                    last_video_index=total_found,
                    user_id=user_id
                )
                return

            to_download = undownloaded[:limit]
            target_limit = len(to_download)
            batch_id = f"batch_{clean_user}_{int(time.time())}"
            create_download_batch(batch_id, clean_user, target_limit, user_id=user_id)

            hub.update_creator(clean_user, {
                "status": "downloading",
                "total": total_found,
                "limit": target_limit,
                "completed": completed_count,
                "skipped": skipped_count,
                "failed": failed_count,
                "remaining": target_limit
            }, user_id=user_id)

            for idx, item in enumerate(to_download):
                if task_key in self.stop_flags and self.stop_flags[task_key].is_set():
                    break
                elif clean_user in self.stop_flags and self.stop_flags[clean_user].is_set():
                    break

                if task_key in self.pause_flags:
                    self.pause_flags[task_key].wait()
                elif clean_user in self.pause_flags:
                    self.pause_flags[clean_user].wait()

                video_id = str(item.get('id', ''))
                video_url = item.get('url') or f"https://www.tiktok.com/@{clean_user}/video/{video_id}"
                video_title = item.get('title') or f"Video {idx + 1}"
                video_desc = item.get('description') or video_title
                timestamp_str = time.strftime("%I:%M %p")

                if is_video_downloaded(video_id, user_id=user_id):
                    skipped_count += 1
                    msg = f"Skipped: {video_title[:30]} (Already Downloaded)"
                    add_creator_activity(clean_user, video_id, video_title, "skipped", msg, user_id=user_id)
                    upsert_batch_item(batch_id, video_id, video_title, status="Skipped")
                    continue

                upsert_batch_item(batch_id, video_id, video_title, status="Downloading")
                hub.update_creator(clean_user, {
                    "current_video": video_title,
                    "speed": "Downloading..."
                }, user_id=user_id)

                def dl_hook(d):
                    if d.get('status') == 'downloading':
                        speed = d.get('_speed_str', '1.2 MB/s').strip()
                        p_str = d.get('_percent_str', '0%').replace('%', '').strip()
                        try:
                            file_p = float(p_str)
                        except ValueError:
                            file_p = 0.0
                        hub.update_creator(clean_user, {
                            "speed": speed,
                            "file_percent": file_p
                        }, user_id=user_id)
                    elif d.get('status') == 'finished':
                        hub.update_creator(clean_user, {
                            "speed": "Verifying file..."
                        }, user_id=user_id)

                seq_base = f"Gettik-@{clean_user}-{idx + 1:03d}_{video_id}"
                final_target_file = get_unique_filepath(creator_device_dir, seq_base, "mp4")
                temp_staging_file = TEMP_DIR / f"temp_{clean_user}_{video_id}.%(ext)s"

                ydl_download_opts = {
                    'format': 'best',
                    'outtmpl': str(temp_staging_file),
                    'ffmpeg_location': FFMPEG_BIN,
                    'progress_hooks': [dl_hook],
                    'quiet': True,
                    'no_warnings': True
                }

                try:
                    with yt_dlp.YoutubeDL(ydl_download_opts) as ydl:
                        ydl.download([video_url])

                    upsert_batch_item(batch_id, video_id, video_title, status="Verifying")
                    hub.update_creator(clean_user, {"speed": "Verifying file..."}, user_id=user_id)
                    actual_temp = TEMP_DIR / f"temp_{clean_user}_{video_id}.mp4"
                    if not actual_temp.exists():
                        cands = list(TEMP_DIR.glob(f"temp_{clean_user}_{video_id}.*"))
                        if cands:
                            actual_temp = cands[0]

                    valid_temp, temp_size, temp_err = verify_physical_file(actual_temp)
                    if not valid_temp:
                        raise RuntimeError(f"Temp file verification failed: {temp_err}")

                    upsert_batch_item(batch_id, video_id, video_title, status="Moving")
                    shutil.move(str(actual_temp), str(final_target_file))

                    valid_final, final_size, final_err = verify_physical_file(final_target_file)
                    if not valid_final:
                        raise RuntimeError(f"Device storage verification failed: {final_err}")

                    record_download(
                        video_id=video_id,
                        url=video_url,
                        title=video_title,
                        description=video_desc,
                        creator=f"@{clean_user}",
                        duration=item.get('duration', 0),
                        filesize=final_size,
                        format="mp4",
                        quality="1080p",
                        filepath=str(final_target_file),
                        file_name=final_target_file.name,
                        status="completed",
                        download_batch=clean_user,
                        facebook_upload_status="ready",
                        user_id=user_id
                    )

                    completed_count += 1
                    msg = f"Downloaded: {video_title[:30]}"
                    add_creator_activity(clean_user, video_id, video_title, "downloaded", msg, user_id=user_id)
                    upsert_batch_item(batch_id, video_id, video_title, status="Downloaded", file_path=str(final_target_file), file_size=final_size)

                except Exception as dl_err:
                    failed_count += 1
                    msg = f"Failed: {video_title[:30]} ({str(dl_err)[:30]})"
                    add_creator_activity(clean_user, video_id, video_title, "failed", msg, user_id=user_id)
                    record_download(
                        video_id=video_id,
                        url=video_url,
                        title=video_title,
                        description=video_desc,
                        creator=f"@{clean_user}",
                        status="failed",
                        error_message=str(dl_err),
                        download_batch=clean_user,
                        user_id=user_id
                    )
                    upsert_batch_item(batch_id, video_id, video_title, status="Failed", error_message=str(dl_err))

                rem = max(0, target_limit - (completed_count + skipped_count + failed_count))
                pct = round(((completed_count + skipped_count) / target_limit) * 100, 1) if target_limit > 0 else 0

                hub.update_creator(clean_user, {
                    "completed": completed_count,
                    "failed": failed_count,
                    "remaining": rem,
                    "percent": pct,
                    "activity": {"type": "downloaded" if msg.startswith("Downloaded") else "failed", "text": msg, "time": timestamp_str}
                }, user_id=user_id)

                update_creator_progress(
                    creator_username=clean_user,
                    status="downloading",
                    total_found=total_found,
                    download_limit=target_limit,
                    completed_count=completed_count,
                    remaining_count=rem,
                    failed_count=failed_count,
                    skipped_count=skipped_count,
                    current_video_id=video_id,
                    current_video_title=video_title,
                    current_speed=hub.get_creator(clean_user, user_id=user_id).get("speed", "0 KB/s"),
                    current_percent=pct,
                    last_video_index=idx + 1,
                    user_id=user_id
                )
                update_download_batch(batch_id, completed_count, rem, failed_count, skipped_count, "active")

            final_status = "completed" if (completed_count + skipped_count) >= target_limit else "stopped"
            hub.update_creator(clean_user, {
                "status": final_status,
                "speed": "Downloaded" if final_status == "completed" else "Finished"
            }, user_id=user_id)
            update_creator_progress(
                creator_username=clean_user,
                status=final_status,
                total_found=total_found,
                download_limit=target_limit,
                completed_count=completed_count,
                remaining_count=0 if final_status == "completed" else max(0, target_limit - completed_count - skipped_count - failed_count),
                failed_count=failed_count,
                skipped_count=skipped_count,
                last_video_index=target_limit,
                user_id=user_id
            )
            update_download_batch(batch_id, completed_count, 0 if final_status == "completed" else max(0, target_limit - completed_count - skipped_count - failed_count), failed_count, skipped_count, final_status)

        except Exception as e:
            print(f"[CreatorDownloader] Fatal loop error for {clean_user}: {e}")
            hub.update_creator(clean_user, {"status": "error", "speed": "Error"}, user_id=user_id)

    def on_creator_video_finished(self, job: Dict[str, Any], result: Dict[str, Any], worker_id: int = 1):
        """Called by central DownloadWorkerPool when a creator video completes or fails."""
        batch_id = job.get("batch_id")
        if not batch_id:
            return
        video_id = str(job.get("video_id", ""))
        clean_user = job.get("creator_username", "")
        user_id = job.get("user_id", "default_user")
        video_title = result.get("title") or job.get("title") or f"TikTok Video {video_id}"
        timestamp_str = time.strftime("%I:%M %p")

        state = self.batch_states.get(batch_id)
        if not state:
            state = {
                "creator": clean_user,
                "user_id": user_id,
                "total": job.get("total_batch_jobs", 1),
                "completed": 0,
                "failed": 0,
                "skipped": 0,
                "lock": threading.Lock()
            }
            self.batch_states[batch_id] = state

        is_success = result.get("success", False)
        is_skipped = result.get("status") == "skipped"

        with state["lock"]:
            if is_success:
                state["completed"] += 1
                msg = f"Downloaded: {video_title[:30]}"
                status_str = "Downloaded"
                add_creator_activity(clean_user, video_id, video_title, "downloaded", msg, user_id=user_id)
                upsert_batch_item(
                    batch_id, video_id, video_title,
                    status=status_str,
                    file_path=result.get("filepath", ""),
                    file_size=result.get("filesize", 0)
                )
                hub.update_single_download(video_id, {
                    "status": "completed",
                    "percent": 100.0,
                    "speed": "Downloaded",
                    "filepath": result.get("filepath", ""),
                    "filesize": result.get("filesize", 0),
                    "worker_id": worker_id
                })
            elif is_skipped:
                state["skipped"] += 1
                msg = f"Skipped: {video_title[:30]} (Already Downloaded)"
                status_str = "Skipped"
                add_creator_activity(clean_user, video_id, video_title, "skipped", msg, user_id=user_id)
                upsert_batch_item(batch_id, video_id, video_title, status=status_str)
                hub.update_single_download(video_id, {
                    "status": "completed",
                    "percent": 100.0,
                    "speed": "Skipped (Already Downloaded)",
                    "filepath": result.get("filepath", ""),
                    "filesize": result.get("filesize", 0),
                    "worker_id": worker_id
                })
            else:
                state["failed"] += 1
                err_msg = result.get("error", "Download failed")
                msg = f"Failed: {video_title[:30]} ({err_msg[:30]})"
                status_str = "Failed"
                add_creator_activity(clean_user, video_id, video_title, "failed", msg, user_id=user_id)
                upsert_batch_item(batch_id, video_id, video_title, status=status_str, error_message=err_msg)
                hub.update_single_download(video_id, {
                    "status": "failed",
                    "percent": 0.0,
                    "speed": "Failed",
                    "error": err_msg,
                    "worker_id": worker_id
                })

            completed = state["completed"] + state["skipped"]
            failed = state["failed"]
            total = state["total"]
            rem = max(0, total - (completed + failed))
            pct = round((completed / total) * 100, 1) if total > 0 else 0

            hub.update_creator(clean_user, {
                "completed": completed,
                "failed": failed,
                "skipped": state["skipped"],
                "remaining": rem,
                "percent": pct,
                "activity": {"type": "downloaded" if is_success else "failed", "text": msg, "time": timestamp_str}
            }, user_id=user_id)

            is_finished = (rem == 0)
            batch_status = "completed" if (is_finished and failed == 0) else ("partial" if is_finished else "active")

            update_download_batch(batch_id, completed, rem, failed, state["skipped"], batch_status)
            update_creator_progress(
                creator_username=clean_user,
                status="completed" if is_finished else "downloading",
                total_found=total,
                download_limit=total,
                completed_count=completed,
                remaining_count=rem,
                failed_count=failed,
                skipped_count=state["skipped"],
                current_percent=pct,
                user_id=user_id
            )


# Singleton instance
creator_mgr = CreatorDownloader()
creator_service = creator_mgr

def cancel_user_active_jobs(user_id: str):
    """Cancel all active downloads and background tasks belonging to a specific user."""
    creator_mgr.stop_all_for_user(user_id)
