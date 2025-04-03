# api-gateway/app/routers/user_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Request
from typing import Annotated, Dict, Any, Optional
import structlog
from pydantic import BaseModel # BaseModel no se usa aquí si no hay body

# Usar InitialAuth (valida token, NO requiere company_id)
from app.auth.auth_middleware import InitialAuth
from app.utils.supabase_admin import get_supabase_admin_client # Getter
from supabase import Client as SupabaseClient
from gotrue.errors import AuthApiError # Para capturar errores específicos

from app.core.config import settings # Necesitamos settings para el default company id

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["Users"])

# Variable global para el cliente (menos ideal, ver nota en main.py)
# Es mejor usar Depends(get_supabase_admin_client)
supabase_admin: Optional[SupabaseClient] = None

@router.post(
    "/me/ensure-company",
    status_code=status.HTTP_200_OK,
    summary="Ensure User Company Association",
    description="Checks if the authenticated user (valid JWT required) already has a company ID associated. If not, associates the default company ID using admin privileges. Requires GATEWAY_DEFAULT_COMPANY_ID to be configured.",
    responses={
        status.HTTP_200_OK: {"description": "Company association successful or already existed."},
        # 400 si el servidor no está configurado para asociar
        status.HTTP_400_BAD_REQUEST: {"description": "Default Company ID not configured on server."},
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication token missing or invalid."},
        # 500 para errores de Supabase u otros inesperados
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Failed to check or update user metadata."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Supabase Admin client not available."},
    }
)
async def ensure_company_association(
    # Usa InitialAuth: requiere token válido, pero no company_id
    user_payload: InitialAuth,
    # Inyecta el cliente admin usando el getter (más seguro que global)
    admin_client: Annotated[SupabaseClient, Depends(get_supabase_admin_client)],
):
    """
    Endpoint para asegurar que un usuario autenticado tiene un company_id.
    Si no lo tiene (ni en token ni en DB), asigna el configurado por defecto.
    """
    user_id = user_payload.get("sub")
    # company_id puede o no estar en el payload de InitialAuth
    current_company_id_from_token = user_payload.get("company_id")

    # Vincular contexto de structlog
    bound_log = log.bind(user_id=user_id)
    bound_log.info("Ensure company association endpoint called.")

    # 1. Comprobar si ya existe en el token (optimización rápida)
    if current_company_id_from_token:
        bound_log.info("User token already contains company ID.", company_id=current_company_id_from_token)
        return {"message": "Company association already exists (found in token).", "company_id": current_company_id_from_token}

    # 2. Verificar directamente en Supabase (más fiable que el token)
    try:
        bound_log.debug("Fetching current user data from Supabase Admin...")
        get_user_response = await admin_client.auth.admin.get_user_by_id(user_id)
        user_data = get_user_response.user
        # Extraer metadatos de forma segura
        existing_app_metadata = user_data.app_metadata if user_data and hasattr(user_data, 'app_metadata') else {}
        # Validar que company_id sea un string no vacío si existe
        current_company_id_from_db = existing_app_metadata.get("company_id") if isinstance(existing_app_metadata, dict) else None
        current_company_id_from_db = str(current_company_id_from_db) if current_company_id_from_db else None

        if current_company_id_from_db:
             bound_log.info("User already has company ID associated in database.", company_id=current_company_id_from_db)
             return {"message": "Company association already exists (found in database).", "company_id": current_company_id_from_db}

    except AuthApiError as e:
        # Error específico al llamar a Supabase Admin API
        bound_log.error("Supabase Admin API error fetching user data", status_code=e.status, error_message=e.message)
        # Podría ser 404 si el user_id es inválido (aunque no debería pasar si el token era válido)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify user data: {e.message}"
        )
    except Exception as e:
        # Otro error inesperado al buscar usuario
        bound_log.exception("Unexpected error fetching user data for company check.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while checking user data."
        )

    # 3. Si llegamos aquí, el usuario NO tiene company_id. Proceder a asociar.
    bound_log.info("User lacks company ID. Attempting association.")

    # --- VERIFICACIÓN CRÍTICA: ¿Está configurado el ID por defecto? ---
    company_id_to_assign = settings.DEFAULT_COMPANY_ID
    if not company_id_to_assign:
        bound_log.critical("CONFIGURATION ERROR: GATEWAY_DEFAULT_COMPANY_ID is not set. Cannot associate company.")
        # Devolver 400 Bad Request porque la solicitud no se puede cumplir debido a config del servidor
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, # Cambiado de 500 a 400
            detail="Server configuration error: Default company ID for association is not set."
        )
    # --- FIN VERIFICACIÓN ---

    bound_log.info(f"Attempting to associate user with Default Company ID.", default_company_id=company_id_to_assign)

    # Preparar los nuevos metadatos, manteniendo los existentes si los hubiera
    new_app_metadata = {**(existing_app_metadata or {}), "company_id": company_id_to_assign}

    # 4. Intentar actualizar el usuario en Supabase
    try:
        bound_log.debug("Updating user with new app_metadata via Supabase Admin", new_metadata=new_app_metadata)
        # Usar el cliente inyectado por dependencia
        update_response = await admin_client.auth.admin.update_user_by_id(
            user_id,
            # Asegúrate que el payload de attributes sea el correcto para supabase-py v2+
            attributes={'app_metadata': new_app_metadata}
        )
        # Supabase-py v2+ no devuelve mucho en la respuesta exitosa, la ausencia de error es el indicador.
        # Podríamos verificar update_response.user si es necesario.
        bound_log.info("Successfully updated user app_metadata with company ID.", assigned_company_id=company_id_to_assign)
        return {"message": "Company association successful.", "company_id": company_id_to_assign}

    except AuthApiError as e:
        # Error específico al actualizar usuario en Supabase
        bound_log.error("Supabase Admin API error during user update", status_code=e.status, error_message=e.message)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to associate company: {e.message}"
        )
    except Exception as e:
        # Otro error inesperado durante la actualización
        bound_log.exception("Unexpected error during company association update.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while associating company."
        )