import os
import sys
import re
import time
import subprocess
import shutil
from pathlib import Path
from typing import Tuple, Dict, Any, Optional

VALID_MEDIA_EXTENSIONS = {".mp4", ".mp3", ".mkv", ".webm", ".m4a", ".wav"}

def sanitize_filename(name: str, max_length: int = 150) -> str:
    """
    Sanitize string for safe filesystem usage across Linux, Windows, and macOS.
    Handles:
    - Invalid filesystem characters: / \\ : * ? " < > |
    - Control characters (ASCII 0-31)
    - Unicode characters & emojis (preserves valid UTF-8 letters while stripping dangerous symbols)
    - Truncates long filenames to max_length to avoid filesystem path limits
    - Strips leading/trailing dots and spaces (NTFS safety)
    """
    if not name:
        return "Gettik_Video"

    # Replace forbidden filesystem characters with underscore
    cleaned = re.sub(r'[\\/*?:"<>|]', '_', name)
    
    # Strip ASCII control characters
    cleaned = re.sub(r'[\x00-\x1f\x7f]', '', cleaned)
    
    # Replace sequences of whitespace with a single space
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' .')
    
    # Truncate length if extremely long
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(' ._')

    return cleaned if cleaned else "Gettik_Video"

def sanitize_creator_username(username: str) -> str:
    """
    Sanitize TikTok creator username for safe filesystem directory and filename usage.
    Strips leading/trailing '@', removes path traversal characters (/, \\, ..),
    removes filesystem-forbidden characters (:, *, ?, ", <, >, |, control chars, @).
    Returns clean alphanumeric, dot, underscore, dash username.
    """
    if not username:
        return "creator"
    u = str(username).strip()
    # Strip all forbidden filesystem characters, control characters, and '@'
    u = re.sub(r'[\/\\:\*\?"<>\|\x00-\x1f\x7f@]', '', u)
    # Strip leading/trailing dots, spaces, dashes, underscores
    u = u.strip(' .-_')
    # Prevent empty or traversal tokens
    if not u or u in ('.', '..'):
        return "creator"
    # Keep safe length (max 80 chars)
    return u[:80]

def get_creator_download_dir(username: str, base_dir: Optional[Path] = None, create: bool = True) -> Path:
    """
    Return Downloads/Gettik/@username/ directory.
    Automatically creates the folder if create=True.
    Ensures creator downloads never go into another creator's folder or root folder.
    """
    from src.app.config import get_active_downloads_dir
    root = (Path(base_dir) if base_dir else get_active_downloads_dir()).expanduser().resolve()
    clean_u = sanitize_creator_username(username)
    creator_dir = root / f"@{clean_u}"
    if create:
        creator_dir.mkdir(parents=True, exist_ok=True)
    return creator_dir

def format_creator_video_filename(
    creator_username: str,
    index: Optional[int] = None,
    video_id: Optional[str] = None
) -> str:
    """
    Format consistent, safe video filename for a creator download:
      - If index is provided: Gettik-@<username>-001 (or 002, 003...)
      - If video_id is provided: Gettik-@<username>-<video_id>
      - If neither: Gettik-@<username>-video
    """
    clean_u = sanitize_creator_username(creator_username)
    if index is not None and isinstance(index, (int, float)):
        idx_num = int(index) + 1  # 1-based indexing for display (0 -> 001)
        return f"Gettik-@{clean_u}-{str(idx_num).zfill(3)}"
    elif video_id:
        clean_vid = sanitize_filename(str(video_id), max_length=40)
        return f"Gettik-@{clean_u}-{clean_vid}"
    else:
        return f"Gettik-@{clean_u}-video"

def get_unique_filepath(target_dir: Path, base_name: str, ext: str) -> Path:
    """
    Generate a safe, unique file path in target_dir.
    Never accidentally overwrites an existing file with different content.
    """
    target_dir = Path(target_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    
    clean_base = sanitize_filename(base_name)
    clean_ext = ext.lstrip(".")
    candidate = target_dir / f"{clean_base}.{clean_ext}"
    
    if not candidate.exists():
        return candidate
        
    counter = 1
    while True:
        numbered = target_dir / f"{clean_base}_{counter}.{clean_ext}"
        if not numbered.exists():
            return numbered
        counter += 1

def verify_directory_writable(dir_path: Path) -> Tuple[bool, str]:
    """
    Verify that the target directory exists (or can be created) and is writable.
    Returns (is_writable, error_message).
    """
    try:
        p = Path(dir_path).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        
        # Test file write and deletion
        test_file = p / f".gettik_write_test_{int(time.time() * 1000)}"
        test_file.write_text("test_write")
        if test_file.exists():
            test_file.unlink()
        return True, ""
    except Exception as e:
        return False, "Download cannot continue because the selected folder is unavailable or not writable."

def verify_physical_file(filepath: Path) -> Tuple[bool, int, str]:
    """
    Strictly verify that a physical file exists on device storage, is a regular file,
    has non-zero size, is readable, and has expected media extension.
    Returns (is_valid, size_in_bytes, error_message).
    """
    try:
        if not filepath:
            return False, 0, "No file path provided."
            
        p = Path(filepath).expanduser().resolve()
        if not p.exists():
            return False, 0, f"Physical file does not exist on device at {p}"
        if not p.is_file():
            return False, 0, f"Path is not a regular file at {p}"
        
        size = p.stat().st_size
        if size <= 0:
            return False, 0, f"Physical file is empty (0 bytes) at {p}"
        
        # Verify valid media extension
        ext = p.suffix.lower()
        if ext and ext not in VALID_MEDIA_EXTENSIONS:
            return False, 0, f"Unexpected media extension '{ext}' at {p}"
        
        # Test binary readability
        with open(p, "rb") as f:
            chunk = f.read(1024)
            if not chunk:
                return False, 0, f"Physical file contains no readable bytes at {p}"
                
        return True, size, ""
    except Exception as e:
        return False, 0, f"Verification failed: {str(e)}"

def open_file(filepath_str: str) -> Dict[str, Any]:
    """Open physical media file using the operating system's default media player."""
    p = Path(filepath_str).expanduser().resolve()
    valid, size, err = verify_physical_file(p)
    if not valid:
        return {"status": "error", "message": err}

    try:
        if sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "win32":
            os.startfile(str(p))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"status": "ok", "message": f"Opened file {p.name}", "filepath": str(p)}
    except Exception as e:
        return {"status": "error", "message": f"Could not launch media player: {e}"}

def open_folder(path_or_file_str: str) -> Dict[str, Any]:
    """Open physical folder containing the file in the operating system's file manager."""
    p = Path(path_or_file_str).expanduser().resolve()
    folder = p.parent if p.is_file() else p
    if not folder.exists():
        folder.mkdir(parents=True, exist_ok=True)

    try:
        if sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"status": "ok", "message": f"Opened folder {folder}", "folder": str(folder)}
    except Exception as e:
        return {"status": "error", "message": f"Could not open file manager: {e}"}

def pick_native_directory() -> Optional[str]:
    """
    Launch native OS folder chooser dialog where supported.
    Returns chosen directory string or None if canceled.
    """
    # 1. Linux: Try zenity
    if sys.platform.startswith("linux") and shutil.which("zenity"):
        try:
            res = subprocess.run(
                ["zenity", "--file-selection", "--directory", "--title=Select Gettik Download Folder"],
                capture_output=True,
                text=True,
                timeout=60
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass

    # 2. Linux: Try kdialog
    if sys.platform.startswith("linux") and shutil.which("kdialog"):
        try:
            res = subprocess.run(
                ["kdialog", "--getexistingdirectory", str(Path.home())],
                capture_output=True,
                text=True,
                timeout=60
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass

    # 3. macOS: Try AppleScript
    if sys.platform == "darwin":
        try:
            script = 'POSIX path of (choose folder with prompt "Select Gettik Download Folder")'
            res = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=60
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass

    # 4. Fallback: Tkinter askdirectory
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(title="Select Gettik Download Folder")
        root.destroy()
        if folder:
            return folder
    except Exception:
        pass

    return None
