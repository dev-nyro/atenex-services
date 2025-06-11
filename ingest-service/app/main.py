# ingest-service/app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status, Request
from fastapi.exceptions import RequestValidationError # ResponseValidationError removed as we catch DomainException now
from fastapi.responses import JSONResponse, PlainTextResponse
import structlog
import uvicorn
import logging
import sys
import asyncio
import time
import uuid
from contextlib import asynccontextmanager

from app.core.logging_config import setup_logging
setup_logging() # Initialize logging first

from app.core.config import settings
log = structlog.get_logger("ingest_service.main")
# Corrected import to use the new ingest_endpoint
from app.api.v1.endpoints import ingest_endpoint # LLM_CORRECTION: Use ingest_endpoint
from app.infrastructure.persistence import postgres_connector
from app.infrastructure.vectorstore.milvus_adapter import MilvusAdapter # For Milvus init
from app.domain.exceptions import DomainException # Import base domain exception

SERVICE_READY = False
DB_CONNECTION_OK = False
MILVUS_CONNECTION_OK = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    global SERVICE_READY, DB_CONNECTION_OK, MILVUS_CONNECTION_OK
    log.info("Executing Ingest Service startup sequence...")
    
    # Initialize PostgreSQL
    try:
        pool = await postgres_connector.get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute('SELECT 1')
        log.info("PostgreSQL connection pool initialized and verified successfully.")
        DB_CONNECTION_OK = True
    except Exception as db_exc:
        log.critical("PostgreSQL connection FAILED during startup.", error=str(db_exc), exc_info=True)
        DB_CONNECTION_OK = False

    # Initialize Milvus
    try:
        # Instantiate adapter to trigger its connection and collection setup
        milvus_adapter = MilvusAdapter() 
        # A simple check could be trying to get collection info or count
        # For now, successful instantiation of adapter implies connection was attempted
        # and collection/indexes were ensured.
        # A more robust check might be needed if ensure_collection_and_indexes can fail silently
        # or if a specific health check method is added to VectorStorePort/MilvusAdapter.
        if milvus_adapter._milvus_collection is not None : # Accessing internal for check, ideally use a port method
            log.info("Milvus connection and collection initialized successfully via MilvusAdapter.")
            MILVUS_CONNECTION_OK = True
        else:
            log.critical("Milvus initialization via MilvusAdapter FAILED (collection is None).")
            MILVUS_CONNECTION_OK = False
    except Exception as milvus_exc:
        log.critical("Milvus connection/collection FAILED during startup.", error=str(milvus_exc), exc_info=True)
        MILVUS_CONNECTION_OK = False

    SERVICE_READY = DB_CONNECTION_OK and MILVUS_CONNECTION_OK

    if SERVICE_READY:
        log.info("Ingest Service startup successful. SERVICE IS READY.")
    else:
        log.error("Ingest Service startup FAILED. SERVICE IS NOT READY.", db_ok=DB_CONNECTION_OK, milvus_ok=MILVUS_CONNECTION_OK)

    yield 

    log.info("Executing Ingest Service shutdown sequence...")
    await postgres_connector.close_db_pool()
    # Add Milvus disconnect if necessary, though Pymilvus might handle this on process exit
    # connections.disconnect(alias_name_used_in_adapter) # If MilvusAdapter uses a specific alias
    log.info("Shutdown sequence complete.")


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="1.0.0", # Updated version
    description="Atenex Ingest Service with Hexagonal Architecture.",
    lifespan=lifespan
)

@app.middleware("http")
async def add_request_context_timing_logging(request: Request, call_next):
    start_time = time.perf_counter()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    structlog.contextvars.bind_contextvars(request_id=request_id)
    req_log = log.bind(method=request.method, path=request.url.path)
    req_log.info("Request received")
    request.state.request_id = request_id

    response = None
    try:
        response = await call_next(request)
        process_time_ms = (time.perf_counter() - start_time) * 1000
        resp_log = req_log.bind(status_code=response.status_code, duration_ms=round(process_time_ms, 2))
        log_level_method = "warning" if 400 <= response.status_code < 500 else "error" if response.status_code >= 500 else "info"
        getattr(resp_log, log_level_method)("Request finished")
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    except Exception as e:
        process_time_ms = (time.perf_counter() - start_time) * 1000
        exc_log = req_log.bind(status_code=500, duration_ms=round(process_time_ms, 2))
        
        if isinstance(e, DomainException): # Handle our custom domain exceptions
            exc_log.warning("Domain exception occurred.", detail=str(e), status_code=e.status_code)
            response = JSONResponse(
                status_code=e.status_code,
                content={"detail": str(e)}
            )
        elif isinstance(e, HTTPException): # Handle FastAPI's HTTPExceptions
            exc_log.warning("HTTPException occurred.", detail=e.detail, status_code=e.status_code)
            response = JSONResponse(
                status_code=e.status_code,
                content={"detail": e.detail},
                headers=getattr(e, "headers", None)
            )
        else: # Handle other unexpected exceptions
            exc_log.exception("Unhandled exception during request processing")
            response = JSONResponse(
                status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"detail": "Internal Server Error"}
            )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    finally:
         structlog.contextvars.clear_contextvars()
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    log.warning("Request Validation Error", errors=exc.errors(), path=request.url.path)
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Error de validación en la petición", "errors": exc.errors()},
    )

# Removed specific HTTPException and general Exception handlers as they are now covered by the middleware.

app.include_router(ingest_endpoint.router, prefix=settings.API_V1_STR, tags=["Ingestion V2 (Hexagonal)"])
log.info(f"Included NEW hexagonal ingestion router with prefix: {settings.API_V1_STR}")

@app.get("/health", tags=["Health Check"], summary="Service Health Check")
async def health_check_endpoint(request: Request):
    if SERVICE_READY:
        return PlainTextResponse("OK", status_code=fastapi_status.HTTP_200_OK)
    else:
        log.error("Health check failed: Service not ready.", db_ok=DB_CONNECTION_OK, milvus_ok=MILVUS_CONNECTION_OK)
        raise HTTPException(
            status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, 
            detail=f"Service not ready (DB: {'OK' if DB_CONNECTION_OK else 'FAIL'}, Milvus: {'OK' if MILVUS_CONNECTION_OK else 'FAIL'})"
        )

@app.get("/", tags=["Root"], include_in_schema=False)
async def root_endpoint():
    return {"message": f"{settings.PROJECT_NAME} is running."}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8001)) # Use PORT env var if available
    log_level_str = settings.LOG_LEVEL.lower()
    print(f"----- Starting {settings.PROJECT_NAME} (Hexagonal) locally on port {port} -----")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True, log_level=log_level_str)