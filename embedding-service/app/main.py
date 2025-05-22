# embedding-service/app/main.py
import asyncio
import uuid
from contextlib import asynccontextmanager

import structlog
import uvicorn
from app.api.v1 import schemas
from fastapi import FastAPI, HTTPException, Request, status as fastapi_status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

# Configurar logging primero
from app.core.logging_config import setup_logging
setup_logging() # Initialize logging early

# Import other components after logging is set up
from app.core.config import settings # This now loads the validated settings
from app.api.v1.endpoints import embedding_endpoint
from app.application.ports.embedding_model_port import EmbeddingModelPort
from app.application.use_cases.embed_texts_use_case import EmbedTextsUseCase
from app.infrastructure.embedding_models.openai_adapter import OpenAIAdapter
from app.infrastructure.embedding_models.sentence_transformer_adapter import SentenceTransformerAdapter
from app.dependencies import set_embedding_service_dependencies

log = structlog.get_logger("embedding_service.main")

# Global instances for dependencies
embedding_model_adapter: EmbeddingModelPort | None = None
embed_texts_use_case: EmbedTextsUseCase | None = None
SERVICE_MODEL_READY = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global embedding_model_adapter, embed_texts_use_case, SERVICE_MODEL_READY
    log.info(f"Starting up {settings.PROJECT_NAME}...")
    log.info(f"Active embedding provider: {settings.ACTIVE_EMBEDDING_PROVIDER}")

    model_adapter_instance: EmbeddingModelPort | None = None

    if settings.ACTIVE_EMBEDDING_PROVIDER == "openai":
        if not settings.OPENAI_API_KEY or not settings.OPENAI_API_KEY.get_secret_value():
            log.critical("OpenAI_API_KEY is not set. Cannot initialize OpenAIAdapter.")
            SERVICE_MODEL_READY = False
        else:
            model_adapter_instance = OpenAIAdapter()
    elif settings.ACTIVE_EMBEDDING_PROVIDER == "sentence_transformer":
        model_adapter_instance = SentenceTransformerAdapter()
    else:
        log.critical(f"Unsupported ACTIVE_EMBEDDING_PROVIDER: {settings.ACTIVE_EMBEDDING_PROVIDER}")
        SERVICE_MODEL_READY = False

    if model_adapter_instance:
        try:
            await model_adapter_instance.initialize_model()
            embedding_model_adapter = model_adapter_instance # Assign if successful
            SERVICE_MODEL_READY = True
            log.info(f"Embedding model client initialized successfully using {type(model_adapter_instance).__name__}.")
        except Exception as e:
            SERVICE_MODEL_READY = False
            log.critical(
                f"CRITICAL: Failed to initialize {type(model_adapter_instance).__name__} "
                "embedding model client during startup.",
                error=str(e), exc_info=True
            )
    else: # Handle case where provider was unsupported or OpenAI key missing
        SERVICE_MODEL_READY = False
        log.error("No valid embedding model adapter could be instantiated based on configuration.")


    if embedding_model_adapter and SERVICE_MODEL_READY:
        use_case_instance = EmbedTextsUseCase(embedding_model=embedding_model_adapter)
        embed_texts_use_case = use_case_instance # Assign to global
        set_embedding_service_dependencies(use_case_instance=use_case_instance, ready_flag=True)
        log.info(f"EmbedTextsUseCase instantiated and dependencies set with {type(embedding_model_adapter).__name__}.")
    else:
        set_embedding_service_dependencies(use_case_instance=None, ready_flag=False)
        log.error("Service not fully ready due to embedding model client initialization issues.")

    log.info(f"{settings.PROJECT_NAME} startup sequence finished. Model Client Ready: {SERVICE_MODEL_READY}")
    yield
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    if isinstance(embedding_model_adapter, OpenAIAdapter) and embedding_model_adapter._client:
        try:
            await embedding_model_adapter._client.close()
            log.info("OpenAI async client closed successfully.")
        except Exception as e:
            log.error("Error closing OpenAI async client.", error=str(e), exc_info=True)
    # Add cleanup for SentenceTransformerAdapter if needed (e.g. releasing GPU memory, though Python GC usually handles it)
    log.info("Shutdown complete.")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.2.0", # Incremented version
    description=f"Atenex Embedding Service. Active provider: {settings.ACTIVE_EMBEDDING_PROVIDER}",
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan
)

# --- Middleware for Request ID and Logging ---
@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    start_time = asyncio.get_event_loop().time()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))

    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=str(request.url.path),
        client_host=request.client.host if request.client else "unknown",
    )
    log.info("Request received")

    response = None
    try:
        response = await call_next(request)
        process_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        structlog.contextvars.bind_contextvars(status_code=response.status_code, duration_ms=round(process_time_ms, 2))
        log_level = "warning" if 400 <= response.status_code < 500 else "error" if response.status_code >= 500 else "info"
        getattr(log, log_level)("Request finished") 
        response.headers["X-Request-ID"] = request_id 
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    except Exception as e:
        process_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        structlog.contextvars.bind_contextvars(status_code=500, duration_ms=round(process_time_ms, 2))
        log.exception("Unhandled exception during request processing") 
        response = JSONResponse(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal Server Error", "request_id": request_id}
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    finally:
        structlog.contextvars.clear_contextvars()
    return response

# --- Exception Handlers ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    log.error("HTTP Exception caught", status_code=exc.status_code, detail=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "request_id": structlog.contextvars.get_contextvars().get("request_id")},
        headers=getattr(exc, "headers", None)
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    log.warning("Request Validation Error", errors=exc.errors())
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Validation Error",
            "errors": exc.errors(),
            "request_id": structlog.contextvars.get_contextvars().get("request_id")
        },
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    log.exception("Generic Unhandled Exception caught")
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected internal server error occurred.", "request_id": structlog.contextvars.get_contextvars().get("request_id")}
    )


# --- API Router ---
app.include_router(embedding_endpoint.router, prefix=settings.API_V1_STR, tags=["Embeddings"])
log.info(f"Embedding API router included with prefix: {settings.API_V1_STR}")


# --- Health Check Endpoint ---
@app.get(
    "/health",
    response_model=schemas.HealthCheckResponse,
    tags=["Health Check"],
    summary="Service Health and Model Status"
)
async def health_check():
    global SERVICE_MODEL_READY, embedding_model_adapter
    health_log = log.bind(check="health_status")

    model_status_str = "client_not_initialized"
    model_name_str = None
    model_dim_int = None
    service_overall_status = "error" # Default to error

    if embedding_model_adapter: 
        is_healthy, status_msg = await embedding_model_adapter.health_check()
        model_info = embedding_model_adapter.get_model_info() # Get info regardless of health
        model_name_str = model_info.get("model_name")
        model_dim_int = model_info.get("dimension")

        if is_healthy and SERVICE_MODEL_READY: # Check both adapter health and global service readiness
            model_status_str = "client_ready"
            service_overall_status = "ok"
        elif is_healthy and not SERVICE_MODEL_READY:
             model_status_str = "client_initialization_pending_or_failed" # Adapter might be ok, but global state isn't
             health_log.error("Health check: Adapter client healthy but service global flag indicates not ready.")
        else: # Adapter not healthy
            model_status_str = "client_error" # Use this or status_msg for more detail
            health_log.error("Health check: Embedding model client error.", model_status_message=status_msg)
    else: 
        health_log.warning("Health check: Embedding model adapter not initialized (no valid provider configured or initial error).")
        SERVICE_MODEL_READY = False # Ensure flag is accurate
        # Try to get default model name/dim from config if adapter is None
        if settings.ACTIVE_EMBEDDING_PROVIDER == "openai":
            model_name_str = settings.OPENAI_EMBEDDING_MODEL_NAME
        elif settings.ACTIVE_EMBEDDING_PROVIDER == "sentence_transformer":
            model_name_str = settings.ST_MODEL_NAME
        model_dim_int = settings.EMBEDDING_DIMENSION


    response_payload = schemas.HealthCheckResponse(
        status=service_overall_status,
        service=settings.PROJECT_NAME,
        model_status=model_status_str,
        model_name=model_name_str, 
        model_dimension=model_dim_int 
    ).model_dump(exclude_none=True)

    if not SERVICE_MODEL_READY or service_overall_status == "error":
        health_log.error("Service not ready.", current_status=model_status_str, service_model_ready_flag=SERVICE_MODEL_READY)
        return JSONResponse(
            status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
            content=response_payload
        )

    health_log.info("Health check successful.", model_status=model_status_str)
    return response_payload

# --- Root Endpoint (Simple Ack)  ---
@app.get("/", tags=["Root"], response_class=PlainTextResponse, include_in_schema=False)
async def root():
    return f"{settings.PROJECT_NAME} (Provider: {settings.ACTIVE_EMBEDDING_PROVIDER}) is running."


if __name__ == "__main__":
    # Uvicorn is typically run by Gunicorn in production. This is for local dev.
    # Gunicorn config (workers, etc.) will be handled by gunicorn_conf.py when deployed.
    port_to_run = settings.PORT
    log_level_main = settings.LOG_LEVEL.lower()
    print(f"----- Starting {settings.PROJECT_NAME} locally on port {port_to_run} (Provider: {settings.ACTIVE_EMBEDDING_PROVIDER}) -----")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port_to_run,
        reload=True, # Reload for local dev
        log_level=log_level_main,
        workers=1 # For local dev, uvicorn manages its own loop. Gunicorn workers setting is for prod.
    )

# JFU 2