# ./app/main.py (CORREGIDO - Manejo de error DB en startup)
from fastapi import FastAPI, HTTPException, status as fastapi_status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import structlog
import uvicorn
import logging # Import logging
import sys # Import sys for SystemExit

from app.api.v1.endpoints import ingest
from app.core.config import settings
from app.core.logging_config import setup_logging
from app.db import postgres_client
# Remove milvus_client import (confirmado que no se usa directamente aquí)
# from app.db import milvus_client

# Configurar logging ANTES de importar cualquier otra cosa que loguee
setup_logging()
log = structlog.get_logger(__name__)

# Estado global simple para verificar dependencias críticas
SERVICE_READY = False

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.1.0", # Considerar obtener de variable de entorno o archivo
    description="Microservice for document ingestion and preprocessing using Haystack.",
)

# --- Event Handlers ---
@app.on_event("startup")
async def startup_event():
    global SERVICE_READY
    log.info("Starting up Ingest Service (Haystack)...")
    try:
        # Intentar establecer el pool de PG. Si falla, lanzará excepción.
        await postgres_client.get_db_pool()
        log.info("PostgreSQL connection pool initialization attempt successful (pool might be created on first use).")
        # Milvus connection handled by Haystack DocumentStore on demand within tasks

        # *** CORREGIDO: Marcar como listo SÓLO si la conexión a BD es exitosa ***
        SERVICE_READY = True
        log.info("Ingest Service startup complete and marked as READY.")

    except Exception as e:
        # *** CORREGIDO: Loguear como crítico y SALIR para que K8s marque el pod como fallido ***
        log.critical("CRITICAL: Failed to establish essential PostgreSQL connection pool on startup. Service will exit.", error=str(e), exc_info=True)
        # Esto evitará que el pod se marque como listo si la BD no está accesible al inicio.
        # K8s intentará reiniciar el pod.
        # Nota: Si el error de red es persistente, el pod entrará en CrashLoopBackOff,
        # lo cual es el comportamiento deseado para indicar un problema grave.
        sys.exit(f"Could not connect to PostgreSQL on startup: {e}") # Salir con mensaje de error

@app.on_event("shutdown")
async def shutdown_event():
    log.info("Shutting down Ingest Service (Haystack)...")
    await postgres_client.close_db_pool()
    # No explicit Milvus disconnect needed here if managed by Haystack Store
    log.info("PostgreSQL connection pool closed.")
    log.info("Ingest Service shutdown complete.")

# --- Exception Handlers (Keep as is - logging ya es bueno) ---
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
    # Pydantic v2 usa 'loc' como tupla, v1 como lista. Convertir a string siempre.
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
    Health check endpoint. Checks critical dependencies initialized during startup.
    Returns 503 Service Unavailable if critical dependencies (like DB) failed.
    """
    log.debug("Root endpoint accessed (health check)")
    if not SERVICE_READY:
         # *** CORREGIDO: Retornar 503 si el startup no completó correctamente ***
         log.warning("Health check failed: Service not ready (DB connection likely failed on startup).")
         raise HTTPException(
             status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
             detail="Service is not ready, essential connections might be down."
         )
    # Podrías añadir chequeos más activos aquí si es necesario,
    # como un ping rápido a la base de datos.
    # try:
    #     pool = await postgres_client.get_db_pool()
    #     async with pool.acquire() as conn:
    #         await conn.execute("SELECT 1")
    #     log.debug("Health check: DB ping successful.")
    # except Exception as db_ping_err:
    #     log.error("Health check failed: DB ping error.", error=str(db_ping_err))
    #     raise HTTPException(
    #         status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
    #         detail="Service is unhealthy, cannot connect to database."
    #     )

    return {"status": "ok", "service": settings.PROJECT_NAME}

# --- Main execution (for local development) ---
if __name__ == "__main__":
    log.info("Starting Uvicorn server for local development...")
    # Use standard logging levels for uvicorn
    log_level_str = settings.LOG_LEVEL.lower() # Nombres estándar: debug, info, warning, error
    # Validar que sea un nivel conocido por logging
    if log_level_str not in logging._nameToLevel:
        log_warning(f"Invalid LOG_LEVEL '{settings.LOG_LEVEL}', defaulting Uvicorn log level to 'info'.")
        log_level_str = "info"

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000, # Opcionalmente desde settings: settings.APP_PORT or 8000
        reload=True, # Reload solo para desarrollo local
        log_level=log_level_str # Usar nivel estándar
    )