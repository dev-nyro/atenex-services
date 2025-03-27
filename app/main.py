from fastapi import FastAPI, HTTPException, status as fastapi_status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import structlog
import uvicorn
import logging # Import logging

from app.api.v1.endpoints import ingest
from app.core.config import settings
from app.core.logging_config import setup_logging
from app.db import postgres_client
# Remove milvus_client import
# from app.db import milvus_client

# Configurar logging ANTES de importar cualquier otra cosa que loguee
setup_logging()
log = structlog.get_logger(__name__)

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.1.0",
    description="Microservice for document ingestion and preprocessing using Haystack.",
)

# --- Event Handlers ---
@app.on_event("startup")
async def startup_event():
    log.info("Starting up Ingest Service (Haystack)...")
    try:
        await postgres_client.get_db_pool() # Establish PG pool
        # Milvus connection handled by Haystack DocumentStore on demand
        log.info("PostgreSQL connection pool initialized.")
    except Exception as e:
        log.critical("Failed to establish PostgreSQL connection pool on startup", error=str(e), exc_info=True)
        # Decide if startup should fail
        # raise SystemExit("Could not connect to PostgreSQL") from e
    log.info("Ingest Service startup complete.")

@app.on_event("shutdown")
async def shutdown_event():
    log.info("Shutting down Ingest Service (Haystack)...")
    await postgres_client.close_db_pool()
    # No explicit Milvus disconnect needed here if managed by Haystack Store
    log.info("PostgreSQL connection pool closed.")
    log.info("Ingest Service shutdown complete.")

# --- Exception Handlers (Keep as is) ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    # ... (previous implementation) ...
    log.warning("HTTP Exception caught", status_code=exc.status_code, detail=exc.detail, path=str(request.url))
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    # ... (previous implementation) ...
    log.warning("Request Validation Error", errors=exc.errors(), path=str(request.url))
    error_details = []
    for error in exc.errors():
        field = " -> ".join(map(str, error["loc"]))
        error_details.append({"field": field, "message": error["msg"], "type": error["type"]})
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation Error", "errors": error_details},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    # ... (previous implementation) ...
    log.error("Unhandled Exception caught", error=str(exc), path=str(request.url), exc_info=True)
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred."},
    )


# --- Routers ---
app.include_router(ingest.router, prefix=settings.API_V1_STR, tags=["Ingestion"])

# --- Root Endpoint ---
@app.get("/", tags=["Health Check"])
async def read_root():
    log.debug("Root endpoint accessed (health check)")
    # Add more checks? e.g., DB connectivity?
    return {"status": "ok", "service": settings.PROJECT_NAME}

# --- Main execution (for local development) ---
if __name__ == "__main__":
    log.info("Starting Uvicorn server for local development...")
    # Use standard logging levels for uvicorn
    log_level_str = logging.getLevelName(settings.LOG_LEVEL).lower()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level=log_level_str # Use standard level names
    )

# V 0.0.8