# ./app/main.py (CORREGIDO - Eliminado ping de DB del health check)
from fastapi import FastAPI, HTTPException, status as fastapi_status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import structlog
import uvicorn
import logging
import sys
import asyncio

from app.api.v1.endpoints import query
from app.core.config import settings
from app.core.logging_config import setup_logging
from app.db import postgres_client

# Configurar logging ANTES de importar cualquier otra cosa que loguee
setup_logging()
log = structlog.get_logger(__name__)

# Estado global simple para verificar dependencias críticas al inicio
SERVICE_READY = False

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.1.0",
    description="Microservice for document ingestion and preprocessing using Haystack.",
)

# --- Event Handlers ---
@app.on_event("startup")
async def startup_event():
    global SERVICE_READY
    log.info("Starting up Ingest Service (Haystack)...")
    db_pool_initialized = False
    try:
        # Intenta obtener/crear el pool (con statement_cache_size=0)
        await postgres_client.get_db_pool()
        # Verifica la conexión activa con un ping inicial
        pool = await postgres_client.get_db_pool()
        async with pool.acquire() as conn:
            log.info("Verifying PostgreSQL connection during startup...")
            await asyncio.wait_for(conn.execute("SELECT 1"), timeout=10.0) # Timeout razonable para startup
        log.info("PostgreSQL connection pool initialized and connection verified during startup.")
        db_pool_initialized = True
    except asyncio.TimeoutError:
        log.critical("CRITICAL: Timed out (>10s) verifying PostgreSQL connection on startup.", exc_info=False)
    except Exception as e:
        # get_db_pool ya loguea el error detallado si falla la creación del pool
        log.critical("CRITICAL: Failed to establish/verify essential PostgreSQL connection pool on startup.", error=str(e), exc_info=False)

    # Marca el servicio como listo SOLO si la BD conectó y verificó EN EL ARRANQUE
    if db_pool_initialized:
        SERVICE_READY = True
        log.info("Ingest Service startup sequence completed and service marked as READY.")
    else:
        SERVICE_READY = False # Asegurarse que es False
        log.warning("Ingest Service startup sequence completed but essential DB connection failed. Service marked as NOT READY.")


@app.on_event("shutdown")
async def shutdown_event():
    log.info("Shutting down Ingest Service (Haystack)...")
    await postgres_client.close_db_pool()
    log.info("Ingest Service shutdown complete.")

# --- Exception Handlers (Sin cambios) ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    log.warning("HTTP Exception caught", status_code=exc.status_code, detail=exc.detail, path=str(request.url))
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    log.warning("Request Validation Error", errors=exc.errors(), path=str(request.url))
    error_details = [{"field": " -> ".join(map(str, e.get("loc", []))), "message": e.get("msg", ""), "type": e.get("type", "")} for e in exc.errors()]
    return JSONResponse(status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": "Validation Error", "errors": error_details})

@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    log.error("Unhandled Exception caught", error=str(exc), path=str(request.url), exc_info=True)
    return JSONResponse(status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "An internal server error occurred."})


# --- Routers ---
app.include_router(ingest.router, prefix=settings.API_V1_STR, tags=["Ingestion"])

# --- Root Endpoint / Health Check ---
@app.get("/", tags=["Health Check"], status_code=fastapi_status.HTTP_200_OK)
async def read_root():
    """
    Health check endpoint. Checks ONLY the internal SERVICE_READY flag.
    This flag indicates if the startup sequence (including initial DB connection) completed successfully.
    Returns 503 Service Unavailable if startup failed (SERVICE_READY is False).
    This check is now FAST and does NOT depend on external services during the check itself.
    """
    # *** CORREGIDO: Eliminado el ping activo a la base de datos ***
    health_log = log.bind(check="liveness/readiness")
    health_log.debug("Root endpoint accessed (health check)")

    if not SERVICE_READY:
         # Si el servicio no arrancó correctamente (falló la conexión inicial a la BD)
         health_log.warning("Health check failed: Service is marked as NOT READY (startup issue).")
         raise HTTPException(
             status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
             detail="Service is not ready, essential connections likely failed during startup."
         )

    # Si SERVICE_READY es True, significa que el arranque fue exitoso.
    # No necesitamos verificar la BD activamente aquí en cada probe.
    health_log.debug("Health check successful: Service is marked as READY.")
    return {"status": "ok", "service": settings.PROJECT_NAME, "ready": SERVICE_READY} # ready será True aquí

# --- Main execution (for local development - Sin cambios) ---
if __name__ == "__main__":
    log.info("Starting Uvicorn server for local development...")
    log_level_str = settings.LOG_LEVEL.lower()
    if log_level_str not in logging._nameToLevel:
        log.warning(f"Invalid LOG_LEVEL '{settings.LOG_LEVEL}', defaulting Uvicorn log level to 'info'.")
        log_level_str = "info"
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True, log_level=log_level_str)

#V 0.0.3 (Incrementar versión si se desea)