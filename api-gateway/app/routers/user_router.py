# api-gateway/app/routers/user_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Request
from typing import Annotated, Dict, Any, Optional
import structlog
# from pydantic import BaseModel # No usado

# Dependencias
from app.auth.auth_middleware import InitialAuth
from app.utils.supabase_admin import get_supabase_admin_client
from supabase import Client as SupabaseClient
from gotrue.errors import AuthApiError
from gotrue.types import UserResponse # Importar UserResponse para tipado

from app.core.config import settings

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["Users"])

# Inyección global (menos ideal) - Asegúrate que main.py lo asigna en lifespan
supabase_admin: Optional[SupabaseClient] = None

@router.post(
    "/me/ensure-company",
    # ... (metadata de la ruta sin cambios) ...
    status_code=status.HTTP_200_OK,
    summary="Ensure User Company Association",
    description="Checks if the authenticated user (valid JWT required) already has a company ID associated. If not, associates the default company ID using admin privileges. Requires GATEWAY_DEFAULT_COMPANY_ID to be configured.",
    responses={
        status.HTTP_200_OK: {"description": "Company association successful or already existed."},
        status.HTTP_400_BAD_REQUEST: {"description": "Default Company ID not configured on server."},
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication token missing or invalid."},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Failed to check or update user metadata."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Supabase Admin client not available."},
    }
)
async def ensure_company_association(
    user_payload: InitialAuth,
    admin_client: Annotated[SupabaseClient, Depends(get_supabase_admin_client)],
):
    user_id = user_payload.get("sub")
    current_company_id_from_token = user_payload.get("company_id")
    bound_log = log.bind(user_id=user_id)
    bound_log.info("Ensure company association endpoint called.")

    if current_company_id_from_token:
        bound_log.info("User token already contains company ID.", company_id=current_company_id_from_token)
        return {"message": "Company association already exists (found in token).", "company_id": current_company_id_from_token}

    try:
        bound_log.debug("Fetching current user data from Supabase Admin...")
        # Usar el tipo UserResponse para mejor autocompletado/verificación
        get_user_response: UserResponse = await admin_client.auth.admin.get_user_by_id(user_id)

        # *** CORRECCIÓN: Verificar si user existe ANTES de acceder a atributos ***
        user_data = get_user_response.user if get_user_response else None

        if user_data:
            # Acceder a app_metadata de forma segura
            existing_app_metadata = user_data.app_metadata if hasattr(user_data, 'app_metadata') else {}
            current_company_id_from_db = existing_app_metadata.get("company_id") if isinstance(existing_app_metadata, dict) else None
            current_company_id_from_db = str(current_company_id_from_db) if current_company_id_from_db else None

            if current_company_id_from_db:
                bound_log.info("User already has company ID associated in database.", company_id=current_company_id_from_db)
                return {"message": "Company association already exists (found in database).", "company_id": current_company_id_from_db}
            else:
                # User existe pero no tiene company_id en DB
                 bound_log.info("User found in DB but lacks company ID in app_metadata.")
                 # Continuar al bloque de asignación
        else:
             # No se encontró el usuario en Supabase (¡inesperado si el token era válido!)
             bound_log.error("User not found in Supabase database despite valid token.", user_id=user_id)
             raise HTTPException(
                 status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                 detail="User inconsistency: User authenticated but not found in database."
             )
        # *** FIN CORRECCIÓN ***

    except AuthApiError as e:
        bound_log.error("Supabase Admin API error fetching user data", status_code=e.status, error_message=e.message)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify user data: {e.message}"
        )
    except Exception as e: # Captura otras excepciones inesperadas
        # *** ERROR ORIGINAL DETECTADO AQUÍ ***
        bound_log.exception("Unexpected error processing user data for company check.") # Usar .exception() para traceback
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while checking user data."
        ) from e # Preservar causa original

    # --- Bloque de Asociación (si no tenía ID) ---
    bound_log.info("User lacks company ID. Attempting association.")
    company_id_to_assign = settings.DEFAULT_COMPANY_ID
    if not company_id_to_assign:
        bound_log.critical("CONFIGURATION ERROR: GATEWAY_DEFAULT_COMPANY_ID is not set.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server configuration error: Default company ID for association is not set."
        )

    bound_log.info(f"Attempting to associate user with Default Company ID.", default_company_id=company_id_to_assign)
    # Usar metadatos existentes (recuperados antes) y añadir/sobrescribir company_id
    new_app_metadata = {**(existing_app_metadata or {}), "company_id": company_id_to_assign}

    try:
        bound_log.debug("Updating user with new app_metadata via Supabase Admin", new_metadata=new_app_metadata)
        update_response: UserResponse = await admin_client.auth.admin.update_user_by_id(
            user_id,
            attributes={'app_metadata': new_app_metadata}
        )
        # Verificar si el usuario actualizado tiene el company_id (opcional, por seguridad)
        updated_user_data = update_response.user if update_response else None
        if updated_user_data and updated_user_data.app_metadata and updated_user_data.app_metadata.get("company_id") == company_id_to_assign:
            bound_log.info("Successfully updated user app_metadata with company ID.", assigned_company_id=company_id_to_assign)
            return {"message": "Company association successful.", "company_id": company_id_to_assign}
        else:
             bound_log.error("Failed to confirm company ID update in Supabase response.", update_response=update_response)
             raise HTTPException(status_code=500, detail="Failed to confirm company association update.")

    except AuthApiError as e:
        bound_log.error("Supabase Admin API error during user update", status_code=e.status, error_message=e.message)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to associate company: {e.message}"
        )
    except Exception as e:
        bound_log.exception("Unexpected error during company association update.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while associating company."
        ) from e