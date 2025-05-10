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
from typing import Annotated, Optional
import httpx

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
    SparseRetrieverPort, DiversityFilterPort, ChunkContentRepositoryPort,
    EmbeddingPort # RerankerPort not directly used here anymore for local reranker
)
from app.infrastructure.persistence.postgres_repositories import (
    PostgresChatRepository, PostgresLogRepository, PostgresChunkContentRepository
)
from app.infrastructure.vectorstores.milvus_adapter import MilvusAdapter
from app.infrastructure.llms.gemini_adapter import GeminiAdapter
from app.infrastructure.retrievers.bm25_retriever import BM25sRetriever
from app.infrastructure.filters.diversity_filter import MMRDiversityFilter, StubDiversityFilter
from app.infrastructure.clients.embedding_service_client import EmbeddingServiceClient
from app.infrastructure.embedding.remote_embedding_adapter import RemoteEmbeddingAdapter


from app.application.use_cases.ask_query_use_case import AskQueryUseCase
from app.dependencies import set_ask_query_use_case_instance

from app.infrastructure.persistence import postgres_connector

# Global state
SERVICE_READY = False
# Global instances for simplified DI
chat_repo_instance: Optional[ChatRepositoryPort] = None
log_repo_instance: Optional[LogRepositoryPort] = None
chunk_content_repo_instance: Optional[ChunkContentRepositoryPort] = None
vector_store_instance: Optional[VectorStorePort] = None
llm_instance: Optional[LLMPort] = None
sparse_retriever_instance: Optional[SparseRetrieverPort] = None
diversity_filter_instance: Optional[DiversityFilterPort] = None
embedding_service_client_instance: Optional[EmbeddingServiceClient] = None
embedding_adapter_instance: Optional[EmbeddingPort] = None
ask_query_use_case_instance: Optional[AskQueryUseCase] = None
http_client_instance: Optional[httpx.AsyncClient] = None


# --- Lifespan Manager ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global SERVICE_READY, chat_repo_instance, log_repo_instance, chunk_content_repo_instance, \
           vector_store_instance, llm_instance, sparse_retriever_instance, \
           diversity_filter_instance, ask_query_use_case_instance, \
           embedding_service_client_instance, embedding_adapter_instance, http_client_instance

    SERVICE_READY = False
    log.info(f"Starting up {settings.PROJECT_NAME}...")
    dependencies_ok = True
    critical_failure_message = ""

    # 0. Initialize Global HTTP Client
    try:
        http_client_instance = httpx.AsyncClient(
            timeout=settings.HTTP_CLIENT_TIMEOUT,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20) # Sensible defaults
        )
        log.info("Global HTTP client initialized.")
    except Exception as e_http_client:
        critical_failure_message = "Failed to initialize global HTTP client."
        log.critical(f"CRITICAL: {critical_failure_message}", error=str(e_http_client), exc_info=True)
        dependencies_ok = False


    # 1. Initialize DB Pool
    if dependencies_ok:
        try:
            await postgres_connector.get_db_pool()
            db_ready = await postgres_connector.check_db_connection()
            if db_ready:
                log.info("PostgreSQL connection pool initialized and verified.")
                chat_repo_instance = PostgresChatRepository()
                log_repo_instance = PostgresLogRepository()
                chunk_content_repo_instance = PostgresChunkContentRepository()
            else:
                critical_failure_message = "Failed PostgreSQL connection verification during startup."
                log.critical(f"CRITICAL: {critical_failure_message}")
                dependencies_ok = False
        except Exception as e_pg:
            critical_failure_message = "Failed PostgreSQL pool initialization."
            log.critical(f"CRITICAL: {critical_failure_message}", error=str(e_pg), exc_info=True)
            dependencies_ok = False

    # 2. Initialize Embedding Service Client & Adapter
    if dependencies_ok:
        try:
            # EmbeddingServiceClient creates its own internal httpx.AsyncClient
            # but we could pass the global one if we refactor EmbeddingServiceClient.
            # For now, it's fine as is.
            embedding_service_client_instance = EmbeddingServiceClient(
                base_url=str(settings.EMBEDDING_SERVICE_URL),
                timeout=settings.EMBEDDING_CLIENT_TIMEOUT # Specific timeout for embedding client
            )
            embedding_adapter_instance = RemoteEmbeddingAdapter(client=embedding_service_client_instance)
            await embedding_adapter_instance.initialize() # Tries to get dimension
            
            emb_service_healthy = await embedding_adapter_instance.health_check()
            if emb_service_healthy:
                log.info("Embedding Service client and adapter initialized, health check passed.")
            else:
                critical_failure_message = "Embedding Service health check failed during startup."
                log.critical(f"CRITICAL: {critical_failure_message} URL: {settings.EMBEDDING_SERVICE_URL}")
                dependencies_ok = False
        except Exception as e_embed:
            critical_failure_message = "Failed to initialize Embedding Service client/adapter."
            log.critical(f"CRITICAL: {critical_failure_message}", error=str(e_embed), exc_info=True, url=settings.EMBEDDING_SERVICE_URL)
            dependencies_ok = False


    # 3. Initialize Milvus Adapter
    if dependencies_ok:
        try:
            vector_store_instance = MilvusAdapter()
            await vector_store_instance.connect() # Explicitly connect and load collection
            log.info("Milvus Adapter initialized and collection checked/loaded.")
        except Exception as e_milvus:
            critical_failure_message = "Failed to initialize Milvus Adapter or load collection."
            log.critical(
                f"CRITICAL: {critical_failure_message} Ensure collection '{settings.MILVUS_COLLECTION_NAME}' exists and is accessible.",
                error=str(e_milvus), exc_info=True, adapter_error=getattr(e_milvus, 'message', 'N/A')
            )
            dependencies_ok = False

    # 4. Initialize LLM Adapter
    if dependencies_ok:
        try:
            llm_instance = GeminiAdapter()
            if not llm_instance.model:
                 critical_failure_message = "Gemini Adapter initialized but model failed to load (check API key)."
                 log.critical(f"CRITICAL: {critical_failure_message}")
                 dependencies_ok = False
            else:
                 log.info("Gemini Adapter initialized successfully.")
        except Exception as e_llm:
            critical_failure_message = "Failed to initialize Gemini Adapter."
            log.critical(f"CRITICAL: {critical_failure_message}", error=str(e_llm), exc_info=True)
            dependencies_ok = False
    
    # Initialize optional components
    if dependencies_ok:
        if settings.BM25_ENABLED:
            try:
                if chunk_content_repo_instance: # Make sure repo is available
                    sparse_retriever_instance = BM25sRetriever(chunk_content_repo_instance)
                    log.info("BM25s Retriever initialized.")
                else:
                    log.error("BM25 enabled but ChunkContentRepository failed to initialize. Disabling BM25 for this session.")
                    sparse_retriever_instance = None # Ensure it's None
            except ImportError:
                log.error("BM25sRetriever dependency (bm2s) not installed. BM25 disabled.")
                sparse_retriever_instance = None
            except Exception as e_bm25:
                log.error("Failed to initialize BM25s Retriever. BM25 will be disabled.", error=str(e_bm25), exc_info=True)
                sparse_retriever_instance = None


        if settings.DIVERSITY_FILTER_ENABLED:
            try:
                if embedding_adapter_instance and embedding_adapter_instance.get_embedding_dimension() > 0 : # MMR needs embeddings
                    diversity_filter_instance = MMRDiversityFilter(lambda_mult=settings.QUERY_DIVERSITY_LAMBDA)
                    log.info("MMR Diversity Filter initialized.")
                else:
                    log.warning("MMR Diversity Filter enabled but embedding adapter is not available or has no dimension. Falling back to StubDiversityFilter.")
                    diversity_filter_instance = StubDiversityFilter()
            except Exception as e_diversity:
                log.error("Failed to initialize MMR Diversity Filter. Falling back to StubDiversityFilter.", error=str(e_diversity), exc_info=True)
                diversity_filter_instance = StubDiversityFilter()
        else: # If not enabled, use Stub
            log.info("Diversity filter disabled in settings, using StubDiversityFilter as placeholder.")
            diversity_filter_instance = StubDiversityFilter()

    # 5. Instantiate Use Case
    if dependencies_ok:
         try:
             if not http_client_instance: # Should not happen if dependencies_ok is True
                 raise RuntimeError("HTTP client instance is not available for AskQueryUseCase.")

             ask_query_use_case_instance = AskQueryUseCase(
                 chat_repo=chat_repo_instance,
                 log_repo=log_repo_instance,
                 vector_store=vector_store_instance,
                 llm=llm_instance,
                 embedding_adapter=embedding_adapter_instance,
                 http_client=http_client_instance, # Pass the global HTTP client
                 sparse_retriever=sparse_retriever_instance,
                 chunk_content_repo=chunk_content_repo_instance,
                 diversity_filter=diversity_filter_instance
             )
             log.info("AskQueryUseCase instantiated successfully.")
             SERVICE_READY = True # Mark service as ready only if all critical components are up
             set_ask_query_use_case_instance(ask_query_use_case_instance, SERVICE_READY)
             log.info(f"{settings.PROJECT_NAME} service components initialized. SERVICE READY.")

         except Exception as e_usecase:
              critical_failure_message = "Failed to instantiate AskQueryUseCase."
              log.critical(f"CRITICAL: {critical_failure_message}", error=str(e_usecase), exc_info=True)
              SERVICE_READY = False
              set_ask_query_use_case_instance(None, False) # Ensure it's set to not ready
    else: # If dependencies_ok was false earlier
        log.critical(f"{settings.PROJECT_NAME} startup sequence aborted due to critical failure: {critical_failure_message}")
        log.critical("SERVICE NOT READY.")
        set_ask_query_use_case_instance(None, False)


    if not SERVICE_READY: # Double check the flag before concluding startup
        if not critical_failure_message: critical_failure_message = "Unknown critical dependency failure during startup."
        log.critical(f"Startup finished. Critical failure detected: {critical_failure_message}. SERVICE NOT READY.")


    yield # Application runs here

    # --- Shutdown Logic ---
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    if http_client_instance:
        await http_client_instance.aclose()
        log.info("Global HTTP client closed.")
    await postgres_connector.close_db_pool()
    if vector_store_instance and hasattr(vector_store_instance, 'disconnect'):
        try: await vector_store_instance.disconnect()
        except Exception as e_milvus_close: log.error("Error during Milvus disconnect", error=str(e_milvus_close), exc_info=True)
    if embedding_service_client_instance: # This client has its own httpx client internally
        try: await embedding_service_client_instance.close()
        except Exception as e_emb_client_close: log.error("Error closing EmbeddingServiceClient", error=str(e_emb_client_close), exc_info=True)
    log.info("Shutdown complete.")

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.3.2",
    description="Microservice to handle user queries using RAG pipeline, chat history, remote embedding, and remote reranking.",
    lifespan=lifespan
)

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
        log_level_method = "warning" if 400 <= response.status_code < 500 else "error" if response.status_code >= 500 else "info"
        getattr(resp_log, log_level_method)("Request finished")
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time:.2f}"
    except Exception as e_middleware:
        process_time = (asyncio.get_event_loop().time() - start_time) * 1000
        exc_log = req_log.bind(status_code=500, duration_ms=round(process_time, 2))
        exc_log.exception("Unhandled exception during request processing middleware") # Log with full traceback
        response = JSONResponse(status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "Internal Server Error"})
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time:.2f}"
    finally: structlog.contextvars.clear_contextvars()
    return response

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    log_level_method = log.warning if exc.status_code < 500 else log.error
    log_level_method("HTTP Exception caught by handler", status_code=exc.status_code, detail=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    error_details_val = []
    try: error_details_val = exc.errors()
    except Exception: error_details_val = [{"loc": [], "msg": "Failed to parse validation errors.", "type": "internal_parsing_error"}]
    log.warning("Request Validation Error caught by handler", errors=error_details_val)
    return JSONResponse(status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": error_details_val})

@app.exception_handler(ResponseValidationError)
async def response_validation_exception_handler(request: Request, exc: ResponseValidationError):
    log.error("Response Validation Error caught by handler", errors=exc.errors(), exc_info=True)
    return JSONResponse(status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "Internal Server Error: Failed to serialize response."})

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled Exception caught by generic handler") # Log with full traceback
    return JSONResponse(status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "Internal Server Error"})


def get_chat_repository() -> ChatRepositoryPort:
    if not chat_repo_instance:
        log.error("Dependency Injection Failed: ChatRepository requested but not initialized.")
        raise HTTPException(status_code=503, detail="Chat service component not available.")
    return chat_repo_instance

app.include_router(query_router_module.router, prefix=settings.API_V1_STR, tags=["Query Interaction"])
app.include_router(chat_router_module.router, prefix=settings.API_V1_STR, tags=["Chat Management"])
log.info("Routers included", prefix=settings.API_V1_STR)

@app.get("/", tags=["Health Check"], summary="Service Liveness/Readiness Check")
async def read_root():
    health_log = log.bind(check="liveness_readiness_root")
    if not SERVICE_READY:
        health_log.warning("Health check (root) failed: Service not ready.", service_ready_flag=SERVICE_READY)
        raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service Not Ready")

    # Check Embedding Service Health
    if embedding_adapter_instance:
        emb_adapter_healthy = await embedding_adapter_instance.health_check()
        if not emb_adapter_healthy:
            health_log.error("Health check (root) failed: Embedding Adapter reports unhealthy dependency (Embedding Service).")
            raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Critical dependency (Embedding Service) is unhealthy.")
    else: # Should not happen if SERVICE_READY is true
        health_log.error("Health check (root) warning: Embedding Adapter instance not available, inconsistent with SERVICE_READY state.")
        raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service inconsistency: Embedding adapter missing.")

    # Check Reranker Service Health (if enabled)
    if settings.RERANKER_ENABLED:
        if not http_client_instance: # Should not happen if SERVICE_READY and http_client is critical
            health_log.error("Health check (root) warning: HTTP client instance not available for Reranker health check.")
            raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service inconsistency: HTTP client missing.")
        try:
            reranker_health_url = str(settings.RERANKER_SERVICE_URL).rstrip('/') + "/health"
            response = await http_client_instance.get(reranker_health_url, timeout=5) # Short timeout for health
            if response.status_code == 200:
                 reranker_data = response.json()
                 if reranker_data.get("status") == "ok" and reranker_data.get("model_status") == "loaded":
                     health_log.debug("Reranker service health check successful via root.")
                 else:
                     health_log.error("Health check (root) failed: Reranker service reported unhealthy.", status=response.status_code, details=reranker_data)
                     raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Critical dependency (Reranker Service) is unhealthy.")
            else:
                 health_log.error("Health check (root) failed: Reranker service unresponsive.", status=response.status_code, response_text=response.text)
                 raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Critical dependency (Reranker Service) is unresponsive.")
        except httpx.RequestError as e_rerank_health:
             health_log.error("Health check (root) failed: Error connecting to Reranker service.", error=str(e_rerank_health))
             raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Critical dependency (Reranker Service) connection failed.")
        except Exception as e_rerank_health_generic: # Catch JSONDecodeError or other issues
            health_log.error("Health check (root) failed: Error processing Reranker service health response.", error=str(e_rerank_health_generic))
            raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Critical dependency (Reranker Service) health check processing error.")


    health_log.debug("Health check (root) passed.")
    return PlainTextResponse("OK", status_code=fastapi_status.HTTP_200_OK)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8001")) # Gunicorn/Cloud Run might set PORT
    log_level_str = settings.LOG_LEVEL.lower()
    print(f"----- Starting {settings.PROJECT_NAME} locally on port {port} -----")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True, log_level=log_level_str)

# jfu