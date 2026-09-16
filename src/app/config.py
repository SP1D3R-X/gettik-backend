import os
from pathlib import Path
from typing import List

# Base directories inside 'Gettik' folder
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
STATIC_DIR = SRC_DIR / "static"
DATA_DIR = PROJECT_ROOT / "data"
TEMP_DIR = DATA_DIR / "temp"

# Device-accessible Downloads directory: ~/Downloads/Gettik/
USER_DEVICE_DOWNLOADS_DIR = Path.home() / "Downloads" / "Gettik"
# Environment Settings
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
APP_NAME = "Gettik"
APP_VERSION = "2.2.0"
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
DEBUG = os.getenv("DEBUG", "false" if ENVIRONMENT == "production" else "true").lower() in ("true", "1", "yes")

# Database & Cache Configuration
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
REDIS_URL = os.getenv("REDIS_URL", "").strip()

# Worker Concurrency Configuration (Default: 5 concurrent download processing slots)
try:
    GETTIK_DOWNLOAD_WORKERS = int(os.getenv("GETTIK_DOWNLOAD_WORKERS", "5"))
    if GETTIK_DOWNLOAD_WORKERS < 1:
        GETTIK_DOWNLOAD_WORKERS = 5
except Exception:
    GETTIK_DOWNLOAD_WORKERS = 5

# Security & Secrets
JWT_SECRET = os.getenv("JWT_SECRET", "gettik_jwt_secret_production_key_change_me_2026_xyz")
SESSION_SECRET = os.getenv("SESSION_SECRET", "gettik_session_secret_production_key_change_me_2026_abc")

# Configurable Domain Endpoints (No hardcoded localhost in production!)
API_BASE_URL = os.getenv("API_BASE_URL", f"http://{APP_HOST}:{APP_PORT}").rstrip("/")
ADMIN_BASE_URL = os.getenv("ADMIN_BASE_URL", f"http://{APP_HOST}:{APP_PORT}/admin").rstrip("/")

# CORS Origins Configuration
raw_cors = os.getenv("CORS_ORIGINS", "")
if raw_cors:
    CORS_ORIGINS_LIST: List[str] = [origin.strip() for origin in raw_cors.split(",") if origin.strip()]
else:
    # Safe defaults for local development and standard configured domains
    CORS_ORIGINS_LIST = [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "https://admin.gettik.example",
        "https://gettik.example",
        "https://api.gettik.example",
        API_BASE_URL,
        ADMIN_BASE_URL
    ]

# Persistent Physical Download Storage Path
DOWNLOAD_STORAGE_PATH_ENV = os.getenv("DOWNLOAD_STORAGE_PATH", "").strip()

# Device-accessible Downloads directory: ~/Downloads/Gettik/ or configured storage path
if DOWNLOAD_STORAGE_PATH_ENV:
    USER_DEVICE_DOWNLOADS_DIR = Path(DOWNLOAD_STORAGE_PATH_ENV).expanduser().resolve()
else:
    USER_DEVICE_DOWNLOADS_DIR = Path.home() / "Downloads" / "Gettik"

# Safe directory creation with sandbox protection
try:
    USER_DEVICE_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_DEVICE_DOWNLOADS_DIR = USER_DEVICE_DOWNLOADS_DIR
except OSError:
    # If in read-only sandbox or permissions restricted
    DEFAULT_DEVICE_DOWNLOADS_DIR = PROJECT_ROOT / "downloads"
    DEFAULT_DEVICE_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

# Compatibility pointer
DOWNLOADS_DIR = DEFAULT_DEVICE_DOWNLOADS_DIR

def get_active_downloads_dir() -> Path:
    """
    Retrieve active user download folder dynamically.
    Checks database settings for custom user-configured folder;
    defaults to device storage ~/Downloads/Gettik/ (Linux) or %USERPROFILE%\\Downloads\\Gettik (Windows).
    """
    target = USER_DEVICE_DOWNLOADS_DIR
    try:
        from src.app.database.db import get_settings
        settings = get_settings()
        custom = settings.get("downloadDir")
        if custom:
            target = Path(custom).expanduser().resolve()
    except Exception:
        pass

    try:
        target.mkdir(parents=True, exist_ok=True)
        return target
    except OSError:
        fallback = PROJECT_ROOT / "downloads"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback

def resolve_configured_downloads_dir() -> Path:
    """Returns the user's configured download directory path without attempting fallback."""
    try:
        from src.app.database.db import get_settings
        settings = get_settings()
        custom = settings.get("downloadDir")
        if custom:
            return Path(custom).expanduser().resolve()
    except Exception:
        pass
    return USER_DEVICE_DOWNLOADS_DIR

# Application metadata (defaults defined from environment above)

