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
from gotrue.types import UserResponse, User

from app.core.config import settings

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["Users"])

# Inyección global (menos ideal)
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

    # Definir variable fuera del try para usarla en el bloque de asociación
    existing_app_metadata: Optional[Dict] = None
    current_company_id_from_db: Optional[str] = None

    try:
        bound_log.debug("Fetching current user data from Supabase Admin...")
        get_user_response: UserResponse = await admin_client.auth.admin.get_user_by_id(user_id)
        user_data: Optional[User] = get_user_response.user if get_user_response else None

        if user_data:
            # --- Bloque de acceso a metadatos MÁS DEFENSIVO ---
            bound_log.debug("User data received from Supabase.", user_keys=list(user_data.model_dump().keys()) if user_data else []) # Log keys available

            temp_app_metadata = getattr(user_data, 'app_metadata', None) # Usar getattr con default None

            if isinstance(temp_app_metadata, dict):
                existing_app_metadata = temp_app_metadata # Asignar solo si es un dict
                company_id_raw = existing_app_metadata.get("company_id")
                current_company_id_from_db = str(company_id_raw) if company_id_raw else None
                bound_log.debug("Successfully processed app_metadata.", metadata=existing_app_metadata, found_company_id=current_company_id_from_db)
            else:
                # Si app_metadata no existe o no es un diccionario
                existing_app_metadata = {} # Inicializar como dict vacío
                current_company_id_from_db = None
                bound_log.warning("app_metadata not found or not a dictionary in user data.", app_metadata_type=type(temp_app_metadata).__name__)
            # --- Fin bloque defensivo ---

            if current_company_id_from_db:
                bound_log.info("User already has company ID associated in database.", company_id=current_company_id_from_db)
                return {"message": "Company association already exists (found in database).", "company_id": current_company_id_from_db}
            else:
                 bound_log.info("User confirmed in DB, lacks company ID in app_metadata. Proceeding to association.")
                 # Continuar al bloque de asociación

        else:
             bound_log.error("User not found in Supabase database despite valid token.", user_id=user_id)
             raise HTTPException(
                 status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                 detail="User inconsistency: User authenticated but not found in database."
             )

    except AuthApiError as e:
        bound_log.error("Supabase Admin API error fetching user data", status_code=e.status, error_message=e.message)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify user data: {e.message}"
        )
    except Exception as e:
        # Este es el bloque donde caía el error anterior
        bound_log.exception("Unexpected error during user data retrieval or processing.") # Mensaje más genérico
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while checking user data."
        ) from e

    # --- Bloque de Asociación (Se llega aquí si no tenía ID) ---
    bound_log.info("User lacks company ID. Attempting association.")
    company_id_to_assign = settings.DEFAULT_COMPANY_ID
    if not company_id_to_assign:
        bound_log.critical("CONFIGURATION ERROR: GATEWAY_DEFAULT_COMPANY_ID is not set.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server configuration error: Default company ID for association is not set."
        )

    # Crear los nuevos metadatos asegurándose que existing_app_metadata es un diccionario
    # Si existing_app_metadata es None (nunca debería serlo por la lógica anterior, pero por si acaso), usar {}
    new_app_metadata = {**(existing_app_metadata if existing_app_metadata is not None else {}), "company_id": company_id_to_assign}
    bound_log.debug("Constructed new app_metadata for update.", metadata_to_send=new_app_metadata) # Log extra

    try:
        bound_log.info("Updating user with new app_metadata via Supabase Admin")
        update_response: UserResponse = await admin_client.auth.admin.update_user_by_id(
            user_id,
            attributes={'app_metadata': new_app_metadata}
        )

        # Verificación post-actualización (opcional pero recomendada)
        updated_user_data = update_response.user if update_response else None
        if not (updated_user_data and hasattr(updated_user_data, 'app_metadata') and isinstance(updated_user_data.app_metadata, dict) and updated_user_data.app_metadata.get("company_id") == company_id_to_assign):
             bound_log.error("Failed to confirm company ID update in Supabase response.", update_response_user=updated_user_data.model_dump_json(indent=2) if updated_user_data else "None")
             # Podríamos intentar leer de nuevo, pero por ahora lanzamos error
             raise HTTPException(status_code=500, detail="Failed to confirm company association update after API call.")

        bound_log.info("Successfully updated user app_metadata with company ID.", assigned_company_id=company_id_to_assign)
        return {"message": "Company association successful.", "company_id": company_id_to_assign}

    except AuthApiError as e:
        bound_log.error("Supabase Admin API error during user update", status_code=e.status, error_message=e.message)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to associate company: {e.message}"
        )
    except HTTPException as e: # Re-lanzar HTTPException de la verificación post-update
         raise e
    except Exception as e:
        bound_log.exception("Unexpected error during company association update.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while associating company."
        ) from e