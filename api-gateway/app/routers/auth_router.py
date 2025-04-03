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
from postgrest import APIResponse as PostgrestAPIResponse

from app.core.config import settings

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])

# --- Pydantic Models (sin cambios) ---
class RegisterPayload(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)
    name: Optional[str] = Field(None, min_length=2)

class RegisterResponse(BaseModel):
    message: str
    user_id: Optional[uuid.UUID] = None

# --- Helpers (Solo búsqueda de compañía) ---

def _get_company_id_by_name(admin_client: SupabaseClient, company_name: str) -> Optional[uuid.UUID]:
    """Busca el UUID de una compañía por su nombre en public.companies."""
    bound_log = log.bind(lookup_company_name=company_name)
    try:
        bound_log.debug("Looking up company ID by name...")
        response = admin_client.table("companies").select("id").eq("name", company_name).limit(1).maybe_single().execute()

        if response and response.data:
            company_id = response.data.get("id")
            if company_id:
                 bound_log.info("Company found.", company_id=company_id)
                 return uuid.UUID(company_id)
            else:
                 bound_log.warning("Company query returned data but no ID found.")
                 return None
        elif response and not response.data:
             bound_log.warning("Company not found by name.")
             return None
        else:
            # Añadir chequeo de error Postgrest explícito
            postgrest_error = getattr(response, 'error', None)
            if postgrest_error:
                 bound_log.error("PostgREST error during company lookup.", response_error=postgrest_error)
            else:
                 bound_log.error("Unexpected response (None or no data/error) during company lookup.", response_details=response)
            return None

    except Exception as e:
        bound_log.exception("Error looking up company ID.")
        return None

# --- Endpoint de Registro (Modificado para llamar a RPC) ---
@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=RegisterResponse,
    summary="Register a new user via Backend (RPC Sync)",
    description="Creates user in Supabase Auth with metadata, then calls SQL function to sync public profile.",
    # ... (responses sin cambios) ...
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
    bound_log = log.bind(user_email=payload.email)
    bound_log.info("Backend registration endpoint called (RPC Flow).")

    # 1. Obtener el ID de la compañía por defecto "nyrouwu"
    default_company_name = "nyrouwu"
    company_id = _get_company_id_by_name(admin_client, default_company_name)

    if not company_id:
        bound_log.error(f"Default company '{default_company_name}' not found in database.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Required default company configuration ('{default_company_name}') not found."
        )

    # 2. Preparar metadatos
    user_metadata = {"name": payload.name} if payload.name else {}
    app_metadata = {
        "company_id": str(company_id),
        "roles": ["user"]
    }

    # 3. Crear usuario en Supabase Auth (llamada SÍNCRONA)
    new_user_id: Optional[uuid.UUID] = None
    try:
        bound_log.debug("Attempting to create user in Supabase Auth (sync call)...")
        create_user_response = admin_client.auth.admin.create_user({
            "email": payload.email,
            "password": payload.password,
            "email_confirm": True,
            "user_metadata": user_metadata,
            "app_metadata": app_metadata
        })
        bound_log.debug("Supabase Auth create_user call completed.")

        new_user = create_user_response.user
        if not new_user or not new_user.id:
            bound_log.error("Supabase Auth create_user succeeded but returned no user or ID.", response=create_user_response)
            raise HTTPException(status_code=500, detail="Failed to retrieve user details after creation.")

        new_user_id = new_user.id # Guardar el ID
        bound_log.info("User successfully created in Supabase Auth.", user_id=new_user_id)

    except AuthApiError as e:
        bound_log.error("AuthApiError creating Supabase Auth user.", status_code=e.status, error_message=e.message)
        if "user already exists" in str(e.message).lower() or e.status == 422:
             raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A user with this email already exists.")
        else:
             status_code = e.status if 400 <= e.status < 600 else 500
             raise HTTPException(status_code=status_code, detail=f"Error creating user in auth system: {e.message}")
    except Exception as e:
        bound_log.exception("Unexpected error creating Supabase Auth user.")
        raise HTTPException(status_code=500, detail=f"Internal server error during user creation: {e}")

    # 4. Llamar a la función SQL para crear/actualizar el perfil público (llamada SÍNCRONA)
    if new_user_id:
        try:
            bound_log.debug("Calling RPC public.create_public_profile_for_user...", user_id=new_user_id)
            # .execute() es síncrono para RPC también en supabase-py v1/v2
            rpc_response = admin_client.rpc(
                "create_public_profile_for_user", # Nombre de la función SQL
                {"user_id": str(new_user_id)}      # Parámetros como diccionario
            ).execute()

            # Verificar si la RPC devolvió un error PostgREST
            rpc_error = getattr(rpc_response, 'error', None)
            if rpc_error:
                 bound_log.error("PostgREST error calling create_public_profile_for_user RPC.",
                                 status_code=rpc_response.status_code, error_details=rpc_error, user_id=new_user_id)
                 # Loguear como crítico, pero no fallar la solicitud de registro
                 log.error("CRITICAL: Failed to sync public profile via RPC after auth user creation.", user_id=new_user_id)
            else:
                 bound_log.info("Successfully called RPC to sync public profile.", user_id=new_user_id)

        except Exception as e:
            bound_log.exception("Unexpected error calling create_public_profile_for_user RPC.", user_id=new_user_id)
            log.error("CRITICAL: Unexpected error calling RPC to sync public profile.", user_id=new_user_id)
            # No fallar la solicitud principal

    # 5. Devolver éxito (incluso si la RPC falló, el usuario auth existe)
    return RegisterResponse(
        message="Registration successful. Please check your email for confirmation.",
        user_id=new_user_id
    )