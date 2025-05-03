# File: app/routers/user_router.py
# api-gateway/app/routers/user_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Request, Body
from typing import Dict, Any, Optional, Annotated, List # Añadido List
import structlog
import uuid

from app.auth.auth_middleware import InitialAuth
from app.auth.auth_service import authenticate_user, create_access_token
from app.db import postgres_client
from app.core.config import settings
from pydantic import BaseModel, EmailStr, Field, validator

log = structlog.get_logger(__name__)

# El prefijo /api/v1/users se define en main.py al incluir el router
router = APIRouter()

# --- Modelos Pydantic ---
class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=6)

# --- MODIFICADO: Añadir roles a LoginResponse ---
class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: EmailStr
    full_name: Optional[str] = None
    roles: Optional[List[str]] = None # Añadido campo roles
    company_id: Optional[str] = None

class EnsureCompanyRequest(BaseModel):
    company_id: Optional[str] = None
    @validator('company_id')
    def validate_company_id_format(cls, v):
        if v is not None:
            try: uuid.UUID(v)
            except ValueError: raise ValueError("Provided company_id is not a valid UUID")
        return v

class EnsureCompanyResponse(BaseModel):
    user_id: str
    company_id: str
    message: str
    new_access_token: str
    token_type: str = "bearer"
    # Añadir también roles aquí si el frontend lo necesita tras asegurar compañía
    roles: Optional[List[str]] = None

# --- Endpoints ---
# El path "/login" se resuelve como "/api/v1/users/login" debido al prefijo en main.py
@router.post("/login", response_model=LoginResponse, tags=["Users & Authentication"])
async def login_for_access_token(login_data: LoginRequest):
    log.info("Login attempt initiated", email=login_data.email)
    user = await authenticate_user(login_data.email, login_data.password)
    if not user:
        log.warning("Login failed", email=login_data.email)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password", headers={"WWW-Authenticate": "Bearer"})

    user_id = user.get("id")
    company_id = user.get("company_id")
    email = user.get("email")
    full_name = user.get("full_name")
    roles = user.get("roles") # Obtener roles de la DB

    if not user_id or not email:
        log.error("Authenticated user data missing critical fields (id or email)", keys=list(user.keys()) if user else None)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal login error: Missing user data.")

    # --- MODIFICADO: Pasar roles a create_access_token ---
    access_token = create_access_token(
        user_id=user_id,
        email=email,
        company_id=company_id,
        roles=roles # Pasar la lista de roles
    )

    log.info("Login successful", user_id=str(user_id), company_id=str(company_id) if company_id else "None", roles=roles)

    # --- MODIFICADO: Incluir roles en la respuesta ---
    return LoginResponse(
        access_token=access_token,
        user_id=str(user_id),
        email=email,
        full_name=full_name,
        roles=roles, # Devolver roles al frontend
        company_id=str(company_id) if company_id else None
    )

# El path "/me/ensure-company" se resuelve como "/api/v1/users/me/ensure-company"
@router.post("/me/ensure-company", response_model=EnsureCompanyResponse, tags=["Users & Authentication"])
async def ensure_company_association(
    request: Request,
    user_payload: InitialAuth, # Requiere token válido (firma, exp, user activo), no company_id
    ensure_request: Optional[EnsureCompanyRequest] = Body(None)
):
    user_id_str = user_payload.get("sub")
    req_id = getattr(request.state, 'request_id', 'N/A')
    log_ctx = log.bind(request_id=req_id, user_id=user_id_str)
    log_ctx.info("Ensure company association requested.")

    if not user_id_str:
        log_ctx.error("Ensure company failed: User ID ('sub') missing from token payload.");
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User ID not found in token.")
    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        log_ctx.error("Ensure company failed: Invalid User ID format in token.", sub_value=user_id_str)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user ID format.")

    # Obtener datos actuales del usuario (incluyendo roles actuales)
    current_user_data = await postgres_client.get_user_by_id(user_id)
    if not current_user_data:
        log_ctx.error("Ensure company failed: User from token not found in database.");
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.") # 404 es más apropiado

    current_company_id = current_user_data.get("company_id")
    current_roles = current_user_data.get("roles") # Obtener roles actuales
    user_email = current_user_data.get("email") # Obtener email para el nuevo token

    if not user_email:
        log_ctx.error("Ensure company failed: Email missing for user, cannot generate new token.", user_id=user_id_str)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal error: Missing user email.")

    target_company_id_str: Optional[str] = None
    action_taken = "none" # 'updated', 'confirmed', 'failed'

    # Determinar target company ID
    if ensure_request and ensure_request.company_id:
        target_company_id_str = ensure_request.company_id
        log_ctx.info("Attempting to use company_id from request body.", target_id=target_company_id_str)
    elif current_company_id:
        target_company_id_str = str(current_company_id)
        log_ctx.info("Using user's current company_id.", current_id=target_company_id_str)
    elif settings.DEFAULT_COMPANY_ID:
        target_company_id_str = settings.DEFAULT_COMPANY_ID
        log_ctx.info("Using default company_id from settings.", default_id=target_company_id_str)
    else:
        log_ctx.error("Ensure company failed: Cannot determine target company ID. None provided, user has no current ID, and no default is set.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot associate company: No specific ID provided and no default available for this user."
        )

    # Validar formato del target company ID
    try:
        target_company_id = uuid.UUID(target_company_id_str)
    except (ValueError, TypeError):
        log_ctx.error("Ensure company failed: Invalid target company ID format.", target_value=target_company_id_str)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid target company ID format.")

    # Verificar si la compañía objetivo existe (opcional pero recomendado)
    # try:
    #     target_company = await postgres_client.get_company_by_id(target_company_id)
    #     if not target_company:
    #          log_ctx.warning("Ensure company failed: Target company ID does not exist in DB.", target_id=str(target_company_id))
    #          raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Target company {target_company_id} not found.")
    # except Exception as e:
    #      log_ctx.exception("Ensure company failed: Error verifying target company existence.", error=str(e))
    #      raise HTTPException(status_code=500, detail="Error verifying target company.")


    # Actualizar DB si es necesario
    if target_company_id != current_company_id:
        log_ctx.info("Target company differs from current. Attempting database update.",
                     current_id=str(current_company_id), new_id=str(target_company_id))
        try:
            updated = await postgres_client.update_user_company(user_id, target_company_id)
            if not updated:
                # Esto podría ocurrir si el user_id ya no existe, aunque get_user_by_id lo verificó antes.
                log_ctx.error("Ensure company failed: Database update command affected 0 rows.", target_company_id=str(target_company_id))
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update user company association.")
            action_taken = "updated"
            log_ctx.info("User company association updated in database successfully.")
        except Exception as e:
            log_ctx.exception("Ensure company failed: Error during database update.", error=str(e))
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database error during company update.")
    else:
        action_taken = "confirmed"
        log_ctx.info("Target company matches current. No database update needed.", company_id=str(target_company_id))

    # Generar nuevo token con la compañía final y los roles actuales
    new_access_token = create_access_token(
        user_id=user_id,
        email=user_email,
        company_id=target_company_id, # Usar la compañía final
        roles=current_roles # Incluir los roles actuales en el nuevo token
    )
    log_ctx.info("New access token generated successfully.", final_company_id=str(target_company_id), roles=current_roles)

    if action_taken == "updated":
        message = f"Company association successfully updated to {target_company_id}."
    else: # action_taken == "confirmed"
        message = f"Company association confirmed as {target_company_id}."

    return EnsureCompanyResponse(
        user_id=str(user_id),
        company_id=str(target_company_id),
        message=message,
        new_access_token=new_access_token,
        roles=current_roles # Devolver roles en la respuesta
    )