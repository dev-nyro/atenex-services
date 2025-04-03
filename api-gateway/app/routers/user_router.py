# api-gateway/app/routers/user_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Request
from typing import Annotated, Dict, Any, Optional
import structlog
from pydantic import BaseModel, Field # Importado pero no usado, podría quitarse si no hay body

# --- RESTAURAR DEPENDENCIAS ---
from app.auth.auth_middleware import InitialAuth # Usar la dependencia que NO requiere company_id
from app.utils.supabase_admin import get_supabase_admin_client
from supabase import Client as SupabaseClient
from gotrue.errors import AuthApiError # Para capturar errores específicos de Supabase Auth

from app.core.config import settings # Necesitamos settings para el default company id

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["Users"])

@router.post(
    "/me/ensure-company",
    status_code=status.HTTP_200_OK,
    # --- RESTAURAR DESCRIPCIÓN ORIGINAL ---
    summary="Ensure User Company Association",
    description="Checks if the authenticated user (valid JWT required) already has a company ID associated in their app_metadata. If not, associates the default company ID configured in the gateway using admin privileges. Returns success message or indicates if association already existed.",
    responses={
        status.HTTP_200_OK: {"description": "Company association successful or already existed."},
        status.HTTP_400_BAD_REQUEST: {"description": "Default Company ID not configured on server."},
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication token missing or invalid (signature, expiration, audience)."},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Server configuration error or failed to update user metadata."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Supabase Admin client not available."},
    }
)
async def ensure_company_association(
    # --- RESTAURAR DEPENDENCIAS ---
    user_payload: InitialAuth, # Valida token SIN requerir company_id
    supabase_admin: Annotated[Optional[SupabaseClient], Depends(get_supabase_admin_client)], # Hacer opcional y verificar
):
    # --- RESTAURAR LÓGICA ORIGINAL CON MEJORAS ---
    if not supabase_admin:
        log.error("Supabase Admin client dependency failed.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin client is not available."
        )

    user_id = user_payload.get("sub")
    # company_id puede o no estar en el payload de InitialAuth
    current_company_id_from_token = user_payload.get("company_id")

    # Vincular contexto de structlog para esta petición específica
    bound_log = log.bind(user_id=user_id)

    bound_log.info("Ensure company association endpoint called.")

    if current_company_id_from_token:
        bound_log.info("User token already contains company ID.", company_id=current_company_id_from_token)
        # Podríamos verificar si coincide con el default, pero por ahora asumimos que si existe, está bien.
        return {"message": "Company association already exists.", "company_id": current_company_id_from_token}

    # Si el token no lo tiene, verificar directamente en Supabase (más seguro)
    # Esto evita problemas si el token está desactualizado
    try:
        bound_log.debug("Fetching current user data from Supabase Admin to double-check app_metadata...")
        get_user_response = await supabase_admin.auth.admin.get_user_by_id(user_id)
        user_data = get_user_response.user
        existing_app_metadata = user_data.app_metadata if user_data else {}
        current_company_id_from_db = existing_app_metadata.get("company_id") if existing_app_metadata else None

        if current_company_id_from_db:
             bound_log.info("User already has company ID associated in database.", company_id=current_company_id_from_db)
             # Devolver el ID de la DB que es el más actualizado
             return {"message": "Company association already exists.", "company_id": current_company_id_from_db}

    except AuthApiError as e:
        bound_log.error(f"Supabase Admin API error fetching user data: {e}", status_code=e.status)
        # Podría ser un 404 si el user_id es inválido, aunque no debería si el token era válido
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch user data from authentication system: {e.message}"
        )
    except Exception as e:
        bound_log.exception("Unexpected error fetching user data for company check.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while checking user data."
        )

    # Si llegamos aquí, el usuario NO tiene company_id ni en token ni en DB
    bound_log.info("User lacks company ID. Proceeding with association.")

    company_id_to_assign = settings.DEFAULT_COMPANY_ID
    if not company_id_to_assign:
        bound_log.error("Cannot associate company: GATEWAY_DEFAULT_COMPANY_ID is not configured.")
        # Devolver 400 Bad Request porque es un problema de configuración que impide la operación
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server configuration error: Default company ID for association is not set."
        )

    bound_log.info(f"Attempting to associate user with Default Company ID.", default_company_id=company_id_to_assign)

    # Usar los metadatos existentes recuperados antes si es posible
    new_app_metadata = {**(existing_app_metadata or {}), "company_id": company_id_to_assign}

    try:
        bound_log.debug("Updating user with new app_metadata via Supabase Admin", new_metadata=new_app_metadata)
        update_response = await supabase_admin.auth.admin.update_user_by_id(
            user_id,
            attributes={'app_metadata': new_app_metadata}
        )
        # Verificar si la respuesta indica éxito (puede variar según la librería)
        # En supabase-py v2+, la ausencia de error suele indicar éxito.
        bound_log.info("Successfully updated user app_metadata with company ID.", assigned_company_id=company_id_to_assign)
        return {"message": "Company association successful.", "company_id": company_id_to_assign}

    except AuthApiError as e:
        bound_log.error(f"Supabase Admin API error during user update: {e}", status_code=e.status)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update user data in authentication system: {e.message}"
        )
    except Exception as e:
        bound_log.exception("Unexpected error during company association update.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while associating company."
        )
    # --- FIN LÓGICA ORIGINAL RESTAURADA ---