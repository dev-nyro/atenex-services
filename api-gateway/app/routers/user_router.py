# File: app/routers/user_router.py
# api-gateway/app/routers/user_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Request, Body
from typing import Dict, Any, Optional, Annotated
import structlog
import uuid

from app.auth.auth_middleware import InitialAuth
from app.auth.auth_service import authenticate_user, create_access_token
from app.db import postgres_client
from app.core.config import settings
from pydantic import BaseModel, EmailStr, Field, validator

log = structlog.get_logger(__name__)

# --- REMOVE prefix from router definition ---
router = APIRouter(tags=["Users & Authentication"]) # No prefix here

# --- Modelos Pydantic (Sin cambios) ---
class LoginRequest(BaseModel): email: EmailStr; password: str = Field(..., min_length=6)
class LoginResponse(BaseModel): access_token: str; token_type: str = "bearer"; user_id: str; email: EmailStr; full_name: Optional[str] = None; role: Optional[str] = "user"; company_id: Optional[str] = None
class EnsureCompanyRequest(BaseModel):
    company_id: Optional[str] = None
    @validator('company_id')
    def validate_company_id_format(cls, v):
        if v is not None:
            try: uuid.UUID(v)
            except ValueError: raise ValueError("Provided company_id is not a valid UUID")
        return v
class EnsureCompanyResponse(BaseModel): user_id: str; company_id: str; message: str; new_access_token: str; token_type: str = "bearer"

# --- Endpoints (Relative paths within this router) ---
# Paths should be relative to the /api/v1/users prefix added in main.py
@router.post("/login", response_model=LoginResponse) # Path corrected relative to main prefix
async def login_for_access_token(login_data: LoginRequest):
    log.info("Login attempt initiated", email=login_data.email)
    user = await authenticate_user(login_data.email, login_data.password)
    if not user:
        log.warning("Login failed", email=login_data.email)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password", headers={"WWW-Authenticate": "Bearer"})
    user_id = user.get("id"); company_id = user.get("company_id"); email = user.get("email"); full_name = user.get("full_name"); role = user.get("role", "user")
    if not user_id or not email: log.error("Auth user data missing fields", keys=user.keys()); raise HTTPException(500, "Internal login error")
    access_token = create_access_token(user_id=user_id, email=email, company_id=company_id)
    log.info("Login successful", user_id=str(user_id), company_id=str(company_id) if company_id else "None")
    return LoginResponse(access_token=access_token, user_id=str(user_id), email=email, full_name=full_name, role=role, company_id=str(company_id) if company_id else None)

@router.post("/users/me/ensure-company", response_model=EnsureCompanyResponse) # Path corrected relative to main prefix
async def ensure_company_association(request: Request, user_payload: InitialAuth, ensure_request: Optional[EnsureCompanyRequest] = Body(None)):
    user_id_str = user_payload.get("sub"); req_id = getattr(request.state, 'request_id', 'N/A'); log_ctx = log.bind(request_id=req_id, user_id=user_id_str)
    log_ctx.info("Ensure company association requested.")
    if not user_id_str: log_ctx.error("Ensure fail: User ID missing"); raise HTTPException(400, "User ID not found in token.")
    try: user_id = uuid.UUID(user_id_str)
    except ValueError: log_ctx.error("Ensure fail: Invalid User ID", sub=user_id_str); raise HTTPException(400, "Invalid user ID format.")
    current_user_data = await postgres_client.get_user_by_id(user_id)
    if not current_user_data: log_ctx.error("Ensure fail: User not in DB"); raise HTTPException(404, "User not found in database.")
    current_company_id = current_user_data.get("company_id"); target_company_id_str: Optional[str] = None; action_taken = "none"
    if ensure_request and ensure_request.company_id: target_company_id_str = ensure_request.company_id; log_ctx.info("Using company_id from body", target=target_company_id_str)
    elif not current_company_id and settings.DEFAULT_COMPANY_ID: target_company_id_str = settings.DEFAULT_COMPANY_ID; log_ctx.info("Using default company_id", default=target_company_id_str)
    elif current_company_id: target_company_id_str = str(current_company_id); log_ctx.info("Using current company_id", current=target_company_id_str)
    else: log_ctx.error("Ensure fail: No target company"); raise HTTPException(400,"Cannot associate company: No ID provided and no default configured.")
    try: target_company_id = uuid.UUID(target_company_id_str)
    except ValueError: log_ctx.error("Ensure fail: Invalid target UUID", target=target_company_id_str); raise HTTPException(400, "Invalid target company ID format.")
    if target_company_id != current_company_id:
        log_ctx.info("Updating user company in DB", new_id=str(target_company_id))
        updated = await postgres_client.update_user_company(user_id, target_company_id)
        if not updated: log_ctx.error("DB update failed"); raise HTTPException(500, "Failed to update user company.")
        action_taken = "updated"; log_ctx.info("DB update successful")
    else: action_taken = "confirmed"; log_ctx.info("Company association confirmed, no DB update")
    user_email = current_user_data.get("email")
    if not user_email: log_ctx.error("Cannot generate token, missing email"); raise HTTPException(500, "Internal error generating token.")
    new_access_token = create_access_token(user_id=user_id, email=user_email, company_id=target_company_id)
    log_ctx.info("New token generated", company_id=str(target_company_id))
    if action_taken == "updated": message = f"Company association updated to {target_company_id}."
    else: message = f"Company association confirmed as {target_company_id}."
    return EnsureCompanyResponse(user_id=str(user_id), company_id=str(target_company_id), message=message, new_access_token=new_access_token)