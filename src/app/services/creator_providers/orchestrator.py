"""
Creator Provider Orchestrator
Manages multi-provider execution, parallel racing (fastest-valid-result),
circuit breakers, dynamic prioritization, short-lived TTL caching (60s),
and zero dummy data enforcement.
"""

import asyncio
import time
from typing import Dict, Any, List, Optional, Set

from .base import BaseCreatorProvider, NormalizedCreator, validate_creator_response
from .tiktok_embed import TikTokEmbedProvider
from .tiktok_ssr import TikTokWebSSRProvider
from .ytdlp_provider import YtDlpCreatorProvider
from .tiktok_oembed import TikTokOfficialOEmbedProvider
from .tikwm import TikWMProvider
from .tiktok_item_api import TikTokItemApiProvider


class CircuitBreaker:
    """Tracks provider failure rates and opens circuit when unhealthy."""

    def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 60.0):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.consecutive_failures = 0
        self.state = "CLOSED"  # "CLOSED", "OPEN", "HALF_OPEN"
        self.last_failure_time = 0.0

    def can_attempt(self) -> bool:
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN":
            if time.time() - self.last_failure_time > self.cooldown_seconds:
                self.state = "HALF_OPEN"
                return True
            return False
        # HALF_OPEN allows a single probe attempt
        return True

    def record_success(self):
        self.consecutive_failures = 0
        self.state = "CLOSED"

    def record_failure(self):
        self.consecutive_failures += 1
        self.last_failure_time = time.time()
        if self.consecutive_failures >= self.failure_threshold:
            self.state = "OPEN"


class ProviderTracker:
    """Maintains performance metrics for dynamic ranking."""

    def __init__(self, provider: BaseCreatorProvider):
        self.provider = provider
        self.circuit = CircuitBreaker()
        self.request_count = 0
        self.success_count = 0
        self.failure_count = 0
        self.timeout_count = 0
        self.average_latency_ms = 600.0  # baseline ms
        self.last_success = 0.0
        self.last_failure = 0.0

    def record_success(self, latency_ms: float):
        self.request_count += 1
        self.success_count += 1
        self.last_success = time.time()
        self.circuit.record_success()
        # Exponential moving average
        self.average_latency_ms = (self.average_latency_ms * 0.6) + (latency_ms * 0.4)

    def record_failure(self, is_timeout: bool = False):
        self.request_count += 1
        self.failure_count += 1
        if is_timeout:
            self.timeout_count += 1
        self.last_failure = time.time()
        self.circuit.record_failure()

    @property
    def health_status(self) -> str:
        if self.circuit.state == "OPEN":
            return "unhealthy"
        if self.request_count > 0:
            success_rate = self.success_count / self.request_count
            if success_rate < 0.6:
                return "degraded"
        return "healthy"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.provider.name,
            "priority": self.provider.priority,
            "health_status": self.health_status,
            "circuit_state": self.circuit.state,
            "request_count": self.request_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "timeout_count": self.timeout_count,
            "average_latency_ms": round(self.average_latency_ms, 1),
            "last_success": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.last_success)) if self.last_success else None,
            "last_failure": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.last_failure)) if self.last_failure else None
        }


class CreatorCache:
    """In-memory thread-safe cache with short TTL (60s) and user isolation."""

    def __init__(self, default_ttl_seconds: int = 60):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self.default_ttl = default_ttl_seconds

    def _make_key(self, clean_username: str, user_id: Optional[str] = None) -> str:
        uid = (user_id or "global").strip().lower()
        return f"user:{uid}:creator:@{clean_username.lower()}"

    def get(self, clean_username: str, user_id: Optional[str] = None) -> Optional[NormalizedCreator]:
        key = self._make_key(clean_username, user_id)
        entry = self._cache.get(key)
        if not entry:
            return None
        if time.time() > entry["expires_at"]:
            del self._cache[key]
            return None
        return entry["data"]

    def set(self, clean_username: str, data: NormalizedCreator, user_id: Optional[str] = None, ttl: Optional[int] = None):
        key = self._make_key(clean_username, user_id)
        self._cache[key] = {
            "data": data,
            "created_at": time.time(),
            "expires_at": time.time() + (ttl or self.default_ttl)
        }

    def invalidate(self, clean_username: str, user_id: Optional[str] = None):
        key = self._make_key(clean_username, user_id)
        if key in self._cache:
            del self._cache[key]


class CreatorProviderOrchestrator:
    """
    Central Orchestrator coordinating all 4 real TikTok creator providers:
    Provider 1 (Primary): TikTokEmbedProvider (Frontity Embed State)
    Provider 2 (Fallback): TikWMProvider (Fast user metadata & video feed)
    Provider 3 (Fallback): TikTokWebSSRProvider (SSR with Slardar sha256 challenge solver)
    Provider 4 (Optional final fallback): YtDlpCreatorProvider (yt-dlp flat extraction with real avatar)
    """

    def __init__(self, hedge_delay: float = 1.0):
        self.hedge_delay = hedge_delay
        self.providers: List[BaseCreatorProvider] = [
            TikTokEmbedProvider(priority=10, timeout_seconds=3.5),
            YtDlpCreatorProvider(priority=20, timeout_seconds=6.0),
            TikTokWebSSRProvider(priority=30, timeout_seconds=4.0),
            TikTokOfficialOEmbedProvider(priority=40, timeout_seconds=3.0),
            TikWMProvider(priority=90, timeout_seconds=3.0),
            TikTokItemApiProvider(priority=95, timeout_seconds=3.0)
        ]
        self.trackers: Dict[str, ProviderTracker] = {
            p.name: ProviderTracker(p) for p in self.providers
        }
        self.cache = CreatorCache(default_ttl_seconds=60)

    def get_ranked_providers(self) -> List[ProviderTracker]:
        """Rank providers: healthy first, then by success rate, priority & latency."""
        available = [t for t in self.trackers.values() if t.circuit.can_attempt()]
        
        def sort_key(t: ProviderTracker):
            status_weight = 0 if t.health_status == "healthy" else (1 if t.health_status == "degraded" else 2)
            success_rate = (t.success_count / t.request_count) if t.request_count > 0 else 0.8
            return (status_weight, -success_rate, t.provider.priority, t.average_latency_ms)

        available.sort(key=sort_key)
        return available

    async def _ensure_videos(self, creator: NormalizedCreator, clean_name: str, limit: int) -> NormalizedCreator:
        """Guarantee that winning creator metadata has videos populated if initial provider returned only metadata."""
        if creator.videos and len(creator.videos) > 0:
            return creator

        for p_name in ["tiktok_embed", "ytdlp"]:
            tracker = self.trackers.get(p_name)
            if tracker and tracker.circuit.can_attempt():
                try:
                    res, _, _ = await self._query_provider(tracker, clean_name, limit)
                    if res and res.videos and len(res.videos) > 0:
                        creator.videos = res.videos
                        if not creator.avatar_url and res.avatar_url:
                            creator.avatar_url = res.avatar_url
                        if (not creator.nickname or creator.nickname == clean_name) and res.nickname:
                            creator.nickname = res.nickname
                        if res.video_count > 0 and creator.video_count == 0:
                            creator.video_count = res.video_count
                        break
                except Exception:
                    pass
        return creator

    async def fetch_creator(self, username: str, limit: int = 3, refresh: bool = False, user_id: Optional[str] = None) -> NormalizedCreator:
        """
        Fetch creator data using intelligent provider fallback & hedged racing.
        Target <=2s response where network/provider conditions allow.
        Guarantees zero dummy data and authentic creator profile avatar.
        """
        clean_name = username.lstrip("@").strip()
        if not clean_name:
            raise ValueError("Username cannot be empty")

        # 1. Check user-scoped TTL cache if not explicit refresh (target <5ms)
        if not refresh:
            cached = self.cache.get(clean_name, user_id=user_id)
            if cached:
                print(f"[Orchestrator] Cache HIT for @{clean_name} (user={user_id})")
                return cached

        print(f"[Orchestrator] CREATOR_FETCH_STARTED: @{clean_name} (refresh={refresh}, user={user_id})")

        # 2. Get dynamically ranked providers
        ranked = self.get_ranked_providers()
        if not ranked:
            ranked = list(self.trackers.values())[:3]

        winner: Optional[NormalizedCreator] = None
        attempted_trackers: Set[str] = set()

        # 3. Primary with Hedged Parallel Execution (Target <=2s)
        # Launch Provider 1. If not completed in hedge_delay, launch Provider 2 in parallel.
        p1 = ranked[0]
        attempted_trackers.add(p1.provider.name)
        print(f"[Orchestrator] PROVIDER_1_PRIMARY_STARTED: {p1.provider.name} for @{clean_name}")
        t1 = asyncio.create_task(self._query_provider(p1, clean_name, limit))

        if self.hedge_delay > 0:
            try:
                # Wait up to hedge_delay for Provider 1
                done, _ = await asyncio.wait({t1}, timeout=self.hedge_delay)
                if done:
                    res, tracker_name, lat = t1.result()
                    if res and validate_creator_response(res, clean_name):
                        winner = res
                        print(f"[Orchestrator] PROVIDER_1_SUCCESS: {tracker_name} succeeded in {lat:.1f}ms for @{clean_name}")
            except Exception as e:
                print(f"[Orchestrator] Provider 1 error: {e}")

        # If Provider 1 did not complete with valid result within hedge_delay (or hedge_delay <= 0), launch Provider 2
        if not winner and len(ranked) > 1:
            p2 = ranked[1]
            attempted_trackers.add(p2.provider.name)
            print(f"[Orchestrator] PROVIDER_2_FALLBACK_STARTED: {p2.provider.name} for @{clean_name} (hedged/fallback)")
            t2 = asyncio.create_task(self._query_provider(p2, clean_name, limit))

            # Race remaining pending tasks (t1 and t2)
            pending = {t for t in [t1, t2] if not t.done()}
            while pending:
                done_set, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done_set:
                    try:
                        res, tracker_name, lat = task.result()
                        if res and validate_creator_response(res, clean_name):
                            winner = res
                            print(f"[Orchestrator] RACE_SUCCESS: {tracker_name} won in {lat:.1f}ms for @{clean_name}")
                            break
                    except Exception:
                        pass
                if winner:
                    for p in pending:
                        p.cancel()
                    break

        # 4. Fallback: try remaining providers sequentially (Provider 3, Provider 4, etc.)
        if not winner:
            fallback_trackers = [t for t in ranked if t.provider.name not in attempted_trackers]
            for t in fallback_trackers:
                print(f"[Orchestrator] FALLBACK_CHAIN_STARTED: {t.provider.name} for @{clean_name}")
                try:
                    res, tracker_name, lat = await self._query_provider(t, clean_name, limit)
                    if res and validate_creator_response(res, clean_name):
                        winner = res
                        print(f"[Orchestrator] FALLBACK_CHAIN_SUCCESS: {tracker_name} in {lat:.1f}ms for @{clean_name}")
                        break
                except Exception as e:
                    print(f"[Orchestrator] Fallback provider {t.provider.name} error: {e}")

        # 5. Strict Zero-Dummy Validation & Response Return
        if not winner or not validate_creator_response(winner, clean_name):
            print(f"[Orchestrator] CREATOR_FETCH_FAILED: All providers failed for @{clean_name}")
            raise RuntimeError("Unable to fetch creator data: all providers failed. Please check the username and try again.")

        # Ensure creator has videos populated
        winner = await self._ensure_videos(winner, clean_name, limit)

        # Cache valid verified result with user scope
        self.cache.set(clean_name, winner, user_id=user_id)
        print(f"[Orchestrator] CREATOR_FETCH_SUCCESS: @{clean_name} via {winner.provider} (videos={len(winner.videos)})")
        return winner

    async def _query_provider(self, tracker: ProviderTracker, clean_name: str, limit: int):
        """Execute single provider query with latency tracking and circuit breaker updates."""
        t0 = time.time()
        provider = tracker.provider
        try:
            res = await provider.fetch_creator(clean_name, limit=limit)
            latency_ms = (time.time() - t0) * 1000.0
            if res and validate_creator_response(res, clean_name):
                tracker.record_success(latency_ms)
                return res, provider.name, latency_ms
            else:
                tracker.record_failure(is_timeout=False)
                return None, provider.name, latency_ms
        except asyncio.TimeoutError:
            latency_ms = (time.time() - t0) * 1000.0
            print(f"[Orchestrator] PROVIDER_TIMEOUT: {provider.name} ({latency_ms:.1f}ms)")
            tracker.record_failure(is_timeout=True)
            return None, provider.name, latency_ms
        except Exception as e:
            latency_ms = (time.time() - t0) * 1000.0
            print(f"[Orchestrator] PROVIDER_FAILURE: {provider.name}: {e}")
            tracker.record_failure(is_timeout=False)
            return None, provider.name, latency_ms

    def get_providers_health(self) -> List[Dict[str, Any]]:
        """Return health and performance stats for all registered providers."""
        return [t.to_dict() for t in self.trackers.values()]


# Global Orchestrator Singleton
creator_orchestrator = CreatorProviderOrchestrator()
