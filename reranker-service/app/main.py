# reranker-service/app/main.py
from fastapi import FastAPI, HTTPException, Request, status as fastapi_status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.exceptions import RequestValidationError
from contextlib import asynccontextmanager
import structlog
import uvicorn # For local execution if __name__ == "__main__"
import asyncio
import uuid # For request IDs

# Import core components first
from app.core.config import settings
from app.core.logging_config import setup_logging

# Initialize logging as the very first step
setup_logging()
logger = structlog.get_logger(settings.PROJECT_NAME.lower().replace(" ", "-") + ".main")

# Import API router
from app.api.v1.endpoints import rerank_endpoint

# Import components for dependency setup during lifespan
from app.infrastructure.rerankers.sentence_transformer_adapter import SentenceTransformerRerankerAdapter
from app.application.use_cases.rerank_documents_use_case import RerankDocumentsUseCase
from app.dependencies import set_dependencies # Import setter for dependencies
from app.api.v1.schemas import HealthCheckResponse # For health check response model

# Global state for service readiness, managed by lifespan
SERVICE_IS_READY = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Handles application startup and shutdown events.
    - Loads the reranker model.
    - Sets up shared dependencies.
    - Manages service readiness state.
    """
    global SERVICE_IS_READY
    logger.info(
        f"{settings.PROJECT_NAME} service starting up...", 
        version="0.1.0", # Consider moving version to config if it changes often
        port=settings.PORT,
        log_level=settings.LOG_LEVEL
    )
    
    # Instantiate the concrete adapter. Model loading is a class method.
    model_adapter = SentenceTransformerRerankerAdapter()
    
    try:
        # Trigger model loading. This is a class method that updates static/class variables.
        # Running synchronous model loading in a thread to avoid blocking lifespan.
        await asyncio.to_thread(SentenceTransformerRerankerAdapter.load_model) 
        
        if model_adapter.is_ready(): # is_ready() checks the class-level status
            logger.info(
                "Reranker model adapter initialized and model loaded successfully.",
                model_name=model_adapter.get_model_name()
            )
            # Instantiate use case with the (now ready) adapter
            rerank_use_case = RerankDocumentsUseCase(reranker_model=model_adapter)
            
            # Set shared instances for dependency injection
            set_dependencies(model_adapter=model_adapter, use_case=rerank_use_case)
            SERVICE_IS_READY = True
            logger.info(f"{settings.PROJECT_NAME} is ready to serve requests.")
        else:
            logger.error(
                "Reranker model failed to load during startup. Service will be unhealthy.",
                model_name=settings.MODEL_NAME # Log configured name even if load failed
            )
            SERVICE_IS_READY = False
            # Ensure dependencies reflect unready state if use_case requires a ready model
            set_dependencies(model_adapter=model_adapter, use_case=None) 

    except Exception as e:
        logger.fatal(
            "Critical error during reranker model adapter initialization or loading in lifespan.", 
            error_message=str(e), 
            exc_info=True
        )
        SERVICE_IS_READY = False
        set_dependencies(model_adapter=model_adapter if 'model_adapter' in locals() else None, use_case=None)

    yield # Application runs here

    # --- Shutdown Logic ---
    logger.info(f"{settings.PROJECT_NAME} service shutting down...")
    # Add any cleanup logic here if necessary (e.g., releasing GPU resources explicitly, though PyTorch usually handles this)
    # For this service, model cleanup is not explicitly managed by instance, but by Python's GC when process ends.
    logger.info(f"{settings.PROJECT_NAME} has been shut down.")

# Create FastAPI application instance
app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.1.0", # Should match pyproject.toml
    description="Microservice for reranking documents based on query relevance using CrossEncoder models.",
    lifespan=lifespan,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url=f"{settings.API_V1_STR}/docs",
    redoc_url=f"{settings.API_V1_STR}/redoc"
)

# Middleware for request ID, timing, and logging
@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    # Clear contextvars at the beginning of each request
    structlog.contextvars.clear_contextvars()
    
    # Bind request-specific information for all logs within this request's scope
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(request_id=request_id)

    start_time = asyncio.get_event_loop().time()
    
    response = None
    try:
        response = await call_next(request)
    except Exception as e:
        # This will catch unhandled exceptions from routes/dependencies
        logger.exception("Unhandled exception during request processing by middleware.") # Log with full traceback
        response = JSONResponse(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "An unexpected internal server error occurred."}
        )
    finally:
        process_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        status_code_for_log = response.status_code if response else 500 # Default to 500 if no response
        
        log_method = logger.info
        if status_code_for_log >= 500:
            log_method = logger.error
        elif status_code_for_log >= 400:
            log_method = logger.warning

        log_method(
            "Request finished",
            http_method=request.method,
            http_path=str(request.url.path),
            http_status_code=status_code_for_log,
            http_duration_ms=round(process_time_ms, 2),
            client_host=request.client.host if request.client else "unknown_client"
        )
        if response: # Ensure headers are added only if a response object exists
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
        
        # Clear contextvars after the request is fully processed
        structlog.contextvars.clear_contextvars()
    return response

# Custom Exception Handlers
@app.exception_handler(HTTPException)
async def http_exception_handler_custom(request: Request, exc: HTTPException):
    # Logged by middleware already if it bubbles up
    # logger.error("HTTP Exception handled", status_code=exc.status_code, detail=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler_custom(request: Request, exc: RequestValidationError):
    # Logged by middleware already
    # logger.warning("Request Validation Error handled", errors=exc.errors())
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        # Provide structured error details from Pydantic
        content={"detail": exc.errors()} 
    )

@app.exception_handler(Exception) # Catch-all for any other unhandled exceptions
async def generic_exception_handler_custom(request: Request, exc: Exception):
    # Logged by middleware already
    # logger.error("Generic Unhandled Exception handled", error_type=type(exc).__name__, error_message=str(exc), exc_info=True)
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected internal server error occurred."}
    )

# Include API router
app.include_router(rerank_endpoint.router, prefix=settings.API_V1_STR, tags=["Reranking Operations"])
logger.info("API routers included.", prefix=settings.API_V1_STR)

# Health Check Endpoint
@app.get(
    "/health", 
    response_model=HealthCheckResponse, 
    tags=["Health"],
    summary="Service Health and Model Status Check"
)
async def health_check():
    # Access model status via the class method of the adapter
    model_status = SentenceTransformerRerankerAdapter.get_model_status()
    current_model_name = settings.MODEL_NAME # Get configured model name

    health_log = logger.bind(service_ready_flag=SERVICE_IS_READY, model_actual_status=model_status)

    if SERVICE_IS_READY and model_status == "loaded":
        health_log.debug("Health check: OK")
        return HealthCheckResponse(
            status="ok",
            service=settings.PROJECT_NAME,
            model_status=model_status,
            model_name=current_model_name
        )
    else:
        unhealthy_reason = "Service dependencies not fully initialized."
        if model_status != "loaded":
            unhealthy_reason = f"Model status is '{model_status}'."
        
        health_log.warning("Health check: FAILED", reason=unhealthy_reason)
        # Return 503 with JSON body as per schema
        return JSONResponse(
            status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "error",
                "service": settings.PROJECT_NAME,
                "model_status": model_status,
                "model_name": current_model_name,
                "message": f"Service is not ready. {unhealthy_reason}"
            }
        )

# Root endpoint for basic "is it alive" check or simple info
@app.get("/", include_in_schema=False)
async def root_redirect():
    return PlainTextResponse(
        f"{settings.PROJECT_NAME} is running. See {settings.API_V1_STR}/docs for API documentation."
    )

# For local development: uvicorn app.main:app --reload --port 8004
if __name__ == "__main__":
    logger.info(f"Starting {settings.PROJECT_NAME} locally with Uvicorn...")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0", # Listen on all available IPs
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(), # Uvicorn's own log level
        reload=True # Enable auto-reload for development
    )

# jfu