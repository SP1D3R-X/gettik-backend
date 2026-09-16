"""
Provider 2: TikWM Creator API Provider
Queries TikWM's high-speed user endpoints for real profile metadata and video discovery.
"""

import urllib.parse
from typing import Dict, Any, List, Optional

from .base import BaseCreatorProvider, NormalizedCreator, NormalizedVideo
from .http_client import get_async_http_client


class TikWMProvider(BaseCreatorProvider):
    def __init__(self, priority: int = 20, timeout_seconds: float = 5.0):
        super().__init__(name="tikwm", priority=priority, timeout_seconds=timeout_seconds)

    async def fetch_creator(self, username: str, limit: int = 20) -> Optional[NormalizedCreator]:
        clean_user = username.lstrip("@").strip()
        if not clean_user:
            return None

        client = get_async_http_client()
        encoded = urllib.parse.quote(clean_user)

        avatar_url = ""
        nickname = clean_user
        bio = ""
        verified = False
        followers = 0
        following = 0
        likes = 0
        video_count = 0
        videos: List[NormalizedVideo] = []
        seen_vids = set()

        cursor = 0
        has_more = True
        target_count = limit if limit > 0 else 1000

        try:
            while has_more and len(videos) < target_count:
                fetch_count = min(50, target_count - len(videos))
                posts_url = f"https://www.tikwm.com/api/user/posts?unique_id={encoded}&count={fetch_count}&cursor={cursor}"
                resp = await client.get(posts_url, timeout=self.timeout_seconds)
                if resp.status_code != 200:
                    break

                res_data = resp.json()
                if res_data.get("code") != 0 or not res_data.get("data"):
                    if not videos:
                        return await self._fetch_via_user_info(clean_user, client)
                    break

                d = res_data["data"]
                raw_videos = d.get("videos") or []
                if not raw_videos:
                    break

                # Extract creator metadata on first page
                if not avatar_url:
                    if "user" in d:
                        u = d["user"]
                        avatar_url = u.get("avatarLarger") or u.get("avatarMedium") or u.get("avatarThumb") or ""
                        nickname = u.get("nickname") or clean_user
                        bio = u.get("signature") or ""
                        verified = bool(u.get("verified", False))
                        stats = d.get("stats") or {}
                        followers = int(stats.get("followerCount", 0))
                        following = int(stats.get("followingCount", 0))
                        likes = int(stats.get("heartCount", 0))
                        video_count = int(stats.get("videoCount", 0))

                    if not avatar_url and raw_videos:
                        author = raw_videos[0].get("author") or {}
                        avatar_url = author.get("avatar") or author.get("avatarThumb") or ""
                        nickname = author.get("nickname") or clean_user

                for v in raw_videos:
                    vid = str(v.get("video_id") or v.get("id") or "")
                    if not vid or vid in seen_vids:
                        continue
                    seen_vids.add(vid)
                    v_title = v.get("title") or f"TikTok Video {vid}"
                    v_cover = v.get("origin_cover") or v.get("cover") or ""
                    videos.append(NormalizedVideo(
                        id=vid,
                        creator_username=clean_user,
                        canonical_url=f"https://www.tiktok.com/@{clean_user}/video/{vid}",
                        title=v_title[:120],
                        description=v_title,
                        thumbnail_url=v_cover,
                        duration=int(v.get("duration", 0)),
                        views=str(v.get("play_count", 0)),
                        likes=str(v.get("digg_count", 0)),
                        comments=str(v.get("comment_count", 0)),
                        shares=str(v.get("share_count", 0)),
                        published_at=str(v.get("create_time", "")),
                        direct_play_url=v.get("play")
                    ))
                    if len(videos) >= target_count:
                        break

                has_more = bool(d.get("hasMore", False))
                next_cursor = d.get("cursor")
                if next_cursor is None or str(next_cursor) == str(cursor):
                    break
                cursor = next_cursor

            if not avatar_url:
                info_res = await self._fetch_via_user_info(clean_user, client)
                if info_res:
                    info_res.videos = videos
                    return info_res
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

    async def _fetch_via_user_info(self, clean_user: str, client) -> Optional[NormalizedCreator]:
        """Query /api/user/info for standalone creator details."""
        try:
            info_url = f"https://www.tikwm.com/api/user/info?unique_id={urllib.parse.quote(clean_user)}"
            resp = await client.get(info_url, timeout=self.timeout_seconds)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get("code") != 0 or not data.get("data"):
                return None
            d = data["data"]
            user = d.get("user") or {}
            stats = d.get("stats") or {}
            avatar = user.get("avatarLarger") or user.get("avatarMedium") or user.get("avatarThumb") or ""
            if not avatar:
                return None

            return NormalizedCreator(
                id=clean_user,
                username=f"@{clean_user}",
                clean_username=clean_user,
                nickname=user.get("nickname") or clean_user,
                avatar_url=avatar,
                bio=user.get("signature") or "",
                follower_count=int(stats.get("followerCount", 0)),
                following_count=int(stats.get("followingCount", 0)),
                likes_count=int(stats.get("heartCount", 0)),
                video_count=int(stats.get("videoCount", 0)),
                verified=bool(user.get("verified", False)),
                provider=self.name,
                videos=[]
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
            "type": "rest_api"
        }

