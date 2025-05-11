import time
import uuid
import structlog
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status as fastapi_status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.exceptions import RequestValidationError, ResponseValidationError, HTTPException

# Setup logging first
from app.core.logging_config import setup_logging
setup_logging() # Initialize logging system

# Then import other modules that might use logging
from app.core.config import settings
from app.api.v1.endpoints import process_endpoint

log = structlog.get_logger(settings.PROJECT_NAME)

# --- Lifespan Management ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"{settings.PROJECT_NAME} startup sequence initiated...")
    # Add any async resource initialization here (e.g., DB pools if needed)
    # For docproc-service, it's mostly stateless or initializes resources per request/use case.
    log.info(f"{settings.PROJECT_NAME} is ready and running.")
    yield
    # Add any async resource cleanup here
    log.info(f"{settings.PROJECT_NAME} shutdown sequence initiated...")
    log.info(f"{settings.PROJECT_NAME} shutdown complete.")

# --- FastAPI App Instance ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.1.0",
    description="Atenex Document Processing Service for text extraction and chunking.",
    lifespan=lifespan
)

# --- Middlewares ---
@app.middleware("http")
async def add_request_context_timing_logging(request: Request, call_next):
    start_time = time.perf_counter()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    
    structlog.contextvars.bind_contextvars(request_id=request_id)
    # For access in endpoint logs if not using contextvars directly there
    request.state.request_id = request_id 

    req_log = log.bind(method=request.method, path=request.url.path, client_host=request.client.host if request.client else "unknown")
    req_log.info("Request received")

    response = None
    try:
        response = await call_next(request)
        process_time_ms = (time.perf_counter() - start_time) * 1000
        
        resp_log = req_log.bind(status_code=response.status_code, duration_ms=round(process_time_ms, 2))
        log_level_method = "warning" if 400 <= response.status_code < 500 else "error" if response.status_code >= 500 else "info"
        getattr(resp_log, log_level_method)("Request finished") # Use getattr to call log method

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    except Exception as e:
        process_time_ms = (time.perf_counter() - start_time) * 1000
        exc_log = req_log.bind(status_code=500, duration_ms=round(process_time_ms, 2)) # Default to 500 for unhandled
        exc_log.exception("Unhandled exception during request processing") # Logs with traceback
        
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
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    request_id = getattr(request.state, 'request_id', 'N/A')
    log_method = log.warning if exc.status_code < 500 else log.error
    log_method(
        "HTTP Exception caught", 
        status_code=exc.status_code, 
        detail=exc.detail,
        request_id=request_id
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "request_id": request_id},
        headers=getattr(exc, "headers", None),
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    request_id = getattr(request.state, 'request_id', 'N/A')
    log.warning("Request Validation Error", errors=exc.errors(), path=request.url.path, request_id=request_id)
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation Error", "errors": exc.errors(), "request_id": request_id},
    )

@app.exception_handler(ResponseValidationError)
async def response_validation_error_handler(request: Request, exc: ResponseValidationError):
    request_id = getattr(request.state, 'request_id', 'N/A')
    log.error("Response Validation Error", errors=exc.errors(), path=request.url.path, request_id=request_id, exc_info=True)
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal Error: Response validation failed", "errors": exc.errors(), "request_id": request_id},
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, 'request_id', 'N/A')
    log.exception("Unhandled global exception caught", path=request.url.path, request_id=request_id) # Ensures full traceback
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected internal server error occurred.", "request_id": request_id}
    )

# --- API Router Inclusion ---
app.include_router(process_endpoint.router, prefix=settings.API_V1_STR, tags=["Document Processing"])
log.info(f"Included processing router with prefix: {settings.API_V1_STR}")


# --- Health Check Endpoint ---
@app.get(
    "/health",
    tags=["Health Check"],
    summary="Performs a health check of the service.",
    response_description="Returns the health status of the service.",
    status_code=fastapi_status.HTTP_200_OK,
)
async def health_check():
    log.debug("Health check endpoint called")
    return {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "version": app.version # FastAPI app version
    }

# --- Root Endpoint ---
@app.get("/", include_in_schema=False)
async def root():
    return PlainTextResponse(f"{settings.PROJECT_NAME} is running.")

if __name__ == "__main__":
    import uvicorn
    log.info(f"Starting {settings.PROJECT_NAME} locally on port {settings.PORT}")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(),
        reload=True # Enable reload for local development
    )

# === 0.1.0 ===
# - jfu 2