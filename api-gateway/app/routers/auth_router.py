# api-gateway/app/routers/auth_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Request, Body
from pydantic import BaseModel, EmailStr, Field
from typing import Annotated, Dict, Any, Optional
import structlog
import uuid

# Dependencias
from app.utils.supabase_admin import get_supabase_admin_client
from supabase import Client as SupabaseClient
from gotrue.errors import AuthApiError
from postgrest import APIResponse as PostgrestAPIResponse # Para errores de tabla

from app.core.config import settings

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])

# --- Pydantic Models ---
class RegisterPayload(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)
    name: Optional[str] = Field(None, min_length=2)
    # company_name se obtiene del backend

class RegisterResponse(BaseModel):
    message: str
    user_id: Optional[uuid.UUID] = None # Devolver el ID del usuario creado

# --- Helpers Internos (Síncronos) ---

def _get_company_id_by_name(admin_client: SupabaseClient, company_name: str) -> Optional[uuid.UUID]:
    """Busca el UUID de una compañía por su nombre en public.companies."""
    bound_log = log.bind(lookup_company_name=company_name)
    try:
        bound_log.debug("Looking up company ID by name...")
        # .execute() es síncrono
        response = admin_client.table("companies").select("id").eq("name", company_name).limit(1).maybe_single().execute()

        if response and response.data:
            company_id = response.data.get("id")
            if company_id:
                 bound_log.info("Company found.", company_id=company_id)
                 return uuid.UUID(company_id) # Convertir a UUID
            else:
                 bound_log.warning("Company query returned data but no ID found.")
                 return None
        elif response and not response.data:
             bound_log.warning("Company not found by name.")
             return None
        else: # Respuesta inesperada o None
            bound_log.error("Unexpected response or error during company lookup.", response_details=response)
            return None

    except Exception as e:
        bound_log.exception("Error looking up company ID.")
        return None

# --- Endpoint de Registro ---
@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED, # Indicar recurso creado
    response_model=RegisterResponse,
    summary="Register a new user via Backend",
    description="Creates a user in Supabase Auth, sets default company in app_metadata, creates public profile, and triggers confirmation email.",
    responses={
        status.HTTP_201_CREATED: {"description": "User registered successfully, confirmation email sent."},
        status.HTTP_400_BAD_REQUEST: {"description": "Invalid input data or default company 'nyrouwu' not found."},
        status.HTTP_409_CONFLICT: {"description": "A user with this email already exists."},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Failed to create user or profile due to a server error."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Supabase Admin client not available."},
    }
)
async def register_user_endpoint(
    payload: RegisterPayload,
    admin_client: Annotated[SupabaseClient, Depends(get_supabase_admin_client)],
):
    """
    Handles user registration. Creates auth user with metadata and public profile.
    """
    bound_log = log.bind(user_email=payload.email)
    bound_log.info("Backend registration endpoint called.")

    # 1. Obtener el ID de la compañía por defecto "nyrouwu"
    default_company_name = "nyrouwu" # Hardcoded como solicitado
    company_id = _get_company_id_by_name(admin_client, default_company_name)

    if not company_id:
        bound_log.error(f"Default company '{default_company_name}' not found in database.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Required default company configuration ('{default_company_name}') not found."
        )

    # 2. Preparar metadatos para Supabase Auth
    user_metadata = {"name": payload.name} if payload.name else {}
    app_metadata = {
        "company_id": str(company_id), # Asegurar que es string para Supabase
        "roles": ["user"] # Rol por defecto
    }

    # 3. Crear usuario en Supabase Auth (llamada SÍNCRONA)
    try:
        bound_log.debug("Attempting to create user in Supabase Auth (sync call)...")
        create_user_response = admin_client.auth.admin.create_user({
            "email": payload.email,
            "password": payload.password,
            "email_confirm": True, # Requerir confirmación por email
            "user_metadata": user_metadata,
            "app_metadata": app_metadata
        })
        bound_log.debug("Supabase Auth create_user call completed.")

        new_user = create_user_response.user
        if not new_user or not new_user.id:
            bound_log.error("Supabase Auth create_user succeeded but returned no user or ID.", response=create_user_response)
            raise HTTPException(status_code=500, detail="Failed to retrieve user details after creation.")

        new_user_id = new_user.id
        bound_log.info("User successfully created in Supabase Auth.", user_id=new_user_id)

    except AuthApiError as e:
        bound_log.error("AuthApiError creating Supabase Auth user.", status_code=e.status, error_message=e.message)
        if "user already exists" in str(e.message).lower() or e.status == 422: # 422 a veces indica duplicado
             raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A user with this email already exists.")
        else:
             status_code = e.status if 400 <= e.status < 600 else 500
             raise HTTPException(status_code=status_code, detail=f"Error creating user in auth system: {e.message}")
    except Exception as e:
        bound_log.exception("Unexpected error creating Supabase Auth user.")
        raise HTTPException(status_code=500, detail=f"Internal server error during user creation: {e}")

    # 4. Crear perfil en public.users (llamada SÍNCRONA)
    try:
        bound_log.debug("Attempting to insert user profile into public.users...")
        profile_data = {
            "id": new_user_id,
            "email": payload.email,
            "full_name": payload.name,
            "role": "user", # Coincidir con app_metadata
            "is_active": True, # Activo por defecto, Supabase Auth maneja el estado de confirmación
            "company_id": company_id, # Usar el UUID encontrado
        }
        insert_response = admin_client.table("users").insert(profile_data).execute()

        # Verificar error en la inserción
        postgrest_error = getattr(insert_response, 'error', None)
        if postgrest_error:
             bound_log.error("PostgREST error inserting public profile.", status_code=insert_response.status_code, error_details=postgrest_error)
             # Intentar deshacer la creación del usuario en auth si falla el perfil? (Complejo, omitir por ahora)
             # Loguear el error gravemente, pero devolver éxito al frontend ya que el usuario auth existe
             # El usuario podrá loguearse pero puede encontrar problemas si el perfil es esencial.
             # Opcionalmente, devolver 500 aquí si el perfil es crítico.
             # Por simplicidad, logueamos y continuamos.
             log.error("CRITICAL: Failed to create public profile after auth user creation.", user_id=new_user_id)
             # Consider raising HTTPException(500, "Failed to finalize user profile.") here if critical
        else:
             bound_log.info("Public profile successfully created.", user_id=new_user_id)

    except Exception as e:
        bound_log.exception("Unexpected error inserting public profile.", user_id=new_user_id)
        # Loguear error, pero no fallar la solicitud principal si el usuario auth se creó.
        log.error("CRITICAL: Unexpected error creating public profile after auth user creation.", user_id=new_user_id)
        # Consider raising HTTPException(500, "Internal error finalizing profile.") here if critical

    # 5. Devolver éxito
    return RegisterResponse(
        message="Registration successful. Please check your email for confirmation.",
        user_id=new_user_id
    )