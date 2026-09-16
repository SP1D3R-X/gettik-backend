"""
Gettik Authentication & Administrative Routes
"""

import re
import uuid
from typing import Optional, Dict, Any, List
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Depends, Header, Request

from src.app.services.auth_service import (
    authenticate_user,
    verify_session,
    logout_session,
    generate_serial_key,
    hash_password,
    request_password_recovery,
    change_user_password
)
from src.app.services.device_service import get_system_device_fingerprint, get_device_info
from src.app.database.db import (
    get_all_users,
    get_user_by_email,
    get_user_by_id,
    create_user_record,
    update_user_record,
    get_all_licenses,
    get_license_by_id,
    create_license_record,
    update_license_record,
    get_all_devices,
    deactivate_device,
    create_license_request_record,
    get_latest_license_request_for_user,
    add_audit_log
)

router = APIRouter(prefix="/api")

# --- Request Models ---

class LoginRequest(BaseModel):
    email: str
    password: str
    serial_key: str
    device_fingerprint: Optional[str] = None

class ForgotPasswordRequest(BaseModel):
    email: str
    serial_key: str

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: Optional[str] = None

class UserLicenseRequestPayload(BaseModel):
    email: str
    password: Optional[str] = None
    message: Optional[str] = None
    note: Optional[str] = None

class AdminCreateUserRequest(BaseModel):
    email: str
    password: str
    role: str = "user"
    plan: str = "Pro Plan"
    status: str = "ACTIVE"
    serial_key: Optional[str] = None
    max_devices: int = 1
    expiry_date: Optional[str] = None

class AdminUpdateUserRequest(BaseModel):
    status: Optional[str] = None
    role: Optional[str] = None
    plan: Optional[str] = None
    password: Optional[str] = None

class AdminCreateLicenseRequest(BaseModel):
    user_id: str
    expiry_date: str
    max_devices: int = 1
    status: str = "ACTIVE"

class AdminUpdateLicenseRequest(BaseModel):
    status: Optional[str] = None
    expiry_date: Optional[str] = None
    max_devices: Optional[int] = None

# --- Security Dependencies ---

def extract_token_from_header(authorization: Optional[str] = Header(None)) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return authorization

def get_current_user(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """Dependency ensuring caller is an authenticated user."""
    token = extract_token_from_header(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication token required.")
    session_data = verify_session(token)
    if not session_data:
        raise HTTPException(status_code=401, detail="Session is expired or invalid. Please log in.")
    return session_data

def get_current_admin(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """Dependency ensuring caller has administrative privileges."""
    session_data = get_current_user(authorization)
    user = session_data["user"]
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Administrative access required.")
    return session_data

# =========================================================================
# Authentication Endpoints
# =========================================================================

@router.post("/auth/login")
async def api_login(req: LoginRequest, request: Request):
    """
    Authenticate user with Email, Password, and Serial Key.
    Validates account, license, and device limits.
    """
    client_ip = request.client.host if request.client else "127.0.0.1"
    try:
        auth_result = authenticate_user(
            email=req.email,
            password=req.password,
            serial_key=req.serial_key,
            device_fingerprint=req.device_fingerprint,
            client_ip=client_ip
        )
        return auth_result
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Unable to connect to Gettik services. Please try again.")

@router.post("/auth/forgot-password")
async def api_forgot_password(req: ForgotPasswordRequest, request: Request):
    """
    Recover account password using Email and active or last expired Serial Key.
    Generates a secure temporary password (e.g. Gettik786-style) expiring in 10 minutes.
    """
    client_ip = request.client.host if request.client else "127.0.0.1"
    try:
        recovery_result = request_password_recovery(
            email=req.email,
            serial_key=req.serial_key,
            client_ip=client_ip
        )
        return recovery_result
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Unable to process password recovery. Please try again.")

@router.get("/me")
@router.get("/auth/me")
async def api_get_current_user(auth: Dict[str, Any] = Depends(get_current_user)):
    """Return authenticated profile, active license, and device status."""
    return {"status": "ok", "data": auth}

@router.get("/me/license")
@router.get("/auth/me/license")
async def api_get_my_license(auth: Dict[str, Any] = Depends(get_current_user)):
    """Return authoritative authenticated license details and device limits."""
    from datetime import datetime, timedelta
    from src.app.database.db import (
        get_user_license,
        get_active_device_count_for_license,
        calculate_days_remaining,
        update_license_record
    )

    user = auth.get("user", {})
    lic = auth.get("license", {})
    license_rec = get_user_license(user["id"]) if user.get("id") else None

    if not license_rec:
        return {
            "status": "ok",
            "data": {
                "status": "INACTIVE",
                "serial_key": None,
                "duration_days": 0,
                "issued_at": None,
                "expires_at": None,
                "expiry_date": None,
                "remaining_days": 0,
                "days_remaining": 0,
                "max_devices": 1,
                "active_devices": 0
            }
        }

    status = (license_rec.get("status") or "ACTIVE").upper()
    duration_days = license_rec.get("duration_days") or 30
    issued_at = license_rec.get("issued_at") or license_rec.get("start_date") or license_rec.get("created_at") or ""
    expiry_date = license_rec.get("expiry_date") or ""

    # If expiry_date is missing, calculate server-side from issued_at + duration_days
    if not expiry_date and issued_at:
        try:
            iss_clean = issued_at.replace("T", " ").split(".")[0]
            iss_dt = datetime.strptime(iss_clean, "%Y-%m-%d %H:%M:%S")
            exp_dt = iss_dt + timedelta(days=duration_days)
            expiry_date = exp_dt.strftime("%Y-%m-%d %H:%M:%S")
            update_license_record(license_rec["id"], {"expiry_date": expiry_date})
        except Exception:
            pass

    days_remaining = calculate_days_remaining(expiry_date)
    active_devices = get_active_device_count_for_license(license_rec["id"])

    return {
        "status": "ok",
        "data": {
            "status": status,
            "serial_key": license_rec.get("serial_key"),
            "duration_days": duration_days,
            "issued_at": issued_at,
            "expires_at": expiry_date,
            "expiry_date": expiry_date,
            "remaining_days": days_remaining,
            "days_remaining": days_remaining,
            "max_devices": license_rec.get("max_devices", 1),
            "active_devices": active_devices
        }
    }

@router.post("/auth/logout")
async def api_logout(authorization: Optional[str] = Header(None)):
    """
    Deactivate current session token.
    CRITICAL: Device slot remains authorized in database!
    """
    token = extract_token_from_header(authorization)
    if token:
        logout_session(token)
    return {"status": "ok", "message": "Logged out successfully."}

@router.post("/auth/change-password")
async def api_change_password(
    req: ChangePasswordRequest,
    request: Request,
    auth: Dict[str, Any] = Depends(get_current_user)
):
    """
    Update password for the authenticated user.
    Validates current/temporary password, updates bcrypt hash, and invalidates temporary password permanently.
    """
    user = auth.get("user", {})
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required.")

    if req.confirm_password is not None and req.new_password != req.confirm_password:
        raise HTTPException(status_code=400, detail="New passwords do not match.")

    client_ip = request.client.host if request.client else "127.0.0.1"
    try:
        change_user_password(
            user_id=user_id,
            current_password=req.current_password,
            new_password=req.new_password,
            client_ip=client_ip
        )
        return {"status": "ok", "message": "Password changed successfully."}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Unable to update password. Please try again.")

@router.get("/system/device-info")
async def api_device_info():
    """Return deterministic local hardware information."""
    return {"status": "ok", "data": get_device_info()}

@router.post("/auth/license-request")
async def api_submit_license_request(req: UserLicenseRequestPayload, request: Request):
    """
    Allow user to submit an access request from inside the application.
    The request enters PENDING status. NO serial key is generated or issued automatically.
    """
    clean_email = req.email.strip().lower() if req.email else ""
    if not clean_email or not re.match(r"^[^@]+@[^@]+\.[^@]+$", clean_email):
        raise HTTPException(status_code=400, detail="A valid email address is required.")

    # Find or create user
    user = get_user_by_email(clean_email)
    if not user:
        # Create user record so credentials are established
        user_id = f"usr_{uuid.uuid4().hex[:12]}"
        pw_hash = hash_password(req.password if req.password else "GettikDefault2026!")
        user = create_user_record(
            user_id=user_id,
            email=clean_email,
            password_hash=pw_hash,
            role="user",
            plan="Standard Plan",
            status="ACTIVE"
        )
    elif req.password:
        if not user.get("password_hash"):
            update_user_record(user["id"], {"password_hash": hash_password(req.password)})

    # Check if user already has an active PENDING request
    latest_req = get_latest_license_request_for_user(user["id"])
    if latest_req and latest_req["status"] == "PENDING":
        return {
            "status": "pending",
            "request_id": latest_req["id"],
            "message": "Your request has already been submitted and is waiting for administrator approval."
        }

    # Create new PENDING request
    req_id = f"REQ-{uuid.uuid4().hex[:8].upper()}"
    user_note = req.message or req.note or ""
    license_req = create_license_request_record(
        request_id=req_id,
        user_id=user["id"],
        message=user_note
    )

    client_ip = request.client.host if request.client else "127.0.0.1"
    add_audit_log(
        actor_id=user["id"],
        actor_email=clean_email,
        action="License Request Submitted",
        target_type="license_requests",
        target_id=req_id,
        details=f"User {clean_email} submitted access request: {user_note or 'No message'}",
        ip_address=client_ip
    )

    return {
        "status": "ok",
        "request_id": req_id,
        "message": "Your request has been submitted and is waiting for administrator approval."
    }

@router.get("/auth/license-request/status")
async def api_get_license_request_status(email: str):
    """
    Allow user to check the status of their license request.
    CRITICAL: Never exposes the private Serial Key!
    """
    clean_email = email.strip().lower() if email else ""
    if not clean_email:
        raise HTTPException(status_code=400, detail="Email is required.")

    user = get_user_by_email(clean_email)
    if not user:
        raise HTTPException(status_code=404, detail="No account or license request found for this email.")

    latest_req = get_latest_license_request_for_user(user["id"])
    if not latest_req:
        raise HTTPException(status_code=404, detail="No license request found for this account.")

    status = latest_req["status"]
    if status == "PENDING":
        friendly_message = "Your request has been submitted and is waiting for administrator approval."
    elif status == "APPROVED":
        friendly_message = "License Issued. An administrator has approved your request and issued your license. Please use the Serial Key privately provided to you to log in."
    elif status == "REJECTED":
        reason = latest_req.get("rejection_reason")
        friendly_message = f"Your request was rejected.{' Reason: ' + reason if reason else ''}"
    else:
        friendly_message = f"Status: {status}"

    return {
        "status": "ok",
        "request_id": latest_req["id"],
        "request_status": status,
        "submitted_at": latest_req["created_at"],
        "reviewed_at": latest_req.get("reviewed_at"),
        "message": friendly_message
    }

# =========================================================================
# Administrative Endpoints (Users, Licenses, Devices)
# =========================================================================

@router.get("/admin/users")
async def api_admin_get_users(admin: Dict[str, Any] = Depends(get_current_admin)):
    """List all registered users."""
    users = get_all_users()
    return {"status": "ok", "data": users}

@router.post("/admin/users")
async def api_admin_create_user(req: AdminCreateUserRequest, admin: Dict[str, Any] = Depends(get_current_admin)):
    """
    Administrator creates a user and optionally generates their serial key.
    Normal users have NO public registration.
    """
    existing = get_user_by_email(req.email)
    if existing:
        raise HTTPException(status_code=400, detail="A user with this email already exists.")

    user_id = f"usr_{uuid.uuid4().hex[:12]}"
    pw_hash = hash_password(req.password)
    user = create_user_record(
        user_id=user_id,
        email=req.email,
        password_hash=pw_hash,
        role=req.role,
        plan=req.plan,
        status=req.status
    )

    # Automatically generate license if not provided or if expiry requested
    serial = req.serial_key.strip() if req.serial_key else generate_serial_key(req.max_devices)
    expiry = req.expiry_date or "2027-09-12 23:59:59"
    license_rec = create_license_record(
        serial_key=serial,
        assigned_user_id=user_id,
        expiry_date=expiry,
        max_devices=req.max_devices,
        status="ACTIVE"
    )

    return {
        "status": "created",
        "user": user,
        "license": license_rec
    }

@router.put("/admin/users/{user_id}")
async def api_admin_update_user(user_id: str, req: AdminUpdateUserRequest, admin: Dict[str, Any] = Depends(get_current_admin)):
    """Update user status or reset password."""
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    fields: Dict[str, Any] = {}
    if req.status:
        fields["status"] = req.status
    if req.role:
        fields["role"] = req.role
    if req.plan:
        fields["plan"] = req.plan
    if req.password:
        fields["password_hash"] = hash_password(req.password)

    update_user_record(user_id, fields)
    return {"status": "updated", "user": get_user_by_id(user_id)}

@router.get("/admin/licenses")
async def api_admin_get_licenses(admin: Dict[str, Any] = Depends(get_current_admin)):
    """List all licenses with associated devices."""
    licenses = get_all_licenses()
    return {"status": "ok", "data": licenses}

@router.post("/admin/licenses")
async def api_admin_generate_license(req: AdminCreateLicenseRequest, admin: Dict[str, Any] = Depends(get_current_admin)):
    """Generate a new cryptographic license key: Gettik-XXXX-N."""
    serial = generate_serial_key(req.max_devices)
    lic = create_license_record(
        serial_key=serial,
        assigned_user_id=req.user_id,
        expiry_date=req.expiry_date,
        max_devices=req.max_devices,
        status=req.status
    )
    return {"status": "created", "license": lic}

@router.put("/admin/licenses/{license_id}")
async def api_admin_update_license(license_id: int, req: AdminUpdateLicenseRequest, admin: Dict[str, Any] = Depends(get_current_admin)):
    """Manage license status (ACTIVE, SUSPENDED, REVOKED), devices, or expiry."""
    lic = get_license_by_id(license_id)
    if not lic:
        raise HTTPException(status_code=404, detail="License not found.")

    fields: Dict[str, Any] = {}
    if req.status:
        fields["status"] = req.status.upper()
    if req.expiry_date:
        fields["expiry_date"] = req.expiry_date
    if req.max_devices is not None:
        fields["max_devices"] = req.max_devices

    update_license_record(license_id, fields)
    return {"status": "updated", "license": get_license_by_id(license_id)}

@router.get("/admin/devices")
async def api_admin_get_devices(admin: Dict[str, Any] = Depends(get_current_admin)):
    """List all registered devices across licenses."""
    devices = get_all_devices()
    return {"status": "ok", "data": devices}

@router.post("/admin/devices/{device_id}/deactivate")
async def api_admin_deactivate_device(device_id: int, admin: Dict[str, Any] = Depends(get_current_admin)):
    """
    Deactivate or revoke an authorized device slot.
    Frees up the device slot for the user's license!
    """
    deactivate_device(device_id, status="DEACTIVATED")
    return {"status": "deactivated", "device_id": device_id}

