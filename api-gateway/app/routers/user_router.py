# api-gateway/app/routers/user_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Body
from typing import Annotated, Dict, Any, Optional
import structlog
from pydantic import BaseModel, Field # Para el cuerpo de la petición (opcional)

from app.auth.auth_middleware import InitialAuth # Usar la dependencia que NO requiere company_id
from app.utils.supabase_admin import get_supabase_admin_client
from app.core.config import settings
from supabase import Client as SupabaseClient # Tipado para el cliente admin
from gotrue.errors import AuthApiError # Para capturar errores específicos de Supabase Admin

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["Users"])

# Opcional: Modelo Pydantic si queremos pasar company_id en el body
# class CompanyAssociationRequest(BaseModel):
#     company_id: str = Field(..., description="The Company ID to associate with the user.")

@router.post(
    "/me/ensure-company",
    status_code=status.HTTP_200_OK,
    summary="Ensure User Company Association",
    description="Checks if the authenticated user has a company ID associated in their token metadata. If not, attempts to associate a default company ID using admin privileges.",
    response_description="Returns success message if association exists or was successful.",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Default Company ID not configured"},
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication token missing or invalid"},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Failed to update user metadata"},
    }
)
async def ensure_company_association(
    # Usar InitialAuth: valida token pero no requiere company_id
    user_payload: InitialAuth,
    # Inyectar cliente Supabase Admin
    supabase_admin: Annotated[SupabaseClient, Depends(get_supabase_admin_client)],
    # Opcional: Recibir company_id del body (menos seguro, preferible determinar en backend)
    # request_body: Optional[CompanyAssociationRequest] = None
):
    user_id = user_payload.get("sub")
    current_company_id = user_payload.get("company_id") # Puede ser None

    log_ctx = {"user_id": user_id}
    log.info("Ensure company association endpoint called.", **log_ctx)

    if current_company_id:
        log.info("User already has company ID associated.", company_id=current_company_id, **log_ctx)
        return {"message": "Company association already exists."}

    # Determinar el Company ID a asignar
    # Opción 1: Usar el default configurado en settings (recomendado para empezar)
    company_id_to_assign = settings.DEFAULT_COMPANY_ID
    if not company_id_to_assign:
        log.error("Cannot associate company: GATEWAY_DEFAULT_COMPANY_ID is not configured.", **log_ctx)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error: Default company ID not set."
        )

    # Opción 2: Usar el ID del body (si se implementa el modelo Pydantic)
    # if request_body:
    #    company_id_to_assign = request_body.company_id
    # else:
    #    # Fallar si se esperaba del body y no vino
    #    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Company ID required in request body.")

    # Opción 3: Lógica más compleja (ej: lookup por email domain) - implementar aquí si es necesario

    log.info(f"Attempting to associate user with Company ID: {company_id_to_assign}", **log_ctx)

    try:
        # Obtener metadatos existentes para no sobrescribir otros datos
        # Nota: Esto requiere otra llamada, puede ser opcional si sabes que app_metadata está vacío
        get_user_response = await supabase_admin.auth.admin.get_user_by_id(user_id)
        existing_app_metadata = get_user_response.user.app_metadata or {}
        existing_user_metadata = get_user_response.user.user_metadata or {} # Preservar user_metadata también

        # Crear nuevos metadatos fusionando los existentes con el nuevo company_id
        new_app_metadata = {**existing_app_metadata, "company_id": company_id_to_assign}
        # Podrías añadir roles aquí también si es necesario:
        # new_app_metadata["roles"] = ["user"] # Ejemplo

        update_response = await supabase_admin.auth.admin.update_user_by_id(
            user_id,
            attributes={
                'app_metadata': new_app_metadata,
                # Opcional: Actualizar user_metadata si es necesario, pero usualmente no desde aquí
                # 'user_metadata': existing_user_metadata
            }
        )
        log.info("Successfully updated user app_metadata with company ID.", company_id=company_id_to_assign, **log_ctx)
        # update_response.user contiene el usuario actualizado
        return {"message": "Company association successful.", "company_id": company_id_to_assign}

    except AuthApiError as e:
        log.error(f"Supabase Admin API error during user update: {e}", status_code=e.status, **log_ctx)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update user data in authentication system: {e.message}"
        )
    except Exception as e:
        log.exception("Unexpected error during company association.", **log_ctx)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while associating company."
        )