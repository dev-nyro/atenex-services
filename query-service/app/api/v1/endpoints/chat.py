# query-service/app/api/v1/endpoints/chat.py
import uuid
from typing import List, Optional
import structlog

from fastapi import APIRouter, Depends, HTTPException, status, Path, Query

from app.api.v1 import schemas
from app.db import postgres_client
from .query import get_current_user_id, get_current_company_id # Reutilizar dependencias

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Endpoint para Listar Chats ---
@router.get(
    "/chats",
    response_model=List[schemas.ChatSummary],
    status_code=status.HTTP_200_OK,
    summary="List User Chats",
    description="Retrieves a list of chat summaries for the authenticated user and company.",
)
async def list_chats(
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
    limit: int = Query(default=50, ge=1, le=200), # Añadir paginación
    offset: int = Query(default=0, ge=0)
):
    endpoint_log = log.bind(user_id=str(user_id), company_id=str(company_id), limit=limit, offset=offset)
    endpoint_log.info("Request received to list chats")
    if not user_id: raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not authenticated")

    try:
        # *** CORREGIDO: Llamar a la función renombrada ***
        chats = await postgres_client.get_user_chats(
            user_id=user_id, company_id=company_id, limit=limit, offset=offset
        )
        endpoint_log.info("Chats listed successfully", count=len(chats))
        return chats
    except Exception as e:
        endpoint_log.exception("Error listing chats")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve chat list.")

# --- Endpoint para Obtener Mensajes de un Chat ---
@router.get(
    "/chats/{chat_id}/messages",
    response_model=List[schemas.ChatMessage],
    status_code=status.HTTP_200_OK,
    summary="Get Chat Messages",
    description="Retrieves messages for a specific chat, verifying ownership.",
    responses={404: {"description": "Chat not found or access denied."}}
)
async def get_chat_messages_endpoint( # Renombrar función endpoint para evitar colisión
    chat_id: uuid.UUID = Path(..., description="The ID of the chat."),
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
    limit: int = Query(default=100, ge=1, le=500), # Añadir paginación
    offset: int = Query(default=0, ge=0)
):
    endpoint_log = log.bind(user_id=str(user_id), company_id=str(company_id), chat_id=str(chat_id), limit=limit, offset=offset)
    endpoint_log.info("Request received to get chat messages")
    if not user_id: raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not authenticated")

    try:
        # *** CORREGIDO: Llamar a la función renombrada ***
        # La función DB ya verifica propiedad y devuelve lista vacía si no aplica
        messages = await postgres_client.get_chat_messages(
            chat_id=chat_id, user_id=user_id, company_id=company_id, limit=limit, offset=offset
        )
        # Si quisiéramos devolver 404 si no hay chat/mensajes:
        # if not messages and not await postgres_client.check_chat_ownership(chat_id, user_id, company_id):
        #      raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found or access denied.")

        endpoint_log.info("Chat messages retrieved successfully", count=len(messages))
        return messages
    except Exception as e:
        endpoint_log.exception("Error getting chat messages")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve chat messages.")

# --- Endpoint para Borrar un Chat ---
@router.delete(
    "/chats/{chat_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Chat",
    description="Deletes a chat and its messages, verifying ownership.",
    responses={404: {"description": "Chat not found or access denied."}, 204: {}}
)
async def delete_chat_endpoint(
    chat_id: uuid.UUID = Path(..., description="The ID of the chat to delete."),
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
):
    endpoint_log = log.bind(user_id=str(user_id), company_id=str(company_id), chat_id=str(chat_id))
    endpoint_log.info("Request received to delete chat")
    if not user_id: raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not authenticated")

    try:
        # La función DB ya verifica propiedad internamente
        deleted = await postgres_client.delete_chat(chat_id=chat_id, user_id=user_id, company_id=company_id)
        if deleted:
            endpoint_log.info("Chat deleted successfully")
            return None # Respuesta 204
        else:
            endpoint_log.warning("Chat not found or access denied for deletion")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found or access denied.")
    except Exception as e:
        endpoint_log.exception("Error deleting chat")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to delete chat.")