# query-service/app/api/v1/endpoints/query.py
import uuid
from typing import Dict, Any, Optional, List
import structlog
import asyncio
import re

from fastapi import APIRouter, Depends, HTTPException, status, Header, Body, Request

from app.api.v1 import schemas
from app.core.config import settings
# LLM_REFACTOR_STEP_3: Import use case and dependencies for injection (example)
from app.application.use_cases.ask_query_use_case import AskQueryUseCase
from app.infrastructure.persistence.postgres_repositories import (
    PostgresChatRepository, PostgresLogRepository, PostgresChunkContentRepository
)
from app.infrastructure.vectorstores.milvus_adapter import MilvusAdapter
from app.infrastructure.llms.gemini_adapter import GeminiAdapter
# LLM_REFACTOR_STEP_3: Import domain model for mapping
from app.domain.models import RetrievedChunk

from app.utils.helpers import truncate_text
from .chat import get_current_company_id, get_current_user_id

log = structlog.get_logger(__name__)

router = APIRouter()

GREETING_REGEX = re.compile(r"^\s*(hola|hello|hi|buenos días|buenas tardes|buenas noches|hey|qué tal|hi there)\s*[\.,!?]*\s*$", re.IGNORECASE)


# --- Dependency Injection Setup (Simplified Example) ---
# LLM_REFACTOR_STEP_3: In a real app, use a framework like FastAPI Injector or manual setup in main.py
# For now, we instantiate dependencies directly here for simplicity.
def get_ask_query_use_case() -> AskQueryUseCase:
    # Instantiate concrete implementations
    chat_repo = PostgresChatRepository()
    log_repo = PostgresLogRepository()
    vector_store = MilvusAdapter()
    llm_adapter = GeminiAdapter()
    chunk_content_repo = PostgresChunkContentRepository() # Needed for future BM25

    # Instantiate the use case with concrete dependencies
    # For optional ones (BM25, Reranker, Filter), pass None for now
    use_case = AskQueryUseCase(
        chat_repo=chat_repo,
        log_repo=log_repo,
        vector_store=vector_store,
        llm=llm_adapter,
        sparse_retriever=None, # Not implemented yet
        chunk_content_repo=chunk_content_repo, # Pass if needed by sparse retriever
        reranker=None,         # Not implemented yet
        diversity_filter=None  # Not implemented yet
    )
    return use_case

# --- Endpoint Refactored to use AskQueryUseCase ---
@router.post(
    "/ask",
    response_model=schemas.QueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Process Query / Manage Chat",
    description="Handles user queries via RAG pipeline or simple greeting, manages chat state.",
)
async def process_query(
    request_body: schemas.QueryRequest = Body(...),
    company_id: uuid.UUID = Depends(get_current_company_id),
    user_id: uuid.UUID = Depends(get_current_user_id),
    # LLM_REFACTOR_STEP_3: Inject the use case instance
    use_case: AskQueryUseCase = Depends(get_ask_query_use_case),
    request: Request = None # Keep for request ID
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    endpoint_log = log.bind(
        request_id=request_id,
        company_id=str(company_id),
        user_id=str(user_id),
        query=truncate_text(request_body.query, 100),
        provided_chat_id=str(request_body.chat_id) if request_body.chat_id else "None"
    )
    endpoint_log.info("Processing query request via Use Case")

    try:
        # LLM_REFACTOR_STEP_3: Call the use case execute method
        answer, retrieved_chunks_domain, log_id, final_chat_id = await use_case.execute(
            query=request_body.query,
            company_id=company_id,
            user_id=user_id,
            chat_id=request_body.chat_id,
            top_k=request_body.retriever_top_k
        )

        # LLM_REFACTOR_STEP_3: Map domain results (RetrievedChunk) to API schema (RetrievedDocument)
        retrieved_docs_api = [
            schemas.RetrievedDocument(
                id=chunk.id,
                score=chunk.score,
                content_preview=truncate_text(chunk.content, 150) if chunk.content else None,
                metadata=chunk.metadata,
                document_id=chunk.document_id,
                file_name=chunk.file_name
            ) for chunk in retrieved_chunks_domain
        ]

        endpoint_log.info("Use case executed successfully, returning response", num_retrieved=len(retrieved_docs_api))
        return schemas.QueryResponse(
            answer=answer,
            retrieved_documents=retrieved_docs_api,
            query_log_id=log_id,
            chat_id=final_chat_id
        )

    # Keep specific error handling, Use Case should raise appropriate exceptions
    except HTTPException as http_exc:
        # Re-raise HTTP exceptions directly (like 403, 400 from UseCase)
        raise http_exc
    except ConnectionError as ce:
        # Catch connection errors raised by adapters via UseCase
        endpoint_log.error("Dependency connection error reported by Use Case", error=str(ce), exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"A required service is unavailable.")
    except Exception as e:
        # Catch unexpected errors from UseCase
        endpoint_log.exception("Unhandled exception during use case execution")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An internal error occurred.")