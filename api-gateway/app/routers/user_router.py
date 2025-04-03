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
# *** CORRECCIÓN: Eliminada la importación problemática ***
# from postgrest.utils import SyncMaybeSingleResponse

from app.core.config import settings

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["Users"])

# --- Helpers Síncronos para Auth Admin (Sin await) ---

def _get_auth_user_data_sync(admin_client: SupabaseClient, user_id: str) -> Optional[User]:
    """Helper SÍNCRONO para obtener datos de auth.users."""
    bound_log = log.bind(user_id=user_id)
    try:
        bound_log.debug("Attempting SYNC call to admin_client.auth.admin.get_user_by_id")
        auth_user_response: UserResponse = admin_client.auth.admin.get_user_by_id(user_id)
        bound_log.debug("SYNC call to get_user_by_id completed.")
        return auth_user_response.user if auth_user_response else None
    except AuthApiError as e:
        bound_log.error("AuthApiError fetching auth user data (sync)", status_code=e.status, error_message=e.message)
        if e.status == 404: return None
        status_code = e.status if 400 <= e.status < 600 else 500
        raise HTTPException(status_code=status_code, detail=f"Auth API error fetching user: {e.message}")
    except Exception as e:
        bound_log.exception("Unexpected error fetching auth user data (sync)", error_type=type(e).__name__)
        # Importante: No relanzar el TypeError aquí si ocurre, dejar que el flujo principal lo maneje si es necesario
        raise HTTPException(status_code=500, detail=f"Internal error fetching user data: {e}")

def _update_auth_user_metadata_sync(admin_client: SupabaseClient, user_id: str, new_metadata: Dict) -> Optional[User]:
    """Helper SÍNCRONO para actualizar app_metadata en auth.users."""
    bound_log = log.bind(user_id=user_id)
    try:
        bound_log.debug("Attempting SYNC call to admin_client.auth.admin.update_user_by_id", metadata_to_set=new_metadata)
        update_response: UserResponse = admin_client.auth.admin.update_user_by_id(
            user_id, attributes={'app_metadata': new_metadata}
        )
        bound_log.debug("SYNC call to update_user_by_id completed.")
        return update_response.user if update_response else None
    except AuthApiError as e:
        bound_log.error("AuthApiError updating auth user metadata (sync)", status_code=e.status, error_message=e.message)
        status_code = e.status if 400 <= e.status < 600 else 500
        raise HTTPException(status_code=status_code, detail=f"Auth API error updating metadata: {e.message}")
    except Exception as e:
        bound_log.exception("Unexpected error updating auth user metadata (sync)", error_type=type(e).__name__)
        raise HTTPException(status_code=500, detail=f"Internal error updating user metadata: {e}")

# --- Helper Asíncrono para Perfil Público (Usa llamadas DB Síncronas) ---

async def _ensure_public_profile_sync(
    admin_client: SupabaseClient,
    user_id: str,
    company_id: str, # Company ID ya validado/asignado
    email: Optional[str],
    name: Optional[str]
) -> Dict[str, Any]:
    """
    Asegura que el perfil público exista y tenga el company_id correcto.
    Usa llamadas síncronas a la DB. Lanza HTTPException en error.
    """
    bound_log = log.bind(user_id=user_id, target_company_id=company_id)
    bound_log.debug("Ensuring public user profile exists and matches company ID.")

    try:
        # 1. Intentar obtener el perfil existente usando maybe_single()
        select_builder = admin_client.table("users").select("id, email, full_name, role, company_id").eq("id", user_id)
        # *** CORRECCIÓN: Eliminado el type hint SyncMaybeSingleResponse ***
        profile_response = select_builder.maybe_single().execute() # Devuelve dict o None

        existing_profile = profile_response.data

        # 2. Si el perfil existe, verificar y actualizar company_id si es necesario
        if existing_profile:
            bound_log.info("Public profile found.", current_company_id=existing_profile.get('company_id'))
            if str(existing_profile.get('company_id')) != str(company_id):
                bound_log.warning("Public profile company ID mismatch. Updating.",
                                  profile_cid=existing_profile.get('company_id'), expected_cid=company_id)
                update_builder = admin_client.table("users").update({"company_id": company_id}).eq("id", user_id).select().single()
                update_response = update_builder.execute() # SIN await
                if update_response.data:
                    bound_log.info("Successfully updated company ID in public profile.")
                    return update_response.data
                else:
                    bound_log.error("Failed to update or retrieve public profile after company ID update.", update_error=getattr(update_response,'error', None))
                    raise HTTPException(status_code=500, detail="Failed to update company ID in public profile.")
            else:
                bound_log.debug("Public profile company ID already matches.")
                return existing_profile

        # 3. Si el perfil NO existe, crearlo
        else:
            bound_log.info("Public profile not found. Creating...")
            profile_data = {
                "id": user_id, "email": email, "full_name": name or "User",
                "role": "user", "is_active": True, "company_id": company_id,
            }
            insert_builder = admin_client.table("users").insert(profile_data, returning="representation").select().single()
            insert_response = insert_builder.execute() # SIN await

            if insert_response.data:
                bound_log.info("Successfully created public user profile.", created_profile=insert_response.data)
                return insert_response.data
            else:
                bound_log.error("Failed to insert or retrieve public profile after creation.", insert_error=getattr(insert_response,'error', None))
                raise HTTPException(status_code=500, detail="Failed to create public user profile.")

    except HTTPException as e:
        raise e
    except Exception as e:
        bound_log.exception("Unexpected error ensuring public profile sync.")
        raise HTTPException(status_code=500, detail=f"Database error ensuring public profile: {e}")

# --- Endpoint Principal ---

@router.post(
    "/me/ensure-company",
    # Metadata sin cambios...
    status_code=status.HTTP_200_OK,
    summary="Ensure User Profile and Company Association (Sync Auth Calls)",
    description="Refactored: Fetches auth user (sync), updates auth metadata if needed (sync), then ensures public profile exists and matches.",
    response_description="Success message indicating profile and company status.",
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
    raw_user_metadata_token = user_payload.get('raw_user_meta_data')
    user_metadata_token = user_payload.get('user_metadata', raw_user_metadata_token)
    if isinstance(user_metadata_token, dict):
        name_from_token = user_metadata_token.get('name') or user_metadata_token.get('full_name') or user_metadata_token.get('user_name')

    bound_log = log.bind(user_id=user_id)
    bound_log.info("Ensure profile and company association endpoint called (Final Sync Flow).")

    final_company_id: Optional[str] = None
    auth_user: Optional[User] = None

    try:
        # --- Paso 1: Obtener datos de auth.users (Llamada SÍNCRONA) ---
        bound_log.debug("Fetching auth user data (sync call)...")
        auth_user = _get_auth_user_data_sync(admin_client, user_id)

        if not auth_user:
            bound_log.error("Auth user not found via admin API despite valid token.")
            raise HTTPException(status_code=404, detail="Authenticated user not found in backend.")

        email_for_profile = auth_user.email or email_from_token
        name_for_profile = None
        if auth_user.user_metadata:
            name_for_profile = auth_user.user_metadata.get('name') or auth_user.user_metadata.get('full_name') or auth_user.user_metadata.get('user_name')
        name_for_profile = name_for_profile or name_from_token

        # --- Paso 2: Verificar/Asignar Company ID en auth.users (Llamada SÍNCRONA si es necesario) ---
        app_metadata = getattr(auth_user, 'app_metadata', {})
        if not isinstance(app_metadata, dict): app_metadata = {}

        company_id_from_auth = app_metadata.get("company_id")
        company_id_from_auth = str(company_id_from_auth) if company_id_from_auth else None

        if company_id_from_auth:
            bound_log.info("User already has company ID in auth metadata.", company_id=company_id_from_auth)
            final_company_id = company_id_from_auth
        else:
            bound_log.info("User lacks company ID in auth metadata. Attempting association...")
            company_id_to_assign = settings.DEFAULT_COMPANY_ID
            if not company_id_to_assign:
                bound_log.critical("CONFIGURATION ERROR: GATEWAY_DEFAULT_COMPANY_ID is not set.")
                raise HTTPException(status_code=400, detail="Server configuration error: Default company ID is not set.")

            bound_log.info("Associating user with Default Company ID.", default_company_id=company_id_to_assign)
            new_app_metadata = {**app_metadata, "company_id": company_id_to_assign}

            updated_user = _update_auth_user_metadata_sync(admin_client, user_id, new_app_metadata) # Llamada SÍNCRONA

            updated_metadata = getattr(updated_user, 'app_metadata', {}) if updated_user else {}
            confirmed_company_id = updated_metadata.get("company_id")

            if str(confirmed_company_id) == str(company_id_to_assign):
                bound_log.info("Successfully updated auth.users app_metadata with company ID.")
                final_company_id = company_id_to_assign
            else:
                bound_log.error("Failed to confirm company ID update in auth.users metadata response.",
                                response_metadata=updated_metadata, expected_company_id=company_id_to_assign)
                raise HTTPException(status_code=500, detail="Failed to confirm company ID update in authentication system.")

        # --- Paso 3: Sincronizar Perfil Público (Helper usa llamadas DB síncronas) ---
        if not final_company_id:
             bound_log.error("Logic error: final_company_id is not set before public profile sync.")
             raise HTTPException(status_code=500, detail="Internal error determining company ID.")

        public_profile = await _ensure_public_profile_sync(
            admin_client, user_id, final_company_id, email_for_profile, name_for_profile
        )
        bound_log.info("Public profile synchronization completed.", final_profile_id=public_profile.get("id"))

        # --- Paso 4: Devolver Éxito ---
        return {"message": "Company association successful or already existed.", "company_id": final_company_id}

    except HTTPException as e:
        raise e # Dejar que el manejador global loguee
    except Exception as e:
        bound_log.exception("Unexpected error during ensure_company_association final flow.")
        raise HTTPException(status_code=500, detail="An unexpected internal server error occurred.")