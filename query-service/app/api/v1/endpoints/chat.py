# ./app/api/v1/endpoints/chat.py
import uuid
from typing import List, Optional
import structlog

from fastapi import APIRouter, Depends, HTTPException, status, Path, Query

from app.api.v1 import schemas
from app.db import postgres_client
# Importar dependencias de autenticación/autorización
from .query import get_current_user_id, get_current_company_id # Reutilizar las dependencias existentes

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Endpoint para Listar Chats ---
@router.get(
    "/query/chats",
    response_model=List[schemas.ChatSummary],
    status_code=status.HTTP_200_OK,
    summary="List User Chats",
    description="Retrieves a list of chat summaries for the authenticated user and company, ordered by last updated time.",
)
async def list_chats(
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
):
    endpoint_log = log.bind(user_id=str(user_id), company_id=str(company_id))
    endpoint_log.info("Request received to list chats")

    if not user_id: # Asegurarse de que el usuario está identificado
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not authenticated")

    try:
        chats = await postgres_client.get_chats_for_user(user_id=user_id, company_id=company_id)
        endpoint_log.info("Chats listed successfully", count=len(chats))
        return chats
    except Exception as e:
        endpoint_log.exception("Error listing chats")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve chat list.")

# --- Endpoint para Obtener Mensajes de un Chat ---
@router.get(
    "/query/chats/{chat_id}/messages",
    response_model=List[schemas.ChatMessage],
    status_code=status.HTTP_200_OK,
    summary="Get Chat Messages",
    description="Retrieves all messages for a specific chat, verifying user ownership.",
    responses={
        404: {"description": "Chat not found or user does not have access."},
    }
)
async def get_chat_messages(
    chat_id: uuid.UUID = Path(..., description="The ID of the chat to retrieve messages from."),
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
):
    endpoint_log = log.bind(user_id=str(user_id), company_id=str(company_id), chat_id=str(chat_id))
    endpoint_log.info("Request received to get chat messages")

    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not authenticated")

    try:
        # La función get_messages_for_chat ya incluye la verificación de propiedad
        messages = await postgres_client.get_messages_for_chat(
            chat_id=chat_id,
            user_id=user_id,
            company_id=company_id
        )

        # Si get_messages_for_chat devuelve [] porque no se encontró o no hay acceso,
        # podríamos querer devolver 404 en lugar de 200 con lista vacía.
        # Para hacer eso, necesitaríamos que check_chat_ownership lance una excepción o
        # hacer el check aquí primero. Por simplicidad, mantenemos el comportamiento de devolver 200 OK [].
        # Si se requiere 404, descomentar y ajustar:
        # owner_check = await postgres_client.check_chat_ownership(chat_id, user_id, company_id)
        # if not owner_check:
        #     endpoint_log.warning("Chat not found or access denied")
        #     raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found or access denied.")
        # messages = await postgres_client.get_messages_for_chat(...) # Llamar sin el check interno

        endpoint_log.info("Chat messages retrieved successfully", count=len(messages))
        return messages
    except Exception as e:
        endpoint_log.exception("Error getting chat messages")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve chat messages.")


# --- Endpoint para Borrar un Chat ---
@router.delete(
    "/query/chats/{chat_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Chat",
    description="Deletes a specific chat and all its associated messages, verifying user ownership.",
    responses={
        404: {"description": "Chat not found or user does not have access."},
        204: {"description": "Chat deleted successfully."},
    }
)
async def delete_chat_endpoint(
    chat_id: uuid.UUID = Path(..., description="The ID of the chat to delete."),
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
):
    endpoint_log = log.bind(user_id=str(user_id), company_id=str(company_id), chat_id=str(chat_id))
    endpoint_log.info("Request received to delete chat")

    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not authenticated")

    try:
        deleted = await postgres_client.delete_chat(
            chat_id=chat_id,
            user_id=user_id,
            company_id=company_id
        )
        if deleted:
            endpoint_log.info("Chat deleted successfully")
            # No se devuelve contenido en un 204
            return None
        else:
            # Si delete_chat devuelve False, significa que no se encontró o no se tenía permiso
            endpoint_log.warning("Chat not found or access denied for deletion")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found or access denied.")
    except Exception as e:
        endpoint_log.exception("Error deleting chat")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to delete chat.")

# --- Endpoints Opcionales (POST /chats, PATCH /chats/{chatId}) ---
# POST /api/v1/chats: No se implementa aquí porque la creación se maneja en POST /query.
# PATCH /api/v1/chats/{chatId}: No implementado por ahora (para renombrar).