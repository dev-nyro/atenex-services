# query-service/app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status, Request, Depends
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse, Response, PlainTextResponse
import structlog
import uvicorn
import logging
import sys
import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Optional # Explicit type hint for Optionals

# Configurar logging primero
from app.core.config import settings
from app.core.logging_config import setup_logging
setup_logging()
log = structlog.get_logger("query_service.main")

# Import Routers
from app.api.v1.endpoints import query as query_router_module
from app.api.v1.endpoints import chat as chat_router_module

# Import Ports and Adapters/Repositories for Dependency Injection
from app.application.ports import (
    ChatRepositoryPort, LogRepositoryPort, VectorStorePort, LLMPort,
    SparseRetrieverPort, RerankerPort, DiversityFilterPort, ChunkContentRepositoryPort
)
from app.infrastructure.persistence.postgres_repositories import (
    PostgresChatRepository, PostgresLogRepository, PostgresChunkContentRepository
)
from app.infrastructure.vectorstores.milvus_adapter import MilvusAdapter
from app.infrastructure.llms.gemini_adapter import GeminiAdapter
from app.infrastructure.retrievers.bm25_retriever import BM25sRetriever
from app.infrastructure.rerankers.bge_reranker import BGEReranker
# --- CORRECTION: Import both MMR and Stub Filters ---
from app.infrastructure.filters.diversity_filter import MMRDiversityFilter, StubDiversityFilter
# --- END CORRECTION ---

# Import Use Case
from app.application.use_cases.ask_query_use_case import AskQueryUseCase

# Import DB Connector
from app.infrastructure.persistence import postgres_connector

# Global state
SERVICE_READY = False
# Global instances for simplified DI (replace with proper container if needed)
chat_repo_instance: Optional[ChatRepositoryPort] = None
log_repo_instance: Optional[LogRepositoryPort] = None
chunk_content_repo_instance: Optional[ChunkContentRepositoryPort] = None
vector_store_instance: Optional[VectorStorePort] = None
llm_instance: Optional[LLMPort] = None
sparse_retriever_instance: Optional[SparseRetrieverPort] = None
reranker_instance: Optional[RerankerPort] = None
diversity_filter_instance: Optional[DiversityFilterPort] = None # Will hold either MMR or Stub
ask_query_use_case_instance: Optional[AskQueryUseCase] = None


# --- Lifespan Manager ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global SERVICE_READY, chat_repo_instance, log_repo_instance, chunk_content_repo_instance, \
           vector_store_instance, llm_instance, sparse_retriever_instance, reranker_instance, \
           diversity_filter_instance, ask_query_use_case_instance

    log.info(f"Starting up {settings.PROJECT_NAME}...")
    db_pool_initialized = False
    dependencies_ok = True # Assume OK until proven otherwise

    # 1. Initialize DB Pool
    try:
        await postgres_connector.get_db_pool()
        db_ready = await postgres_connector.check_db_connection()
        if db_ready:
            log.info("PostgreSQL connection pool initialized and verified.")
            db_pool_initialized = True
            chat_repo_instance = PostgresChatRepository()
            log_repo_instance = PostgresLogRepository()
            chunk_content_repo_instance = PostgresChunkContentRepository()
        else:
            log.critical("CRITICAL: Failed PostgreSQL connection verification during startup.")
            dependencies_ok = False
    except Exception as e:
        log.critical("CRITICAL: Failed PostgreSQL pool initialization.", error=str(e), exc_info=True)
        dependencies_ok = False

    # 2. Initialize other adapters
    if dependencies_ok:
        try:
            vector_store_instance = MilvusAdapter()
            await vector_store_instance._get_collection()
            log.info("Milvus Adapter initialized and collection checked/loaded.")
        except Exception as e:
            log.critical("CRITICAL: Failed to initialize Milvus Adapter or load collection.", error=str(e), exc_info=True)
            dependencies_ok = False

    if dependencies_ok:
        try:
            llm_instance = GeminiAdapter()
            if not llm_instance.model:
                 log.critical("CRITICAL: Gemini Adapter initialized but model failed to load (check API key).")
                 log.warning("Continuing startup despite Gemini model load failure.")
            else:
                 log.info("Gemini Adapter initialized successfully.")
        except Exception as e:
            log.critical("CRITICAL: Failed to initialize Gemini Adapter.", error=str(e), exc_info=True)
            dependencies_ok = False

    # Initialize optional components
    if dependencies_ok and settings.BM25_ENABLED:
        try:
            if chunk_content_repo_instance:
                 sparse_retriever_instance = BM25sRetriever(chunk_content_repo_instance)
                 log.info("BM25s Retriever initialized.")
            else:
                 log.error("BM25 enabled but ChunkContentRepository failed to initialize. Disabling BM25.")
                 sparse_retriever_instance = None
        except ImportError:
            log.error("BM25sRetriever dependency (bm2s) not installed. BM25 disabled.")
            sparse_retriever_instance = None
        except Exception as e:
            log.error("Failed to initialize BM25s Retriever.", error=str(e), exc_info=True)
            sparse_retriever_instance = None

    if dependencies_ok and settings.RERANKER_ENABLED:
        try:
            reranker_instance = BGEReranker()
            if not reranker_instance.model:
                log.warning("BGE Reranker initialized but model loading failed. Reranking might not work.")
            else:
                 log.info("BGE Reranker initialized.")
        except ImportError:
            log.error("BGEReranker dependency (sentence-transformers) not installed. Reranker disabled.")
            reranker_instance = None
        except Exception as e:
            log.error("Failed to initialize BGE Reranker.", error=str(e), exc_info=True)
            reranker_instance = None

    # --- CORRECTION: Initialize Diversity Filter based on setting ---
    if dependencies_ok and settings.DIVERSITY_FILTER_ENABLED:
        try:
            diversity_filter_instance = MMRDiversityFilter(lambda_mult=settings.QUERY_DIVERSITY_LAMBDA)
            log.info("MMR Diversity Filter initialized.")
        except Exception as e:
            log.error("Failed to initialize MMR Diversity Filter. Falling back to Stub.", error=str(e), exc_info=True)
            # Fallback to Stub if MMR fails
            diversity_filter_instance = StubDiversityFilter()
    else:
        # If disabled, explicitly set to None or Stub (depending on desired behavior if accidentally called)
        # Using Stub makes the AskQueryUseCase logic simpler as it doesn't need to check for None
        log.info("Diversity filter disabled, using StubDiversityFilter as placeholder.")
        diversity_filter_instance = StubDiversityFilter()
    # --- END CORRECTION ---


    # 3. Instantiate Use Case
    if dependencies_ok and chat_repo_instance and log_repo_instance and vector_store_instance and llm_instance:
         try:
             ask_query_use_case_instance = AskQueryUseCase(
                 chat_repo=chat_repo_instance,
                 log_repo=log_repo_instance,
                 vector_store=vector_store_instance,
                 llm=llm_instance,
                 sparse_retriever=sparse_retriever_instance,
                 chunk_content_repo=chunk_content_repo_instance,
                 reranker=reranker_instance,
                 diversity_filter=diversity_filter_instance # Pass the initialized filter (MMR or Stub)
             )
             log.info("AskQueryUseCase instantiated successfully.")
             SERVICE_READY = True
             log.info(f"{settings.PROJECT_NAME} service components initialized. SERVICE READY.")
         except Exception as e:
              log.critical("CRITICAL: Failed to instantiate AskQueryUseCase.", error=str(e), exc_info=True)
              SERVICE_READY = False
    else:
        log.critical(f"{settings.PROJECT_NAME} startup finished. Some critical dependencies failed. SERVICE NOT READY.")
        SERVICE_READY = False


    yield # Application runs here

    # --- Shutdown ---
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    await postgres_connector.close_db_pool()
    if vector_store_instance and hasattr(vector_store_instance, 'disconnect'):
        await vector_store_instance.disconnect()
    log.info("Shutdown complete.")

# --- FastAPI App Initialization ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.3.0", # Final Version
    description="Microservice to handle user queries using a refactored RAG pipeline and chat history.",
    lifespan=lifespan
)

# --- Middleware ---
@app.middleware("http")
async def add_request_id_timing_logging(request: Request, call_next):
    start_time = asyncio.get_event_loop().time()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    structlog.contextvars.bind_contextvars(request_id=request_id)
    req_log = log.bind(method=request.method, path=str(request.url.path), client=request.client.host if request.client else "unknown")
    req_log.info("Request received")
    response = None
    try:
        response = await call_next(request)
        process_time = (asyncio.get_event_loop().time() - start_time) * 1000
        resp_log = req_log.bind(status_code=response.status_code, duration_ms=round(process_time, 2))
        log_level = "warning" if 400 <= response.status_code < 500 else "error" if response.status_code >= 500 else "info"
        getattr(resp_log, log_level)("Request finished")
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time:.2f}"
    except Exception as e:
        process_time = (asyncio.get_event_loop().time() - start_time) * 1000
        exc_log = req_log.bind(status_code=500, duration_ms=round(process_time, 2))
        exc_log.exception("Unhandled exception during request processing")
        response = JSONResponse(status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "Internal Server Error"})
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time:.2f}"
    finally: structlog.contextvars.clear_contextvars()
    return response

# --- Exception Handlers ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    log_level = log.warning if exc.status_code < 500 else log.error
    log_level("HTTP Exception caught", status_code=exc.status_code, detail=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    error_details = []
    try: error_details = exc.errors()
    except Exception: error_details = [{"loc": [], "msg": "Failed to parse validation errors.", "type": "internal_parsing_error"}]
    log.warning("Request Validation Error", errors=error_details)
    return JSONResponse(status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": error_details})

@app.exception_handler(ResponseValidationError)
async def response_validation_exception_handler(request: Request, exc: ResponseValidationError):
    log.error("Response Validation Error", errors=exc.errors(), exc_info=True)
    return JSONResponse(status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "Internal Server Error: Failed to serialize response."})

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled Exception caught by generic handler")
    return JSONResponse(status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "Internal Server Error"})


# --- Simplified Dependency Injection Functions ---
def get_chat_repository() -> ChatRepositoryPort:
    if not chat_repo_instance: raise HTTPException(status_code=503, detail="Chat Repository not available")
    return chat_repo_instance

def get_ask_query_use_case() -> AskQueryUseCase:
    if not ask_query_use_case_instance: raise HTTPException(status_code=503, detail="Ask Query Use Case not available")
    return ask_query_use_case_instance

# --- Routers ---
app.include_router(query_router_module.router, prefix=settings.API_V1_STR, tags=["Query Interaction"])
app.include_router(chat_router_module.router, prefix=settings.API_V1_STR, tags=["Chat Management"])
log.info("Routers included", prefix=settings.API_V1_STR)

# --- Root Endpoint / Health Check ---
@app.get("/", tags=["Health Check"], summary="Service Liveness/Readiness Check")
async def read_root():
    health_log = log.bind(check="liveness_readiness")
    if not SERVICE_READY:
        health_log.warning("Health check failed: Service not ready.")
        raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service Not Ready")

    health_log.debug("Health check passed.")
    return PlainTextResponse("OK", status_code=fastapi_status.HTTP_200_OK)

# --- Main execution ---
if __name__ == "__main__":
    port = 8002
    log_level_str = settings.LOG_LEVEL.lower()
    print(f"----- Starting {settings.PROJECT_NAME} locally on port {port} -----")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True, log_level=log_level_str)