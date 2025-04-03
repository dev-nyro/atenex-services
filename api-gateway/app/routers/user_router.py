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
from postgrest.builder import SyncFilterRequestBuilder # Para tipado del select

from app.core.config import settings

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["Users"])

# Inyección global (menos ideal)
supabase_admin: Optional[SupabaseClient] = None

# --- Helper _create_public_user_profile (Revisado para retornar None en error no HTTP) ---
async def _create_public_user_profile(
    admin_client: SupabaseClient,
    user_id: str,
    email: Optional[str],
    name: Optional[str]
) -> Optional[Dict[str, Any]]: # Ahora puede retornar None si falla internamente
    """Helper para crear el perfil en public.users. Retorna el perfil o None."""
    bound_log = log.bind(user_id=user_id)
    bound_log.info("Public user profile not found, creating...")
    user_profile_data = {
        "id": user_id,
        "email": email,
        "full_name": name or "User", # Asegurar un valor para full_name
        "role": "user",
        # Añadir otros valores por defecto necesarios para tu tabla 'users'
        "is_active": True,
        # "company_id": None, # Se asignará después
    }
    try:
        # Llamada síncrona
        insert_builder: SyncFilterRequestBuilder = admin_client.table("users").insert(user_profile_data, returning="representation")
        insert_response: PostgrestAPIResponse = insert_builder.execute()

        # Verificar errores de PostgREST explícitamente
        postgrest_error = getattr(insert_response, 'error', None)
        if postgrest_error:
             bound_log.error("PostgREST error during public profile insert.",
                             status_code=insert_response.status_code,
                             error_details=postgrest_error)
             # Lanzar excepción para que sea capturada en el endpoint principal
             raise HTTPException(status_code=500, detail=f"Database error creating profile: {postgrest_error.get('message', 'Unknown DB error')}")

        if insert_response.data and len(insert_response.data) > 0:
            bound_log.info("Successfully created public user profile.", profile_data=insert_response.data[0])
            return insert_response.data[0]
        else:
            bound_log.error("Profile insert succeeded but no data returned.", response_status=insert_response.status_code)
            # Considerar esto un error, ya que esperábamos datos de vuelta
            raise HTTPException(status_code=500, detail="Failed to retrieve profile after creation.")

    except HTTPException as e: # Re-lanzar HTTPExceptions
        raise e
    except Exception as e:
        # Otros errores inesperados durante la inserción
        bound_log.exception("Unexpected error creating public user profile.")
        # NO lanzar HTTPException aquí, retornar None y dejar que el caller maneje
        # raise HTTPException(status_code=500, detail=f"Internal error creating profile: {e}") from e
        return None # Indicar fallo


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

    public_profile: Optional[Dict] = None
    auth_user: Optional[User] = None # Para almacenar datos de auth si es necesario crear perfil

    # --- Paso 1: Verificar/Crear perfil público ---
    try:
        bound_log.debug("Checking for existing public user profile...")
        # Usar select genérico sin maybe_single() para evitar error 406 si RLS está mal
        select_builder: SyncFilterRequestBuilder = admin_client.table("users").select("id, email, full_name, role, company_id").eq("id", user_id).limit(1) # Seleccionar campos necesarios
        select_response: PostgrestAPIResponse = select_builder.execute()

        if select_response is None:
             bound_log.critical("Supabase client returned None unexpectedly from select query.", user_id=user_id)
             raise HTTPException(status_code=500, detail="Internal error communicating with database (select response was None).")

        postgrest_error = getattr(select_response, 'error', None)
        if postgrest_error:
             bound_log.error("PostgREST error checking public profile.", status_code=select_response.status_code, error_details=postgrest_error)
             raise HTTPException(status_code=select_response.status_code if select_response.status_code >= 400 else 500,
                                 detail=f"Database error checking profile: {postgrest_error.get('message', 'Unknown PostgREST error')}")

        # Ahora es seguro acceder a .data
        if select_response.data and len(select_response.data) > 0:
            public_profile = select_response.data[0]
            bound_log.info("Public user profile found.")
        else:
            # Perfil no existe, proceder a crearlo
            bound_log.info("Public profile not found. Fetching auth user data.")
            try:
                auth_user_response: UserResponse = await admin_client.auth.admin.get_user_by_id(user_id)
                auth_user = auth_user_response.user if auth_user_response else None
                if not auth_user: raise HTTPException(status_code=500, detail="User data inconsistency.")

                email_for_profile = auth_user.email or email_from_token
                name_for_profile = (auth_user.user_metadata.get('name') or auth_user.user_metadata.get('full_name')) if auth_user.user_metadata else name_from_token

                public_profile = await _create_public_user_profile(admin_client, user_id, email_for_profile, name_for_profile)

                # Verificar si la creación falló (retornó None)
                if public_profile is None:
                    bound_log.error("Profile creation helper returned None, indicating an internal error.")
                    raise HTTPException(status_code=500, detail="Failed during profile creation process.")

            except Exception as creation_e:
                 bound_log.exception("Error during profile creation steps.")
                 if isinstance(creation_e, HTTPException): raise creation_e
                 raise HTTPException(status_code=500, detail=f"Failed during profile creation: {creation_e}")

    except HTTPException as e: # Captura HTTPExceptions de los bloques anteriores
        raise e
    except Exception as e:
        # Captura el AttributeError o cualquier otro error inesperado aquí
        error_details = traceback.format_exc()
        bound_log.exception("Unexpected error checking/creating public user profile.", error_message=str(e), traceback=error_details)
        # Devolver el mensaje de error específico al frontend
        raise HTTPException(status_code=500, detail=f"Error accessing user profile data: {e}")

    # --- Paso 2: Verificar/Asociar Company ID ---
    # Si llegamos aquí, tenemos un `public_profile` válido (existente o recién creado)
    # Necesitamos verificar `company_id` en `auth.users` (app_metadata)

    # Re-obtener auth_user si no lo obtuvimos durante la creación del perfil
    if not auth_user:
        try:
            bound_log.debug("Re-fetching auth user data for company check.")
            auth_user_response = await admin_client.auth.admin.get_user_by_id(user_id)
            auth_user = auth_user_response.user if auth_user_response else None
            if not auth_user: raise HTTPException(status_code=500, detail="User data inconsistency.")
        except Exception as e:
            bound_log.exception("Failed to re-fetch auth user data.")
            raise HTTPException(status_code=500, detail="Failed to get user data for company check.")

    app_metadata = getattr(auth_user, 'app_metadata', {}) if auth_user else {}
    company_id_from_auth = app_metadata.get("company_id") if isinstance(app_metadata, dict) else None
    company_id_from_auth = str(company_id_from_auth) if company_id_from_auth else None

    if company_id_from_auth:
        bound_log.info("User already has company ID in auth.users metadata.", company_id=company_id_from_auth)
        # Sincronizar con public.users si es necesario
        public_company_id = public_profile.get('company_id') # Ya tenemos public_profile
        if str(public_company_id) != company_id_from_auth:
             bound_log.warning("Mismatch company ID. Updating profile.", auth_cid=company_id_from_auth, profile_cid=public_company_id)
             try:
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
        update_auth_response: UserResponse = await admin_client.auth.admin.update_user_by_id(
            user_id, attributes={'app_metadata': new_app_metadata}
        )
        updated_user_auth = update_auth_response.user if update_auth_response else None
        if not (updated_user_auth and getattr(updated_user_auth, 'app_metadata', {}).get("company_id") == company_id_to_assign):
             bound_log.error("Failed to confirm company ID update in auth.users metadata response.", update_response=updated_user_auth)
             raise HTTPException(status_code=500, detail="Failed to confirm company ID update in auth system.")
        bound_log.info("Successfully updated auth.users app_metadata.")

        # Actualizar public.users
        try:
            bound_log.debug("Updating public.users company_id column.")
            # Llamada síncrona
            update_public_response = admin_client.table("users").update({"company_id": company_id_to_assign}).eq("id", user_id).execute()
            # Verificar si hubo error en la actualización pública
            public_update_error = getattr(update_public_response, 'error', None)
            if public_update_error:
                 bound_log.error("PostgREST error updating company_id in public.users.", error_details=public_update_error)
                 # Considerar si esto es un error fatal o solo advertencia
            else:
                 bound_log.info("Successfully updated public.users company_id.")
        except Exception as pub_e:
             bound_log.exception("Failed to update company_id in public.users table.")

        return {"message": "Company association successful.", "company_id": company_id_to_assign}

    except AuthApiError as e:
        bound_log.error("Supabase Admin API error during user metadata update", status_code=e.status, error_message=e.message)
        raise HTTPException(status_code=500, detail=f"Failed to associate company in auth system: {e.message}")
    except HTTPException as e: raise e
    except Exception as e:
        bound_log.exception("Unexpected error during company association update.")
        raise HTTPException(status_code=500, detail="An unexpected error occurred while associating company.") from e
