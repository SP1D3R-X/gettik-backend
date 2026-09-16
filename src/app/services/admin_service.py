"""
Gettik Dedicated Admin Service
Handles administrator authentication, session lifecycle, RBAC authorization,
credential management, audit logging, and system diagnostics.
"""

import os
import re
import secrets
import time
import shutil
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from pathlib import Path

from src.app.database.db import (
    get_admin_by_email,
    get_admin_by_id,
    get_all_admins,
    create_admin_record,
    update_admin_record,
    delete_admin_record,
    create_admin_session,
    get_admin_session_by_token,
    deactivate_admin_session,
    deactivate_all_admin_sessions,
    add_audit_log,
    get_audit_logs,
    get_system_settings_dict,
    update_system_setting,
    get_admin_dashboard_metrics,
    get_admin_users_extended,
    get_admin_licenses_extended,
    get_admin_devices_extended,
    get_admin_downloads_extended,
    get_admin_creators_extended,
    get_admin_analytics_data,
    get_db_connection,
    DB_PATH,
    create_license_record,
    extend_license_days,
    get_license_request_by_id,
    update_license_request_record,
    get_user_by_id
)
from src.app.services.auth_service import hash_password, verify_password, generate_serial_key
from src.app.config import APP_NAME, APP_VERSION, DEFAULT_DEVICE_DOWNLOADS_DIR, get_active_downloads_dir
from src.app.services.progress_hub import hub
from src.app.services.creator_service import creator_mgr
from src.app.services.system_utils import verify_directory_writable

_START_TIME = time.time()

# Rate limiting for admin login: {ip: [timestamps]}
_ADMIN_FAILED_ATTEMPTS: Dict[str, list] = {}
_ADMIN_RATE_LIMIT_WINDOW = 300
_ADMIN_RATE_LIMIT_MAX = 30

def _check_admin_rate_limit(client_ip: str):
    now = time.time()
    history = _ADMIN_FAILED_ATTEMPTS.get(client_ip, [])
    history = [t for t in history if now - t < _ADMIN_RATE_LIMIT_WINDOW]
    _ADMIN_FAILED_ATTEMPTS[client_ip] = history
    if len(history) >= _ADMIN_RATE_LIMIT_MAX:
        raise RuntimeError("Too many failed admin login attempts. Account temporarily locked. Please try again later.")

def _record_admin_failed_attempt(client_ip: str):
    now = time.time()
    if client_ip not in _ADMIN_FAILED_ATTEMPTS:
        _ADMIN_FAILED_ATTEMPTS[client_ip] = []
    _ADMIN_FAILED_ATTEMPTS[client_ip].append(now)

def _clear_admin_failed_attempts(client_ip: str):
    _ADMIN_FAILED_ATTEMPTS.pop(client_ip, None)


def authenticate_admin(email: str, password: str, client_ip: str = "127.0.0.1") -> Dict[str, Any]:
    """
    Authenticate an administrator with Email and Password ONLY.
    There is NO serial key or license key requirement for admins.
    """
    _check_admin_rate_limit(client_ip)

    clean_email = email.strip().lower() if email else ""
    if not clean_email or not password:
        _record_admin_failed_attempt(client_ip)
        raise RuntimeError("Email and password are required.")

    admin = get_admin_by_email(clean_email)
    if not admin:
        _record_admin_failed_attempt(client_ip)
        # Generic error to prevent email enumeration
        raise RuntimeError("Invalid administrator credentials.")

    # Check status
    if (admin.get("status") or "").upper() != "ACTIVE":
        _record_admin_failed_attempt(client_ip)
        raise RuntimeError("This administrator account has been disabled.")

    # Verify password hash
    if not verify_password(password, admin["password_hash"]):
        _record_admin_failed_attempt(client_ip)
        raise RuntimeError("Invalid administrator credentials.")

    _clear_admin_failed_attempts(client_ip)

    # Generate cryptographically secure admin token
    token = f"adm_{secrets.token_urlsafe(32)}"
    expires_at = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    create_admin_session(token, admin["id"], expires_at)

    # Update last login timestamp
    update_admin_record(admin["id"], {"last_login_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

    # Record Audit Log
    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action="Admin Login",
        target_type="admins",
        target_id=admin["id"],
        details=f"Admin {admin['email']} successfully logged in from {client_ip}",
        ip_address=client_ip
    )

    return {
        "status": "ok",
        "token": token,
        "admin": {
            "id": admin["id"],
            "name": admin["name"],
            "email": admin["email"],
            "role": admin["role"],
            "permissions": admin.get("permissions", "*"),
            "status": admin["status"]
        }
    }


def verify_admin_session(token: str) -> Optional[Dict[str, Any]]:
    """
    Verify an administrator session token.
    Enforces expiration check, active status check, and returns admin profile.
    """
    if not token or not token.startswith("adm_"):
        return None

    session = get_admin_session_by_token(token)
    if not session:
        return None

    # Check expiration
    try:
        exp_dt = datetime.strptime(session["expires_at"], "%Y-%m-%d %H:%M:%S")
        if datetime.now() > exp_dt:
            deactivate_admin_session(token)
            return None
    except Exception:
        pass

    admin = get_admin_by_id(session["admin_id"])
    if not admin or (admin.get("status") or "").upper() != "ACTIVE":
        return None

    return {
        "id": admin["id"],
        "name": admin["name"],
        "email": admin["email"],
        "role": admin["role"],
        "permissions": admin.get("permissions", "*"),
        "status": admin["status"]
    }


def logout_admin(token: str, client_ip: str = "127.0.0.1") -> bool:
    """Invalidate administrator session on logout and record audit log."""
    if not token:
        return True

    session = get_admin_session_by_token(token)
    if session:
        admin = get_admin_by_id(session["admin_id"])
        if admin:
            add_audit_log(
                actor_id=admin["id"],
                actor_email=admin["email"],
                action="Admin Logout",
                target_type="admins",
                target_id=admin["id"],
                details=f"Admin {admin['email']} logged out",
                ip_address=client_ip
            )
    return deactivate_admin_session(token)


def update_admin_email(admin_id: str, current_password: str, new_email: str, client_ip: str = "127.0.0.1") -> Dict[str, Any]:
    """Change admin email after password re-verification."""
    admin = get_admin_by_id(admin_id)
    if not admin:
        raise RuntimeError("Administrator not found.")

    if not verify_password(current_password, admin["password_hash"]):
        raise RuntimeError("Current password verification failed.")

    clean_email = new_email.strip().lower()
    if not clean_email or not re.match(r"^[^@]+@[^@]+\.[^@]+$", clean_email):
        raise RuntimeError("Invalid email format.")

    existing = get_admin_by_email(clean_email)
    if existing and existing["id"] != admin_id:
        raise RuntimeError("This email address is already in use by another administrator.")

    old_email = admin["email"]
    update_admin_record(admin_id, {"email": clean_email})

    add_audit_log(
        actor_id=admin["id"],
        actor_email=clean_email,
        action="Admin Email Changed",
        target_type="admins",
        target_id=admin_id,
        details=f"Admin email changed from {old_email} to {clean_email}",
        ip_address=client_ip
    )

    return get_admin_by_id(admin_id)


def update_admin_password(admin_id: str, current_password: str, new_password: str, current_token: Optional[str] = None, client_ip: str = "127.0.0.1") -> bool:
    """Change admin password, hash with bcrypt, and revoke other sessions."""
    admin = get_admin_by_id(admin_id)
    if not admin:
        raise RuntimeError("Administrator not found.")

    if not verify_password(current_password, admin["password_hash"]):
        raise RuntimeError("Current password verification failed.")

    if len(new_password) < 8:
        raise RuntimeError("New password must be at least 8 characters long.")

    new_hash = hash_password(new_password)
    update_admin_record(admin_id, {"password_hash": new_hash})

    # Invalidate other active sessions for security while keeping current session active
    deactivate_all_admin_sessions(admin_id, except_token=current_token)

    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action="Admin Password Changed",
        target_type="admins",
        target_id=admin_id,
        details="Admin password successfully updated; other active sessions revoked",
        ip_address=client_ip
    )
    return True


def approve_license_request(
    request_id: str,
    admin_id: str,
    duration_days: int = 30,
    max_devices: int = 1,
    client_ip: str = "127.0.0.1"
) -> Dict[str, Any]:
    """
    Approve a pending license request, generate unique cryptographically secure serial key,
    calculate duration and expiry in days, and assign license to user.
    """
    req = get_license_request_by_id(request_id)
    if not req:
        raise RuntimeError("License request not found.")
    if req["status"] != "PENDING":
        raise RuntimeError(f"License request cannot be approved because it is already {req['status']}.")

    admin = get_admin_by_id(admin_id)
    if not admin:
        raise RuntimeError("Administrator not found.")

    if duration_days <= 0:
        raise RuntimeError("Access duration must be at least 1 day.")

    user = get_user_by_id(req["user_id"])
    if not user:
        raise RuntimeError("User associated with this request not found.")

    # Generate unique cryptographically secure Serial Key: Gettik-XXXX-N
    serial_key = generate_serial_key(max_devices=max_devices)
    now_dt = datetime.now()
    issued_at = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    expires_at = (now_dt + timedelta(days=duration_days)).strftime("%Y-%m-%d %H:%M:%S")

    # Create license record
    license_rec = create_license_record(
        serial_key=serial_key,
        assigned_user_id=user["id"],
        expiry_date=expires_at,
        max_devices=max_devices,
        status="ACTIVE",
        duration_days=duration_days,
        issued_at=issued_at
    )

    # Update request record
    update_license_request_record(request_id, {
        "status": "APPROVED",
        "reviewed_at": issued_at,
        "reviewed_by_admin_id": admin["id"],
        "license_id": license_rec["id"]
    })

    # Audit logging
    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action="License Request Approved",
        target_type="license_requests",
        target_id=request_id,
        details=f"Approved license request {request_id} for {user['email']}",
        ip_address=client_ip
    )
    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action="License Issued",
        target_type="licenses",
        target_id=license_rec["id"],
        details=f"Issued {duration_days}-day license ({serial_key}) to {user['email']} (Max {max_devices} devices)",
        ip_address=client_ip
    )

    return {
        "success": True,
        "status": "APPROVED",
        "request_id": request_id,
        "license": license_rec,
        "serial_key": license_rec["serial_key"],
        "duration_days": license_rec.get("duration_days", duration_days),
        "max_devices": license_rec.get("max_devices", max_devices),
        "expiry_date": license_rec.get("expiry_date", expires_at),
        "user_email": user["email"]
    }


def reject_license_request(
    request_id: str,
    admin_id: str,
    reason: str = "",
    client_ip: str = "127.0.0.1"
) -> bool:
    """Reject a pending license request with optional reason."""
    req = get_license_request_by_id(request_id)
    if not req:
        raise RuntimeError("License request not found.")
    if req["status"] != "PENDING":
        raise RuntimeError(f"License request cannot be rejected because it is already {req['status']}.")

    admin = get_admin_by_id(admin_id)
    if not admin:
        raise RuntimeError("Administrator not found.")

    now_dt = datetime.now()
    reviewed_at = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    update_license_request_record(request_id, {
        "status": "REJECTED",
        "reviewed_at": reviewed_at,
        "reviewed_by_admin_id": admin["id"],
        "rejection_reason": reason.strip()
    })

    user_email = req.get("user_email") or req["user_id"]
    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action="License Request Rejected",
        target_type="license_requests",
        target_id=request_id,
        details=f"Rejected license request {request_id} for {user_email}. Reason: {reason.strip() or 'None'}",
        ip_address=client_ip
    )
    return True


def extend_license(
    license_id: int,
    admin_id: str,
    additional_days: int,
    client_ip: str = "127.0.0.1"
) -> Dict[str, Any]:
    """Extend an existing license duration in days."""
    if additional_days <= 0:
        raise RuntimeError("Additional days must be a positive integer.")

    admin = get_admin_by_id(admin_id)
    if not admin:
        raise RuntimeError("Administrator not found.")

    updated_lic = extend_license_days(license_id, additional_days)

    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action="License Extended",
        target_type="licenses",
        target_id=license_id,
        details=f"Extended license {updated_lic['serial_key']} by {additional_days} days (New expiry: {updated_lic['expiry_date']})",
        ip_address=client_ip
    )
    return updated_lic


def get_system_health_status() -> Dict[str, Any]:
    """Perform real system, database, worker, and storage health diagnostics."""
    health: Dict[str, Any] = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "app": {
            "name": APP_NAME,
            "version": APP_VERSION,
            "uptime_seconds": int(time.time() - _START_TIME)
        },
        "database": {},
        "workers": {},
        "storage": {}
    }

    # 1. Database Health Check
    try:
        with get_db_connection() as conn:
            check_res = conn.execute("PRAGMA quick_check;").fetchone()
            is_ok = check_res[0] == "ok" if check_res else False
            db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
            health["database"] = {
                "status": "healthy" if is_ok else "degraded",
                "integrity": check_res[0] if check_res else "unknown",
                "size_bytes": db_size,
                "size_mb": round(db_size / (1024 * 1024), 2),
                "path": str(DB_PATH)
            }
    except Exception as e:
        health["database"] = {"status": "error", "message": str(e)}
        health["status"] = "degraded"

    # 2. Worker & Hub Status
    try:
        from src.app.workers.redis_queue import download_queue
        from src.app.workers.download_worker import download_worker_pool
        active_summary = hub.get_active_summary()
        redis_h = download_queue.get_health()
        pool_st = download_worker_pool.get_status()
        health["workers"] = {
            "status": "healthy",
            "capacity": pool_st.get("capacity", 5),
            "active_workers": pool_st.get("active_workers", 0),
            "available_slots": pool_st.get("available_slots", 5),
            "active_single_downloads": active_summary.get("total_active", 0),
            "creator_worker": "running",
            "queue_backend": redis_h.get("backend", "memory"),
            "queue_depth": pool_st.get("queued_count", redis_h.get("queue_depth", 0)),
            "redis_connected": redis_h.get("connected", False)
        }
        health["redis"] = redis_h
    except Exception as e:
        health["workers"] = {"status": "error", "message": str(e)}

    # 3. Storage Health Check
    try:
        download_dir = get_active_downloads_dir()
        writable, err = verify_directory_writable(download_dir)
        total, used, free = shutil.disk_usage(str(download_dir))
        health["storage"] = {
            "status": "healthy" if writable else "error",
            "download_dir": str(download_dir),
            "is_writable": writable,
            "disk_total_gb": round(total / (1024 ** 3), 2),
            "disk_used_gb": round(used / (1024 ** 3), 2),
            "disk_free_gb": round(free / (1024 ** 3), 2),
            "free_percent": round((free / total) * 100, 1)
        }
    except Exception as e:
        health["storage"] = {"status": "error", "message": str(e)}
        health["status"] = "degraded"

    return health
