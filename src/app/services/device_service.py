"""
Hardware & Device Fingerprinting Service
Generates and maintains a stable, deterministic hardware identifier (HWID)
for the physical machine running Gettik.
"""

import hashlib
import platform
import uuid
from pathlib import Path
from typing import Dict, Any

_CACHED_HWID = None

def get_system_device_fingerprint() -> str:
    """
    Generate a stable, deterministic hardware identifier for the local system.
    Guaranteed to remain identical across:
    - Application restart
    - User logout & login
    - Operating system reboot
    """
    global _CACHED_HWID
    if _CACHED_HWID:
        return _CACHED_HWID

    machine_id = ""
    # Check Linux standard machine-id locations
    for p in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
        try:
            path = Path(p)
            if path.exists():
                content = path.read_text().strip()
                if content:
                    machine_id = content
                    break
        except Exception:
            pass

    # Hardware node and platform components
    node = str(uuid.getnode()) # MAC address integer
    hostname = platform.node()
    arch = platform.machine()
    proc = platform.processor() or "generic"

    raw_signature = f"{machine_id}::{node}::{hostname}::{arch}::{proc}"
    digest = hashlib.sha256(raw_signature.encode("utf-8")).hexdigest()[:24].upper()
    _CACHED_HWID = f"HWID-{digest}"
    return _CACHED_HWID

def get_device_info() -> Dict[str, Any]:
    """Return hardware and environment details for this device."""
    return {
        "fingerprint": get_system_device_fingerprint(),
        "hostname": platform.node(),
        "platform": platform.system(),
        "architecture": platform.machine(),
        "app_version": "2.2.0"
    }

