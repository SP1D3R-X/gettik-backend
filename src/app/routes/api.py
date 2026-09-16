import threading
from typing import Optional, Dict, Any, List
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Depends
from src.app.routes.auth_routes import get_current_user

from src.app.config import get_active_downloads_dir
from src.app.services.ytdlp_service import fetch_video_metadata, download_video
from src.app.services.creator_service import creator_mgr
from src.app.services.progress_hub import hub
from src.app.services.system_utils import open_file, open_folder, pick_native_directory, verify_directory_writable
from src.app.workers.redis_queue import download_queue
from src.app.workers.download_worker import download_worker_pool
from src.app.database.db import (
    get_recent_downloads,
    get_download_by_id,
    get_download_by_video_id,
    delete_download,
    record_download,
    get_active_job,
    get_db_connection,
    update_download_retry_db,
    save_creator,
    get_saved_creators,
    get_user_creator,
    delete_user_creator,
    get_user_creator_settings,
    update_user_creator_settings,
    get_download_batches,
    get_download_batch_by_id,
    get_stats,
    get_settings,
    update_settings,
    get_creator_progress,
    get_creator_activities,
    get_creator_by_username,
    reconcile_all_downloads,
    create_feedback_record,
    get_feedback_by_id,
    get_user_feedback_list,
    update_feedback_record,
    add_feedback_audit_log
)

router = APIRouter(prefix="/api")

# --- Request Models ---
class VideoFetchRequest(BaseModel):
    url: str

class VideoDownloadRequest(BaseModel):
    url: str
    format: str = "mp4"

class CreatorFetchRequest(BaseModel):
    username: str
    refresh: Optional[bool] = False
    limit: Optional[int] = 3

class CreatorSaveRequest(BaseModel):
    username: str
    nickname: Optional[str] = None
    avatar_url: Optional[str] = None
    bio: Optional[str] = None
    download_limit: Optional[int] = 3
    preferred_quality: Optional[str] = "mp4"

class CreatorSettingsUpdateRequest(BaseModel):
    default_download_limit: Optional[int] = None
    download_limit: Optional[int] = None
    preferred_quality: Optional[str] = None
    auto_download_new: Optional[bool] = None

class CreatorDownloadRequest(BaseModel):
    limit: Optional[int] = None
    quality: Optional[str] = "HD"

class CreatorStartRequest(BaseModel):
    username: str
    limit: int = 3

class CreatorActionRequest(BaseModel):
    username: str

class ChangeFolderRequest(BaseModel):
    folder: str

# --- User Profile Endpoints ---
@router.get("/me")
async def api_get_me(auth: Dict[str, Any] = Depends(get_current_user)):
    """Return authenticated user profile with real name, email, role, and plan."""
    user = auth["user"]
    return {
        "id": user["id"],
        "name": user.get("name") or user.get("username") or user["email"].split("@")[0],
        "email": user["email"],
        "username": user.get("username") or user["email"].split("@")[0],
        "role": user.get("role", "user"),
        "plan": user.get("plan", "Pro Plan"),
        "status": user.get("status", "ACTIVE")
    }

# --- Video Endpoints ---
@router.post("/video/fetch")
async def api_fetch_video(req: VideoFetchRequest, auth: Dict[str, Any] = Depends(get_current_user)):
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        meta = await loop.run_in_executor(None, fetch_video_metadata, req.url)
        return {"status": "ok", "data": meta}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/video/download")
@router.post("/download")
async def api_download_video(req: VideoDownloadRequest, auth: Dict[str, Any] = Depends(get_current_user)):
    try:
        import re
        import hashlib
        user_id = auth["user"]["id"]
        clean_url = req.url.strip()

        # Extract canonical video ID or derive identifier for instant tracking
        v_match = re.search(r'/video/(\d+)', clean_url)
        video_id = v_match.group(1) if v_match else hashlib.md5(clean_url.encode()).hexdigest()[:12]

        # Duplicate protection check: if already active in queue or workers
        active = get_active_job(user_id, video_id)
        if active or download_queue.is_video_queued(video_id):
            return {
                "status": (active.get("status") if active else "queued").upper(),
                "success": True,
                "download_id": video_id,
                "message": "Video is already queued or downloading."
            }

        # Insert or update download record in DB with 'queued' state
        record_download(
            video_id=video_id,
            url=clean_url,
            title=f"TikTok Video {video_id}",
            creator="",
            status="queued",
            format=req.format or "mp4",
            quality="1080p",
            user_id=user_id
        )

        hub.update_single_download(video_id, {
            "status": "queued",
            "percent": 0.0,
            "speed": "Queued in download pool",
            "video_id": video_id,
            "user_id": user_id,
            "format": req.format or "mp4"
        })

        # Ensure background worker pool is active
        download_worker_pool.start_worker_pool()

        # Enqueue job to central 5-worker queue
        job_payload = {
            "video_id": video_id,
            "url": clean_url,
            "format": req.format or "mp4",
            "quality": "1080p",
            "user_id": user_id,
            "type": "single",
            "creator_username": "",
            "title": f"TikTok Video {video_id}"
        }
        download_queue.enqueue_download(job_payload)

        return {
            "status": "QUEUED",
            "success": True,
            "download_id": video_id,
            "message": "Download accepted and queued"
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/video/progress/{video_id}")
async def api_video_progress(video_id: str):
    prog = hub.get_single_download(video_id)
    if not prog:
        return {"status": "unknown"}
    return {"status": "ok", "data": prog}

@router.get("/downloads/active")
async def api_get_active_downloads(auth: Dict[str, Any] = Depends(get_current_user)):
    user_id = auth["user"]["id"]
    summary = hub.get_active_summary(user_id=user_id)
    recent = get_recent_downloads(limit=5, user_id=user_id)
    summary["recent_completed"] = recent
    summary["device_download_dir"] = str(get_active_downloads_dir())
    summary["worker_status"] = download_worker_pool.get_status()
    return {"status": "ok", "data": summary}

@router.get("/workers/status")
async def api_workers_status(auth: Dict[str, Any] = Depends(get_current_user)):
    return {"status": "ok", "data": download_worker_pool.get_status()}

# --- Creator Endpoints ---
@router.post("/creator/fetch")
async def api_fetch_creator(req: CreatorFetchRequest, auth: Dict[str, Any] = Depends(get_current_user)):
    try:
        info = await creator_mgr.async_fetch_creator_info(
            req.username,
            user_id=auth["user"]["id"],
            refresh=bool(req.refresh),
            limit=req.limit or 3
        )
        return {"status": "ok", "data": info}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/creators")
async def api_save_creator(req: CreatorSaveRequest, auth: Dict[str, Any] = Depends(get_current_user)):
    user_id = auth["user"]["id"]
    clean_user = req.username.lstrip("@").strip()
    c = save_creator(
        username=clean_user,
        nickname=req.nickname or clean_user,
        avatar=req.avatar_url or "",
        bio=req.bio or f"TikTok Creator @{clean_user}",
        download_limit=req.download_limit or 3,
        preferred_quality=req.preferred_quality or "mp4",
        user_id=user_id
    )
    return {"status": "ok", "data": c}

@router.get("/creators")
async def api_get_creators(auth: Dict[str, Any] = Depends(get_current_user)):
    saved = get_saved_creators(user_id=auth["user"]["id"])
    return {"status": "ok", "data": saved}

@router.get("/creators/{creator_id}")
async def api_get_creator(creator_id: int, auth: Dict[str, Any] = Depends(get_current_user)):
    creator = get_user_creator(auth["user"]["id"], creator_id)
    if not creator:
        raise HTTPException(status_code=404, detail="Creator not found")
    return {"status": "ok", "data": creator}

@router.delete("/creators/{creator_id}")
async def api_delete_creator(creator_id: int, auth: Dict[str, Any] = Depends(get_current_user)):
    deleted = delete_user_creator(auth["user"]["id"], creator_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Creator not found or already deleted")
    return {"status": "deleted", "creator_id": creator_id}

@router.get("/creators/{creator_id}/settings")
async def api_get_creator_settings(creator_id: int, auth: Dict[str, Any] = Depends(get_current_user)):
    """Retrieve persistent creator settings (default_download_limit, preferred_quality) for this user."""
    user_id = auth["user"]["id"]
    creator = get_user_creator(user_id, creator_id)
    if not creator:
        raise HTTPException(status_code=404, detail="Creator not found")
    
    settings = get_user_creator_settings(user_id, creator_id) or {}
    limit = settings.get("default_download_limit") or creator.get("download_limit", 3)
    quality = settings.get("preferred_quality") or creator.get("preferred_quality", "1080p")
    
    return {
        "creator_id": creator_id,
        "default_download_limit": limit,
        "preferred_quality": quality
    }

@router.put("/creators/{creator_id}/settings")
@router.post("/creators/{creator_id}/settings")
async def api_update_creator_settings(creator_id: int, req: CreatorSettingsUpdateRequest, auth: Dict[str, Any] = Depends(get_current_user)):
    user_id = auth["user"]["id"]
    lim = req.default_download_limit if req.default_download_limit is not None else req.download_limit
    if lim is not None and (lim < 1 or lim > 10000):
        raise HTTPException(status_code=400, detail="Download limit must be between 1 and 10000")

    updated = update_user_creator_settings(
        user_id=user_id,
        creator_id=creator_id,
        download_limit=lim,
        preferred_quality=req.preferred_quality,
        auto_download_new=req.auto_download_new,
        default_download_limit=lim
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Creator settings not found")
    
    settings = get_user_creator_settings(user_id, creator_id) or {}
    return {
        "status": "ok",
        "message": "Creator settings updated",
        "creator_id": creator_id,
        "default_download_limit": settings.get("default_download_limit", lim or 3),
        "preferred_quality": settings.get("preferred_quality", req.preferred_quality or "1080p")
    }

@router.post("/creators/{creator_id}/download")
async def api_download_creator(creator_id: int, req: Optional[CreatorDownloadRequest] = None, auth: Dict[str, Any] = Depends(get_current_user)):
    """Start creator batch download enqueuing videos into the 5-worker download queue."""
    user_id = auth["user"]["id"]
    creator = get_user_creator(user_id, creator_id)
    if not creator:
        raise HTTPException(status_code=404, detail="Creator not found")

    settings = get_user_creator_settings(user_id, creator_id) or {}
    saved_limit = settings.get("default_download_limit") or creator.get("download_limit", 3)
    
    target_limit = req.limit if (req and req.limit is not None and req.limit > 0) else saved_limit
    clean_user = creator["username"].lstrip("@").strip()

    batch_info = creator_mgr.start_batch_download(clean_user, target_limit, user_id=user_id)
    return {
        "status": "started",
        "creator_id": creator_id,
        "username": creator["username"],
        "limit": target_limit,
        "batch_id": batch_info.get("batch_id"),
        "queued": batch_info.get("queued", target_limit),
        "active_workers": batch_info.get("active_workers", 0),
        "message": f"Creator batch download queued for {target_limit} videos across 5 concurrent workers."
    }

@router.post("/creator/start")
async def api_start_creator_download(req: CreatorStartRequest, auth: Dict[str, Any] = Depends(get_current_user)):
    batch_info = creator_mgr.start_batch_download(req.username, req.limit, user_id=auth["user"]["id"])
    return {
        "status": "started",
        "username": req.username,
        "limit": req.limit,
        "batch_id": batch_info.get("batch_id"),
        "queued": batch_info.get("queued", req.limit)
    }

@router.post("/downloads/{download_id}/retry")
@router.post("/video/retry/{download_id}")
async def api_retry_download(download_id: str, auth: Dict[str, Any] = Depends(get_current_user)):
    """Re-enqueue a failed or stopped download into the 5-worker download queue."""
    user_id = auth["user"]["id"]
    dl = None
    if download_id.isdigit():
        dl = get_download_by_id(int(download_id), user_id=user_id)
    if not dl:
        dl = get_download_by_video_id(str(download_id), user_id=user_id)
    if not dl:
        raise HTTPException(status_code=404, detail="Download record not found.")

    vid = str(dl["video_id"])
    active = get_active_job(user_id, vid)
    if active and active.get("status") in ('queued', 'downloading', 'retrying', 'verifying', 'moving'):
        return {
            "status": active["status"].upper(),
            "success": True,
            "download_id": vid,
            "message": "Download is already active or queued."
        }

    # Reset DB state to queued
    with get_db_connection() as conn:
        conn.execute("""
            UPDATE downloads
            SET status = 'queued',
                attempt_number = 1,
                error_message = NULL,
                worker_id = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND user_id = ?
        """, (dl["id"], user_id))
        conn.commit()

    hub.update_single_download(vid, {
        "status": "queued",
        "percent": 0.0,
        "speed": "Re-queued for retry",
        "video_id": vid,
        "user_id": user_id,
        "format": dl.get("format", "mp4")
    })

    download_worker_pool.start_worker_pool()
    job_payload = {
        "video_id": vid,
        "url": dl.get("url"),
        "format": dl.get("format", "mp4"),
        "quality": dl.get("quality", "1080p"),
        "user_id": user_id,
        "type": "creator" if dl.get("download_batch") else "single",
        "creator_username": dl.get("creator_id") or dl.get("creator", "").lstrip("@"),
        "title": dl.get("title", f"TikTok Video {vid}")
    }
    download_queue.enqueue_download(job_payload)

    return {
        "status": "QUEUED",
        "success": True,
        "download_id": vid,
        "message": "Video download re-queued for processing."
    }

@router.post("/downloads/continue-stopped")
@router.post("/downloads/retry-all")
@router.post("/queue/continue")
async def api_continue_stopped_downloads(auth: Dict[str, Any] = Depends(get_current_user)):
    """
    Continue and re-enqueue all downloads and queue messages that were stopped or
    failed due to network issues (DNS errors, connection timeouts, or offline network drops).
    """
    user = auth["user"]
    user_id = None if user.get("role") == "admin" else user.get("id")
    recovered = download_worker_pool.continue_network_stopped_jobs(user_id=user_id)
    return {
        "status": "ok",
        "success": True,
        "recovered_count": len(recovered),
        "message": f"Successfully resumed {len(recovered)} download(s) stopped by network issues.",
        "recovered_video_ids": [r.get("video_id") for r in recovered]
    }

@router.get("/downloads/network-stopped-count")
async def api_network_stopped_count(auth: Dict[str, Any] = Depends(get_current_user)):
    """Return number of downloads currently stopped or failed due to network issues."""
    user = auth["user"]
    user_id = None if user.get("role") == "admin" else user.get("id")
    from src.app.database.db import get_network_stopped_count
    cnt = get_network_stopped_count(user_id=user_id)
    return {"status": "ok", "count": cnt}

@router.post("/creator/pause")
async def api_pause_creator_download(req: CreatorActionRequest, auth: Dict[str, Any] = Depends(get_current_user)):
    creator_mgr.pause(req.username, user_id=auth["user"]["id"])
    return {"status": "paused", "username": req.username}

@router.post("/creator/resume")
async def api_resume_creator_download(req: CreatorActionRequest, auth: Dict[str, Any] = Depends(get_current_user)):
    creator_mgr.resume(req.username, user_id=auth["user"]["id"])
    return {"status": "resumed", "username": req.username}

@router.post("/creator/stop")
async def api_stop_creator_download(req: CreatorActionRequest, auth: Dict[str, Any] = Depends(get_current_user)):
    creator_mgr.stop(req.username, user_id=auth["user"]["id"])
    return {"status": "stopped", "username": req.username}

@router.get("/creator/progress/{username}")
async def api_get_creator_progress(username: str, auth: Dict[str, Any] = Depends(get_current_user)):
    user_id = auth["user"]["id"]
    clean_user = username.lstrip("@").strip()
    mem_prog = hub.get_creator(clean_user, user_id=user_id)
    db_prog = get_creator_progress(clean_user, user_id=user_id) or {}
    activities = get_creator_activities(clean_user, limit=20, user_id=user_id)

    mem_status = mem_prog.get("status") if mem_prog else None
    db_status = db_prog.get("status", "idle")
    effective_status = mem_status if mem_status and mem_status != "idle" else db_status

    creator_info = get_creator_by_username(clean_user) or {}
    avatar = creator_info.get("avatar_url") or creator_info.get("avatar") or ""
    nickname = creator_info.get("nickname") or clean_user

    creator_videos = hub.get_creator_videos(clean_user, user_id=user_id)

    res = {
        "creator": f"@{clean_user}",
        "username": clean_user,
        "nickname": nickname,
        "avatar": avatar,
        "avatar_url": avatar,
        "status": effective_status,
        "total": mem_prog.get("total") or db_prog.get("total_found", 0),
        "limit": mem_prog.get("limit") or db_prog.get("download_limit", 50),
        "completed": mem_prog.get("completed") if mem_prog and mem_prog.get("completed", 0) > 0 else db_prog.get("completed_count", 0),
        "remaining": mem_prog.get("remaining") or db_prog.get("remaining_count", 0),
        "failed": mem_prog.get("failed") or db_prog.get("failed_count", 0),
        "skipped": mem_prog.get("skipped") or db_prog.get("skipped_count", 0),
        "current_video": mem_prog.get("current_video") or db_prog.get("current_video_title", ""),
        "speed": mem_prog.get("speed") or db_prog.get("current_speed", "0 KB/s"),
        "percent": mem_prog.get("percent") or db_prog.get("current_percent", 0.0),
        "activities": mem_prog.get("activities") or activities,
        "videos": creator_videos
    }
    return {"status": "ok", "data": res}

# --- Device File & Folder Action Endpoints ---
def _find_user_download(download_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    dl = None
    if str(download_id).isdigit():
        dl = get_download_by_id(int(download_id), user_id=user_id)
    if not dl:
        dl = get_download_by_video_id(str(download_id), user_id=user_id)
    return dl

@router.get("/downloads/{download_id}")
async def api_get_download(download_id: str, auth: Dict[str, Any] = Depends(get_current_user)):
    dl = _find_user_download(download_id, auth["user"]["id"])
    if not dl:
        raise HTTPException(status_code=404, detail="Download record not found.")
    return {"status": "ok", "data": dl}

@router.post("/downloads/{download_id}/open-file")
async def api_open_download_file(download_id: str, auth: Dict[str, Any] = Depends(get_current_user)):
    dl = _find_user_download(download_id, auth["user"]["id"])
    if not dl or not dl.get("filepath"):
        raise HTTPException(status_code=404, detail="Download record or filepath not found.")
    res = open_file(dl["filepath"])
    if res["status"] == "error":
        raise HTTPException(status_code=400, detail=res["message"])
    return res

@router.post("/downloads/{download_id}/open-folder")
async def api_open_download_folder(download_id: str, auth: Dict[str, Any] = Depends(get_current_user)):
    dl = _find_user_download(download_id, auth["user"]["id"])
    if not dl or not dl.get("filepath"):
        raise HTTPException(status_code=404, detail="Download record not found.")
    res = open_folder(dl["filepath"])
    if res["status"] == "error":
        raise HTTPException(status_code=400, detail=res["message"])
    return res

# --- Batch Endpoints ---
@router.get("/download-batches")
async def api_get_download_batches(auth: Dict[str, Any] = Depends(get_current_user)):
    user_id = auth["user"]["id"]
    batches = get_download_batches(user_id=user_id)
    return {"status": "ok", "data": batches}

@router.get("/download-batches/{batch_id}")
async def api_get_download_batch(batch_id: str, auth: Dict[str, Any] = Depends(get_current_user)):
    user_id = auth["user"]["id"]
    batch = get_download_batch_by_id(batch_id, user_id=user_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Download batch not found.")
    return {"status": "ok", "data": batch}

@router.post("/settings/open-folder")
async def api_open_active_download_folder(auth: Dict[str, Any] = Depends(get_current_user)):
    target = get_active_downloads_dir()
    res = open_folder(str(target))
    if res["status"] == "error":
        raise HTTPException(status_code=400, detail=res["message"])
    return res

@router.post("/settings/change-folder")
async def api_change_download_folder(req: ChangeFolderRequest, auth: Dict[str, Any] = Depends(get_current_user)):
    from pathlib import Path
    p = Path(req.folder).expanduser().resolve()
    writable, err = verify_directory_writable(p)
    if not writable:
        raise HTTPException(status_code=400, detail="Download cannot continue because the selected folder is unavailable or not writable.")
    update_settings({"downloadDir": str(p)})
    return {"status": "saved", "folder": str(p)}

@router.post("/settings/choose-folder")
async def api_choose_download_folder(auth: Dict[str, Any] = Depends(get_current_user)):
    """Trigger native OS folder picker dialog."""
    import asyncio
    from pathlib import Path
    loop = asyncio.get_event_loop()
    chosen = await loop.run_in_executor(None, pick_native_directory)
    if chosen:
        p = Path(chosen).expanduser().resolve()
        writable, err = verify_directory_writable(p)
        if not writable:
            raise HTTPException(status_code=400, detail="The selected folder is unavailable or not writable.")
        update_settings({"downloadDir": str(p)})
        return {"status": "saved", "folder": str(p)}
    return {"status": "cancelled", "folder": str(get_active_downloads_dir())}

# --- History & Stats Endpoints ---
@router.get("/history")
async def api_get_history(auth: Dict[str, Any] = Depends(get_current_user)):
    user_id = auth["user"]["id"]
    reconcile_all_downloads(user_id=user_id)
    downloads = get_recent_downloads(limit=50, user_id=user_id)
    return {"status": "ok", "data": downloads}

@router.delete("/history/{download_id}")
async def api_delete_history(download_id: int, auth: Dict[str, Any] = Depends(get_current_user)):
    user_id = auth["user"]["id"]
    delete_download(download_id, user_id=user_id)
    return {"status": "deleted", "id": download_id}

@router.get("/stats")
@router.get("/statistics")
async def api_get_stats(days: Optional[int] = None, auth: Dict[str, Any] = Depends(get_current_user)):
    user_id = auth["user"]["id"]
    filter_days = None if (days is None or days <= 0) else days
    st = get_stats(user_id=user_id, days=filter_days)
    return {"status": "ok", "data": st}

# --- Settings Endpoints ---
@router.get("/settings")
async def api_get_settings(auth: Dict[str, Any] = Depends(get_current_user)):
    s = get_settings()
    s["activeDownloadDir"] = str(get_active_downloads_dir())
    return {"status": "ok", "data": s}

@router.post("/settings")
async def api_update_settings(settings_data: Dict[str, Any], auth: Dict[str, Any] = Depends(get_current_user)):
    update_settings(settings_data)
    return {"status": "saved"}


# =========================================================================
# User Feedback Endpoints (Sections 35-41, 57-58)
# =========================================================================

ALLOWED_FEEDBACK_TYPES = {
    "General Feedback",
    "Bug Report",
    "Feature Request",
    "Download Problem",
    "Creator Fetch Problem",
    "Other"
}

class FeedbackCreateRequest(BaseModel):
    type: str = "General Feedback"
    subject: str
    message: str
    rating: Optional[int] = None
    attachment_url: Optional[str] = None
    # Optional field that may be passed by clients - backend will strictly ignore it
    user_id: Optional[str] = None

@router.post("/feedback", status_code=201)
async def api_submit_feedback(req: FeedbackCreateRequest, auth: Dict[str, Any] = Depends(get_current_user)):
    """
    Submit real user feedback.
    The backend strictly derives user_id from the authenticated token.
    Never trusts client-provided user_id.
    """
    user_id = auth["user"]["id"]
    fb_type = req.type.strip() if req.type else "General Feedback"
    if fb_type not in ALLOWED_FEEDBACK_TYPES:
        fb_type = "General Feedback"

    subject = req.subject.strip()
    if not subject:
        raise HTTPException(status_code=400, detail="Feedback subject cannot be empty.")

    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Feedback message cannot be empty.")

    rating = req.rating
    if rating is not None:
        try:
            rating = max(1, min(5, int(rating)))
        except (ValueError, TypeError):
            rating = None

    rec = create_feedback_record({
        "user_id": user_id,
        "type": fb_type,
        "subject": subject,
        "message": message,
        "rating": rating,
        "status": "OPEN",
        "priority": "MEDIUM",
        "attachment_url": req.attachment_url
    })

    return {
        "status": "ok",
        "message": "Feedback submitted successfully.",
        "data": rec
    }

@router.get("/feedback/my")
async def api_get_my_feedback(auth: Dict[str, Any] = Depends(get_current_user)):
    """Retrieve all feedback submitted by the authenticated user."""
    user_id = auth["user"]["id"]
    feedbacks = get_user_feedback_list(user_id)
    return {"status": "ok", "data": feedbacks}

@router.get("/feedback/{feedback_id}")
async def api_get_feedback_detail(feedback_id: str, auth: Dict[str, Any] = Depends(get_current_user)):
    """
    Retrieve single feedback detail.
    Strictly verifies ownership: User A cannot view User B's feedback.
    """
    user_id = auth["user"]["id"]
    rec = get_feedback_by_id(feedback_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Feedback not found.")

    if rec.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="You do not have permission to view this feedback.")

    return {"status": "ok", "data": rec}

@router.post("/feedback/{feedback_id}/reopen")
async def api_reopen_feedback(feedback_id: str, auth: Dict[str, Any] = Depends(get_current_user)):
    """
    Reopen resolved/closed feedback if user indicates problem persists.
    Strictly verifies ownership and updates audit log.
    """
    user_id = auth["user"]["id"]
    rec = get_feedback_by_id(feedback_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Feedback not found.")

    if rec.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="You do not have permission to modify this feedback.")

    prev_status = rec.get("status", "OPEN")
    updated = update_feedback_record(feedback_id, {
        "status": "OPEN",
        "resolved_at": None,
        "resolved_by_admin_id": None
    })

    add_feedback_audit_log(
        feedback_id=feedback_id,
        actor_id=user_id,
        actor_email=auth["user"].get("email"),
        action="FEEDBACK_REOPENED",
        previous_value=prev_status,
        new_value="OPEN"
    )

    return {
        "status": "ok",
        "message": "Feedback reopened successfully.",
        "data": updated
    }

