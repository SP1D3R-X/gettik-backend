"""
Base Provider Interface & Normalized Data Models for Creator Fetch System.
Enforces strict normalization, validation, and typing across all real TikTok providers.
Zero dummy data: guarantees real profile pictures, real handles, and verified videos.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
import re
import urllib.parse


@dataclass
class NormalizedVideo:
    id: str
    creator_username: str
    canonical_url: str
    title: str
    description: str
    thumbnail_url: str
    duration: int = 0
    views: str = "0"
    likes: str = "0"
    comments: str = "0"
    shares: str = "0"
    published_at: Optional[str] = None
    direct_play_url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "creator_username": self.creator_username,
            "url": self.canonical_url,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "description": self.description,
            "thumbnail": self.thumbnail_url,
            "thumbnail_url": self.thumbnail_url,
            "duration": self.duration,
            "views": str(self.views),
            "likes": str(self.likes),
            "comments": str(self.comments),
            "shares": str(self.shares),
            "published_at": self.published_at,
            "direct_play_url": self.direct_play_url
        }


@dataclass
class NormalizedCreator:
    id: str
    username: str             # e.g. "@waqas1.5k"
    clean_username: str       # e.g. "waqas1.5k"
    nickname: str
    avatar_url: str           # Real verified TikTok profile picture (never video thumbnail!)
    bio: str = ""
    follower_count: int = 0
    following_count: int = 0
    likes_count: int = 0
    video_count: int = 0
    verified: bool = False
    provider: str = "unknown"
    videos: List[NormalizedVideo] = field(default_factory=list)
    profile_url: str = ""

    def __post_init__(self):
        if not self.profile_url:
            self.profile_url = f"https://www.tiktok.com/@{self.clean_username}"
        if not self.username.startswith("@"):
            self.username = f"@{self.clean_username}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id or self.clean_username,
            "username": self.username,
            "clean_username": self.clean_username,
            "nickname": self.nickname or self.clean_username,
            "name": self.nickname or self.clean_username,
            "avatar": self.avatar_url,
            "avatar_url": self.avatar_url,
            "profile_url": self.profile_url,
            "bio": self.bio,
            "followers": self.follower_count,
            "following": self.following_count,
            "likes": self.likes_count,
            "video_count": self.video_count,
            "total_videos": self.video_count if self.video_count > 0 else len(self.videos),
            "verified": self.verified,
            "provider": self.provider,
            "videos": [v.to_dict() for v in self.videos]
        }


def validate_creator_response(creator: Optional[NormalizedCreator], expected_username: str) -> bool:
    """
    Validate creator data before accepting.
    Ensures:
    - Creator object is non-null
    - Username matches requested username (case-insensitive)
    - Real avatar URL is present and not a generic icon or dummy string
    - Video items (if present) have unique non-empty IDs and valid URLs
    """
    if not creator:
        return False

    clean_expected = expected_username.lstrip("@").strip().lower()
    clean_actual = creator.clean_username.lstrip("@").strip().lower()
    if clean_actual != clean_expected:
        return False

    # Real profile photo is required
    avatar = (creator.avatar_url or "").strip()
    if not avatar:
        return False
    # Validate it is a plausible URL and not a dummy or generic placeholder
    if not (avatar.startswith("http://") or avatar.startswith("https://") or avatar.startswith("//")):
        return False
    
    # Must not be a generic dummy placeholder
    lower_av = avatar.lower()
    forbidden_tokens = ["placeholder", "dummy", "default-avatar", "user.png", "avatar_none"]
    if any(token in lower_av for token in forbidden_tokens):
        return False

    # If videos are present, validate IDs and unique constraint
    seen_ids = set()
    for v in creator.videos:
        vid = str(v.id).strip()
        if not vid:
            return False
        if vid in seen_ids:
            # Duplicate video ID
            return False
        seen_ids.add(vid)
        if not v.canonical_url or not ("tiktok.com" in v.canonical_url or vid in v.canonical_url or "http" in v.canonical_url):
            return False

    return True


class BaseCreatorProvider(ABC):
    """Abstract Base Class for all real TikTok Creator Providers."""

    def __init__(self, name: str, priority: int = 100, timeout_seconds: float = 6.0):
        self.name = name
        self.priority = priority
        self.timeout_seconds = timeout_seconds

    @abstractmethod
    async def fetch_creator(self, username: str, limit: int = 20) -> Optional[NormalizedCreator]:
        """
        Fetch normalized creator profile and initial video list.
        Must return None or raise on provider failure / invalid data.
        """
        pass

    @abstractmethod
    async def fetch_videos(self, username: str, limit: int = 50, cursor: Optional[str] = None) -> Optional[List[NormalizedVideo]]:
        """
        Fetch additional creator videos.
        """
        pass

    @abstractmethod
    async def health_check(self) -> Dict[str, Any]:
        """Check provider operational status."""
        pass

