"""
Gettik Production Redis Queue & Progress PubSub Client
Provides decoupled asynchronous background queueing for long-running video and creator downloads.
Includes transparent fallback to in-memory queues when Redis is not configured or in local development.
"""

import json
import time
import queue
from typing import Dict, Any, Optional
from src.app.config import REDIS_URL

class DownloadQueueManager:
    def __init__(self):
        self.redis_client = None
        self._is_connected = False
        self._memory_queue = queue.Queue()
        self._memory_progress: Dict[str, Dict[str, Any]] = {}
        self._init_client()

    def _init_client(self):
        if REDIS_URL:
            try:
                import redis
                client = redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
                client.ping()
                self.redis_client = client
                self._is_connected = True
                print(f"[RedisQueue] Successfully connected to Redis at {REDIS_URL}")
            except Exception as e:
                print(f"[RedisQueue] Redis connection failed ({e}), falling back to in-memory queue.")
                self.redis_client = None
                self._is_connected = False
        else:
            self.redis_client = None
            self._is_connected = False

    def is_redis_active(self) -> bool:
        if not self._is_connected or not self.redis_client:
            return False
        try:
            return self.redis_client.ping()
        except Exception:
            self._is_connected = False
            return False

    def enqueue_download(self, job_data: Dict[str, Any]) -> str:
        """Push a video download task onto the queue."""
        payload = json.dumps(job_data)
        if self.is_redis_active():
            self.redis_client.rpush("gettik:queue:downloads", payload)
        else:
            self._memory_queue.put(job_data)
        return job_data.get("video_id", "")

    def dequeue_download(self, timeout: int = 2) -> Optional[Dict[str, Any]]:
        """Pop a video download task from the queue with blocking timeout."""
        if self.is_redis_active():
            item = self.redis_client.blpop("gettik:queue:downloads", timeout=timeout)
            if item:
                # item is tuple: (key_name, value)
                return json.loads(item[1])
            return None
        else:
            try:
                return self._memory_queue.get(timeout=timeout)
            except queue.Empty:
                return None

    def publish_progress(self, video_id: str, progress_data: Dict[str, Any]):
        """Publish real-time download progress and cache state."""
        progress_data["updated_at"] = time.time()
        payload = json.dumps(progress_data)
        if self.is_redis_active():
            try:
                # Cache latest progress for 2 hours
                self.redis_client.set(f"gettik:progress:{video_id}", payload, ex=7200)
                # Publish event for subscribers
                self.redis_client.publish(f"gettik:events:{video_id}", payload)
            except Exception as e:
                print(f"[RedisQueue] Progress pubsub error: {e}")
        else:
            self._memory_progress[video_id] = progress_data

    def get_progress(self, video_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve latest progress for a given video ID."""
        if self.is_redis_active():
            try:
                data = self.redis_client.get(f"gettik:progress:{video_id}")
                return json.loads(data) if data else None
            except Exception:
                return None
        return self._memory_progress.get(video_id)

    def get_queue_depth(self) -> int:
        """Return the current number of pending tasks in queue."""
        if self.is_redis_active():
            try:
                return int(self.redis_client.llen("gettik:queue:downloads"))
            except Exception:
                return 0
        return self._memory_queue.qsize()

    def get_queued_jobs(self) -> list:
        """Return snapshot list of pending jobs in queue without popping."""
        if self.is_redis_active():
            try:
                raw_items = self.redis_client.lrange("gettik:queue:downloads", 0, -1)
                return [json.loads(i) for i in raw_items]
            except Exception:
                return []
        else:
            with self._memory_queue.mutex:
                return [dict(j) if isinstance(j, dict) else j for j in list(self._memory_queue.queue)]

    def is_video_queued(self, video_id: str) -> bool:
        """Check if a video ID is currently sitting in the queue waiting for a worker."""
        jobs = self.get_queued_jobs()
        return any(str(j.get("video_id", "")) == str(video_id) for j in jobs)

    def clear_queue(self):
        """Clear all pending tasks from queue (e.g. for test cleanup or emergency reset)."""
        if self.is_redis_active():
            try:
                self.redis_client.delete("gettik:queue:downloads")
            except Exception:
                pass
        with self._memory_queue.mutex:
            self._memory_queue.queue.clear()

    def get_health(self) -> Dict[str, Any]:
        """Diagnostic health information for Admin and Health endpoints."""
        from src.app.config import GETTIK_DOWNLOAD_WORKERS
        connected = self.is_redis_active()
        return {
            "status": "healthy" if (connected or not REDIS_URL) else "degraded",
            "redis_configured": bool(REDIS_URL),
            "connected": connected,
            "backend": "redis" if connected else "memory_fallback",
            "queue_depth": self.get_queue_depth(),
            "worker_capacity": GETTIK_DOWNLOAD_WORKERS
        }

# Global queue singleton
download_queue = DownloadQueueManager()


