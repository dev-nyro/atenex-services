# ./app/main.py (CORREGIDO - Falla al iniciar si la BD no conecta)
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
# Remove milvus_client import if not used directly here
# from app.db import milvus_client

# Configurar logging ANTES de importar cualquier otra cosa que loguee
setup_logging()
log = structlog.get_logger(__name__)

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.1.0", # Puedes actualizar la versión si quieres
    description="Microservice for document ingestion and preprocessing using Haystack.",
)

# --- Event Handlers ---
@app.on_event("startup")
async def startup_event():
    log.info("Starting up Ingest Service (Haystack)...")
    try:
        # Intenta obtener el pool, esto fuerza la conexión inicial
        await postgres_client.get_db_pool()
        log.info("PostgreSQL connection pool initialized successfully.")
        # La conexión a Milvus es manejada por Haystack DocumentStore bajo demanda, no se inicia aquí.
    except Exception as e:
        # *** CORRECCIÓN: Hacer que la aplicación falle si la BD no conecta ***
        log.critical("CRITICAL: Failed to establish PostgreSQL connection pool on startup. Aborting.", error=str(e), exc_info=True)
        # Descomentar para detener el servicio si la BD no está disponible al inicio
        raise SystemExit(f"Could not connect to PostgreSQL on startup: {e}") from e
        # Si comentas la línea anterior, el servicio iniciará pero fallará en las peticiones a BD
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
    log.warning("HTTP Exception caught", status_code=exc.status_code, detail=exc.detail, path=str(request.url))
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None), # Propagate headers if present
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    log.warning("Request Validation Error", errors=exc.errors(), path=str(request.url))
    error_details = []
    for error in exc.errors():
        # Proporcionar más contexto en el campo si es posible
        field = " -> ".join(map(str, error["loc"]))
        error_details.append({"field": field, "message": error["msg"], "type": error["type"]})
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation Error", "errors": error_details},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    # Loguear la excepción no manejada con traceback
    log.error("Unhandled Exception caught", error=str(exc), path=str(request.url), exc_info=True)
    # Devolver una respuesta genérica para no exponer detalles internos
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred. Please check service logs."},
    )


# --- Routers ---
app.include_router(ingest.router, prefix=settings.API_V1_STR, tags=["Ingestion"])

# --- Root Endpoint ---
@app.get("/", tags=["Health Check"])
async def read_root():
    # Considerar añadir una comprobación real de la BD aquí si es necesario
    # try:
    #     pool = await postgres_client.get_db_pool()
    #     async with pool.acquire() as conn:
    #         await conn.execute("SELECT 1")
    #     db_status = "ok"
    # except Exception:
    #     db_status = "error"
    #     log.warning("Health check failed to connect to DB")
    log.debug("Root endpoint accessed (basic health check)")
    return {"status": "ok", "service": settings.PROJECT_NAME} #, "db_status": db_status}

# --- Main execution (for local development) ---
if __name__ == "__main__":
    log.info("Starting Uvicorn server for local development...")
    # Usar niveles de log estándar para uvicorn
    # Obtener el nivel de log de settings y convertirlo al nombre estándar si es necesario
    log_level_str = settings.LOG_LEVEL.lower()
    if log_level_str not in ["critical", "error", "warning", "info", "debug", "trace"]:
        log_level_str = "info" # Default a info si no es válido

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000, # Puerto estándar para la API
        reload=True, # Habilitar auto-reload para desarrollo
        log_level=log_level_str # Usar el nivel de log de la configuración
    )

# V 0.0.18