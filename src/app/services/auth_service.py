"""
Gettik Authentication Service
Enforces the authoritative 7-step server-side authentication pipeline:
1. Input validation
2. User lookup (generic error if missing)
3. Bcrypt password verification
4. Serial key validation
5. Account status validation (ACTIVE)
6. License status & expiry validation
7. Deterministic device fingerprinting & license device limits
"""

import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
import bcrypt

from src.app.database.db import (
    get_user_by_email,
    get_user_by_id,
    create_user_record,
    update_user_record,
    get_license_by_serial,
    get_license_by_id,
    get_user_license,
    create_license_record,
    update_license_record,
    get_device_by_fingerprint_and_license,
    get_active_device_count_for_license,
    register_device,
    update_device_last_seen,
    create_session_record,
    get_session_by_token,
    deactivate_session,
    is_license_active,
    get_user_last_expired_license,
    get_all_licenses_for_user,
    add_audit_log
)
from src.app.services.device_service import get_system_device_fingerprint

# In-memory rate limiting: {identifier: [timestamps]}
_FAILED_ATTEMPTS: Dict[str, list] = {}
RATE_LIMIT_WINDOW_SECONDS = 300 # 5 minutes
RATE_LIMIT_MAX_ATTEMPTS = 6     # max failed attempts in window

# Password recovery rate limiting: max 5 recovery attempts per 10-minute window
_RECOVERY_ATTEMPTS: Dict[str, list] = {}
RECOVERY_RATE_LIMIT_WINDOW = 600 # 10 minutes
RECOVERY_RATE_LIMIT_MAX = 5      # max 5 attempts in window

def _check_recovery_rate_limit(identifier: str):
    """Ensure client has not exceeded recovery attempt threshold."""
    now = time.time()
    history = _RECOVERY_ATTEMPTS.get(identifier, [])
    history = [t for t in history if now - t < RECOVERY_RATE_LIMIT_WINDOW]
    _RECOVERY_ATTEMPTS[identifier] = history
    if len(history) >= RECOVERY_RATE_LIMIT_MAX:
        raise RuntimeError("Too many recovery attempts. Please try again later.")

def _record_recovery_attempt(identifier: str):
    now = time.time()
    if identifier not in _RECOVERY_ATTEMPTS:
        _RECOVERY_ATTEMPTS[identifier] = []
    _RECOVERY_ATTEMPTS[identifier].append(now)

def _clear_recovery_attempts(identifier: str):
    _RECOVERY_ATTEMPTS.pop(identifier, None)

SERIAL_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

def generate_serial_key(max_devices: int = 1) -> str:
    """Generate a unique cryptographically random serial key: Gettik-XXXX-N with collision check."""
    dev_count = max(1, int(max_devices))
    for _ in range(50):
        chars = ''.join(secrets.choice(SERIAL_ALPHABET) for _ in range(4))
        key = f"Gettik-{chars}-{dev_count}"
        if not get_license_by_serial(key):
            return key
    raise RuntimeError("Failed to generate a unique serial key after multiple attempts.")

def hash_password(plain_password: str) -> str:
    """Hash plaintext password using bcrypt with salt."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(plain_password.encode("utf-8"), salt).decode("utf-8")

def verify_password(plain_password: str, password_hash: str) -> bool:
    """Verify plaintext password against bcrypt hash."""
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False

def _check_rate_limit(identifier: str):
    """Ensure client hasn't exceeded failed login threshold."""
    now = time.time()
    history = _FAILED_ATTEMPTS.get(identifier, [])
    # Prune old timestamps
    history = [t for t in history if now - t < RATE_LIMIT_WINDOW_SECONDS]
    _FAILED_ATTEMPTS[identifier] = history
    if len(history) >= RATE_LIMIT_MAX_ATTEMPTS:
        raise RuntimeError("Too many failed attempts. Please try again later.")

def _record_failed_attempt(identifier: str):
    now = time.time()
    if identifier not in _FAILED_ATTEMPTS:
        _FAILED_ATTEMPTS[identifier] = []
    _FAILED_ATTEMPTS[identifier].append(now)

def _clear_failed_attempts(identifier: str):
    _FAILED_ATTEMPTS.pop(identifier, None)

def generate_temporary_password() -> str:
    """Generate a secure temporary password (e.g. Gettik786-style)."""
    code = secrets.randbelow(900) + 100
    return f"Gettik{code}"

def request_password_recovery(
    email: str,
    serial_key: str,
    client_ip: str = "127.0.0.1"
) -> Dict[str, Any]:
    """
    Handle user password recovery request.
    Verifies email and serial key.
    Accepts the key if it is currently ACTIVE, OR the user's LAST EXPIRED license key.
    Generates a secure temporary password with exact 10-minute server-side expiry.
    """
    clean_email = email.strip().lower() if email else ""
    clean_serial = serial_key.strip().upper() if serial_key else ""
    rate_id = f"{client_ip}:{clean_email}"
    _check_recovery_rate_limit(rate_id)

    if not clean_email or not re.match(r"^[^@]+@[^@]+\.[^@]+$", clean_email):
        _record_recovery_attempt(rate_id)
        raise RuntimeError("A valid email address is required.")

    if not clean_serial:
        _record_recovery_attempt(rate_id)
        raise RuntimeError("A valid serial key is required.")

    # 1. Lookup user by email
    user = get_user_by_email(clean_email)
    if not user:
        _record_recovery_attempt(rate_id)
        raise RuntimeError("Invalid email or serial key.")

    # 2. Lookup license by serial
    lic = get_license_by_serial(clean_serial)
    if not lic:
        _record_recovery_attempt(rate_id)
        raise RuntimeError("Invalid email or serial key.")

    # 3. Verify Email + Key belong to the same account
    if lic.get("assigned_user_id") != user["id"]:
        _record_recovery_attempt(rate_id)
        raise RuntimeError("The serial key does not belong to this account.")

    # 4. Check eligibility:
    #    - Currently ACTIVE, OR
    #    - The user's LAST EXPIRED license key.
    is_active = is_license_active(lic)
    last_expired = get_user_last_expired_license(user["id"])
    is_last_exp = (last_expired is not None and last_expired["id"] == lic["id"])

    if not (is_active or is_last_exp):
        _record_recovery_attempt(rate_id)
        raise RuntimeError("The serial key must be currently active or your last expired license key.")

    # 5. Security: Do not reactivate or extend expired licenses!
    # The license status and expiry in DB remain unchanged.

    # 6. Generate secure temporary password
    temp_pw = generate_temporary_password()
    temp_hash = hash_password(temp_pw)
    expiry_dt = datetime.now() + timedelta(minutes=10)
    expiry_str = expiry_dt.strftime("%Y-%m-%d %H:%M:%S")

    # 7. Store in DB (hashed with bcrypt, 10-minute expiry, must_change_password flag)
    update_user_record(user["id"], {
        "temp_password_hash": temp_hash,
        "temp_password_expiry": expiry_str,
        "must_change_password": 1
    })

    # Clear rate limit on successful generation
    _clear_recovery_attempts(rate_id)

    # Audit log
    add_audit_log(
        actor_id=user["id"],
        actor_email=user["email"],
        action="Password Recovery Issued",
        target_type="users",
        target_id=user["id"],
        details="Temporary password issued (valid for 10 minutes)",
        ip_address=client_ip
    )

    return {
        "status": "ok",
        "temporary_password": temp_pw,
        "expires_in_minutes": 10,
        "expires_at": expiry_str,
        "message": "Temporary password generated successfully. It expires in exactly 10 minutes."
    }

def authenticate_user(
    email: str,
    password: str,
    serial_key: str,
    device_fingerprint: Optional[str] = None,
    client_ip: str = "127.0.0.1"
) -> Dict[str, Any]:
    """
    Execute the authoritative 7-step authentication pipeline.
    Raises RuntimeError with exact required error states on failure.
    """
    rate_limit_id = f"{client_ip}:{email.strip().lower()}"
    _check_rate_limit(rate_limit_id)

    # -------------------------------------------------------------------------
    # STEP 1: Validate input
    # -------------------------------------------------------------------------
    clean_email = email.strip().lower() if email else ""
    if not clean_email or not re.match(r"^[^@]+@[^@]+\.[^@]+$", clean_email):
        _record_failed_attempt(rate_limit_id)
        raise RuntimeError("Invalid email or password.")

    if not password:
        _record_failed_attempt(rate_limit_id)
        raise RuntimeError("Invalid email or password.")

    clean_serial = serial_key.strip().upper() if serial_key else ""
    if not clean_serial:
        _record_failed_attempt(rate_limit_id)
        raise RuntimeError("The serial key is invalid.")

    # -------------------------------------------------------------------------
    # STEP 2: Find user by email
    # -------------------------------------------------------------------------
    user = get_user_by_email(clean_email)
    if not user:
        _record_failed_attempt(rate_limit_id)
        # Generic error — do not reveal whether user exists
        raise RuntimeError("Invalid email or password.")

    # -------------------------------------------------------------------------
    # STEP 3: Verify password against stored bcrypt hash (or temporary password)
    # -------------------------------------------------------------------------
    is_temp_password = False
    temp_hash = user.get("temp_password_hash")
    temp_expiry_str = user.get("temp_password_expiry")

    if user.get("password_hash") and verify_password(password, user["password_hash"]):
        is_temp_password = False
    elif temp_hash and verify_password(password, temp_hash):
        if not temp_expiry_str:
            _record_failed_attempt(rate_limit_id)
            raise RuntimeError("The temporary password has expired. Please request a new one.")
        try:
            exp_clean = temp_expiry_str.replace("T", " ").split(".")[0]
            temp_expiry_dt = datetime.strptime(exp_clean, "%Y-%m-%d %H:%M:%S")
            if datetime.now() > temp_expiry_dt:
                _record_failed_attempt(rate_limit_id)
                raise RuntimeError("The temporary password has expired. Please request a new one.")
        except Exception:
            _record_failed_attempt(rate_limit_id)
            raise RuntimeError("The temporary password has expired. Please request a new one.")
        is_temp_password = True
    else:
        _record_failed_attempt(rate_limit_id)
        raise RuntimeError("Invalid email or password.")

    # -------------------------------------------------------------------------
    # STEP 4: Validate serial key exists & belongs to user
    # -------------------------------------------------------------------------
    license_rec = get_license_by_serial(clean_serial)
    if not license_rec:
        _record_failed_attempt(rate_limit_id)
        raise RuntimeError("The serial key is invalid.")

    # If license is assigned to another user, reject; if unassigned, bind to this user account
    if license_rec.get("assigned_user_id") and license_rec["assigned_user_id"] != user["id"]:
        _record_failed_attempt(rate_limit_id)
        raise RuntimeError("The serial key is invalid.")
    elif not license_rec.get("assigned_user_id"):
        update_license_record(license_rec["id"], {"assigned_user_id": user["id"]})
        license_rec["assigned_user_id"] = user["id"]

    # -------------------------------------------------------------------------
    # STEP 5: Validate account status
    # -------------------------------------------------------------------------
    user_status = (user.get("status") or "ACTIVE").upper()
    if user_status == "DISABLED":
        _record_failed_attempt(rate_limit_id)
        raise RuntimeError("This account is currently disabled.")
    elif user_status == "SUSPENDED":
        _record_failed_attempt(rate_limit_id)
        raise RuntimeError("Your Gettik account access has been suspended by an administrator.")
    elif user_status != "ACTIVE":
        _record_failed_attempt(rate_limit_id)
        raise RuntimeError("This account is currently disabled.")

    # -------------------------------------------------------------------------
    # STEP 6: Validate license status & expiration
    # -------------------------------------------------------------------------
    lic_status = (license_rec.get("status") or "ACTIVE").upper()
    if lic_status == "SUSPENDED":
        _record_failed_attempt(rate_limit_id)
        raise RuntimeError("This license is currently suspended.")
    elif lic_status == "REVOKED":
        _record_failed_attempt(rate_limit_id)
        raise RuntimeError("This license has been revoked.")

    is_expired_lic = (lic_status == "EXPIRED")
    expiry_str = license_rec.get("expiry_date", "")
    if expiry_str:
        try:
            # Handle ISO formats and standard SQLite timestamps
            exp_clean = expiry_str.replace("T", " ").split(".")[0]
            expiry_dt = datetime.strptime(exp_clean, "%Y-%m-%d %H:%M:%S")
            if datetime.now() > expiry_dt:
                is_expired_lic = True
                update_license_record(license_rec["id"], {"status": "EXPIRED"})
        except ValueError:
            pass

    if is_expired_lic:
        last_exp = get_user_last_expired_license(user["id"])
        if is_temp_password and last_exp and last_exp["id"] == license_rec["id"]:
            # Allowed for temporary login to update credentials.
            # Do NOT reactivate or extend license!
            pass
        else:
            _record_failed_attempt(rate_limit_id)
            raise RuntimeError("This license has expired.")
    elif lic_status != "ACTIVE":
        _record_failed_attempt(rate_limit_id)
        raise RuntimeError("The serial key is invalid.")

    # -------------------------------------------------------------------------
    # STEP 7: Validate device & enforce device limits
    # -------------------------------------------------------------------------
    # Always resolve to deterministic system HWID if not supplied or for authoritative integrity
    hwid = device_fingerprint.strip() if device_fingerprint else get_system_device_fingerprint()
    existing_dev = get_device_by_fingerprint_and_license(hwid, license_rec["id"])

    if existing_dev:
        dev_status = (existing_dev.get("status") or "ACTIVE").upper()
        if dev_status == "REVOKED":
            _record_failed_attempt(rate_limit_id)
            raise RuntimeError("This device has been revoked by an administrator.")
        elif dev_status == "DEACTIVATED":
            # Check slot capacity before reactivating
            active_count = get_active_device_count_for_license(license_rec["id"])
            max_devices = license_rec.get("max_devices", 1)
            if active_count >= max_devices:
                _record_failed_attempt(rate_limit_id)
                raise RuntimeError("This license has reached its device limit. Please contact an administrator.")
            deactivate_device(existing_dev["id"], status="ACTIVE")
        # Device is already recognized/reactivated: update last seen
        update_device_last_seen(existing_dev["id"])
    else:
        # New device attempting to authorize under this license
        active_count = get_active_device_count_for_license(license_rec["id"])
        max_devices = license_rec.get("max_devices", 1)
        if active_count >= max_devices:
            _record_failed_attempt(rate_limit_id)
            raise RuntimeError("This license has reached its device limit. Please contact an administrator.")
        # Register new device
        register_device(hwid, user["id"], license_rec["id"])

    # -------------------------------------------------------------------------
    # SUCCESS: Create Authenticated Session
    # -------------------------------------------------------------------------
    _clear_failed_attempts(rate_limit_id)

    # Update user last login
    update_user_record(user["id"], {"last_login_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

    # Generate secure 256-bit session token
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    create_session_record(token, user["id"], hwid, expires_at)

    return {
        "status": "ok",
        "token": token,
        "user": {
            "id": user["id"],
            "name": user.get("name") or user.get("username") or user["email"].split("@")[0],
            "email": user["email"],
            "username": user.get("username") or user["email"].split("@")[0],
            "role": user.get("role", "user"),
            "plan": user.get("plan", "Pro Plan"),
            "status": user.get("status", "ACTIVE"),
            "must_change_password": bool(user.get("must_change_password") or is_temp_password)
        },
        "license": _format_license_payload(license_rec),
        "device_fingerprint": hwid
    }

def _format_license_payload(license_rec: Dict[str, Any]) -> Dict[str, Any]:
    """Build authoritative license payload with real duration, issue date, and remaining days."""
    from src.app.database.db import calculate_days_remaining
    expiry = license_rec.get("expiry_date") or ""
    days_left = calculate_days_remaining(expiry)
    duration = license_rec.get("duration_days") or 30
    issued = license_rec.get("issued_at") or license_rec.get("start_date") or license_rec.get("created_at") or ""
    
    return {
        "serial_key": license_rec.get("serial_key"),
        "status": license_rec.get("status", "ACTIVE"),
        "expiry_date": expiry,
        "expires_at": expiry,
        "max_devices": license_rec.get("max_devices", 1),
        "duration_days": duration,
        "issued_at": issued,
        "days_remaining": days_left
    }

def verify_session(token: str) -> Optional[Dict[str, Any]]:
    """
    Authoritative 6-point server-side session and access verification:
    1. Check session existence.
    2. Validate user account status (ACTIVE, DISABLED, SUSPENDED).
    3. Validate user license assignment & status (ACTIVE, REVOKED, SUSPENDED, EXPIRED).
    4. Validate hardware device slot authorization (ACTIVE, REVOKED, DEACTIVATED).
    5. Check active session flag.
    6. Check session expiration timestamp.

    Raises HTTPException(401) on missing/invalid token or session expiration.
    Raises HTTPException(403) on revoked/disabled/suspended access with exact explanation.
    """
    from fastapi import HTTPException
    from src.app.database.db import get_db_connection

    if not token:
        raise HTTPException(status_code=401, detail="Authentication token required.")

    with get_db_connection() as conn:
        session = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()

    if not session:
        raise HTTPException(status_code=401, detail="Session is expired or invalid. Please log in.")

    # 1. User validation
    user = get_user_by_id(session["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User account not found.")

    user_status = (user.get("status") or "").upper()
    if user_status == "DISABLED":
        raise HTTPException(status_code=403, detail="Your Gettik user account has been disabled by an administrator.")
    if user_status == "SUSPENDED":
        raise HTTPException(status_code=403, detail="Your Gettik account access has been suspended by an administrator.")
    if user_status != "ACTIVE":
        raise HTTPException(status_code=403, detail="Your Gettik user account is not active.")

    must_change = bool(user.get("must_change_password"))

    # 2. License validation
    license_rec = get_user_license(user["id"])
    if not license_rec:
        raise HTTPException(status_code=403, detail="No active license assigned to this account.")

    lic_status = (license_rec.get("status") or "").upper()
    if lic_status == "REVOKED":
        raise HTTPException(status_code=403, detail="Your Gettik access has been revoked by an administrator.")
    if lic_status == "SUSPENDED":
        raise HTTPException(status_code=403, detail="Your Gettik license has been suspended by an administrator.")

    # 3. License expiration check
    is_lic_expired = (lic_status == "EXPIRED")
    expiry_str = license_rec.get("expiry_date", "")
    if expiry_str:
        try:
            exp_clean = expiry_str.replace("T", " ").split(".")[0]
            expiry_dt = datetime.strptime(exp_clean, "%Y-%m-%d %H:%M:%S")
            if datetime.now() > expiry_dt:
                is_lic_expired = True
                update_license_record(license_rec["id"], {"status": "EXPIRED"})
        except ValueError:
            pass

    if is_lic_expired:
        if not must_change:
            deactivate_session(token)
            raise HTTPException(status_code=403, detail="Your Gettik license has expired.")
    elif lic_status != "ACTIVE":
        raise HTTPException(status_code=403, detail="Your Gettik license is not active.")

    # 4. Device status validation
    hwid = session["device_fingerprint"]
    dev = get_device_by_fingerprint_and_license(hwid, license_rec["id"])
    if dev:
        dev_status = (dev.get("status") or "").upper()
        if dev_status == "REVOKED":
            raise HTTPException(status_code=403, detail="This device has been revoked by an administrator.")
        if dev_status == "DEACTIVATED":
            raise HTTPException(status_code=403, detail="This device has been deactivated by an administrator.")
        if dev_status != "ACTIVE":
            raise HTTPException(status_code=403, detail="This device is not authorized.")
        update_device_last_seen(dev["id"])
    else:
        raise HTTPException(status_code=403, detail="This device is not authorized for your license.")

    # 5. Session active flag check
    if session["is_active"] != 1:
        raise HTTPException(status_code=401, detail="Session is expired or invalid. Please log in.")

    # 6. Session expiration check
    try:
        exp_dt = datetime.strptime(session["expires_at"], "%Y-%m-%d %H:%M:%S")
        if datetime.now() > exp_dt:
            deactivate_session(token)
            raise HTTPException(status_code=401, detail="Session has expired. Please log in.")
    except Exception:
        pass

    return {
        "user": {
            "id": user["id"],
            "name": user.get("name") or user.get("username") or user["email"].split("@")[0],
            "email": user["email"],
            "username": user.get("username") or user["email"].split("@")[0],
            "role": user.get("role", "user"),
            "plan": user.get("plan", "Pro Plan"),
            "status": user.get("status", "ACTIVE"),
            "must_change_password": must_change
        },
        "license": _format_license_payload(license_rec),
        "device_fingerprint": session["device_fingerprint"]
    }

def logout_session(token: str) -> bool:
    """
    Deactivate active session token.
    CRITICAL: Does NOT free device slot in devices table!
    """
    if not token:
        return True
    return deactivate_session(token)

def change_user_password(
    user_id: str,
    current_password: str,
    new_password: str,
    client_ip: str = "127.0.0.1"
) -> bool:
    """
    Change user password after verifying current or temporary password.
    Enforces minimum length of 8 chars.
    Invalidates temporary password permanently upon success.
    New password becomes the normal login password.
    """
    user = get_user_by_id(user_id)
    if not user:
        raise RuntimeError("User account not found.")

    if not current_password:
        raise RuntimeError("Current password is required.")

    if not new_password or len(new_password) < 8:
        raise RuntimeError("New password must be at least 8 characters long.")

    # Check current password: could be either permanent password OR unexpired temporary password
    matches_normal = bool(user.get("password_hash") and verify_password(current_password, user["password_hash"]))
    matches_temp = False
    temp_hash = user.get("temp_password_hash")
    temp_exp = user.get("temp_password_expiry")
    if temp_hash and verify_password(current_password, temp_hash):
        if user.get("must_change_password"):
            matches_temp = True
        elif temp_exp:
            try:
                exp_clean = temp_exp.replace("T", " ").split(".")[0]
                exp_dt = datetime.strptime(exp_clean, "%Y-%m-%d %H:%M:%S")
                if datetime.now() <= exp_dt:
                    matches_temp = True
            except Exception:
                pass

    if not (matches_normal or matches_temp):
        raise RuntimeError("Current password verification failed.")

    # Hash new password using bcrypt
    new_hash = hash_password(new_password)

    # Invalidate temporary password permanently, save new permanent password, reset must_change_password
    update_user_record(user_id, {
        "password_hash": new_hash,
        "temp_password_hash": None,
        "temp_password_expiry": None,
        "must_change_password": 0
    })

    add_audit_log(
        actor_id=user["id"],
        actor_email=user["email"],
        action="Password Changed",
        target_type="users",
        target_id=user["id"],
        details="User password successfully updated; temporary password invalidated permanently.",
        ip_address=client_ip
    )
    return True

