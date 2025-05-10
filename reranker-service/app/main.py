# reranker-service/app/main.py
from fastapi import FastAPI, HTTPException, Request, status as fastapi_status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.exceptions import RequestValidationError
import structlog
import uvicorn
import asyncio
import uuid
from contextlib import asynccontextmanager

from app.core.config import settings
from app.core.logging_config import setup_logging

# Initialize logging as the first step
setup_logging()
logger = structlog.get_logger("reranker_service.main")

# Import API router
from app.api.v1.endpoints import rerank_endpoint

# Import components for dependency setup
from app.infrastructure.rerankers.sentence_transformer_adapter import SentenceTransformerRerankerAdapter
from app.application.use_cases.rerank_documents_use_case import RerankDocumentsUseCase
from app.dependencies import set_dependencies # Import setter for dependencies

# Global state for service readiness
SERVICE_IS_READY = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    global SERVICE_IS_READY
    logger.info("Reranker service starting up...", service_name="AtenexRerankerService")
    
    # Initialize and load the model adapter
    # This adapter has internal static state for the model, so we just instantiate it
    # and call its load_model method.
    model_adapter = SentenceTransformerRerankerAdapter()
    try:
        # The load_model method in the adapter handles its own logging and status
        # It's synchronous but should be relatively quick or managed internally if very long.
        # For very long loads, consider a background task that updates readiness.
        await asyncio.to_thread(model_adapter.load_model) # Run sync load_model in a thread
        
        if model_adapter.is_ready():
            logger.info("Reranker model adapter initialized and model loaded successfully.")
            # Instantiate use case with the ready adapter
            rerank_use_case = RerankDocumentsUseCase(reranker_model=model_adapter)
            
            # Set dependencies for endpoint injection
            set_dependencies(model_adapter=model_adapter, use_case=rerank_use_case)
            SERVICE_IS_READY = True
            logger.info("Reranker service is ready.")
        else:
            logger.error("Reranker model failed to load. Service will be unhealthy.")
            SERVICE_IS_READY = False
            set_dependencies(model_adapter=None, use_case=None) # Ensure dependencies are not set

    except Exception as e:
        logger.fatal("Critical error during reranker model adapter initialization.", error=str(e), exc_info=True)
        SERVICE_IS_READY = False
        set_dependencies(model_adapter=None, use_case=None)

    yield
    logger.info("Reranker service shutting down...")
    # Cleanup if needed (e.g., explicitly releasing GPU memory if PyTorch doesn't do it well)

app = FastAPI(
    title="Atenex Reranker Service",
    version="0.1.0",
    description="Microservice for reranking documents based on query relevance.",
    lifespan=lifespan,
    openapi_url="/api/v1/openapi.json", # Standardize openapi url
    docs_url="/api/v1/docs", # Standardize docs url
    redoc_url="/api/v1/redoc" # Standardize redoc url
)

# Middleware for request ID, timing, and logging
@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    structlog.contextvars.clear_contextvars()
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(request_id=request_id)

    start_time = asyncio.get_event_loop().time()
    
    response = None
    try:
        response = await call_next(request)
    except Exception as e:
        logger.exception("Unhandled exception during request processing.")
        response = JSONResponse(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal Server Error"}
        )
    finally:
        process_time = (asyncio.get_event_loop().time() - start_time) * 1000
        status_code = response.status_code if response else 500
        
        log_method = logger.info
        if status_code >= 500:
            log_method = logger.error
        elif status_code >= 400:
            log_method = logger.warning

        log_method(
            "Request finished",
            method=request.method,
            path=str(request.url.path),
            status_code=status_code,
            duration_ms=round(process_time, 2),
            client_host=request.client.host if request.client else "unknown"
        )
        if response:
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time-Ms"] = f"{process_time:.2f}"
        
        structlog.contextvars.clear_contextvars()
    return response

# Exception Handlers
@app.exception_handler(HTTPException)
async def http_exception_handler_custom(request: Request, exc: HTTPException):
    logger.error(
        "HTTP Exception",
        status_code=exc.status_code,
        detail=exc.detail,
        path=str(request.url.path),
        method=request.method
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler_custom(request: Request, exc: RequestValidationError):
    logger.warning(
        "Request Validation Error",
        errors=exc.errors(),
        path=str(request.url.path),
        method=request.method
    )
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors()}
    )

@app.exception_handler(Exception)
async def generic_exception_handler_custom(request: Request, exc: Exception):
    logger.error(
        "Unhandled Exception",
        error_type=type(exc).__name__,
        error_message=str(exc),
        path=str(request.url.path),
        method=request.method,
        exc_info=True
    )
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected internal server error occurred."}
    )


# Include API router
app.include_router(rerank_endpoint.router, prefix="/api/v1", tags=["Reranking"])

# Health Check Endpoint
@app.get("/health", response_model=None, status_code=fastapi_status.HTTP_200_OK, tags=["Health"])
async def health_check():
    model_status = SentenceTransformerRerankerAdapter.get_model_status() # Access static method
    model_name = settings.MODEL_NAME

    if SERVICE_IS_READY and model_status == "loaded":
        return JSONResponse(content={
            "status": "ok",
            "service": "Atenex Reranker Service",
            "model_status": model_status,
            "model_name": model_name
        })
    else:
        logger.warning("Health check failed or service not ready", service_ready=SERVICE_IS_READY, model_status=model_status)
        raise HTTPException(
            status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "error",
                "service": "Atenex Reranker Service",
                "model_status": model_status,
                "model_name": model_name,
                "message": "Service is not ready or model loading failed."
            }
        )

@app.get("/", include_in_schema=False)
async def root_redirect():
    return PlainTextResponse("Atenex Reranker Service is running. See /api/v1/docs for API documentation.")

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(),
        reload=True # Set to False in production
    )