"""
Provider 4: TikTok Web Item List API Provider
Directly interfaces with TikTok's Web Item List endpoints for video discovery and creator author items.
"""

import json
import urllib.parse
from typing import Dict, Any, List, Optional

from .base import BaseCreatorProvider, NormalizedCreator, NormalizedVideo
from .http_client import get_async_http_client


class TikTokItemApiProvider(BaseCreatorProvider):
    def __init__(self, priority: int = 40, timeout_seconds: float = 6.0):
        super().__init__(name="tiktok_item_api", priority=priority, timeout_seconds=timeout_seconds)

    async def fetch_creator(self, username: str, limit: int = 20) -> Optional[NormalizedCreator]:
        clean_user = username.lstrip("@").strip()
        if not clean_user:
            return None

        client = get_async_http_client()
        # Query TikTok web post item_list endpoint
        params = {
            "aid": "1988",
            "app_name": "tiktok_web",
            "device_platform": "web_pc",
            "count": str(min(limit, 30)),
            "cursor": "0"
        }
        
        # We can query user detail or item list
        url = f"https://www.tiktok.com/api/post/item_list/?{urllib.parse.urlencode(params)}"
        headers = {
            "Referer": f"https://www.tiktok.com/@{clean_user}"
        }

        try:
            resp = await client.get(url, headers=headers, timeout=self.timeout_seconds)
            if resp.status_code != 200:
                return None

            data = resp.json()
            item_list = data.get("itemList") or []
            if not item_list:
                return None

            first_item = item_list[0]
            author = first_item.get("author") or {}
            
            # Verify author matches requested user
            auth_unique = author.get("uniqueId") or author.get("unique_id") or ""
            if auth_unique and auth_unique.lower() != clean_user.lower():
                return None

            avatar_url = author.get("avatarLarger") or author.get("avatarMedium") or author.get("avatarThumb") or ""
            if not avatar_url:
                return None

            nickname = author.get("nickname") or clean_user
            bio = author.get("signature") or ""
            verified = bool(author.get("verified", False))

            author_stats = first_item.get("authorStats") or {}
            followers = int(author_stats.get("followerCount", 0))
            following = int(author_stats.get("followingCount", 0))
            likes = int(author_stats.get("heartCount", 0))
            video_count = int(author_stats.get("videoCount", 0))

            videos: List[NormalizedVideo] = []
            for item in item_list[:limit]:
                vid = str(item.get("id") or "")
                if not vid:
                    continue
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
            "type": "web_api"
        }

