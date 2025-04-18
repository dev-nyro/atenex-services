# query-service/app/api/v1/endpoints/query.py
import uuid
from typing import Dict, Any, Optional, List
import structlog
import asyncio
import re # LLM_COMMENT: Import regex for greeting detection

from fastapi import APIRouter, Depends, HTTPException, status, Header, Body, Request

from app.api.v1 import schemas
from app.core.config import settings
from app.db import postgres_client
# LLM_COMMENT: Import the refactored pipeline function
from app.pipelines.rag_pipeline import run_rag_pipeline
from haystack import Document # LLM_COMMENT: Keep Haystack Document import
from app.utils.helpers import truncate_text
# LLM_COMMENT: Import shared dependencies from chat module
from .chat import get_current_company_id, get_current_user_id

log = structlog.get_logger(__name__)

router = APIRouter()

# LLM_COMMENT: Simple regex to detect greetings (adapt as needed)
GREETING_REGEX = re.compile(r"^\s*(hola|hello|hi|buenos días|buenas tardes|buenas noches|hey|qué tal|hi there)\s*[\.,!?]*\s*$", re.IGNORECASE)

# --- Endpoint Principal Modificado para usar dependencias de X- Headers ---
@router.post(
    "/ask", # LLM_COMMENT: Standardized internal endpoint path
    response_model=schemas.QueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Process Query / Manage Chat", # LLM_COMMENT: Updated summary
    description="Handles user queries, manages chat state (create/continue), performs RAG or simple response, and logs interaction. Uses X-Company-ID and X-User-ID.", # LLM_COMMENT: Updated description
)
async def process_query(
    request_body: schemas.QueryRequest = Body(...),
    # LLM_COMMENT: Use dependencies to get authenticated user/company IDs
    company_id: uuid.UUID = Depends(get_current_company_id),
    user_id: uuid.UUID = Depends(get_current_user_id),
    request: Request = None # LLM_COMMENT: Inject Request for potential header access
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    endpoint_log = log.bind(
        request_id=request_id,
        company_id=str(company_id),
        user_id=str(user_id),
        # LLM_COMMENT: Log truncated query for brevity
        query=truncate_text(request_body.query, 100),
        provided_chat_id=str(request_body.chat_id) if request_body.chat_id else "None"
    )
    endpoint_log.info("Processing query request")

    current_chat_id: uuid.UUID
    is_new_chat = False

    try:
        # --- 1. Determine Chat ID ---
        # LLM_COMMENT: Check if continuing an existing chat or starting a new one
        if request_body.chat_id:
            endpoint_log.debug("Checking ownership of existing chat", chat_id=str(request_body.chat_id))
            # LLM_COMMENT: Verify user owns the chat using DB check
            if not await postgres_client.check_chat_ownership(request_body.chat_id, user_id, company_id):
                 endpoint_log.warning("Access denied or chat not found for provided chat_id")
                 raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chat not found or access denied.")
            current_chat_id = request_body.chat_id
            endpoint_log = endpoint_log.bind(chat_id=str(current_chat_id))
            endpoint_log.info("Continuing existing chat")
        else:
            endpoint_log.info("No chat_id provided, creating new chat")
            # LLM_COMMENT: Generate a title based on the first query, truncated
            initial_title = f"Chat: {truncate_text(request_body.query, 40)}"
            current_chat_id = await postgres_client.create_chat(user_id=user_id, company_id=company_id, title=initial_title)
            is_new_chat = True
            endpoint_log = endpoint_log.bind(chat_id=str(current_chat_id))
            endpoint_log.info("New chat created", title=initial_title)

        # --- 2. Save User Message ---
        # LLM_COMMENT: Always save the user's message regardless of intent
        endpoint_log.info("Saving user message to DB")
        await postgres_client.save_message(
            chat_id=current_chat_id, role='user', content=request_body.query
        )
        endpoint_log.info("User message saved")

        # --- 3. Handle Greetings / Simple Intents (Bypass RAG) ---
        # LLM_COMMENT: Check if the query matches a simple greeting pattern
        if GREETING_REGEX.match(request_body.query):
            endpoint_log.info("Greeting detected, bypassing RAG pipeline")
            # LLM_COMMENT: Provide a canned, friendly response for greetings
            answer = "¡Hola! ¿En qué puedo ayudarte hoy con la información de tus documentos?"
            retrieved_docs_for_response: List[schemas.RetrievedDocument] = [] # LLM_COMMENT: No documents retrieved for greetings
            log_id = None # LLM_COMMENT: Optionally skip logging greetings, or log differently

            # Save canned assistant response
            await postgres_client.save_message(
                 chat_id=current_chat_id, role='assistant', content=answer, sources=None
            )
            endpoint_log.info("Saved canned greeting response")

        # --- 4. Execute RAG Pipeline (for non-greetings) ---
        else:
            endpoint_log.info("Proceeding with RAG pipeline execution")
            # LLM_COMMENT: Call the refactored pipeline function
            answer, retrieved_docs_haystack, log_id = await run_rag_pipeline(
                query=request_body.query,
                company_id=str(company_id),
                user_id=str(user_id),
                top_k=request_body.retriever_top_k, # LLM_COMMENT: Pass top_k if provided
                chat_id=current_chat_id # LLM_COMMENT: Pass chat_id for logging context
            )
            endpoint_log.info("RAG pipeline finished")

            # LLM_COMMENT: Format retrieved Haystack Documents into response schema
            retrieved_docs_for_response = [schemas.RetrievedDocument.from_haystack_doc(doc)
                                           for doc in retrieved_docs_haystack]

            # LLM_COMMENT: Prepare sources list specifically for saving in the assistant message
            assistant_sources_for_db: List[Dict[str, Any]] = []
            for schema_doc in retrieved_docs_for_response:
                source_info = {
                    "chunk_id": schema_doc.id,
                    "document_id": schema_doc.document_id,
                    "file_name": schema_doc.file_name,
                    "score": schema_doc.score,
                    "preview": schema_doc.content_preview # LLM_COMMENT: Include preview in stored sources
                }
                assistant_sources_for_db.append(source_info)

            # LLM_COMMENT: Save the actual assistant message generated by the pipeline
            endpoint_log.info("Saving assistant message from pipeline")
            await postgres_client.save_message(
                chat_id=current_chat_id,
                role='assistant',
                content=answer,
                sources=assistant_sources_for_db if assistant_sources_for_db else None # LLM_COMMENT: Store formatted sources
            )
            endpoint_log.info("Assistant message saved")

        # --- 5. Return Response ---
        endpoint_log.info("Query processed successfully, returning response", is_new_chat=is_new_chat, num_retrieved=len(retrieved_docs_for_response))
        return schemas.QueryResponse(
            answer=answer,
            retrieved_documents=retrieved_docs_for_response, # LLM_COMMENT: Return formatted documents
            query_log_id=log_id, # LLM_COMMENT: Include the log_id if logging was successful
            chat_id=current_chat_id # LLM_COMMENT: Always return the current chat_id
        )

    # LLM_COMMENT: Keep existing robust error handling, map pipeline errors if needed
    except ValueError as ve:
        endpoint_log.warning("Input validation error", error=str(ve), exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except ConnectionError as ce: # LLM_COMMENT: Catch ConnectionError from pipeline steps
        endpoint_log.error("Dependency connection error", error=str(ce), exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"A required service is unavailable: {ce}")
    except HTTPException as http_exc:
        # LLM_COMMENT: Re-raise specific HTTP exceptions (like 403 from check_chat_ownership)
        raise http_exc
    except Exception as e:
        endpoint_log.exception("Unhandled exception during query processing")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An internal error occurred: {type(e).__name__}")