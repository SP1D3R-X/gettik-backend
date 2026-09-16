"""
Gettik Asynchronous Background Download Worker Pool & Central Concurrency Manager.
Enforces:
  1. Exactly ONE central download queue consumed by GETTIK_DOWNLOAD_WORKERS (default: 5)
  2. Hard server-side concurrency limit (never exceeds 5 simultaneous downloads)
  3. Single video and Creator batch downloads share the exact same 5-worker pool
  4. Atomic job claiming to prevent duplicate worker execution
  5. Recovery of stale / orphaned jobs on startup
  6. Integration with the 3-attempt provider retry system & physical verification
"""

import sys
import os
import time
import signal
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Any, Optional, List, Set

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.app.config import (
    get_active_downloads_dir,
    APP_NAME,
    APP_VERSION,
    GETTIK_DOWNLOAD_WORKERS
)
from src.app.workers.redis_queue import download_queue
from src.app.services.download_engine import download_video_with_retry
from src.app.services.progress_hub import hub
from src.app.database.db import (
    init_db,
    claim_download_job,
    recover_stale_downloads,
    recover_network_stopped_downloads,
    get_network_stopped_count,
    get_download_stats_counts
)

# Run DB schema check on worker start
init_db()

_RUNNING = True


def signal_handler(signum, frame):
    global _RUNNING
    print(f"\n[WorkerPool] Caught signal {signum}, gracefully shutting down...")
    _RUNNING = False


try:
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
except Exception:
    pass


class DownloadWorkerPool:
    """
    Central Bounded Concurrency & Worker Slot Manager.
    Enforces a strict ceiling of GETTIK_DOWNLOAD_WORKERS (default: 5)
    concurrent download jobs across the entire Gettik application.
    """
    def __init__(self, capacity: Optional[int] = None):
        self.capacity = capacity or GETTIK_DOWNLOAD_WORKERS
        self._semaphore = threading.BoundedSemaphore(self.capacity)
        self._active_videos: Set[str] = set()
        self._active_count = 0
        self._lock = threading.Lock()
        self._worker_threads: List[threading.Thread] = []
        self._is_running = False

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._active_count

    def is_video_active(self, video_id: str) -> bool:
        with self._lock:
            return str(video_id) in self._active_videos

    def acquire_slot(self, video_id: Optional[str] = None, timeout: Optional[float] = None) -> bool:
        """
        Acquire 1 of the 5 concurrent worker slots.
        Also enforces duplicate protection if video_id is supplied.
        """
        if video_id:
            vid_str = str(video_id)
            with self._lock:
                if vid_str in self._active_videos:
                    # Video is already actively being processed by another worker slot
                    return False

        if timeout is not None:
            acquired = self._semaphore.acquire(timeout=timeout)
        else:
            acquired = self._semaphore.acquire()

        if acquired:
            with self._lock:
                self._active_count += 1
                if video_id:
                    self._active_videos.add(str(video_id))
        return acquired

    def release_slot(self, video_id: Optional[str] = None):
        """Release a worker slot upon download completion or failure."""
        with self._lock:
            if video_id and str(video_id) in self._active_videos:
                self._active_videos.discard(str(video_id))
            if self._active_count > 0:
                self._active_count -= 1
        try:
            self._semaphore.release()
        except ValueError:
            pass

    @contextmanager
    def slot(self, video_id: Optional[str] = None, timeout: Optional[float] = None):
        """Context manager helper for safe worker slot acquisition and release."""
        acquired = self.acquire_slot(video_id=video_id, timeout=timeout)
        if not acquired:
            raise RuntimeError(f"Could not acquire download worker slot for video {video_id} (All {self.capacity} slots busy or video already active).")
        try:
            yield
        finally:
            self.release_slot(video_id=video_id)

    def process_job(self, job: Dict[str, Any], worker_id: int = 1) -> Dict[str, Any]:
        """
        Execute a single download job with 3-provider retry pipeline & physical verification.
        Worker remains occupied until the logical job completes or fails all retries.
        """
        video_id = str(job.get("video_id", ""))
        job_type = job.get("type", "single")
        creator_user = job.get("creator_username", "")

        print(f"[WORKER {worker_id}] Claimed download {video_id} (Type: {job_type})")

        # Execute download via production multi-strategy retry engine
        result = download_video_with_retry(job, worker_id=worker_id)

        # Notify creator manager if this job was part of a creator batch
        if job_type == "creator":
            try:
                from src.app.services.creator_service import creator_mgr
                creator_mgr.on_creator_video_finished(job, result, worker_id=worker_id)
            except Exception as e:
                print(f"[WORKER {worker_id}] Error notifying creator manager for {video_id}: {e}")

        status_str = result.get("status", "completed")
        print(f"[WORKER {worker_id}] Download {video_id} finished with status '{status_str}'")
        return result

    def _worker_loop(self, worker_id: int):
        """Dedicated loop for Worker #{worker_id} consuming from central queue."""
        while self._is_running and _RUNNING:
            try:
                # Dequeue next pending download task (blocking with 1s timeout)
                job = download_queue.dequeue_download(timeout=1)
                if not job:
                    continue

                video_id = str(job.get("video_id", ""))
                user_id = str(job.get("user_id", "default_user"))

                # Check if creator download is currently paused or stopped
                if job.get("type") == "creator":
                    from src.app.services.creator_service import creator_mgr
                    clean_u = job.get("creator_username", "")
                    if creator_mgr.is_stopped(clean_u, user_id=user_id):
                        print(f"[WORKER {worker_id}] Skipping video {video_id} (Creator batch stopped)")
                        continue
                    creator_mgr.wait_if_paused(clean_u, user_id=user_id)

                # Acquire worker slot (enforces capacity & duplicate active video check)
                if not self.acquire_slot(video_id=video_id, timeout=0.1):
                    # Job is already active in another worker slot, skip
                    continue

                try:
                    # Atomic claim in SQLite
                    claim_download_job(video_id, worker_id=worker_id, user_id=user_id)
                    # Process download through the 3-provider retry pipeline
                    self.process_job(job, worker_id=worker_id)
                except Exception as proc_err:
                    print(f"[WORKER {worker_id}] Unhandled error processing video {video_id}: {proc_err}")
                finally:
                    self.release_slot(video_id=video_id)

            except Exception as loop_err:
                time.sleep(0.5)

    def start_worker_pool(self, num_workers: Optional[int] = None):
        """Start the background worker slot threads and recover stale jobs."""
        count = num_workers or self.capacity
        if self._is_running:
            return
        self._is_running = True

        # Recovery on startup: reset stale downloading jobs and re-enqueue queued jobs
        try:
            stale_jobs = recover_stale_downloads()
            if stale_jobs:
                print(f"[WorkerPool] Recovered {len(stale_jobs)} orphaned / queued download jobs on startup.")
                for sj in stale_jobs:
                    download_queue.enqueue_download({
                        "video_id": sj.get("video_id"),
                        "url": sj.get("url"),
                        "title": sj.get("title"),
                        "format": sj.get("format", "mp4"),
                        "quality": sj.get("quality", "1080p"),
                        "user_id": sj.get("user_id", "default_user"),
                        "creator": sj.get("creator"),
                        "creator_username": sj.get("creator_id") or sj.get("creator", "").lstrip("@"),
                        "type": "creator" if sj.get("download_batch") else "single"
                    })
        except Exception as rec_err:
            print(f"[WorkerPool] Stale download recovery check: {rec_err}")

        # Launch the 5 worker threads
        self._worker_threads = []
        for i in range(1, count + 1):
            t = threading.Thread(
                target=self._worker_loop,
                args=(i,),
                daemon=True,
                name=f"GettikDownloadWorker-{i}"
            )
            t.start()
            self._worker_threads.append(t)
        print(f"[WorkerPool] Successfully started {len(self._worker_threads)} concurrent download worker slots (Capacity: {self.capacity}).")

    def stop_worker_pool(self):
        """Gracefully stop all worker slots."""
        self._is_running = False

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            stats = get_download_stats_counts()
            q_depth = download_queue.get_queue_depth()
            return {
                "capacity": self.capacity,
                "active_workers": self._active_count,
                "available_slots": max(0, self.capacity - self._active_count),
                "active_videos": list(self._active_videos),
                "queued_count": q_depth,
                "pool_running": self._is_running,
                "stats": stats
            }

    def continue_network_stopped_jobs(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Resume all downloads and queue messages stopped or failed due to network issues.
        Re-enqueues them into the central download queue and activates the 5-worker pool.
        """
        recovered_jobs = recover_network_stopped_downloads(user_id=user_id)
        for job in recovered_jobs:
            vid = str(job.get("video_id", ""))
            creator_u = job.get("creator_id") or (job.get("creator") or "").lstrip("@")
            hub.update_single_download(vid, {
                "status": "queued",
                "percent": 0.0,
                "speed": "Reconnected — queued",
                "video_id": vid,
                "user_id": job.get("user_id", "default_user"),
                "format": job.get("format", "mp4"),
                "title": job.get("title", f"Video {vid}")
            })
            download_queue.enqueue_download({
                "video_id": vid,
                "url": job.get("url"),
                "title": job.get("title"),
                "format": job.get("format", "mp4"),
                "quality": job.get("quality", "1080p"),
                "user_id": job.get("user_id", "default_user"),
                "creator": job.get("creator"),
                "creator_username": creator_u,
                "type": "creator" if job.get("download_batch") else "single"
            })

        self.start_worker_pool()
        return recovered_jobs


# Global singleton instance
download_worker_pool = DownloadWorkerPool()


# Backward compatibility functions
def process_job(job: dict):
    return download_worker_pool.process_job(job, worker_id=1)


def run_worker_loop():
    """Main worker processing loop for standalone worker process."""
    print("=" * 60)
    print(f"  {APP_NAME} Background Download Worker Pool v{APP_VERSION}")
    print(f"  Worker Slots:   {download_worker_pool.capacity}")
    print(f"  Redis Active:   {download_queue.is_redis_active()}")
    print(f"  Storage Target: {get_active_downloads_dir()}")
    print("=" * 60)
    download_worker_pool.start_worker_pool()

    while _RUNNING:
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            break
