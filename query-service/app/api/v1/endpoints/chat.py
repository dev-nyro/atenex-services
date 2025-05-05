# query-service/app/api/v1/endpoints/chat.py
import uuid
from typing import List, Optional
import structlog
from fastapi import (
    APIRouter, Depends, HTTPException, status, Path, Query, Header, Request,
    Response
)

from app.api.v1 import schemas
# LLM_REFACTOR_STEP_4: Import Repository directly for simple operations
from app.infrastructure.persistence.postgres_repositories import PostgresChatRepository
from app.application.ports.repository_ports import ChatRepositoryPort

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Headers Dependencies (Sin cambios) ---
async def get_current_company_id(x_company_id: Optional[str] = Header(None, alias="X-Company-ID")) -> uuid.UUID:
    # ... (código existente sin cambios)
    if not x_company_id:
        log.warning("Missing required X-Company-ID header")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try:
        return uuid.UUID(x_company_id)
    except ValueError:
        log.warning("Invalid UUID format in X-Company-ID header", header_value=x_company_id)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid X-Company-ID header format")

async def get_current_user_id(x_user_id: Optional[str] = Header(None, alias="X-User-ID")) -> uuid.UUID:
    # ... (código existente sin cambios)
    if not x_user_id:
        log.warning("Missing required X-User-ID header")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-User-ID")
    try:
        return uuid.UUID(x_user_id)
    except ValueError:
        log.warning("Invalid UUID format in X-User-ID header", header_value=x_user_id)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid X-User-ID header format")

# --- Dependency for Chat Repository ---
# LLM_REFACTOR_STEP_4: Inject repository dependency (simplified)
def get_chat_repository() -> ChatRepositoryPort:
    """Provides an instance of the Chat Repository."""
    # In a real setup, this would come from a DI container configured in main.py
    return PostgresChatRepository()


# --- Endpoints Refactored to use Repository Dependency ---

@router.get(
    "/chats",
    response_model=List[schemas.ChatSummary],
    status_code=status.HTTP_200_OK,
    summary="List User Chats",
    description="Retrieves a list of chat summaries using X-Company-ID and X-User-ID headers.",
)
async def list_chats(
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    # LLM_REFACTOR_STEP_4: Inject repository
    chat_repo: ChatRepositoryPort = Depends(get_chat_repository),
    request: Request = None
):
    request_id = request.headers.get("x-request-id") if request else str(uuid.uuid4())
    endpoint_log = log.bind(request_id=request_id, user_id=str(user_id), company_id=str(company_id), limit=limit, offset=offset)
    endpoint_log.info("Request received to list chats")

    try:
        # LLM_REFACTOR_STEP_4: Use the injected repository method
        chats_domain = await chat_repo.get_user_chats(
            user_id=user_id, company_id=company_id, limit=limit, offset=offset
        )
        endpoint_log.info("Chats listed successfully", count=len(chats_domain))
        # Map domain to schema if necessary (ChatSummary is compatible for now)
        return [schemas.ChatSummary(**chat.model_dump()) for chat in chats_domain]
    except Exception as e:
        endpoint_log.exception("Error listing chats")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve chat list.")


@router.get(
    "/chats/{chat_id}/messages",
    response_model=List[schemas.ChatMessage],
    status_code=status.HTTP_200_OK,
    summary="Get Chat Messages",
    description="Retrieves messages for a specific chat using X-Company-ID and X-User-ID headers.",
    responses={403: {"description": "Chat not found or access denied."}} # Changed 404 to 403 as check_ownership returns false
)
async def get_chat_messages_endpoint(
    chat_id: uuid.UUID = Path(..., description="The ID of the chat."),
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    # LLM_REFACTOR_STEP_4: Inject repository
    chat_repo: ChatRepositoryPort = Depends(get_chat_repository),
    request: Request = None
):
    request_id = request.headers.get("x-request-id") if request else str(uuid.uuid4())
    endpoint_log = log.bind(request_id=request_id, user_id=str(user_id), company_id=str(company_id), chat_id=str(chat_id), limit=limit, offset=offset)
    endpoint_log.info("Request received to get chat messages")

    try:
        # LLM_REFACTOR_STEP_4: Use the injected repository method
        # The repo method already includes the ownership check
        messages_domain = await chat_repo.get_chat_messages(
            chat_id=chat_id, user_id=user_id, company_id=company_id, limit=limit, offset=offset
        )
        # If ownership check failed inside repo, it returns empty list, no exception needed here.
        endpoint_log.info("Chat messages retrieved successfully", count=len(messages_domain))
        # Map domain to schema if necessary (ChatMessage is compatible for now)
        return [schemas.ChatMessage(**msg.model_dump()) for msg in messages_domain]
    except Exception as e:
        # Catch potential DB errors from the repository
        endpoint_log.exception("Error getting chat messages")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve chat messages.")


@router.delete(
    "/chats/{chat_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Chat",
    description="Deletes a chat and associated data using X-Company-ID and X-User-ID headers.",
    responses={404: {"description": "Chat not found or access denied."}, 204: {}} # 404 if ownership fails
)
async def delete_chat_endpoint(
    chat_id: uuid.UUID = Path(..., description="The ID of the chat to delete."),
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
    # LLM_REFACTOR_STEP_4: Inject repository
    chat_repo: ChatRepositoryPort = Depends(get_chat_repository),
    request: Request = None
):
    request_id = request.headers.get("x-request-id") if request else str(uuid.uuid4())
    endpoint_log = log.bind(request_id=request_id, user_id=str(user_id), company_id=str(company_id), chat_id=str(chat_id))
    endpoint_log.info("Request received to delete chat")

    try:
        # LLM_REFACTOR_STEP_4: Use the injected repository method
        # The repo method includes ownership check and returns bool
        deleted = await chat_repo.delete_chat(chat_id=chat_id, user_id=user_id, company_id=company_id)
        if deleted:
            endpoint_log.info("Chat deleted successfully")
            # Return 204 No Content requires a Response object
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        else:
            # If not deleted, it means ownership check failed or chat didn't exist
            endpoint_log.warning("Chat not found or access denied for deletion")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found or access denied.")
    except Exception as e:
        # Catch potential DB errors from the repository
        endpoint_log.exception("Error deleting chat")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to delete chat.")