# api-gateway/app/routers/user_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Request
from typing import Annotated, Dict, Any, Optional
import structlog
import traceback

# Dependencias
from app.auth.auth_middleware import InitialAuth
from app.utils.supabase_admin import get_supabase_admin_client
from supabase import Client as SupabaseClient
from gotrue.errors import AuthApiError
from gotrue.types import UserResponse, User
from postgrest import APIResponse as PostgrestAPIResponse

from app.core.config import settings

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["Users"])

# Inyección global (menos ideal)
supabase_admin: Optional[SupabaseClient] = None

async def _create_public_user_profile(
    admin_client: SupabaseClient,
    user_id: str,
    email: Optional[str],
    name: Optional[str]
) -> Dict[str, Any]:
    """Helper para crear el perfil en public.users."""
    bound_log = log.bind(user_id=user_id)
    bound_log.info("Public user profile not found, creating...")
    user_profile_data = {
        "id": user_id,
        "email": email,
        "full_name": name, # Asume columna 'full_name' en public.users
        "role": "user",    # Rol por defecto
        # Asegúrate de incluir aquí CUALQUIER otro campo NOT NULL de tu tabla public.users
        # "company_id": None, # Se actualizará después si es necesario
        # "is_active": True, # Ejemplo
    }
    try:
        # *** CORRECCIÓN: QUITAR await de llamada síncrona ***
        insert_response: PostgrestAPIResponse = admin_client.table("users").insert(user_profile_data).execute()

        if insert_response.data and len(insert_response.data) > 0:
            bound_log.info("Successfully created public user profile.", profile_data=insert_response.data[0])
            return insert_response.data[0]
        else:
            bound_log.error("Failed to create or retrieve public user profile after insert.", response_status=insert_response.status_code, response_error=getattr(insert_response, 'error', None))
            raise HTTPException(status_code=500, detail="Failed to create user profile.")

    except Exception as e:
        bound_log.exception("Error creating public user profile.")
        detail = f"Database error creating profile: {e}"
        raise HTTPException(status_code=500, detail=detail) from e

@router.post(
    "/me/ensure-company",
    # ... (metadata sin cambios) ...
    status_code=status.HTTP_200_OK,
    summary="Ensure User Profile and Company Association",
    description="Checks if the public user profile exists and creates it if not. Then checks if the user has a company ID associated and associates the default one if missing. Requires GATEWAY_DEFAULT_COMPANY_ID.",
    responses={
        status.HTTP_200_OK: {"description": "Profile verified/created and company association successful or already existed."},
        status.HTTP_400_BAD_REQUEST: {"description": "Default Company ID not configured on server."},
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication token missing or invalid."},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Failed to check or update user data/profile."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Supabase Admin client not available."},
    }
)
async def ensure_company_association(
    user_payload: InitialAuth,
    admin_client: Annotated[SupabaseClient, Depends(get_supabase_admin_client)],
):
    user_id = user_payload.get("sub")
    email_from_token = user_payload.get("email")
    name_from_token: Optional[str] = None
    user_metadata_token = user_payload.get('user_metadata')
    if isinstance(user_metadata_token, dict):
        name_from_token = user_metadata_token.get('name') or user_metadata_token.get('full_name')

    bound_log = log.bind(user_id=user_id)
    bound_log.info("Ensure profile and company association endpoint called.")

    # 1. Verificar/Crear perfil en public.users
    public_profile: Optional[Dict] = None
    auth_user: Optional[User] = None # Para almacenar datos de auth si es necesario crear perfil

    try:
        bound_log.debug("Checking for existing public user profile...")
        # *** CORRECCIÓN: QUITAR await de llamada síncrona ***
        select_response: PostgrestAPIResponse = admin_client.table("users").select("*").eq("id", user_id).maybe_single().execute()

        if select_response.data:
            public_profile = select_response.data
            bound_log.info("Public user profile found.")
        else:
            # Perfil no existe, necesitamos crearlo. Obtener datos de auth.users
            bound_log.info("Public profile not found. Fetching auth user data to create profile.")
            try:
                 # *** CORRECTO: Usar await para llamada a auth.admin ***
                auth_user_response: UserResponse = await admin_client.auth.admin.get_user_by_id(user_id)
                auth_user = auth_user_response.user if auth_user_response else None

                if not auth_user:
                     bound_log.error("User exists in token but not found in auth.users via admin API.")
                     raise HTTPException(status_code=500, detail="User data inconsistency.")

                email_for_profile = auth_user.email or email_from_token
                name_for_profile = (auth_user.user_metadata.get('name') or auth_user.user_metadata.get('full_name')) if auth_user.user_metadata else name_from_token

                # Llamar al helper para crear el perfil (que usa llamada síncrona sin await)
                public_profile = await _create_public_user_profile(
                    admin_client, user_id, email_for_profile, name_for_profile
                )

            except AuthApiError as e:
                 bound_log.error("Supabase Admin API error fetching auth user data for profile creation", status_code=e.status, error_message=e.message)
                 raise HTTPException(status_code=500, detail=f"Failed to fetch auth data for profile: {e.message}")
            # No necesitamos el except HTTPException aquí si _create_public_user_profile lo maneja
            except Exception as e:
                 bound_log.exception("Unexpected error during auth user fetch or profile creation.")
                 raise HTTPException(status_code=500, detail="Failed during profile creation process.")

    except Exception as e:
        # Este es el bloque donde ocurría el TypeError por el await incorrecto
        bound_log.exception("Error checking/creating public user profile.")
        if isinstance(e, HTTPException): raise e
        # Añadir el mensaje de error específico al detalle para depuración
        raise HTTPException(status_code=500, detail=f"Error accessing user profile data: {e}")


    # 2. Verificar/Asociar Company ID
    # Si tuvimos que crear el perfil, ya tenemos auth_user. Si no, lo buscamos.
    if not auth_user:
        try:
            bound_log.debug("Re-fetching auth user data for company check.")
             # *** CORRECTO: Usar await para llamada a auth.admin ***
            auth_user_response = await admin_client.auth.admin.get_user_by_id(user_id)
            auth_user = auth_user_response.user if auth_user_response else None
            if not auth_user:
                bound_log.error("User data disappeared after profile check. Critical inconsistency.")
                raise HTTPException(status_code=500, detail="User data inconsistency.")
        except AuthApiError as e: # Capturar errores al re-buscar también
            bound_log.error("Supabase Admin API error re-fetching auth user data", status_code=e.status, error_message=e.message)
            raise HTTPException(status_code=500, detail=f"Failed to re-verify user auth data: {e.message}")
        except Exception as e:
            bound_log.exception("Failed to re-fetch auth user data before company check.")
            raise HTTPException(status_code=500, detail="Failed to get user data for company check.")

    # Extraer app_metadata
    app_metadata = getattr(auth_user, 'app_metadata', {}) if auth_user else {}
    company_id_from_auth = app_metadata.get("company_id") if isinstance(app_metadata, dict) else None
    company_id_from_auth = str(company_id_from_auth) if company_id_from_auth else None

    if company_id_from_auth:
        bound_log.info("User already has company ID in auth.users metadata.", company_id=company_id_from_auth)
        # Sincronizar con public.users si es necesario
        public_company_id = public_profile.get('company_id') if public_profile else None
        if str(public_company_id) != company_id_from_auth: # Comparar como strings
             bound_log.warning("Mismatch between auth metadata and public profile company ID. Updating profile.",
                               auth_cid=company_id_from_auth, profile_cid=public_company_id)
             try:
                 # *** CORRECCIÓN: QUITAR await de llamada síncrona ***
                 admin_client.table("users").update({"company_id": company_id_from_auth}).eq("id", user_id).execute()
             except Exception:
                 bound_log.exception("Failed to sync existing company ID to public profile.")
        return {"message": "Company association already exists.", "company_id": company_id_from_auth}

    # --- Asociar Compañía ---
    bound_log.info("User lacks company ID in auth metadata. Attempting association.")
    company_id_to_assign = settings.DEFAULT_COMPANY_ID
    if not company_id_to_assign:
        bound_log.critical("CONFIGURATION ERROR: GATEWAY_DEFAULT_COMPANY_ID is not set.")
        raise HTTPException(status_code=400, detail="Server configuration error: Default company ID is not set.")

    bound_log.info("Associating user with Default Company ID.", default_company_id=company_id_to_assign)
    new_app_metadata = {**app_metadata, "company_id": company_id_to_assign}

    try:
        bound_log.debug("Updating auth.users app_metadata.", metadata_to_send=new_app_metadata)
         # *** CORRECTO: Usar await para llamada a auth.admin ***
        update_auth_response: UserResponse = await admin_client.auth.admin.update_user_by_id(
            user_id, attributes={'app_metadata': new_app_metadata}
        )
        updated_user_auth = update_auth_response.user if update_auth_response else None
        if not (updated_user_auth and getattr(updated_user_auth, 'app_metadata', {}).get("company_id") == company_id_to_assign):
            bound_log.error("Failed to confirm company ID update in auth.users metadata response.", update_response=updated_user_auth)
            raise HTTPException(status_code=500, detail="Failed to confirm company ID update in auth system.")
        bound_log.info("Successfully updated auth.users app_metadata.")

        # Actualizar también public.users
        try:
            bound_log.debug("Updating public.users company_id column.")
             # *** CORRECCIÓN: QUITAR await de llamada síncrona ***
            admin_client.table("users").update({"company_id": company_id_to_assign}).eq("id", user_id).execute()
            bound_log.info("Successfully updated public.users company_id.")
        except Exception as pub_e:
             bound_log.exception("Failed to update company_id in public.users table, but auth metadata was updated.")

        return {"message": "Company association successful.", "company_id": company_id_to_assign}

    except AuthApiError as e:
        bound_log.error("Supabase Admin API error during user metadata update", status_code=e.status, error_message=e.message)
        raise HTTPException(status_code=500, detail=f"Failed to associate company in auth system: {e.message}")
    except HTTPException as e: raise e
    except Exception as e:
        bound_log.exception("Unexpected error during company association update.")
        raise HTTPException(status_code=500, detail="An unexpected error occurred while associating company.") from e