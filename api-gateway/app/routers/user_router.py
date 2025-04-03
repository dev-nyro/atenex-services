# api-gateway/app/routers/user_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Request
from typing import Annotated, Dict, Any, Optional
import structlog
import traceback
import inspect # <--- Importar inspect para diagnóstico

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
) -> Optional[Dict[str, Any]]:
    """Helper para crear el perfil en public.users. Retorna el perfil o None."""
    bound_log = log.bind(user_id=user_id)
    bound_log.info("Public user profile not found, creating...")
    user_profile_data = {
        "id": user_id,
        "email": email,
        "full_name": name or "User", # Usar un valor por defecto si no hay nombre
        "role": "user", # Rol por defecto
        "is_active": True,
        # Añadir otros valores por defecto necesarios para tu tabla 'public.users'
    }
    try:
        insert_builder = admin_client.table("users").insert(user_profile_data, returning="representation")
        # .execute() es SÍNCRONO, NO usar await aquí
        insert_response: PostgrestAPIResponse = insert_builder.execute()

        postgrest_error = getattr(insert_response, 'error', None)
        if postgrest_error:
             bound_log.error("PostgREST error during public profile insert.", status_code=insert_response.status_code, error_details=postgrest_error)
             status_code = insert_response.status_code if 400 <= insert_response.status_code < 600 else 500
             raise HTTPException(status_code=status_code, detail=f"Database error creating profile: {postgrest_error.get('message', 'Unknown DB error')}")

        if insert_response.data and len(insert_response.data) > 0:
            bound_log.info("Successfully created public user profile.", profile_data=insert_response.data[0])
            return insert_response.data[0] # Devolver el perfil creado
        else:
            bound_log.error("Profile insert reported success but no data returned.", response_status=insert_response.status_code, response_data=insert_response.data)
            raise HTTPException(status_code=500, detail="Failed to retrieve profile after creation.")

    except HTTPException as e:
        raise e
    except Exception as e:
        bound_log.exception("Unexpected error creating public user profile.")
        return None


@router.post(
    "/me/ensure-company",
    status_code=status.HTTP_200_OK,
    summary="Ensure User Profile and Company Association",
    description="Checks if the authenticated user has a profile in public.users and an associated company ID in auth.users. If not, creates the profile (if missing) and associates the default company ID.",
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
    bound_log.info("Ensure profile and company association endpoint called.")

    public_profile: Optional[Dict] = None
    auth_user: Optional[User] = None

    # --- Paso 1: Verificar/Crear perfil público en 'public.users' ---
    try:
        bound_log.debug("Checking for existing public user profile...")
        select_builder = admin_client.table("users").select("id, email, full_name, role, company_id").eq("id", user_id).limit(1)
        select_response: PostgrestAPIResponse = select_builder.execute()

        if select_response is None:
             bound_log.critical("Supabase client returned None unexpectedly from select query.", user_id=user_id)
             raise HTTPException(status_code=500, detail="Internal error communicating with database (select response was None).")

        postgrest_error = getattr(select_response, 'error', None)
        if postgrest_error:
             bound_log.error("PostgREST error checking public profile.", status_code=select_response.status_code, error_details=postgrest_error)
             status_code = select_response.status_code if select_response.status_code >= 400 else 500
             raise HTTPException(status_code=status_code, detail=f"Database error checking profile: {postgrest_error.get('message', 'Unknown PostgREST error')}")

        if select_response.data and len(select_response.data) > 0:
            public_profile = select_response.data[0]
            bound_log.info("Public user profile found.")
        else:
            bound_log.info("Public profile not found. Fetching auth user data to create profile.")
            try:
                # *** INICIO DIAGNÓSTICO ***
                get_user_method = admin_client.auth.admin.get_user_by_id
                bound_log.debug("Inspecting admin_client.auth.admin.get_user_by_id",
                                method_type=str(type(get_user_method)),
                                is_coroutine_function=inspect.iscoroutinefunction(get_user_method),
                                is_async_gen_function=inspect.isasyncgenfunction(get_user_method),
                                is_function=inspect.isfunction(get_user_method),
                                is_method=inspect.ismethod(get_user_method)
                               )
                # *** FIN DIAGNÓSTICO ***

                # Llamada original que causa el error
                auth_user_response: UserResponse = await get_user_method(user_id) # Usar la variable para asegurar que es lo que inspeccionamos
                # auth_user_response: UserResponse = await admin_client.auth.admin.get_user_by_id(user_id) # Línea original comentada

                auth_user = auth_user_response.user if auth_user_response else None

                if not auth_user:
                    bound_log.error("User data inconsistency: Auth user not found via admin API despite valid token.", user_id=user_id)
                    raise HTTPException(status_code=500, detail="User data inconsistency found.")

                email_for_profile = auth_user.email or email_from_token
                name_for_profile = None
                if auth_user.user_metadata:
                     name_for_profile = auth_user.user_metadata.get('name') or auth_user.user_metadata.get('full_name') or auth_user.user_metadata.get('user_name')
                name_for_profile = name_for_profile or name_from_token

                public_profile = await _create_public_user_profile(admin_client, user_id, email_for_profile, name_for_profile)

                if public_profile is None:
                    raise HTTPException(status_code=500, detail="Failed during profile creation process after fetching auth data.")

            except HTTPException as http_exc:
                 raise http_exc
            except Exception as creation_e:
                 # Aquí es donde se captura y loguea el TypeError
                 bound_log.exception("Error during profile creation steps.", error_type=type(creation_e).__name__)
                 # Relanzar la excepción original para mantener el detalle del error
                 raise HTTPException(status_code=500, detail=f"Failed during profile creation: {creation_e}") from creation_e


    except HTTPException as e:
        raise e
    except Exception as e:
        error_details = traceback.format_exc()
        bound_log.exception("Unexpected error checking/creating public user profile.", error_message=str(e), traceback=error_details)
        raise HTTPException(status_code=500, detail=f"Error accessing user profile data: {e}")

    # --- Paso 2: Verificar/Asociar Company ID en 'auth.users.app_metadata' ---
    if not auth_user: # Re-fetch si el perfil ya existía
        try:
            bound_log.debug("Re-fetching auth user data for company check (profile existed).")

            # *** INICIO DIAGNÓSTICO (Repetido por si acaso el flujo llega aquí) ***
            get_user_method_refetch = admin_client.auth.admin.get_user_by_id
            bound_log.debug("Inspecting admin_client.auth.admin.get_user_by_id (re-fetch)",
                            method_type=str(type(get_user_method_refetch)),
                            is_coroutine_function=inspect.iscoroutinefunction(get_user_method_refetch),
                           )
            # *** FIN DIAGNÓSTICO ***

            auth_user_response = await get_user_method_refetch(user_id)
            # auth_user_response = await admin_client.auth.admin.get_user_by_id(user_id) # Línea original comentada
            auth_user = auth_user_response.user if auth_user_response else None
            if not auth_user:
                bound_log.error("User data inconsistency: Auth user not found via admin API on second fetch.", user_id=user_id)
                raise HTTPException(status_code=500, detail="User data inconsistency.")
        except HTTPException as http_exc: raise http_exc
        except Exception as e:
             # *** INICIO DIAGNÓSTICO (Error en re-fetch) ***
             bound_log.exception("Failed to re-fetch auth user data for company check.", error_type=type(e).__name__)
             # Si el error aquí es el mismo TypeError, loguearlo específicamente
             if isinstance(e, TypeError) and "can't be used in 'await' expression" in str(e):
                  bound_log.error("TypeError encountered again during auth user re-fetch.")
             # *** FIN DIAGNÓSTICO ***
             raise HTTPException(status_code=500, detail="Failed to get user data for company check.")


    # ... (resto del código para asociar company_id permanece igual) ...
    # Verificar si ya existe company_id en app_metadata de auth.users
    app_metadata = getattr(auth_user, 'app_metadata', {}) if auth_user else {}
    if not isinstance(app_metadata, dict):
        bound_log.warning("User app_metadata is not a dictionary.", received_metadata=app_metadata)
        app_metadata = {}

    company_id_from_auth = app_metadata.get("company_id")
    company_id_from_auth = str(company_id_from_auth) if company_id_from_auth else None

    if company_id_from_auth:
        bound_log.info("User already has company ID in auth.users metadata.", company_id=company_id_from_auth)
        public_company_id = public_profile.get('company_id') if public_profile else None
        if str(public_company_id) != company_id_from_auth:
             bound_log.warning("Mismatch company ID between auth.users and public.users. Updating profile.",
                               auth_cid=company_id_from_auth, profile_cid=public_company_id)
             try:
                 update_builder = admin_client.table("users").update({"company_id": company_id_from_auth}).eq("id", user_id)
                 update_response = update_builder.execute()
                 update_error = getattr(update_response, 'error', None)
                 if update_error:
                      bound_log.error("Failed to sync existing company ID to public profile.", error_details=update_error)
                 else:
                      bound_log.info("Successfully synced company ID to public profile.")
             except Exception as sync_e:
                 bound_log.exception("Error syncing existing company ID to public profile.", error=sync_e)
        return {"message": "Company association already exists.", "company_id": company_id_from_auth}

    bound_log.info("User lacks company ID in auth metadata. Attempting association.")
    company_id_to_assign = settings.DEFAULT_COMPANY_ID
    if not company_id_to_assign:
        bound_log.critical("CONFIGURATION ERROR: GATEWAY_DEFAULT_COMPANY_ID is not set, cannot associate company.")
        raise HTTPException(status_code=400, detail="Server configuration error: Default company ID is not set.")

    bound_log.info("Associating user with Default Company ID.", default_company_id=company_id_to_assign)
    new_app_metadata = {**app_metadata, "company_id": company_id_to_assign}

    try:
        bound_log.debug("Updating auth.users app_metadata.", metadata_to_send=new_app_metadata)
        update_auth_response: UserResponse = await admin_client.auth.admin.update_user_by_id(
            user_id, attributes={'app_metadata': new_app_metadata}
        )

        updated_user_auth = update_auth_response.user if update_auth_response else None
        updated_metadata = getattr(updated_user_auth, 'app_metadata', {}) if updated_user_auth else {}
        if not (updated_metadata and updated_metadata.get("company_id") == company_id_to_assign):
             bound_log.error("Failed to confirm company ID update in auth.users metadata response.",
                           response_metadata=updated_metadata, expected_company_id=company_id_to_assign)
             raise HTTPException(status_code=500, detail="Failed to confirm company ID update in authentication system.")

        bound_log.info("Successfully updated auth.users app_metadata with company ID.")

        try:
            bound_log.debug("Updating public.users company_id column.")
            update_public_builder = admin_client.table("users").update({"company_id": company_id_to_assign}).eq("id", user_id)
            update_public_response = update_public_builder.execute()
            public_update_error = getattr(update_public_response, 'error', None)
            if public_update_error:
                 bound_log.error("PostgREST error updating company_id in public.users.", error_details=public_update_error)
            else:
                 bound_log.info("Successfully updated public.users company_id.")
        except Exception as pub_e:
             bound_log.exception("Failed to update company_id in public.users table after auth update.")

        return {"message": "Company association successful.", "company_id": company_id_to_assign}

    except AuthApiError as e:
        bound_log.error("Supabase Admin API error during user metadata update", status_code=e.status, error_message=e.message)
        status_code = e.status if 400 <= e.status < 600 else 500
        raise HTTPException(status_code=status_code, detail=f"Failed to associate company in auth system: {e.message}")
    except HTTPException as e: raise e
    except Exception as e:
        bound_log.exception("Unexpected error during company association update.")
        raise HTTPException(status_code=500, detail="An unexpected error occurred while associating company.") from e