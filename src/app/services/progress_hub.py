import asyncio
import json
import time
from typing import Dict, Any, Optional
from typing import Dict, Any, Optional, List

class ProgressHub:
    def __init__(self):
        self.single_downloads: Dict[str, Dict[str, Any]] = {}
        self.creator_progress: Dict[str, Dict[str, Any]] = {}

    def update_single_download(self, video_id: str, data: Dict[str, Any]):
        if video_id not in self.single_downloads:
            self.single_downloads[video_id] = {}
        self.single_downloads[video_id].update(data)
        self.single_downloads[video_id]["updated_at"] = time.time()
    def update_single_download(self, video_id: str, data: Optional[Dict[str, Any]] = None, **kwargs):
        vid_str = str(video_id)
        if vid_str not in self.single_downloads:
            self.single_downloads[vid_str] = {
                "video_id": vid_str,
                "job_id": vid_str,
                "title": f"Video {vid_str}",
                "creator": "",
                "creator_username": "",
                "thumbnail": "",
                "quality": "1080p",
                "format": "mp4",
                "status": "queued",
                "percent": 0.0,
                "speed": "Queued",
                "worker_id": None,
                "attempt": 1,
                "max_attempts": 3,
                "type": "single"
            }
        update_dict = {}
        if data:
            update_dict.update(data)
        if kwargs:
            update_dict.update(kwargs)
        self.single_downloads[vid_str].update(update_dict)
        self.single_downloads[vid_str]["video_id"] = vid_str
        self.single_downloads[vid_str]["job_id"] = vid_str
        self.single_downloads[vid_str]["updated_at"] = time.time()

    def get_single_download(self, video_id: str) -> Optional[Dict[str, Any]]:
        return self.single_downloads.get(video_id)
        return self.single_downloads.get(str(video_id))

    def get_creator_videos(self, creator_username: str, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieve real-time and persistent video jobs for this creator."""
        clean = creator_username.lstrip("@").strip().lower()
        items_by_id: Dict[str, Dict[str, Any]] = {}

        # 1. First load recent persistent records from SQLite
        try:
            from src.app.database.db import get_db_connection
            with get_db_connection() as conn:
                query = """
                    SELECT video_id, title, creator, creator_avatar, thumbnail, quality, format,
                           status, filesize, filepath, error_message, updated_at, created_at
                    FROM downloads
                    WHERE (
                        LOWER(REPLACE(REPLACE(COALESCE(creator_id, ''), '@', ''), ' ', '')) = ?
                        OR LOWER(REPLACE(REPLACE(COALESCE(creator, ''), '@', ''), ' ', '')) = ?
                    )
                """
                params = [clean, clean]
                if user_id:
                    query += " AND user_id = ?"
                    params.append(user_id)
                query += " ORDER BY id DESC LIMIT 100"
                rows = conn.execute(query, params).fetchall()
                for r in rows:
                    d = dict(r)
                    vid = str(d["video_id"])
                    st = d.get("status") or "completed"
                    pct = 100.0 if st == "completed" else 0.0
                    items_by_id[vid] = {
                        "video_id": vid,
                        "job_id": vid,
                        "title": d.get("title") or f"Video {vid}",
                        "creator": d.get("creator") or f"@{clean}",
                        "creator_username": clean,
                        "thumbnail": d.get("thumbnail") or "",
                        "quality": d.get("quality") or "1080p",
                        "format": d.get("format") or "mp4",
                        "status": st,
                        "percent": pct,
                        "speed": "Downloaded" if st == "completed" else (d.get("error_message") or st.title()),
                        "worker_id": None,
                        "attempt": 1,
                        "max_attempts": 3,
                        "filepath": d.get("filepath") or "",
                        "error": d.get("error_message") or "",
                        "updated_at": d.get("updated_at") or time.time()
                    }
        except Exception:
            pass

        # 2. In-memory live downloads override and complement (real-time %, live worker ID, speed)
        for vid, item in list(self.single_downloads.items()):
            if user_id and item.get("user_id") and item.get("user_id") != user_id:
                continue
            item_clean = (item.get("creator_username") or item.get("creator") or "").lstrip("@").strip().lower()
            if item_clean == clean:
                items_by_id[str(vid)] = {
                    "video_id": str(vid),
                    "job_id": str(vid),
                    "title": item.get("title") or f"Video {vid}",
                    "creator": item.get("creator") or f"@{clean}",
                    "creator_username": clean,
                    "thumbnail": item.get("thumbnail") or "",
                    "quality": item.get("quality") or "1080p",
                    "format": item.get("format") or "mp4",
                    "status": item.get("status", "queued"),
                    "percent": float(item.get("percent", 0.0)),
                    "speed": item.get("speed", ""),
                    "worker_id": item.get("worker_id"),
                    "attempt": item.get("attempt", 1),
                    "max_attempts": item.get("max_attempts", 3),
                    "filepath": item.get("filepath", ""),
                    "error": item.get("error", ""),
                    "updated_at": item.get("updated_at", time.time())
                }

        # Sort order: active (downloading/verifying/retrying) first, then queued, then completed/failed
        def sort_priority(it):
            st = it.get("status", "queued")
            if st in ("downloading", "retrying", "verifying", "moving"):
                return (0, -it.get("updated_at", 0))
            if st == "queued":
                return (1, -it.get("updated_at", 0))
            if st == "completed":
                return (2, -it.get("updated_at", 0))
            return (3, -it.get("updated_at", 0))

        results = list(items_by_id.values())
        results.sort(key=sort_priority)
        return results

    def _creator_key(self, username: str, user_id: str = "default_user") -> str:
        clean_user = username.lstrip("@").strip()
        return f"{user_id}:{clean_user}"

    def update_creator(self, username: str, data: Dict[str, Any], user_id: str = "default_user"):
        key = self._creator_key(username, user_id)
        if key not in self.creator_progress:
            self.creator_progress[key] = {
                "status": "idle",
                "completed": 0,
                "remaining": 0,
                "failed": 0,
                "skipped": 0,
                "limit": 50,
                "total": 0,
                "current_video": "",
                "speed": "0 KB/s",
                "percent": 0.0,
                "activities": []
            }
        
        # If new activity provided, prepend
        if "activity" in data and data["activity"]:
            act = data.pop("activity")
            self.creator_progress[key]["activities"].insert(0, act)
            # Keep max 50 recent activities
            self.creator_progress[key]["activities"] = self.creator_progress[key]["activities"][:50]

        self.creator_progress[key].update(data)
        self.creator_progress[key]["updated_at"] = time.time()

    def get_creator(self, username: str, user_id: str = "default_user") -> Dict[str, Any]:
        key = self._creator_key(username, user_id)
        clean_user = username.lstrip("@").strip()
        res = self.creator_progress.get(key)
        if not res and clean_user in self.creator_progress:
            res = self.creator_progress[clean_user]
        return res or {
            "status": "idle",
            "completed": 0,
            "remaining": 0,
            "failed": 0,
            "skipped": 0,
            "limit": 50,
            "total": 0,
            "current_video": "",
            "speed": "0 KB/s",
            "percent": 0.0,
            "activities": []
        }

    def get_active_summary(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        now = time.time()
        active_single = []
        recent_completed_single = []
        active_items = []
        recent_completed_items = []

        for vid, item in list(self.single_downloads.items()):
            if user_id and item.get("user_id") and item.get("user_id") != user_id:
                continue
            status = item.get("status", "idle")
            updated = item.get("updated_at", 0)

            clean_creator = item.get("creator_username") or item.get("creator", "").lstrip("@")
            formatted_creator = f"@{clean_creator}" if clean_creator else "TikTok"

            video_payload = {
                "video_id": str(vid),
                "job_id": str(vid),
                "title": item.get("title") or f"Video {vid}",
                "creator": formatted_creator,
                "creator_username": clean_creator,
                "status": status,
                "percent": float(item.get("percent", 0.0)),
                "speed": item.get("speed", "Queued" if status == "queued" else ""),
                "format": item.get("format", "mp4"),
                "quality": item.get("quality", "1080p"),
                "thumbnail": item.get("thumbnail", ""),
                "worker_id": item.get("worker_id"),
                "attempt": item.get("attempt", 1),
                "max_attempts": item.get("max_attempts", 3),
                "type": item.get("type", "single"),
                "batch_id": item.get("batch_id"),
                "can_retry": False
            }

            if status in ("queued", "downloading", "retrying", "verifying", "moving", "fetching"):
                active_single.append({
                    "video_id": vid,
                    "title": item.get("title") or f"Video {vid}",
                    "creator": item.get("creator", ""),
                    "status": status,
                    "percent": item.get("percent", 0.0),
                    "speed": item.get("speed", "Queued" if status == "queued" else ""),
                    "format": item.get("format", "mp4"),
                    "thumbnail": item.get("thumbnail", ""),
                    "worker_id": item.get("worker_id"),
                    "attempt": item.get("attempt", 1),
                    "max_attempts": 3,
                    "can_retry": False
                })
                active_items.append(video_payload)
            elif status == "completed":
                if now - updated <= 8.0:
                    recent_completed_single.append({
                        "video_id": vid,
                        "title": item.get("title") or f"Video {vid}",
                        "creator": item.get("creator", ""),
                        "status": "completed",
                        "percent": 100.0,
                        "filepath": item.get("filepath", ""),
                        "completed_at": updated
                    })
                if now - updated <= 15.0:
                    video_payload["percent"] = 100.0
                    video_payload["filepath"] = item.get("filepath", "")
                    video_payload["completed_at"] = updated
                    recent_completed_items.append(video_payload)
            elif status == "failed":
                if now - updated <= 60.0:
                    active_single.append({
                        "video_id": vid,
                        "title": item.get("title") or f"Video {vid}",
                        "creator": item.get("creator", ""),
                        "status": "failed",
                        "percent": 0.0,
                        "speed": item.get("speed", "Failed"),
                        "error": item.get("error", "Download failed"),
                        "format": item.get("format", "mp4"),
                        "worker_id": item.get("worker_id"),
                        "attempt": item.get("attempt", 3),
                        "max_attempts": 3,
                        "can_retry": True
                    })
                    video_payload["can_retry"] = True
                    video_payload["error"] = item.get("error", "Download failed")
                    active_items.append(video_payload)

        active_creators = []
        for user_key, item in list(self.creator_progress.items()):
            if user_id:
                if ":" in user_key:
                    uid, user = user_key.split(":", 1)
                    if uid != user_id:
                        continue
                else:
                    user = user_key
            else:
                user = user_key.split(":", 1)[1] if ":" in user_key else user_key

            c_status = item.get("status", "idle")
            if c_status in ("queued", "downloading", "fetching", "paused"):
                active_creators.append({
                    "creator": f"@{user}",
                    "username": user,
                    "status": c_status,
                    "completed": item.get("completed", 0),
                    "total": item.get("total", 0),
                    "remaining": item.get("remaining", 0),
                    "failed": item.get("failed", 0),
                    "skipped": item.get("skipped", 0),
                    "limit": item.get("limit", 50),
                    "current_video": item.get("current_video", ""),
                    "speed": item.get("speed", "0 KB/s"),
                    "percent": item.get("percent", 0.0)
                })

        creator_active_count = sum(max(1, c.get("remaining", 1)) for c in active_creators if c.get("remaining", 0) > 0) if active_creators else 0
        if not creator_active_count and active_creators:
            creator_active_count = len(active_creators)
        active_non_failed = [s for s in active_items if s["status"] != "failed"]
        total_active_jobs = len(active_non_failed)
        if not total_active_jobs and active_creators:
            total_active_jobs = sum(max(1, c.get("remaining", 1)) for c in active_creators if c.get("remaining", 0) > 0)

        active_non_failed = [s for s in active_single if s["status"] != "failed"]
        total_active_jobs = len(active_non_failed) + creator_active_count
        all_pcts = [s["percent"] for s in active_non_failed] + [c["percent"] for c in active_creators]
        if all_pcts:
            overall_percent = round(sum(all_pcts) / len(all_pcts), 1)
        else:
            overall_percent = 0.0
        all_pcts = [s["percent"] for s in active_non_failed]
        if not all_pcts and active_creators:
            all_pcts = [c["percent"] for c in active_creators]

        just_completed = any((now - c.get("completed_at", 0)) <= 3.0 for c in recent_completed_single)
        overall_percent = round(sum(all_pcts) / len(all_pcts), 1) if all_pcts else 0.0
        just_completed = any((now - c.get("completed_at", 0)) <= 3.0 for c in recent_completed_items)

        worker_stats = {"capacity": 5, "active_workers": 0, "queued_count": 0}
        try:
            from src.app.workers.download_worker import download_worker_pool
            worker_stats = download_worker_pool.get_status()
        except Exception:
            pass

        return {
            "has_active": total_active_jobs > 0,
            "total_active": total_active_jobs,
            "overall_percent": overall_percent,
            "active_single": active_single,
            "active_single": active_items + recent_completed_items,
            "active_videos": active_items + recent_completed_items,
            "active_creators": active_creators,
            "just_completed": just_completed,
            "recent_completed_single": recent_completed_single,
            "recent_completed_single": recent_completed_items,
            "recent_completed": recent_completed_items,
            "worker_capacity": worker_stats.get("capacity", 5),
            "active_workers": worker_stats.get("active_workers", 0),
            "queued_count": worker_stats.get("queued_count", 0)
        }

hub = ProgressHub()



