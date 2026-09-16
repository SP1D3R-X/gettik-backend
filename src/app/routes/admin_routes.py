"""
Gettik Dedicated Admin API Routes
Full server-side administrative control panel endpoints:
- Authentication & Sessions (Email + Password ONLY)
- Dashboard Metrics
- User Management
- License Management & Cryptographic Serial Generation
- Device Management & Slot Deactivation
- Creator Records
- Download Records
- Analytics
- Admin RBAC Management
- Admin Account & Security Settings
- System Settings
- Immutable Audit Logs
- Live System Health
"""

import uuid
from typing import Optional, Dict, Any, List
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Depends, Header, Request, Query

from src.app.services.admin_service import (
    authenticate_admin,
    verify_admin_session,
    logout_admin,
    update_admin_email,
    update_admin_password,
    get_system_health_status,
    approve_license_request,
    reject_license_request,
    extend_license
)
from src.app.database.db import (
    get_admin_dashboard_metrics,
    get_admin_users_extended,
    get_admin_licenses_extended,
    get_admin_devices_extended,
    get_admin_downloads_extended,
    get_admin_creators_extended,
    get_admin_analytics_data,
    get_all_admins,
    get_admin_by_id,
    get_admin_by_email,
    create_admin_record,
    update_admin_record,
    delete_admin_record,
    get_audit_logs,
    add_audit_log,
    get_system_settings_dict,
    update_system_setting,
    get_user_by_email,
    get_user_by_id,
    create_user_record,
    update_user_record,
    get_license_by_id,
    create_license_record,
    update_license_record,
    deactivate_device,
    get_device_by_id,
    deactivate_sessions_for_user,
    deactivate_sessions_for_license,
    deactivate_sessions_for_device,
    get_all_license_requests,
    get_license_request_by_id,
    extend_license_days,
    delete_license_record,
    get_all_feedback_admin,
    get_feedback_by_id,
    get_feedback_metrics,
    update_feedback_record,
    add_feedback_audit_log,
    get_feedback_audit_logs
)
from src.app.services.auth_service import hash_password, generate_serial_key
from src.app.services.creator_service import cancel_user_active_jobs

router = APIRouter(prefix="/api/admin")

# --- Request Models ---

class AdminLoginRequest(BaseModel):
    email: str
    password: str

class AdminEmailChangeRequest(BaseModel):
    current_password: str
    new_email: str

class AdminPasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str

class CreateAdminUserRequest(BaseModel):
    name: str
    email: str
    password: str
    role: str = "ADMIN" # SUPER_ADMIN, ADMIN, SUPPORT
    permissions: str = "*"

class UpdateAdminUserRequest(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    permissions: Optional[str] = None
    status: Optional[str] = None

class CreateUserPayload(BaseModel):
    name: Optional[str] = None
    email: str
    password: str
    username: Optional[str] = None
    plan: str = "Pro Plan"
    status: str = "ACTIVE"
    max_devices: int = 1
    expiry_date: Optional[str] = None
    serial_key: Optional[str] = None

class UpdateUserPayload(BaseModel):
    name: Optional[str] = None
    username: Optional[str] = None
    email: Optional[str] = None
    plan: Optional[str] = None
    status: Optional[str] = None
    password: Optional[str] = None
    reason: Optional[str] = ""

class ApproveLicenseRequestPayload(BaseModel):
    duration_days: int = 30
    max_devices: int = 1

class RejectLicenseRequestPayload(BaseModel):
    reason: Optional[str] = ""

class ExtendLicenseDaysPayload(BaseModel):
    additional_days: int

class CreateLicensePayload(BaseModel):
    user_id: Optional[str] = None
    duration_days: Optional[int] = 30
    expiry_date: Optional[str] = None
    max_devices: int = 1
    status: str = "ACTIVE"

class BatchGenerateLicensesPayload(BaseModel):
    count: int = 5
    duration_days: Optional[int] = 30
    expiry_date: Optional[str] = None
    max_devices: int = 1
    status: str = "ACTIVE"

class UpdateLicensePayload(BaseModel):
    status: Optional[str] = None
    expiry_date: Optional[str] = None
    max_devices: Optional[int] = None
    assigned_user_id: Optional[str] = None
    duration_days: Optional[int] = None
    reason: Optional[str] = ""

class DeactivateDevicePayload(BaseModel):
    reason: Optional[str] = ""

class UpdateSettingsPayload(BaseModel):
    settings: Dict[str, Any]

# --- Admin Authorization Dependency ---

def extract_token_from_header(authorization: Optional[str] = Header(None)) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return authorization

def get_current_admin(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """
    Authoritative dependency verifying caller is an authenticated administrator.
    Normal user tokens return 403 Forbidden.
    Missing/expired tokens return 401 Unauthorized.
    """
    token = extract_token_from_header(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Administrator authentication token required.")
    admin = verify_admin_session(token)
    if not admin:
        raise HTTPException(status_code=401, detail="Admin session is expired or invalid. Please log in.")
    return admin

def require_super_admin(admin: Dict[str, Any] = Depends(get_current_admin)) -> Dict[str, Any]:
    """Dependency restricting administrative operations to SUPER_ADMIN role."""
    if admin.get("role") != "SUPER_ADMIN":
        raise HTTPException(status_code=403, detail="Super Administrator privileges required.")
    return admin

# =========================================================================
# 1. Admin Authentication Endpoints (Email + Password ONLY)
# =========================================================================

@router.post("/auth/login")
async def api_admin_login(req: AdminLoginRequest, request: Request):
    """
    Authenticate an administrator with Email and Password ONLY.
    NO serial key or license key field.
    """
    client_ip = request.client.host if request.client else "127.0.0.1"
    try:
        res = authenticate_admin(req.email, req.password, client_ip=client_ip)
        return res
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal admin authentication error.")

@router.get("/me")
@router.get("/auth/me")
async def api_admin_me(admin: Dict[str, Any] = Depends(get_current_admin)):
    """Return active administrator profile."""
    return {"status": "ok", "admin": admin}

@router.post("/auth/logout")
async def api_admin_logout(request: Request, authorization: Optional[str] = Header(None)):
    """
    Logout administrator and invalidate session server-side.
    """
    token = extract_token_from_header(authorization)
    client_ip = request.client.host if request.client else "127.0.0.1"
    if token:
        logout_admin(token, client_ip=client_ip)
    return {"status": "ok", "message": "Admin session invalidated successfully."}

# =========================================================================
# 2. Admin Account & Security Settings
# =========================================================================

@router.put("/account/email")
async def api_admin_update_email(req: AdminEmailChangeRequest, request: Request, admin: Dict[str, Any] = Depends(get_current_admin)):
    """Change admin email after re-verifying current password."""
    client_ip = request.client.host if request.client else "127.0.0.1"
    try:
        updated = update_admin_email(admin["id"], req.current_password, req.new_email, client_ip=client_ip)
        return {"status": "ok", "message": "Email updated successfully.", "admin": updated}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.put("/account/password")
async def api_admin_update_password(
    req: AdminPasswordChangeRequest,
    request: Request,
    admin: Dict[str, Any] = Depends(get_current_admin),
    authorization: Optional[str] = Header(None)
):
    """Change admin password after verifying current password. Revokes other active sessions."""
    client_ip = request.client.host if request.client else "127.0.0.1"
    token = extract_token_from_header(authorization)
    try:
        update_admin_password(admin["id"], req.current_password, req.new_password, current_token=token, client_ip=client_ip)
        return {"status": "ok", "message": "Password changed successfully. Other active sessions revoked."}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

# =========================================================================
# 3. Admin RBAC Management (Super Admin Only)
# =========================================================================

@router.get("/admins")
@router.get("/rbac/admins")
async def api_get_admins(admin: Dict[str, Any] = Depends(get_current_admin)):
    """List all administrators."""
    admins = get_all_admins()
    return {"status": "ok", "data": admins}

@router.post("/admins")
async def api_create_admin(req: CreateAdminUserRequest, request: Request, admin: Dict[str, Any] = Depends(require_super_admin)):
    """Create a new administrator (SUPER_ADMIN role only)."""
    existing = get_admin_by_email(req.email)
    if existing:
        raise HTTPException(status_code=400, detail="An administrator with this email already exists.")
    admin_id = f"adm_{uuid.uuid4().hex[:10]}"
    pw_hash = hash_password(req.password)
    new_admin = create_admin_record(
        admin_id=admin_id,
        name=req.name,
        email=req.email,
        password_hash=pw_hash,
        role=req.role.upper(),
        permissions=req.permissions,
        status="ACTIVE"
    )
    client_ip = request.client.host if request.client else "127.0.0.1"
    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action="Admin Created",
        target_type="admins",
        target_id=admin_id,
        details=f"Created administrator {req.email} with role {req.role}",
        ip_address=client_ip
    )
    return {"status": "created", "admin": new_admin}

@router.put("/admins/{admin_id}")
async def api_update_admin(admin_id: str, req: UpdateAdminUserRequest, request: Request, admin: Dict[str, Any] = Depends(require_super_admin)):
    """Update administrator details or role."""
    target = get_admin_by_id(admin_id)
    if not target:
        raise HTTPException(status_code=404, detail="Administrator not found.")
    fields: Dict[str, Any] = {}
    if req.name: fields["name"] = req.name
    if req.role: fields["role"] = req.role.upper()
    if req.permissions: fields["permissions"] = req.permissions
    if req.status: fields["status"] = req.status.upper()
    update_admin_record(admin_id, fields)
    client_ip = request.client.host if request.client else "127.0.0.1"
    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action="Admin Updated",
        target_type="admins",
        target_id=admin_id,
        details=f"Updated administrator {target['email']} fields: {list(fields.keys())}",
        ip_address=client_ip
    )
    return {"status": "updated", "admin": get_admin_by_id(admin_id)}

@router.delete("/admins/{admin_id}")
async def api_delete_admin(admin_id: str, request: Request, admin: Dict[str, Any] = Depends(require_super_admin)):
    """Delete an administrator."""
    if admin_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own administrator account.")
    target = get_admin_by_id(admin_id)
    if not target:
        raise HTTPException(status_code=404, detail="Administrator not found.")
    delete_admin_record(admin_id)
    client_ip = request.client.host if request.client else "127.0.0.1"
    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action="Admin Deleted",
        target_type="admins",
        target_id=admin_id,
        details=f"Deleted administrator {target['email']}",
        ip_address=client_ip
    )
    return {"status": "deleted", "admin_id": admin_id}

# =========================================================================
# 4. Admin Dashboard Metrics (Real Database Data)
# =========================================================================

@router.get("/dashboard/metrics")
async def api_dashboard_metrics(admin: Dict[str, Any] = Depends(get_current_admin)):
    """Return real database statistics for the Admin Dashboard."""
    metrics = get_admin_dashboard_metrics()
    return {"status": "ok", "data": metrics}

# =========================================================================
# 5. User Management Endpoints
# =========================================================================

@router.get("/users")
async def api_admin_list_users(
    search: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    admin: Dict[str, Any] = Depends(get_current_admin)
):
    """List users with search, filter, and license/device counts."""
    users = get_admin_users_extended(search=search, status=status, limit=limit, offset=offset)
    return {"status": "ok", "data": users}

@router.post("/users")
async def api_admin_create_user(req: CreateUserPayload, request: Request, admin: Dict[str, Any] = Depends(get_current_admin)):
    """Create a user account and auto-generate their license key."""
    clean_email = req.email.strip().lower()
    existing = get_user_by_email(clean_email)
    if existing:
        raise HTTPException(status_code=400, detail="A user with this email already exists.")

    user_id = f"usr_{uuid.uuid4().hex[:12]}"
    pw_hash = hash_password(req.password)
    user = create_user_record(
        user_id=user_id,
        email=clean_email,
        password_hash=pw_hash,
        role="user",
        plan=req.plan,
        status=req.status,
        name=req.name or req.username
    )
    if req.username:
        update_user_record(user_id, {"username": req.username})

    # Generate license
    serial = req.serial_key.strip() if req.serial_key else generate_serial_key(req.max_devices)
    expiry = req.expiry_date or "2027-09-12 23:59:59"
    license_rec = create_license_record(
        serial_key=serial,
        assigned_user_id=user_id,
        expiry_date=expiry,
        max_devices=req.max_devices,
        status="ACTIVE"
    )

    client_ip = request.client.host if request.client else "127.0.0.1"
    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action="User Created",
        target_type="users",
        target_id=user_id,
        details=f"Created user {clean_email} with license {serial}",
        ip_address=client_ip
    )

    return {
        "status": "created",
        "user": get_user_by_id(user_id),
        "license": license_rec,
        "serial_key": license_rec["serial_key"],
        "duration_days": license_rec.get("duration_days", 30),
        "max_devices": license_rec.get("max_devices", req.max_devices)
    }

@router.get("/users/{user_id}")
async def api_admin_get_user(user_id: str, admin: Dict[str, Any] = Depends(get_current_admin)):
    """Get single user extended details."""
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return {"status": "ok", "data": user}

@router.put("/users/{user_id}")
async def api_admin_update_user(user_id: str, req: UpdateUserPayload, request: Request, admin: Dict[str, Any] = Depends(get_current_admin)):
    """Update user status, plan, username, or reset password with instant session invalidation."""
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    fields: Dict[str, Any] = {}
    if req.name: fields["name"] = req.name
    if req.username: fields["username"] = req.username
    if req.email:
        clean_new_email = req.email.strip().lower()
        existing_with_email = get_user_by_email(clean_new_email)
        if existing_with_email and existing_with_email["id"] != user_id:
            raise HTTPException(status_code=400, detail="A user with this email address already exists.")
        fields["email"] = clean_new_email
    if req.plan: fields["plan"] = req.plan
    if req.status: fields["status"] = req.status.upper()
    if req.password: fields["password_hash"] = hash_password(req.password)

    update_user_record(user_id, fields)

    # If user access was disabled or suspended, terminate active sessions & cancel running download jobs
    revocation_note = ""
    if req.status and req.status.upper() in ["DISABLED", "SUSPENDED"]:
        closed_sessions = deactivate_sessions_for_user(user_id)
        cancel_user_active_jobs(user_id)
        revocation_note = f" (Terminated {closed_sessions} active sessions & cancelled running jobs)"

    client_ip = request.client.host if request.client else "127.0.0.1"
    reason_note = f" Reason: {req.reason.strip()}." if req.reason else ""
    action_title = f"User {req.status.upper()}" if req.status else "User Updated"
    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action=action_title,
        target_type="users",
        target_id=user_id,
        details=f"Updated user {user['email']}.{reason_note}{revocation_note} (Fields: {list(fields.keys())})",
        ip_address=client_ip
    )
    return {"status": "updated", "user": get_user_by_id(user_id)}

@router.delete("/users/{user_id}")
async def api_admin_delete_user(user_id: str, request: Request, admin: Dict[str, Any] = Depends(get_current_admin)):
    """Disable a user account and immediately invalidate all sessions."""
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    update_user_record(user_id, {"status": "DISABLED"})
    closed_sessions = deactivate_sessions_for_user(user_id)
    cancel_user_active_jobs(user_id)
    client_ip = request.client.host if request.client else "127.0.0.1"
    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action="User Disabled",
        target_type="users",
        target_id=user_id,
        details=f"Disabled user account {user['email']}. Terminated {closed_sessions} active sessions.",
        ip_address=client_ip
    )
    return {"status": "disabled", "user_id": user_id}

# =========================================================================
# 5b. License Request Endpoints
# =========================================================================

@router.get("/license-requests")
async def api_admin_list_license_requests(
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    admin: Dict[str, Any] = Depends(get_current_admin)
):
    """List license access requests from users."""
    requests = get_all_license_requests(status=status, search=search, limit=limit, offset=offset)
    return {"status": "ok", "data": requests}

@router.get("/license-requests/{request_id}")
async def api_admin_get_license_request(
    request_id: str,
    admin: Dict[str, Any] = Depends(get_current_admin)
):
    """Get single license request details."""
    req = get_license_request_by_id(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="License request not found.")
    return {"status": "ok", "data": req}

@router.post("/license-requests/{request_id}/approve")
async def api_admin_approve_license_request(
    request_id: str,
    payload: ApproveLicenseRequestPayload,
    request: Request,
    admin: Dict[str, Any] = Depends(get_current_admin)
):
    """
    Approve license request with custom duration in DAYS and maximum device limit.
    Generates unique Serial Key and assigns license.
    """
    client_ip = request.client.host if request.client else "127.0.0.1"
    try:
        res = approve_license_request(
            request_id=request_id,
            admin_id=admin["id"],
            duration_days=payload.duration_days,
            max_devices=payload.max_devices,
            client_ip=client_ip
        )
        return {
            "status": "ok",
            "data": res,
            "license": res.get("license"),
            "serial_key": res.get("serial_key") or (res.get("license") or {}).get("serial_key"),
            "duration_days": res.get("duration_days") or (res.get("license") or {}).get("duration_days"),
            "max_devices": res.get("max_devices") or (res.get("license") or {}).get("max_devices"),
            "user_email": res.get("user_email")
        }
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to approve license request: {str(e)}")

@router.post("/license-requests/{request_id}/reject")
async def api_admin_reject_license_request(
    request_id: str,
    payload: RejectLicenseRequestPayload,
    request: Request,
    admin: Dict[str, Any] = Depends(get_current_admin)
):
    """Reject a pending license request with optional reason."""
    client_ip = request.client.host if request.client else "127.0.0.1"
    try:
        reject_license_request(
            request_id=request_id,
            admin_id=admin["id"],
            reason=payload.reason or "",
            client_ip=client_ip
        )
        return {"status": "ok", "message": "License request rejected."}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reject license request: {str(e)}")

# =========================================================================
# 6. License Management Endpoints
# =========================================================================

@router.get("/licenses")
async def api_admin_list_licenses(
    search: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    admin: Dict[str, Any] = Depends(get_current_admin)
):
    """List licenses with associated user details and device counts."""
    licenses = get_admin_licenses_extended(search=search, status=status, limit=limit, offset=offset)
    return {"status": "ok", "data": licenses}

@router.post("/licenses")
async def api_admin_create_license(req: CreateLicensePayload, request: Request, admin: Dict[str, Any] = Depends(get_current_admin)):
    """Generate a single cryptographic license key: Gettik-XXXX-N."""
    serial = generate_serial_key(req.max_devices)
    dur = req.duration_days or 30
    status = req.status.upper() if req.status else ("ACTIVE" if req.user_id else "UNUSED")
    lic = create_license_record(
        serial_key=serial,
        assigned_user_id=req.user_id,
        expiry_date=req.expiry_date,
        max_devices=req.max_devices,
        status=status,
        duration_days=dur
    )
    client_ip = request.client.host if request.client else "127.0.0.1"
    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action="License Generated",
        target_type="licenses",
        target_id=lic["id"],
        details=f"Generated {dur}-day license {serial} for user {req.user_id or 'Unassigned'}",
        ip_address=client_ip
    )
    return {
        "status": "created",
        "license": lic,
        "serial_key": lic["serial_key"],
        "duration_days": lic.get("duration_days", dur),
        "max_devices": lic.get("max_devices", req.max_devices),
        "user_email": req.user_id or "Unassigned"
    }

@router.post("/licenses/batch-generate")
async def api_admin_batch_licenses(req: BatchGenerateLicensesPayload, request: Request, admin: Dict[str, Any] = Depends(get_current_admin)):
    """Generate multiple unassigned licenses in batch."""
    created = []
    dur = req.duration_days or 30
    status = req.status.upper() if req.status else "UNUSED"
    for _ in range(min(req.count, 50)):
        serial = generate_serial_key(req.max_devices)
        lic = create_license_record(
            serial_key=serial,
            assigned_user_id=None,
            expiry_date=req.expiry_date,
            max_devices=req.max_devices,
            status=status,
            duration_days=dur
        )
        created.append(lic)
    client_ip = request.client.host if request.client else "127.0.0.1"
    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action="Batch Licenses Generated",
        target_type="licenses",
        target_id=None,
        details=f"Generated batch of {len(created)} ({dur}-day) licenses",
        ip_address=client_ip
    )
    return {"status": "created", "count": len(created), "licenses": created}

@router.post("/licenses/{license_id}/extend-days")
@router.post("/licenses/{license_id}/extend")
async def api_admin_extend_license_days(
    license_id: int,
    payload: ExtendLicenseDaysPayload,
    request: Request,
    admin: Dict[str, Any] = Depends(get_current_admin)
):
    """Extend an existing license duration by adding additional days."""
    client_ip = request.client.host if request.client else "127.0.0.1"
    try:
        updated = extend_license(
            license_id=license_id,
            admin_id=admin["id"],
            additional_days=payload.additional_days,
            client_ip=client_ip
        )
        return {"status": "ok", "license": updated}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to extend license: {str(e)}")

@router.put("/licenses/{license_id}")
@router.put("/licenses/{license_id}/status")
async def api_admin_update_license(license_id: int, req: UpdateLicensePayload, request: Request, admin: Dict[str, Any] = Depends(get_current_admin)):
    """Update license status (ACTIVE, SUSPENDED, REVOKED), expiration, or devices with instant session termination."""
    lic = get_license_by_id(license_id)
    if not lic:
        raise HTTPException(status_code=404, detail="License not found.")
    fields: Dict[str, Any] = {}
    if req.status: fields["status"] = req.status.upper()
    if req.expiry_date: fields["expiry_date"] = req.expiry_date
    if req.max_devices is not None: fields["max_devices"] = req.max_devices
    if req.assigned_user_id is not None: fields["assigned_user_id"] = req.assigned_user_id

    update_license_record(license_id, fields)

    revocation_note = ""
    if req.status and req.status.upper() in ["REVOKED", "SUSPENDED"]:
        closed = deactivate_sessions_for_license(license_id)
        if lic.get("assigned_user_id"):
            cancel_user_active_jobs(lic["assigned_user_id"])
        revocation_note = f" (Terminated {closed} active sessions & cancelled running jobs)"

    client_ip = request.client.host if request.client else "127.0.0.1"
    reason_note = f" Reason: {req.reason.strip()}." if req.reason else ""
    action_title = f"License {req.status.upper()}" if req.status else "License Updated"
    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action=action_title,
        target_type="licenses",
        target_id=license_id,
        details=f"Updated license {lic['serial_key']}.{reason_note}{revocation_note} (Fields: {list(fields.keys())})",
        ip_address=client_ip
    )
    return {"status": "updated", "license": get_license_by_id(license_id)}

@router.delete("/licenses/{license_id}")
async def api_admin_delete_license(
    license_id: int,
    request: Request,
    admin: Dict[str, Any] = Depends(get_current_admin)
):
    """
    Delete license immediately without deleting user account, downloads, or creator records.
    Verifies admin RBAC, invalidates active sessions, cleans up license devices, logs audit event.
    """
    if admin.get("role") not in ["SUPER_ADMIN", "ADMIN"]:
        raise HTTPException(status_code=403, detail="Permission denied: License deletion requires Administrator privileges.")

    lic = get_license_by_id(license_id)
    if not lic:
        raise HTTPException(status_code=404, detail="License not found.")

    # Cancel active tasks for the assigned user if present
    if lic.get("assigned_user_id"):
        try:
            cancel_user_active_jobs(lic["assigned_user_id"])
        except Exception:
            pass

    # Delete license and cleanly handle relational dependencies
    success = delete_license_record(license_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete license from database.")

    client_ip = request.client.host if request.client else "127.0.0.1"
    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action="LICENSE_DELETED",
        target_type="licenses",
        target_id=str(license_id),
        details=f"Permanently deleted license ID {license_id} (Key: {lic.get('serial_key')}, User: {lic.get('assigned_user_id') or 'Unassigned'}, Duration: {lic.get('duration_days', 'N/A')}d)",
        ip_address=client_ip
    )

    return {
        "status": "deleted",
        "license_id": license_id,
        "serial_key": lic.get("serial_key"),
        "message": f"License {lic.get('serial_key')} deleted successfully."
    }

# =========================================================================
# 7. Device Management Endpoints (Explicit Slot Deactivation)
# =========================================================================

@router.get("/devices")
async def api_admin_list_devices(
    search: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    admin: Dict[str, Any] = Depends(get_current_admin)
):
    """List all registered hardware device slots."""
    devices = get_admin_devices_extended(search=search, status=status, limit=limit, offset=offset)
    return {"status": "ok", "data": devices}

@router.post("/devices/{device_id}/deactivate")
async def api_admin_deactivate_device(device_id: int, request: Request, payload: Optional[DeactivateDevicePayload] = None, admin: Dict[str, Any] = Depends(get_current_admin)):
    """
    Deactivate an authorized device slot.
    CRITICAL: Frees the device slot on the associated license and terminates sessions for this hardware!
    """
    dev = get_device_by_id(device_id)
    deactivate_device(device_id, status="DEACTIVATED")
    closed = 0
    if dev and dev.get("device_fingerprint"):
        closed = deactivate_sessions_for_device(dev["device_fingerprint"])
    client_ip = request.client.host if request.client else "127.0.0.1"
    reason_text = f" Reason: {payload.reason.strip()}." if payload and payload.reason else ""
    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action="Device Deactivated",
        target_type="devices",
        target_id=device_id,
        details=f"Admin deactivated device ID {device_id} (slot released).{reason_text} Terminated {closed} active sessions.",
        ip_address=client_ip
    )
    return {"status": "deactivated", "device_id": device_id}

@router.post("/devices/{device_id}/reactivate")
async def api_admin_reactivate_device(device_id: int, request: Request, admin: Dict[str, Any] = Depends(get_current_admin)):
    """Reactivate a previously deactivated device slot."""
    deactivate_device(device_id, status="ACTIVE")
    client_ip = request.client.host if request.client else "127.0.0.1"
    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action="Device Reactivated",
        target_type="devices",
        target_id=device_id,
        details=f"Admin reactivated device ID {device_id}",
        ip_address=client_ip
    )
    return {"status": "reactivated", "device_id": device_id}

@router.post("/devices/{device_id}/revoke")
async def api_admin_revoke_device(device_id: int, request: Request, payload: Optional[DeactivateDevicePayload] = None, admin: Dict[str, Any] = Depends(get_current_admin)):
    """Permanently revoke a device slot and terminate its active sessions."""
    dev = get_device_by_id(device_id)
    deactivate_device(device_id, status="REVOKED")
    closed = 0
    if dev and dev.get("device_fingerprint"):
        closed = deactivate_sessions_for_device(dev["device_fingerprint"])
    client_ip = request.client.host if request.client else "127.0.0.1"
    reason_text = f" Reason: {payload.reason.strip()}." if payload and payload.reason else ""
    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action="Device Revoked",
        target_type="devices",
        target_id=device_id,
        details=f"Admin revoked device ID {device_id}.{reason_text} Terminated {closed} active sessions.",
        ip_address=client_ip
    )
    return {"status": "revoked", "device_id": device_id}

# =========================================================================
# 8. Creator Management Endpoints
# =========================================================================

@router.get("/creators")
async def api_admin_list_creators(
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    admin: Dict[str, Any] = Depends(get_current_admin)
):
    """List real creator records for admin monitoring."""
    creators = get_admin_creators_extended(search=search, limit=limit, offset=offset)
    return {"status": "ok", "data": creators}

# =========================================================================
# 9. Download Management Endpoints
# =========================================================================

@router.get("/downloads")
async def api_admin_list_downloads(
    search: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    admin: Dict[str, Any] = Depends(get_current_admin)
):
    """List real download records with video details and error messages."""
    downloads = get_admin_downloads_extended(search=search, status=status, limit=limit, offset=offset)
    return {"status": "ok", "data": downloads}

# =========================================================================
# 10. Analytics Endpoints
# =========================================================================

@router.get("/analytics/overview")
@router.get("/statistics/overview")
async def api_admin_analytics(
    days: Optional[int] = Query(30, ge=0, le=3650),
    admin: Dict[str, Any] = Depends(get_current_admin)
):
    """Retrieve aggregate analytics, KPIs, chart time-series, and activity logs."""
    filter_days = None if (days is None or days <= 0) else days
    data = get_admin_analytics_data(days=filter_days)
    return {"status": "ok", "data": data}

# =========================================================================
# 11. System Settings Endpoints
# =========================================================================

@router.get("/settings")
async def api_admin_get_settings(admin: Dict[str, Any] = Depends(get_current_admin)):
    """Get all categorized system settings."""
    settings = get_system_settings_dict()
    return {"status": "ok", "data": settings}

@router.post("/settings")
async def api_admin_update_settings(req: UpdateSettingsPayload, request: Request, admin: Dict[str, Any] = Depends(get_current_admin)):
    """Update system settings with audit log."""
    for key, val in req.settings.items():
        update_system_setting(key, str(val), category="general", updated_by=admin["email"])
    client_ip = request.client.host if request.client else "127.0.0.1"
    add_audit_log(
        actor_id=admin["id"],
        actor_email=admin["email"],
        action="Settings Changed",
        target_type="system_settings",
        target_id=None,
        details=f"Updated settings: {list(req.settings.keys())}",
        ip_address=client_ip
    )
    return {"status": "saved", "data": get_system_settings_dict()}

# =========================================================================
# 12. Audit Logs Endpoints
# =========================================================================

@router.get("/audit-logs")
async def api_admin_audit_logs(
    limit: int = 100,
    offset: int = 0,
    action: Optional[str] = None,
    admin: Dict[str, Any] = Depends(get_current_admin)
):
    """Retrieve immutable audit logs."""
    logs = get_audit_logs(limit=limit, offset=offset, action_filter=action)
    return {"status": "ok", "data": logs}

# =========================================================================
# 13. System Health Diagnostics
# =========================================================================

@router.get("/health")
@router.get("/system/health")
async def api_admin_system_health(admin: Dict[str, Any] = Depends(get_current_admin)):
    """Return real system health: backend, database quick check, worker status, disk usage."""
    health = get_system_health_status()
    return {"status": "ok", "data": health}


# =========================================================================
# 14. Dedicated Admin Feedback Management Endpoints (Sections 35-58)
# =========================================================================

class AdminFeedbackUpdateRequest(BaseModel):
    status: Optional[str] = None
    priority: Optional[str] = None
    assigned_admin_id: Optional[str] = None

class AdminFeedbackResponseRequest(BaseModel):
    response: str

class AdminFeedbackResolveRequest(BaseModel):
    response: Optional[str] = None

class AdminFeedbackAssignRequest(BaseModel):
    admin_id: Optional[str] = None

@router.get("/feedback")
async def api_admin_get_feedback(
    search: Optional[str] = None,
    type: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    assigned_admin_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    admin: Dict[str, Any] = Depends(get_current_admin)
):
    """
    Retrieve feedback list with full search and filtering capabilities.
    Returns real user and assigned administrator information.
    """
    feedbacks = get_all_feedback_admin(
        search=search,
        type_filter=type,
        status_filter=status,
        priority_filter=priority,
        assigned_admin_id=assigned_admin_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset
    )
    return {"status": "ok", "data": feedbacks, "total": len(feedbacks)}

@router.get("/feedback/metrics")
async def api_admin_feedback_metrics(admin: Dict[str, Any] = Depends(get_current_admin)):
    """Return live dynamic database counts for feedback statuses."""
    metrics = get_feedback_metrics()
    return {"status": "ok", "data": metrics}

@router.get("/feedback/{feedback_id}")
async def api_admin_get_feedback_detail(feedback_id: str, admin: Dict[str, Any] = Depends(get_current_admin)):
    """
    Retrieve comprehensive feedback details including untruncated message,
    user details, admin response, and full audit logs.
    """
    rec = get_feedback_by_id(feedback_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Feedback not found.")
    return {"status": "ok", "data": rec}

@router.patch("/feedback/{feedback_id}")
async def api_admin_patch_feedback(
    feedback_id: str,
    payload: AdminFeedbackUpdateRequest,
    admin: Dict[str, Any] = Depends(get_current_admin)
):
    """
    Update feedback status, priority, or assigned admin.
    Generates immutable audit logs for every changed field.
    """
    rec = get_feedback_by_id(feedback_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Feedback not found.")

    updates: Dict[str, Any] = {}
    admin_id = admin["id"]
    admin_email = admin.get("email")

    if payload.status:
        new_status = payload.status.upper()
        if new_status in ("OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED"):
            if new_status != rec.get("status"):
                updates["status"] = new_status
                if new_status == "RESOLVED":
                    from datetime import datetime
                    updates["resolved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    updates["resolved_by_admin_id"] = admin_id
                add_feedback_audit_log(
                    feedback_id=feedback_id,
                    actor_id=admin_id,
                    actor_email=admin_email,
                    admin_id=admin_id,
                    action="FEEDBACK_STATUS_CHANGED",
                    previous_value=rec.get("status"),
                    new_value=new_status
                )

    if payload.priority:
        new_priority = payload.priority.upper()
        if new_priority in ("LOW", "MEDIUM", "HIGH", "URGENT"):
            if new_priority != rec.get("priority"):
                updates["priority"] = new_priority
                add_feedback_audit_log(
                    feedback_id=feedback_id,
                    actor_id=admin_id,
                    actor_email=admin_email,
                    admin_id=admin_id,
                    action="FEEDBACK_PRIORITY_CHANGED",
                    previous_value=rec.get("priority"),
                    new_value=new_priority
                )

    if payload.assigned_admin_id is not None:
        new_assignee = payload.assigned_admin_id.strip() if payload.assigned_admin_id else None
        if new_assignee != rec.get("assigned_admin_id"):
            updates["assigned_admin_id"] = new_assignee
            add_feedback_audit_log(
                feedback_id=feedback_id,
                actor_id=admin_id,
                actor_email=admin_email,
                admin_id=admin_id,
                action="FEEDBACK_ASSIGNED",
                previous_value=rec.get("assigned_admin_id") or "Unassigned",
                new_value=new_assignee or "Unassigned"
            )

    updated = update_feedback_record(feedback_id, updates) if updates else rec
    return {"status": "ok", "message": "Feedback updated successfully.", "data": updated}

@router.post("/feedback/{feedback_id}/response")
async def api_admin_save_response(
    feedback_id: str,
    payload: AdminFeedbackResponseRequest,
    admin: Dict[str, Any] = Depends(get_current_admin)
):
    """Save or update the official administrative response to user feedback."""
    rec = get_feedback_by_id(feedback_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Feedback not found.")

    response_text = payload.response.strip()
    if not response_text:
        raise HTTPException(status_code=400, detail="Response text cannot be empty.")

    admin_id = admin["id"]
    admin_email = admin.get("email")

    updated = update_feedback_record(feedback_id, {
        "admin_response": response_text
    })

    add_feedback_audit_log(
        feedback_id=feedback_id,
        actor_id=admin_id,
        actor_email=admin_email,
        admin_id=admin_id,
        action="FEEDBACK_RESPONDED",
        previous_value=rec.get("admin_response") or "",
        new_value=response_text
    )

    return {"status": "ok", "message": "Admin response saved.", "data": updated}

@router.post("/feedback/{feedback_id}/resolve")
async def api_admin_resolve_feedback(
    feedback_id: str,
    payload: Optional[AdminFeedbackResolveRequest] = None,
    admin: Dict[str, Any] = Depends(get_current_admin)
):
    """
    Direct 'Solve' workflow for feedback.
    Sets status='RESOLVED', resolved_at=now, and resolved_by_admin_id=current_admin.
    Optionally saves or updates the admin response.
    """
    rec = get_feedback_by_id(feedback_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Feedback not found.")

    from datetime import datetime
    admin_id = admin["id"]
    admin_email = admin.get("email")

    updates: Dict[str, Any] = {
        "status": "RESOLVED",
        "resolved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "resolved_by_admin_id": admin_id
    }

    if payload and payload.response and payload.response.strip():
        updates["admin_response"] = payload.response.strip()

    updated = update_feedback_record(feedback_id, updates)

    add_feedback_audit_log(
        feedback_id=feedback_id,
        actor_id=admin_id,
        actor_email=admin_email,
        admin_id=admin_id,
        action="FEEDBACK_RESOLVED",
        previous_value=rec.get("status"),
        new_value="RESOLVED"
    )

    return {"status": "ok", "message": "Feedback resolved successfully.", "data": updated}

@router.post("/feedback/{feedback_id}/assign")
async def api_admin_assign_feedback(
    feedback_id: str,
    payload: AdminFeedbackAssignRequest,
    admin: Dict[str, Any] = Depends(get_current_admin)
):
    """Assign feedback to a designated administrator by ID."""
    rec = get_feedback_by_id(feedback_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Feedback not found.")

    admin_id = admin["id"]
    admin_email = admin.get("email")
    new_assignee = payload.admin_id.strip() if (payload and payload.admin_id) else None

    updated = update_feedback_record(feedback_id, {"assigned_admin_id": new_assignee})

    add_feedback_audit_log(
        feedback_id=feedback_id,
        actor_id=admin_id,
        actor_email=admin_email,
        admin_id=admin_id,
        action="FEEDBACK_ASSIGNED",
        previous_value=rec.get("assigned_admin_id") or "Unassigned",
        new_value=new_assignee or "Unassigned"
    )

    return {"status": "ok", "message": "Feedback assignment updated.", "data": updated}


# --- Creator Providers Health Monitoring ---
@router.get("/creator-providers")
async def api_admin_get_creator_providers(admin: Dict[str, Any] = Depends(get_current_admin)):
    """Expose creator provider health, latency metrics, and circuit states (no secrets)."""
    from src.app.services.creator_providers import creator_orchestrator
    return {
        "status": "ok",
        "data": creator_orchestrator.get_providers_health()
    }



