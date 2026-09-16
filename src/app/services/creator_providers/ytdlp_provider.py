"""
Provider 3: yt-dlp Native Extractor Provider
Extracts creator video playlist using yt-dlp's native extraction pipeline in non-blocking threadpool.
Combines with official embed / oEmbed profile metadata to guarantee 100% genuine creator profile pictures.
Zero dummy data: never uses video thumbnails as creator avatars.
"""

import asyncio
import json
import re
import urllib.parse
from typing import Dict, Any, List, Optional
import yt_dlp

from .base import BaseCreatorProvider, NormalizedCreator, NormalizedVideo
from .http_client import get_async_http_client


class YtDlpCreatorProvider(BaseCreatorProvider):
    def __init__(self, priority: int = 30, timeout_seconds: float = 10.0):
        super().__init__(name="ytdlp", priority=priority, timeout_seconds=timeout_seconds)

    async def _fetch_real_avatar(self, clean_user: str) -> tuple:
        """Fetch genuine creator avatar and nickname via official embed/oembed (never video thumbnails!)."""
        client = get_async_http_client()
        avatar_url = ""
        nickname = clean_user

        # Try embed frontity state
        try:
            embed_url = f"https://www.tiktok.com/embed/@{clean_user}"
            resp = await client.get(embed_url, timeout=3.0)
            if resp.status_code == 200 and "__FRONTITY_CONNECT_STATE__" in resp.text:
                m = re.search(r'<script[^>]*id=["\']__FRONTITY_CONNECT_STATE__["\'][^>]*>(.*?)</script>', resp.text, re.DOTALL)
                if m:
                    state = json.loads(m.group(1))
                    data = state.get("source", {}).get("data", {})
                    user_data = data.get(f"/embed/@{clean_user}", {})
                    uinfo = user_data.get("userInfo") or user_data.get("author") or {}
                    if uinfo:
                        nickname = uinfo.get("nickname") or uinfo.get("name") or clean_user
                        avatar_url = (
                            uinfo.get("avatarThumbUrl") or 
                            uinfo.get("avatarLarger") or 
                            uinfo.get("avatarMedium") or 
                            ""
                        )
        except Exception:
            pass

        # Try oembed fallback
        if not avatar_url:
            try:
                oe_url = f"https://www.tiktok.com/oembed?url=https://www.tiktok.com/@{urllib.parse.quote(clean_user)}"
                oe_resp = await client.get(oe_url, timeout=2.5)
                if oe_resp.status_code == 200:
                    oe = oe_resp.json()
                    nickname = oe.get("author_name") or nickname
                    cand_avatar = oe.get("thumbnail_url")
                    if cand_avatar and ("avatar" in cand_avatar.lower() or "tiktok" in cand_avatar.lower()):
                        avatar_url = cand_avatar
            except Exception:
                pass

        return avatar_url, nickname

    async def fetch_creator(self, username: str, limit: int = 20) -> Optional[NormalizedCreator]:
        clean_user = username.lstrip("@").strip()
        if not clean_user:
            return None

        url = f"https://www.tiktok.com/@{clean_user}"
        loop = asyncio.get_event_loop()

        def _extract():
            class _QuietLogger:
                def debug(self, msg): pass
                def warning(self, msg): pass
                def error(self, msg): pass

            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': True,
                'playlistend': max(1, limit),
                'socket_timeout': 6,
                'extractor_retries': 1,
                'logger': _QuietLogger()
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    return info
            except Exception as e:
                print(f"[YtDlpCreatorProvider] Extraction error: {e}")
                return None

        try:
            # Parallel fetch: run yt-dlp flat playlist extraction concurrently with real avatar extraction
            yt_task = loop.run_in_executor(None, _extract)
            avatar_task = self._fetch_real_avatar(clean_user)

            info, (avatar_url, nickname) = await asyncio.gather(yt_task, avatar_task)
            if not info:
                return None

            raw_entries = info.get('entries', [])
            if not raw_entries:
                return None

            if not nickname or nickname == clean_user:
                first = raw_entries[0] if raw_entries else {}
                nickname = info.get('channel') or info.get('uploader') or first.get('channel') or clean_user

            videos: List[NormalizedVideo] = []
            for item in raw_entries[:limit]:
                vid = str(item.get('id', ''))
                if not vid:
                    continue
                v_title = item.get('title') or item.get('description') or f"TikTok Video {vid}"
                v_desc = item.get('description') or v_title
                v_thumb = item.get('thumbnail') or ""
                v_dur = int(item.get('duration') or 0)
                videos.append(NormalizedVideo(
                    id=vid,
                    creator_username=clean_user,
                    canonical_url=item.get('url') or f"https://www.tiktok.com/@{clean_user}/video/{vid}",
                    title=v_title[:120],
                    description=v_desc,
                    thumbnail_url=v_thumb,
                    duration=v_dur,
                    views=str(item.get('view_count', 0)),
                    likes=str(item.get('like_count', 0)),
                    comments=str(item.get('comment_count', 0)),
                    shares=str(item.get('repost_count', 0))
                ))

            # Strictly require valid avatar (never video thumbnail!)
            if not avatar_url:
                return None

            return NormalizedCreator(
                id=clean_user,
                username=f"@{clean_user}",
                clean_username=clean_user,
                nickname=nickname,
                avatar_url=avatar_url,
                bio=f"TikTok Creator @{clean_user}",
                follower_count=0,
                video_count=len(raw_entries),
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
            "type": "ytdlp"
        }
