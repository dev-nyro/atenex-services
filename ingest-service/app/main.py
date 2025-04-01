# ./app/main.py (CORREGIDO - Posición de 'global SERVICE_READY')
from fastapi import FastAPI, HTTPException, status as fastapi_status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import structlog
import uvicorn
import logging # Import logging
import sys # Import sys for SystemExit
import asyncio # Import asyncio for health check timeout

from app.api.v1.endpoints import ingest
from app.core.config import settings
from app.core.logging_config import setup_logging
from app.db import postgres_client
# Remove milvus_client import if not used directly (Haystack handles it)
# from app.db import milvus_client

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
    # *** CORREGIDO: Declarar global al inicio si se modifica ***
    global SERVICE_READY
    log.info("Starting up Ingest Service (Haystack)...")
    db_pool_initialized = False
    try:
        # Intenta obtener el pool (lo crea si no existe)
        # get_db_pool ahora lanza excepción si falla, así que no necesitamos asignarlo aquí
        await postgres_client.get_db_pool()
        # Verifica la conexión activa
        pool = await postgres_client.get_db_pool() # Obtener el pool ya existente
        async with pool.acquire() as conn:
            # Verificar conexión con timeout corto
            log.info("Verifying PostgreSQL connection...")
            await asyncio.wait_for(conn.execute("SELECT 1"), timeout=10.0)
        log.info("PostgreSQL connection pool initialized and connection verified.")
        db_pool_initialized = True
    except asyncio.TimeoutError:
        log.critical("CRITICAL: Timed out (>10s) verifying PostgreSQL connection on startup.", exc_info=False)
        # SERVICE_READY permanece False
    except Exception as e:
        # get_db_pool ya loguea el error detallado
        log.critical("CRITICAL: Failed to establish/verify essential PostgreSQL connection pool on startup.", error=str(e), exc_info=False) # No duplicar traceback si ya se logueó
        # SERVICE_READY permanece False

    # Marca el servicio como listo SOLO si la BD conectó y verificó
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
    # log.info("PostgreSQL connection pool closed.") # close_db_pool ya loguea esto
    log.info("Ingest Service shutdown complete.")

# --- Exception Handlers (Mantener como estaban) ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    log.warning("HTTP Exception caught", status_code=exc.status_code, detail=exc.detail, path=str(request.url))
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    log.warning("Request Validation Error", errors=exc.errors(), path=str(request.url))
    error_details = []
    for error in exc.errors():
        field = " -> ".join(map(str, error.get("loc", [])))
        error_details.append({"field": field, "message": error.get("msg", ""), "type": error.get("type", "")})
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation Error", "errors": error_details},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    log.error("Unhandled Exception caught", error=str(exc), path=str(request.url), exc_info=True)
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred."},
    )


# --- Routers ---
app.include_router(ingest.router, prefix=settings.API_V1_STR, tags=["Ingestion"])

# --- Root Endpoint / Health Check ---
@app.get("/", tags=["Health Check"], status_code=fastapi_status.HTTP_200_OK)
async def read_root():
    """
    Health check endpoint. Checks if the service started successfully
    (SERVICE_READY flag) and performs an active database ping.
    Returns 503 Service Unavailable if startup failed or DB ping fails.
    """
    # *** CORREGIDO: Mover la declaración 'global' al inicio de la función ***
    global SERVICE_READY
    health_log = log.bind(check="liveness/readiness")
    health_log.debug("Root endpoint accessed (health check)")

    if not SERVICE_READY:
         health_log.warning("Health check failed: Service is marked as NOT READY (startup issue).")
         raise HTTPException(
             status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
             detail="Service is not ready, essential connections likely failed during startup."
         )

    # Chequeo activo de la base de datos (ping)
    try:
        pool = await postgres_client.get_db_pool() # Reutilizar el pool existente
        async with pool.acquire() as conn:
            # Usa un timeout bajo para el ping
            await asyncio.wait_for(conn.execute("SELECT 1"), timeout=5.0)
        health_log.debug("Health check: DB ping successful.")
        # Si el ping tiene éxito pero SERVICE_READY era False (raro, pero posible si hubo un error temporal), lo corregimos.
        # Esto es una autocorrección leve, pero si el startup falló gravemente, el pod debería reiniciarse.
        if not SERVICE_READY:
             health_log.warning("DB Ping successful, but service was marked as not ready. Setting SERVICE_READY=True now (potential recovery).")
             SERVICE_READY = True # Marcar como listo si el ping funciona ahora
    except asyncio.TimeoutError:
        health_log.error("Health check failed: DB ping timed out (> 5 seconds). Marking service as NOT READY.")
        # Marcar como no listo si falla el ping activo
        SERVICE_READY = False
        raise HTTPException(
            status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service is unhealthy, database connection check timed out."
        )
    except Exception as db_ping_err:
        health_log.error("Health check failed: DB ping error. Marking service as NOT READY.", error=str(db_ping_err))
        # Marcar como no listo si falla el ping activo
        SERVICE_READY = False
        raise HTTPException(
            status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Service is unhealthy, cannot connect to database: {type(db_ping_err).__name__}"
        )

    # Si todo está bien (SERVICE_READY era True y el ping funcionó)
    return {"status": "ok", "service": settings.PROJECT_NAME, "ready": SERVICE_READY}

# --- Main execution (for local development) ---
if __name__ == "__main__":
    log.info("Starting Uvicorn server for local development...")
    log_level_str = settings.LOG_LEVEL.lower()
    if log_level_str not in logging._nameToLevel:
        log.warning(f"Invalid LOG_LEVEL '{settings.LOG_LEVEL}', defaulting Uvicorn log level to 'info'.")
        log_level_str = "info"

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True, # Activa reload para desarrollo local
        log_level=log_level_str
    )

#V 0.0.3