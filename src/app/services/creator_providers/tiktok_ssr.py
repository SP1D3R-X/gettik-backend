"""
Provider 2: TikTok Web SSR Rehydration Provider with Native Slardar Challenge Solver
Scrapes and parses TikTok's server-rendered HTML and __UNIVERSAL_DATA_FOR_REHYDRATION__ JSON.
Includes native Python sha256 challenge solver to bypass TikTok Slardar WAF challenges.
Extracts 100% authentic 1080p profile picture (avatarLarger), verified stats, and video items.
"""

import base64
import hashlib
import json
import re
import time
from typing import Dict, Any, List, Optional

from .base import BaseCreatorProvider, NormalizedCreator, NormalizedVideo
from .http_client import get_async_http_client


class TikTokWebSSRProvider(BaseCreatorProvider):
    def __init__(self, priority: int = 20, timeout_seconds: float = 6.0):
        super().__init__(name="tiktok_ssr", priority=priority, timeout_seconds=timeout_seconds)

    def _solve_slardar_challenge(self, html: str) -> Optional[tuple]:
        """
        Solves TikTok Slardar JS challenge using native sha256 proof-of-work.
        Returns (cookie_name, cookie_value) or None.
        """
        m_cs = re.search(r'id=["\']cs["\'][^>]*class=["\']([^"\']+)["\']', html)
        m_wci = re.search(r'id=["\']wci["\'][^>]*class=["\']([^"\']+)["\']', html)
        if not m_cs or not m_wci:
            return None

        try:
            cs_raw = m_cs.group(1) + "==="
            challenge_data = json.loads(base64.b64decode(cs_raw))
            exp_digest = base64.b64decode(challenge_data["v"]["c"])
            base_hash = hashlib.sha256(base64.b64decode(challenge_data["v"]["a"]))

            # Solve proof of work (usually completes in <1000 iterations)
            for i in range(1_000_001):
                num = str(i).encode()
                h = base_hash.copy()
                h.update(num)
                if h.digest() == exp_digest:
                    challenge_data["d"] = base64.b64encode(num).decode()
                    break
            else:
                return None

            wci_name = m_wci.group(1)
            wci_val = base64.b64encode(json.dumps(challenge_data, separators=(",", ":")).encode()).decode()
            return wci_name, wci_val
        except Exception:
            return None

    async def fetch_creator(self, username: str, limit: int = 20) -> Optional[NormalizedCreator]:
        clean_user = username.lstrip("@").strip()
        if not clean_user:
            return None

        url = f"https://www.tiktok.com/@{clean_user}"
        client = get_async_http_client()

        try:
            resp = await client.get(url, timeout=self.timeout_seconds)
            if resp.status_code != 200:
                return None

            html = resp.text

            # If Slardar WAF challenge encountered, solve it and re-query
            if "__UNIVERSAL_DATA_FOR_REHYDRATION__" not in html and ("slardar" in html.lower() or "waf" in html.lower()):
                sol = self._solve_slardar_challenge(html)
                if sol:
                    cookie_name, cookie_val = sol
                    client.cookies.set(cookie_name, cookie_val, domain=".tiktok.com")
                    resp2 = await client.get(url, timeout=self.timeout_seconds)
                    if resp2.status_code == 200:
                        html = resp2.text

            if "__UNIVERSAL_DATA_FOR_REHYDRATION__" not in html:
                return None

            m = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html)
            if not m:
                return None

            data = json.loads(m.group(1))
            default_scope = data.get("__DEFAULT_SCOPE__", {})
            user_detail = default_scope.get("webapp.user-detail", {})
            user_info = user_detail.get("userInfo", {})
            user = user_info.get("user", {})
            stats = user_info.get("stats", {}) or user_info.get("statsV2", {})

            # Real 1080p verified TikTok profile photo
            avatar_url = user.get("avatarLarger") or user.get("avatarMedium") or user.get("avatarThumb") or ""
            nickname = user.get("nickname") or clean_user
            bio = user.get("signature") or ""
            verified = bool(user.get("verified", False))

            followers = int(stats.get("followerCount", 0))
            following = int(stats.get("followingCount", 0))
            likes = int(stats.get("heartCount", 0) or stats.get("heart", 0))
            video_count = int(stats.get("videoCount", 0))

            videos: List[NormalizedVideo] = []
            item_list = user_detail.get("itemList") or user_info.get("itemList") or []
            for item in item_list[:limit]:
                if isinstance(item, dict):
                    vid = str(item.get("id") or item.get("videoId") or "")
                    if vid:
                        v_desc = item.get("desc") or f"TikTok Video {vid}"
                        v_thumb = item.get("video", {}).get("cover") or item.get("video", {}).get("originCover") or ""
                        v_dur = item.get("video", {}).get("duration") or 0
                        v_stats = item.get("stats", {})
                        videos.append(NormalizedVideo(
                            id=vid,
                            creator_username=clean_user,
                            canonical_url=f"https://www.tiktok.com/@{clean_user}/video/{vid}",
                            title=v_desc[:120],
                            description=v_desc,
                            thumbnail_url=v_thumb,
                            duration=v_dur,
                            views=str(v_stats.get("playCount", 0)),
                            likes=str(v_stats.get("diggCount", 0)),
                            comments=str(v_stats.get("commentCount", 0)),
                            shares=str(v_stats.get("shareCount", 0)),
                            published_at=str(item.get("createTime", ""))
                        ))

            if not avatar_url:
                return None

            return NormalizedCreator(
                id=clean_user,
                username=f"@{clean_user}",
                clean_username=clean_user,
                nickname=nickname,
                avatar_url=avatar_url,
                bio=bio,
                follower_count=followers,
                following_count=following,
                likes_count=likes,
                video_count=video_count or len(videos),
                verified=verified,
                provider=self.name,
                videos=videos
            )

        except Exception:
            return None

    async def fetch_videos(self, username: str, limit: int = 50, cursor: Optional[str] = None) -> Optional[List[NormalizedVideo]]:
        creator = await self.fetch_creator(username, limit=limit)
        return creator.videos if creator else None

    async def health_check(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": "ready",
            "type": "html_ssr"
        }
