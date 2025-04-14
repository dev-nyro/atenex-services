# File: app/routers/user_router.py
# api-gateway/app/routers/user_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Request, Body
from typing import Dict, Any, Optional, Annotated # Asegúrate de importar Annotated
import structlog
import uuid

# Importar dependencias de autenticación y DB
from app.auth.auth_middleware import InitialAuth # Para ensure-company
from app.auth.auth_service import authenticate_user, create_access_token
from app.db import postgres_client
from app.core.config import settings

# Importar modelos Pydantic para request/response
from pydantic import BaseModel, EmailStr, Field, validator

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["Users & Authentication"]) # Agrupamos aquí

# --- Modelos Pydantic para la API ---

class LoginRequest(BaseModel):
    """Payload esperado para el login."""
    email: EmailStr
    password: str = Field(..., min_length=6) # Ajusta min_length si es necesario

class LoginResponse(BaseModel):
    """Respuesta devuelta en un login exitoso."""
    access_token: str
    token_type: str = "bearer"
    user_id: str # UUID como string
    email: EmailStr
    full_name: Optional[str] = None
    role: Optional[str] = "user" # Rol por defecto
    company_id: Optional[str] = None # UUID como string, puede ser None

class EnsureCompanyRequest(BaseModel):
    """Payload opcional para forzar una compañía específica en ensure-company."""
    company_id: Optional[str] = None # UUID como string

    @validator('company_id')
    def validate_company_id_format(cls, v):
        if v is not None:
            try:
                uuid.UUID(v)
            except ValueError:
                raise ValueError("Provided company_id is not a valid UUID")
        return v

class EnsureCompanyResponse(BaseModel):
    """Respuesta devuelta al asociar/confirmar compañía."""
    user_id: str # UUID como string
    company_id: str # UUID como string (la que quedó asociada)
    message: str
    # Devolvemos el nuevo token para que el frontend lo use inmediatamente
    new_access_token: str
    token_type: str = "bearer"


# --- Endpoints ---

@router.post("/login", response_model=LoginResponse)
async def login_for_access_token(login_data: LoginRequest):
    """
    Autentica un usuario con email y contraseña.
    Si es exitoso, devuelve un token JWT y datos básicos del usuario.
    """
    log.info("Login attempt initiated", email=login_data.email)
    user = await authenticate_user(login_data.email, login_data.password)

    if not user:
        log.warning("Login failed: Invalid credentials or inactive user", email=login_data.email)
        # Devolver error genérico para no dar pistas sobre si el email existe
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Extraer datos del usuario autenticado para el token y la respuesta
    user_id = user.get("id")
    company_id = user.get("company_id") # Puede ser None
    email = user.get("email")
    full_name = user.get("full_name")
    role = user.get("role", "user") # Default a 'user' si no está en DB

    if not user_id or not email:
         log.error("Critical error: Authenticated user data missing ID or email", user_dict_keys=user.keys())
         raise HTTPException(status_code=500, detail="Internal server error during login")

    # Crear el token JWT
    access_token = create_access_token(
        user_id=user_id,
        email=email,
        company_id=company_id # Pasar company_id (puede ser None)
    )
    log.info("Login successful, token generated", user_id=str(user_id), company_id=str(company_id) if company_id else "None")

    # Devolver la respuesta
    return LoginResponse(
        access_token=access_token,
        user_id=str(user_id),
        email=email,
        full_name=full_name,
        role=role,
        company_id=str(company_id) if company_id else None
    )


@router.post("/me/ensure-company", response_model=EnsureCompanyResponse)
async def ensure_company_association(
    request: Request, # Necesitamos la request para el log
    # *** CORRECCIÓN: Parámetro sin default va primero ***
    # La dependencia se define únicamente a través de la anotación InitialAuth
    user_payload: InitialAuth,
    # Cuerpo opcional para especificar company_id, default a vacío si no se envía
    ensure_request: Optional[EnsureCompanyRequest] = Body(None)
):
    """
    Endpoint para que un usuario autenticado (con token válido)
    se asocie a una compañía si aún no lo está.
    1. Usa la company_id del body si se proporciona.
    2. Si no, usa la company_id por defecto de la configuración.
    3. Si ya tiene una company_id y no se especifica una nueva, no hace nada.
    4. Si se asocia/cambia, actualiza la DB y genera un NUEVO token con la company_id.
    """
    user_id_str = user_payload.get("sub")
    req_id = getattr(request.state, 'request_id', 'N/A') # Obtener request_id para logs
    log_ctx = log.bind(request_id=req_id, user_id=user_id_str)

    log_ctx.info("Ensure company association requested.")

    if not user_id_str:
        log_ctx.error("Ensure company failed: User ID ('sub') missing in token payload.")
        raise HTTPException(status_code=400, detail="User ID not found in token payload.")

    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        log_ctx.error("Ensure company failed: User ID ('sub') in token is not a valid UUID.", sub_value=user_id_str)
        raise HTTPException(status_code=400, detail="Invalid user ID format in token.")

    # Obtener datos actuales del usuario desde la DB (incluye email, full_name para el nuevo token)
    current_user_data = await postgres_client.get_user_by_id(user_id)
    if not current_user_data:
        # Esto no debería pasar si verify_token funciona, pero es una salvaguarda
        log_ctx.error("Ensure company failed: User found in token but not in database.", user_id=user_id_str)
        raise HTTPException(status_code=404, detail="User associated with token not found in database.")

    current_company_id = current_user_data.get("company_id")
    target_company_id_str: Optional[str] = None
    action_taken = "none"

    # Determinar el company_id objetivo
    if ensure_request and ensure_request.company_id:
        target_company_id_str = ensure_request.company_id
        log_ctx.info("Using company_id provided in request body.", target_company=target_company_id_str)
    elif not current_company_id and settings.DEFAULT_COMPANY_ID:
        target_company_id_str = settings.DEFAULT_COMPANY_ID
        log_ctx.info("User has no company_id, using default from settings.", default_company=target_company_id_str)
    elif current_company_id:
        # Ya tiene compañía y no se pidió cambiarla explícitamente
        target_company_id_str = str(current_company_id) # Usar la actual
        log_ctx.info("User already associated with a company, no change requested.", current_company=target_company_id_str)
    else:
        # No tiene compañía, no se proporcionó una, y no hay default configurado
        log_ctx.error("Ensure company failed: No target company_id provided or configured.", user_id=user_id_str)
        raise HTTPException(
            status_code=400,
            detail="Cannot associate company: No company ID provided and no default is configured for the gateway."
        )

    # Validar el formato del target_company_id_str
    try:
        target_company_id = uuid.UUID(target_company_id_str)
    except ValueError:
        log_ctx.error("Ensure company failed: Target company ID is not a valid UUID.", target_value=target_company_id_str)
        raise HTTPException(status_code=400, detail="Invalid target company ID format.")

    # Actualizar la DB solo si el target_company_id es diferente del actual (o si el actual es None)
    if target_company_id != current_company_id:
        log_ctx.info("Attempting to update user's company in database.", new_company_id=str(target_company_id))
        updated = await postgres_client.update_user_company(user_id, target_company_id)
        if not updated:
            # Podría fallar si el usuario fue eliminado entre get y update, o error DB
            log_ctx.error("Failed to update user's company association in database.", user_id=user_id_str)
            raise HTTPException(status_code=500, detail="Failed to update user's company association.")
        action_taken = "updated"
        log_ctx.info("User company association updated successfully in database.", new_company_id=str(target_company_id))
    else:
        action_taken = "confirmed"
        log_ctx.info("User company association already matches target, no database update needed.", company_id=str(target_company_id))

    # Generar un *nuevo* token JWT con la company_id (ya sea la actualizada o la confirmada)
    # Usar los datos recuperados de la DB para otros claims
    user_email = current_user_data.get("email")
    user_full_name = current_user_data.get("full_name")
    if not user_email:
         log_ctx.error("Critical error: User data from DB missing email, cannot generate new token.", user_id=user_id_str)
         raise HTTPException(status_code=500, detail="Internal server error generating updated token.")

    new_access_token = create_access_token(
        user_id=user_id,
        email=user_email,
        company_id=target_company_id # ¡Asegurarse de usar el target_company_id!
        # Podrías añadir full_name, role aquí si los incluyes en create_access_token
    )
    log_ctx.info("New access token generated with company association.", company_id=str(target_company_id))

    # Determinar mensaje de respuesta
    if action_taken == "updated":
        message = f"Company association successfully updated to {target_company_id}."
    elif action_taken == "confirmed":
        message = f"Company association confirmed as {target_company_id}."
    else: # action_taken == "none" (no debería llegar aquí si la lógica es correcta)
        message = f"User already associated with company {target_company_id}."


    return EnsureCompanyResponse(
        user_id=str(user_id),
        company_id=str(target_company_id),
        message=message,
        new_access_token=new_access_token
    )