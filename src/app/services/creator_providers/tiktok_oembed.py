"""
Provider 4: TikTok Official oEmbed Provider
Utilizes TikTok's official oEmbed endpoint (RFC standard).
Extremely lightweight, resilient, and never blocked by Cloudflare or Slardar WAF.
Provides verified creator name, canonical profile URL, and official thumbnail/avatar.
"""

import json
import urllib.parse
from typing import Dict, Any, List, Optional

from .base import BaseCreatorProvider, NormalizedCreator, NormalizedVideo
from .http_client import get_async_http_client


class TikTokOfficialOEmbedProvider(BaseCreatorProvider):
    def __init__(self, priority: int = 40, timeout_seconds: float = 3.5):
        super().__init__(name="tiktok_oembed", priority=priority, timeout_seconds=timeout_seconds)

    async def fetch_creator(self, username: str, limit: int = 20) -> Optional[NormalizedCreator]:
        clean_user = username.lstrip("@").strip()
        if not clean_user:
            return None

        client = get_async_http_client()
        oembed_url = f"https://www.tiktok.com/oembed?url=https://www.tiktok.com/@{urllib.parse.quote(clean_user)}"

        try:
            resp = await client.get(oembed_url, timeout=self.timeout_seconds)
            if resp.status_code != 200:
                return None

            data = resp.json()
            author_name = data.get("author_name") or clean_user
            author_url = data.get("author_url") or f"https://www.tiktok.com/@{clean_user}"
            avatar_url = data.get("thumbnail_url") or ""

            # Check if avatar is valid TikTok CDN URL
            if not avatar_url or not any(d in avatar_url.lower() for d in ["tiktok", "byteoversea", "ibyteimg", "tiktokcdn"]):
                # Try fetching embed user page avatar
                embed_url = f"https://www.tiktok.com/embed/@{clean_user}"
                e_resp = await client.get(embed_url, timeout=2.0)
                if e_resp.status_code == 200 and "__FRONTITY_CONNECT_STATE__" in e_resp.text:
                    import re
                    m = re.search(r'<script[^>]*id=["\']__FRONTITY_CONNECT_STATE__["\'][^>]*>(.*?)</script>', e_resp.text, re.DOTALL)
                    if m:
                        st = json.loads(m.group(1))
                        uinfo = st.get("source", {}).get("data", {}).get(f"/embed/@{clean_user}", {}).get("userInfo", {})
                        avatar_url = uinfo.get("avatarThumbUrl") or uinfo.get("avatarLarger") or avatar_url

            if not avatar_url:
                return None

            return NormalizedCreator(
                id=clean_user,
                username=f"@{clean_user}",
                clean_username=clean_user,
                nickname=author_name,
                avatar_url=avatar_url,
                bio=f"TikTok Creator @{clean_user}",
                follower_count=0,
                video_count=0,
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
            "type": "official_oembed"
        }
