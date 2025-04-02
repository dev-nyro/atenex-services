# api-gateway/app/routers/user_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Body, Request # Añadir Request
from typing import Annotated, Dict, Any, Optional
import structlog
from pydantic import BaseModel, Field

# Comentar temporalmente las dependencias problemáticas para probar OPTIONS
# from app.auth.auth_middleware import InitialAuth
# from app.utils.supabase_admin import get_supabase_admin_client
# from supabase import Client as SupabaseClient
# from gotrue.errors import AuthApiError

from app.core.config import settings # Necesitamos settings para el default company id

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["Users"])

@router.post(
    "/me/ensure-company",
    status_code=status.HTTP_200_OK,
    summary="Ensure User Company Association (Dependencies Temporarily Disabled for OPTIONS Test)", # Modificar summary
    description="Checks if the authenticated user has a company ID associated... (Dependencies Temporarily Disabled)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Default Company ID not configured"},
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication token missing or invalid"},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Failed to update user metadata / Dependencies disabled"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Dependencies temporarily disabled for testing"}, # Añadir 503
    }
)
async def ensure_company_association(
    # --- DEPENDENCIAS COMENTADAS TEMPORALMENTE ---
    request: Request # Inyectar Request para acceder a headers si fuera necesario (aunque no lo usaremos ahora)
    # user_payload: InitialAuth,
    # supabase_admin: Annotated[SupabaseClient, Depends(get_supabase_admin_client)],
    # --- FIN DEPENDENCIAS COMENTADAS ---
):
    # --- LÓGICA TEMPORAL DE PRUEBA ---
    # Devolver un error 503 para indicar que la lógica real está desactivada,
    # pero si llegamos aquí, significa que el OPTIONS (y el POST inicial) pasaron.
    log.warning("ensure_company_association endpoint called, but dependencies are disabled for testing OPTIONS request.")
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Endpoint logic temporarily disabled for CORS testing. OPTIONS request seems to be passing."
    )
    # --- FIN LÓGICA TEMPORAL ---

    """
    # --- LÓGICA ORIGINAL (COMENTADA) ---
    user_id = user_payload.get("sub")
    current_company_id = user_payload.get("company_id")

    log_ctx = structlog.contextvars.get_contextvars()
    log_ctx["user_id"] = user_id
    bound_log = log.bind(**log_ctx)

    bound_log.info("Ensure company association endpoint called.")

    if current_company_id:
        bound_log.info("User already has company ID associated.", company_id=current_company_id)
        return {"message": "Company association already exists."}

    company_id_to_assign = settings.DEFAULT_COMPANY_ID
    if not company_id_to_assign:
        bound_log.error("Cannot associate company: GATEWAY_DEFAULT_COMPANY_ID is not configured.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error: Default company ID not set."
        )

    bound_log.info(f"Attempting to associate user with Company ID: {company_id_to_assign}")

    try:
        bound_log.debug("Fetching existing user data from Supabase Admin...")
        get_user_response = await supabase_admin.auth.admin.get_user_by_id(user_id)
        existing_app_metadata = get_user_response.user.app_metadata or {}
        new_app_metadata = {**existing_app_metadata, "company_id": company_id_to_assign}
        bound_log.debug("Updating user with new app_metadata", new_metadata=new_app_metadata)

        update_response = await supabase_admin.auth.admin.update_user_by_id(
            user_id,
            attributes={'app_metadata': new_app_metadata}
        )
        bound_log.info("Successfully updated user app_metadata with company ID.", company_id=company_id_to_assign)
        return {"message": "Company association successful.", "company_id": company_id_to_assign}

    except AuthApiError as e:
        bound_log.error(f"Supabase Admin API error during user update: {e}", status_code=e.status)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update user data in authentication system: {e.message}"
        )
    except Exception as e:
        bound_log.exception("Unexpected error during company association.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while associating company."
        )
    # --- FIN LÓGICA ORIGINAL ---
    """