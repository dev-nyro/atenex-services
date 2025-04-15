# query-service/app/api/v1/endpoints/chat.py
import uuid
from typing import List, Optional
import structlog
from fastapi import APIRouter, Depends, HTTPException, status, Path, Query, Header, Request # Importar Request

from app.api.v1 import schemas
from app.db import postgres_client
# --- CORRECCIÓN: Usar las dependencias DEFINIDAS LOCALMENTE ---
# (Se aseguran de que las cabeceras X- estén presentes y sean válidas)

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Dependencias para usar cabeceras X-* ---
async def get_current_company_id(x_company_id: Optional[str] = Header(None, alias="X-Company-ID")) -> uuid.UUID:
    """Obtiene y valida el Company ID de la cabecera X-Company-ID."""
    if not x_company_id:
        log.warning("Missing required X-Company-ID header")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try:
        return uuid.UUID(x_company_id)
    except ValueError:
        log.warning("Invalid UUID format in X-Company-ID header", header_value=x_company_id)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid X-Company-ID header format")

async def get_current_user_id(x_user_id: Optional[str] = Header(None, alias="X-User-ID")) -> uuid.UUID:
    """Obtiene y valida el User ID de la cabecera X-User-ID."""
    if not x_user_id:
        log.warning("Missing required X-User-ID header")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-User-ID")
    try:
        return uuid.UUID(x_user_id)
    except ValueError:
        log.warning("Invalid UUID format in X-User-ID header", header_value=x_user_id)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid X-User-ID header format")

# --- Endpoints Modificados para USAR las nuevas dependencias ---

@router.get(
    "/chats",
    response_model=List[schemas.ChatSummary],
    status_code=status.HTTP_200_OK,
    summary="List User Chats",
    description="Retrieves a list of chat summaries using X-Company-ID and X-User-ID headers.",
)
async def list_chats(
    # *** CORRECCIÓN CLAVE: Usar las dependencias correctas ***
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
    # Ya no se depende de Authorization
    # --- Parámetros de Query ---
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    request: Request = None
):
    request_id = request.headers.get("x-request-id") if request else str(uuid.uuid4())
    endpoint_log = log.bind(request_id=request_id, user_id=str(user_id), company_id=str(company_id), limit=limit, offset=offset)
    endpoint_log.info("Request received to list chats")

    try:
        # Usar la función con el nombre corregido
        chats = await postgres_client.get_user_chats(
            user_id=user_id, company_id=company_id, limit=limit, offset=offset
        )
        endpoint_log.info("Chats listed successfully", count=len(chats))
        return chats
    except Exception as e:
        endpoint_log.exception("Error listing chats")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve chat list.")


@router.get(
    "/chats/{chat_id}/messages",
    response_model=List[schemas.ChatMessage],
    status_code=status.HTTP_200_OK,
    summary="Get Chat Messages",
    description="Retrieves messages for a specific chat using X-Company-ID and X-User-ID headers.",
    responses={404: {"description": "Chat not found or access denied."}}
)
async def get_chat_messages_endpoint(
    chat_id: uuid.UUID = Path(..., description="The ID of the chat."),
    # *** CORRECCIÓN CLAVE: Usar las dependencias correctas ***
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
    # --- Parámetros de Query ---
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    request: Request = None
):
    request_id = request.headers.get("x-request-id") if request else str(uuid.uuid4())
    endpoint_log = log.bind(request_id=request_id, user_id=str(user_id), company_id=str(company_id), chat_id=str(chat_id), limit=limit, offset=offset)
    endpoint_log.info("Request received to get chat messages")

    try:
        # Usar la función con el nombre corregido
        messages = await postgres_client.get_chat_messages(
            chat_id=chat_id, user_id=user_id, company_id=company_id, limit=limit, offset=offset
        )
        endpoint_log.info("Chat messages retrieved successfully", count=len(messages))
        # Nota: la función DB ya devuelve [] si no se encuentra o no es propietario
        return messages
    except Exception as e:
        endpoint_log.exception("Error getting chat messages")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve chat messages.")


@router.delete(
    "/chats/{chat_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Chat",
    description="Deletes a chat using X-Company-ID and X-User-ID headers.",
    responses={404: {"description": "Chat not found or access denied."}, 204: {}}
)
async def delete_chat_endpoint(
    chat_id: uuid.UUID = Path(..., description="The ID of the chat to delete."),
    # *** CORRECCIÓN CLAVE: Usar las dependencias correctas ***
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
    request: Request = None
):
    request_id = request.headers.get("x-request-id") if request else str(uuid.uuid4())
    endpoint_log = log.bind(request_id=request_id, user_id=str(user_id), company_id=str(company_id), chat_id=str(chat_id))
    endpoint_log.info("Request received to delete chat")

    try:
        deleted = await postgres_client.delete_chat(chat_id=chat_id, user_id=user_id, company_id=company_id)
        if deleted:
            endpoint_log.info("Chat deleted successfully")
            # Para 204, no devuelvas nada o devuelve Response(status_code=204)
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        else:
            endpoint_log.warning("Chat not found or access denied for deletion")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found or access denied.")
    except Exception as e:
        endpoint_log.exception("Error deleting chat")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to delete chat.")