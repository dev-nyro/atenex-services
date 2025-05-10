# embedding-service/app/main.py
import asyncio
import uuid
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Request, status as fastapi_status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

# Configurar logging primero
from app.core.logging_config import setup_logging
setup_logging() # Initialize logging early

# Import other components after logging is set up
from app.core.config import settings
from app.api.v1.endpoints import embedding_endpoint
from app.application.ports.embedding_model_port import EmbeddingModelPort
from app.application.use_cases.embed_texts_use_case import EmbedTextsUseCase
from app.infrastructure.embedding_models.fastembed_adapter import FastEmbedAdapter
from app.dependencies import set_embedding_service_dependencies # Import the setter

log = structlog.get_logger("embedding_service.main")

# Global instances for dependencies
embedding_model_adapter: EmbeddingModelPort | None = None
embed_texts_use_case: EmbedTextsUseCase | None = None
SERVICE_MODEL_READY = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global embedding_model_adapter, embed_texts_use_case, SERVICE_MODEL_READY
    log.info(f"Starting up {settings.PROJECT_NAME}...")

    model_adapter_instance = FastEmbedAdapter()
    try:
        await model_adapter_instance.initialize_model()
        embedding_model_adapter = model_adapter_instance # Assign if successful
        SERVICE_MODEL_READY = True
        log.info("Embedding model initialized successfully via FastEmbedAdapter.")
    except Exception as e:
        SERVICE_MODEL_READY = False
        log.critical("CRITICAL: Failed to initialize embedding model during startup.", error=str(e), exc_info=True)
        # embedding_model_adapter will remain None or be the failed instance.
        # The health check will reflect this.

    if embedding_model_adapter and SERVICE_MODEL_READY:
        use_case_instance = EmbedTextsUseCase(embedding_model=embedding_model_adapter)
        embed_texts_use_case = use_case_instance # Assign to global
        set_embedding_service_dependencies(use_case_instance=use_case_instance, ready_flag=True)
        log.info("EmbedTextsUseCase instantiated and dependencies set.")
    else:
        # Ensure dependencies are set to reflect not-ready state
        set_embedding_service_dependencies(use_case_instance=None, ready_flag=False)
        log.error("Service not fully ready due to embedding model initialization issues.")

    log.info(f"{settings.PROJECT_NAME} startup sequence finished. Model Ready: {SERVICE_MODEL_READY}")
    yield
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    # Cleanup if necessary (e.g., close connections, though FastEmbed might not need explicit cleanup)
    log.info("Shutdown complete.")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.1.0",
    description="Atenex Embedding Service for generating text embeddings using FastEmbed.",
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan
)

# --- Middleware for Request ID and Logging ---
@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    start_time = asyncio.get_event_loop().time()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))

    # Bind essential request info to contextvars for all loggers
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=str(request.url.path),
        client_host=request.client.host if request.client else "unknown",
    )
    # Initial log for request received
    log.info("Request received")

    response = None
    try:
        response = await call_next(request)
        process_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        # Bind response status for final log
        structlog.contextvars.bind_contextvars(status_code=response.status_code, duration_ms=round(process_time_ms, 2))
        log_level = "warning" if 400 <= response.status_code < 500 else "error" if response.status_code >= 500 else "info"
        getattr(log, log_level)("Request finished") # Use bound logger
        response.headers["X-Request-ID"] = request_id # Echo request ID
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    except Exception as e:
        process_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        structlog.contextvars.bind_contextvars(status_code=500, duration_ms=round(process_time_ms, 2))
        log.exception("Unhandled exception during request processing") # Use bound logger
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
    # Request context is already bound by middleware
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

    model_status_str = "not_loaded"
    model_name_str = None
    model_dim_int = None

    if embedding_model_adapter: # Check if adapter instance exists
        is_healthy, status_msg = await embedding_model_adapter.health_check()
        if is_healthy:
            model_status_str = "loaded"
            model_info = embedding_model_adapter.get_model_info()
            model_name_str = model_info.get("model_name")
            model_dim_int = model_info.get("dimension")
        else:
            model_status_str = "error"
            health_log.error("Health check: Embedding model error.", model_status_message=status_msg)
    else: # Adapter not even initialized
        health_log.warning("Health check: Embedding model adapter not initialized.")
        SERVICE_MODEL_READY = False # Ensure flag is accurate

    if not SERVICE_MODEL_READY: # This global flag is set by lifespan based on successful init
        health_log.error("Service not ready (model initialization failed or pending).")
        raise HTTPException(
            status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=schemas.HealthCheckResponse(
                status="error",
                service=settings.PROJECT_NAME,
                model_status=model_status_str,
                model_name=model_name_str,
                model_dimension=model_dim_int
            ).model_dump(exclude_none=True)
        )

    health_log.info("Health check successful.", model_status=model_status_str)
    return schemas.HealthCheckResponse(
        status="ok",
        service=settings.PROJECT_NAME,
        model_status=model_status_str,
        model_name=model_name_str,
        model_dimension=model_dim_int
    )

# --- Root Endpoint (Simple Ack)  ---
@app.get("/", tags=["Root"], response_class=PlainTextResponse, include_in_schema=False)
async def root():
    return f"{settings.PROJECT_NAME} is running."


if __name__ == "__main__":
    port_to_run = settings.PORT
    log_level_main = settings.LOG_LEVEL.lower()
    print(f"----- Starting {settings.PROJECT_NAME} locally on port {port_to_run} -----")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port_to_run, reload=True, log_level=log_level_main)

# 0.1.0 version
# jfu 3