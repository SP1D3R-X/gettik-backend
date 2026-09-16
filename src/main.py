import os
import sys
import argparse
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# Ensure the directory containing src is in sys.path
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from src.app.config import STATIC_DIR, APP_NAME, APP_VERSION, APP_HOST, APP_PORT
from fastapi.middleware.cors import CORSMiddleware
from src.app.config import (
    STATIC_DIR, APP_NAME, APP_VERSION, APP_HOST, APP_PORT,
    CORS_ORIGINS_LIST, get_active_downloads_dir
)
from src.app.database.db import init_db
from src.app.routes.api import router as api_router
from src.app.routes.auth_routes import router as auth_router
from src.app.routes.admin_routes import router as admin_router

from src.app.workers.download_worker import download_worker_pool

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Render.com par data directories ensure karo (ephemeral filesystem)
    from src.app.config import DATA_DIR, TEMP_DIR, get_active_downloads_dir
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    get_active_downloads_dir()  # download dir bhi create hoga
    # Initialize database schema and migrations
    init_db()
    # Start the background download worker pool
    download_worker_pool.start_worker_pool()
    yield

app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)

# Production CORS policy configured via environment variables
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS_LIST,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include real backend API routes, auth routes, and admin routes
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(api_router)

# Mount admin assets
ADMIN_DIR = STATIC_DIR / "admin"
if ADMIN_DIR.exists():
    app.mount("/admin-assets", StaticFiles(directory=str(ADMIN_DIR)), name="admin_assets")

# Mount static assets for direct root requests (/css, /js, /images) and /static
if (STATIC_DIR / "css").exists():
    app.mount("/css", StaticFiles(directory=str(STATIC_DIR / "css")), name="css")
if (STATIC_DIR / "js").exists():
    app.mount("/js", StaticFiles(directory=str(STATIC_DIR / "js")), name="js")
if (STATIC_DIR / "images").exists():
    app.mount("/images", StaticFiles(directory=str(STATIC_DIR / "images")), name="images")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/favicon.ico")
async def favicon():
    favicon_path = STATIC_DIR / "favicon.ico"
    if favicon_path.exists():
        return FileResponse(str(favicon_path))
    from fastapi import Response
    return Response(status_code=204)

@app.get("/admin")
@app.get("/admin/")
@app.get("/admin/{full_path:path}")
async def serve_admin(full_path: str = ""):
    admin_index = STATIC_DIR / "admin" / "index.html"
    no_cache_headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    }
    if admin_index.exists():
        return FileResponse(str(admin_index), headers=no_cache_headers)
    return FileResponse(str(STATIC_DIR / "index.html"), headers=no_cache_headers)

@app.get("/")
async def serve_index():
    index_path = STATIC_DIR / "index.html"
    no_cache_headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    }
    return FileResponse(str(index_path), headers=no_cache_headers)

@app.get("/health")
@app.get("/api/health")
async def health_check():
    import shutil
    from src.app.workers.redis_queue import download_queue
    from src.app.services.system_utils import verify_directory_writable
    from src.app.database.db import get_db_connection

    # 1. Database Connectivity Check
    db_status = "healthy"
    try:
        with get_db_connection() as conn:
            conn.execute("SELECT 1;").fetchone()
    except Exception as e:
        db_status = f"unhealthy: {e}"

    # 2. Redis & Queue Health
    redis_health = download_queue.get_health()

    # 3. Storage Volume Check
    download_dir = get_active_downloads_dir()
    is_writable, _ = verify_directory_writable(download_dir)
    total, used, free = shutil.disk_usage(str(download_dir))

    overall_healthy = (db_status == "healthy") and is_writable

    return {
        "status": "healthy" if overall_healthy else "degraded",
        "app": APP_NAME,
        "version": APP_VERSION,
        "database": {
            "status": db_status
        },
        "redis": redis_health,
        "storage": {
            "status": "healthy" if is_writable else "unwritable",
            "path": str(download_dir),
            "free_gb": round(free / (1024 ** 3), 2),
            "total_gb": round(total / (1024 ** 3), 2)
        }
    }

def launch_native_window(url: str):
    """Launch the application as a standalone desktop window."""
    time.sleep(1.2)  # Wait for uvicorn to initialize
    
    user_data_dir = "/tmp/gettik_native_profile"
    # Try chromium/chrome app mode for native desktop experience
    browsers = [
        ["chromium", f"--app={url}", f"--user-data-dir={user_data_dir}", "--window-size=1400,900", "--enable-features=OverlayScrollbar"],
        ["google-chrome", f"--app={url}", f"--user-data-dir={user_data_dir}", "--window-size=1400,900"],
        ["brave-browser", f"--app={url}", f"--user-data-dir={user_data_dir}", "--window-size=1400,900"]
    ]
    
    launched = False
    for cmd in browsers:
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            launched = True
            print(f"[Native Window] Launched in native app mode: {cmd[0]}")
            break
        except FileNotFoundError:
            continue
    
    if not launched:
        print("[Native Window] Chrome/Chromium app mode not found, falling back to default browser.")
        webbrowser.open(url)

def check_environment():
    """Verify and print environment status for Gettik."""
    print("=" * 60)
    print(f"Gettik Environment & Dependency Diagnostic")
    print("=" * 60)
    print(f"Python Version:    {sys.version.split()[0]}")
    print(f"Project Directory: {PROJECT_ROOT}")
    print(f"Static UI Folder:  {STATIC_DIR} (exists: {STATIC_DIR.exists()})")
    
    # Check yt-dlp
    try:
        import yt_dlp.version
        print(f"yt-dlp:            OK (v{yt_dlp.version.__version__})")
    except Exception as e:
        print(f"yt-dlp:            MISSING ({e})")
        
    # Check ffmpeg
    try:
        import imageio_ffmpeg
        ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
        print(f"ffmpeg binary:     OK ({ffmpeg_bin})")
    except Exception as e:
        print(f"ffmpeg binary:     MISSING ({e})")
        
    print("=" * 60)

import socket

def find_available_port(host: str, start_port: int) -> int:
    """Find the next available TCP port by attempting to bind."""
    for port in range(start_port, start_port + 100):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, port))
                return port
        except OSError:
            continue
    return start_port

def main():
    parser = argparse.ArgumentParser(
        prog="gettik",
        description=f"{APP_NAME} v{APP_VERSION} — Desktop TikTok Downloader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python src/main.py                 # Run web server at http://127.0.0.1:8000
  python src/main.py --native        # Launch as native standalone desktop app
  python src/main.py --port 8080     # Run on custom port 8080
  python src/main.py --check-env     # Check dependencies and environment status
"""
    )
    parser.add_argument("--host", default=APP_HOST, help=f"Host address to bind (default: {APP_HOST})")
    parser.add_argument("-p", "--port", type=int, default=APP_PORT, help=f"Port to bind (default: {APP_PORT})")
    parser.add_argument("-n", "--native", action="store_true", help="Launch in standalone native desktop window")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")
    parser.add_argument("--check-env", action="store_true", help="Run environment and dependency diagnostics")

    args = parser.parse_args()

    if args.check_env:
        check_environment()
        return

    # Automatically resolve available port to prevent [Errno 98]
    active_port = find_available_port(args.host, args.port)
    if active_port != args.port:
        print(f"[Port Notice] Port {args.port} is already in use; automatically bound to free port {active_port}.")

    url = f"http://{args.host}:{active_port}"
    print("=" * 60)
    print(f"  {APP_NAME} v{APP_VERSION} — TikTok Downloader")
    print(f"  Server URL:    {url}")
    print(f"  Mode:          {'Native Desktop Window' if args.native else 'Web Server'}")
    print("=" * 60)

    if args.native:
        window_thread = threading.Thread(target=launch_native_window, args=(url,), daemon=True)
        window_thread.start()

    uvicorn.run(app, host=args.host, port=active_port)

if __name__ == "__main__":
    main()
