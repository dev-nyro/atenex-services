# ./app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import structlog
import uvicorn
import logging
import sys
import asyncio

# Importar configuraciones y logging primero
from app.core.config import settings
from app.core.logging_config import setup_logging

# Configurar logging ANTES de importar cualquier otra cosa que loguee
setup_logging()
log = structlog.get_logger(__name__)

# Importar routers y otros módulos después de configurar logging
from app.api.v1.endpoints import query
from app.db import postgres_client
# Importar solo build_rag_pipeline aquí, check_pipeline_dependencies se usará solo en startup
from app.pipelines.rag_pipeline import build_rag_pipeline, check_pipeline_dependencies
from app.api.v1 import schemas

# Estado global para verificar dependencias críticas al inicio
SERVICE_READY = False
# Almacenar el pipeline construido globalmente si se decide inicializar en startup
GLOBAL_RAG_PIPELINE = None
# Variable para almacenar el estado de Milvus verificado en startup
MILVUS_STARTUP_OK = False

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.1.0",
    description="Microservice to handle user queries using a Haystack RAG pipeline with Milvus and Gemini.",
)

# --- Event Handlers ---
@app.on_event("startup")
async def startup_event():
    global SERVICE_READY, GLOBAL_RAG_PIPELINE, MILVUS_STARTUP_OK
    log.info(f"Starting up {settings.PROJECT_NAME}...")
    db_pool_initialized = False
    pipeline_built = False
    milvus_ok = False

    # 1. Inicializar Pool de Base de Datos (Supabase)
    try:
        await postgres_client.get_db_pool()
        db_ready = await postgres_client.check_db_connection()
        if db_ready:
            log.info("PostgreSQL connection pool initialized and connection verified.")
            db_pool_initialized = True
        else:
            log.critical("CRITICAL: Failed to verify PostgreSQL connection on startup.")
    except Exception as e:
        log.critical("CRITICAL: Failed to establish essential PostgreSQL connection pool on startup.", error=str(e), exc_info=True)

    # 2. Construir el Pipeline Haystack y Verificar Milvus *una vez*
    if db_pool_initialized:
        try:
            GLOBAL_RAG_PIPELINE = build_rag_pipeline() # Intenta construir, incluye init de Milvus
            # Si build_rag_pipeline tuvo éxito (no lanzó excepción), significa que MilvusDocumentStore se inicializó
            # Ahora verificamos la conexión activamente una vez
            dep_status = await check_pipeline_dependencies()
            if dep_status.get("milvus_connection") == "ok":
                 log.info("Haystack RAG pipeline built and Milvus connection verified during startup.")
                 pipeline_built = True
                 milvus_ok = True
            else:
                 # El pipeline se construyó (componentes listos) pero Milvus no respondió al check
                 log.warning("Haystack RAG pipeline built, but Milvus connection check failed during startup.", milvus_status=dep_status.get("milvus_connection"))
                 pipeline_built = True
                 milvus_ok = False # Marcar como no OK para el health check inicial
        except Exception as e:
            # Si build_rag_pipeline falla (ej: error al inicializar MilvusDocumentStore)
            log.error("Failed to build Haystack RAG pipeline during startup.", error=str(e), exc_info=True)
            pipeline_built = False
            milvus_ok = False

    # Marcar servicio como listo SOLO si la BD está OK y el pipeline se construyó
    # (Incluso si Milvus falló la *verificación* inicial, el servicio puede intentar recuperarse)
    if db_pool_initialized and pipeline_built:
        SERVICE_READY = True
        MILVUS_STARTUP_OK = milvus_ok # Guardar estado de Milvus en startup
        log.info(f"{settings.PROJECT_NAME} startup sequence completed. Service marked as READY (DB OK, Pipeline Built). Initial Milvus Check: {'OK' if milvus_ok else 'Failed'}")
    else:
        SERVICE_READY = False
        MILVUS_STARTUP_OK = False
        log.critical(f"{settings.PROJECT_NAME} startup failed. Essential dependencies (DB or Pipeline Build) could not be established. Service marked as NOT READY.")


@app.on_event("shutdown")
async def shutdown_event():
    # (Sin cambios)
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    await postgres_client.close_db_pool()
    log.info("PostgreSQL connection pool closed.")
    log.info(f"{settings.PROJECT_NAME} shutdown complete.")

# --- Exception Handlers (Sin cambios) ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    # (Sin cambios)
    log_level = log.warning if exc.status_code < 500 and exc.status_code != 404 else log.error
    log_level("HTTP Exception caught", status_code=exc.status_code, detail=exc.detail, path=str(request.url))
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    # (Sin cambios)
    log.warning("Request Validation Error", errors=exc.errors(), path=str(request.url))
    error_details = [{"loc": err.get("loc"), "msg": err.get("msg"), "type": err.get("type")} for err in exc.errors()]
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation Error", "errors": error_details},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    # (Sin cambios)
    log.exception("Unhandled Exception caught", path=str(request.url))
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred."},
    )


# --- Routers (Sin cambios) ---
app.include_router(query.router, prefix=settings.API_V1_STR, tags=["Query"])

# --- Root Endpoint / Health Check ---
@app.get(
    "/",
    tags=["Health Check"],
    response_model=schemas.HealthCheckResponse,
    summary="Service Health Check"
)
async def read_root():
    """
    Health check endpoint. Checks basic service readiness and database connection.
    Relies on startup checks for initial pipeline/Milvus status.
    Returns 503 Service Unavailable if the service didn't start correctly or DB is down.
    """
    global SERVICE_READY, MILVUS_STARTUP_OK
    log.debug("Root endpoint accessed (health check)")

    # 1. Chequeo primario: ¿El servicio se inició correctamente?
    if not SERVICE_READY:
         log.warning("Health check failed: Service did not start successfully (SERVICE_READY is False).")
         # Devolver 503 inmediatamente si el startup falló
         raise HTTPException(
             status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
             detail="Service failed to initialize required components during startup."
         )

    # 2. Chequeo activo RÁPIDO: Conexión a la base de datos
    db_status = "pending"
    db_ok = False
    try:
        db_ok = await postgres_client.check_db_connection()
        db_status = "ok" if db_ok else "error: Connection failed"
        if db_ok:
             log.debug("Health check: DB ping successful.")
        else:
             log.error("Health check failed: DB ping failed.")
    except Exception as db_err:
        db_status = f"error: {type(db_err).__name__}"
        log.error("Health check failed: DB ping error.", error=str(db_err))
        db_ok = False # Asegurar que db_ok es False si hay excepción

    # 3. Estado de Milvus (Basado en el chequeo de startup)
    # No hacemos chequeo activo aquí para mantener el probe rápido
    milvus_status = "ok" if MILVUS_STARTUP_OK else "error: Failed startup check"
    if not MILVUS_STARTUP_OK:
        log.warning("Health check: Reporting Milvus as potentially down based on startup check.")

    # 4. Determinar estado general y código HTTP
    # El servicio se considera 'listo' para las sondas si la BD está OK.
    # El estado de Milvus se reporta pero no necesariamente causa 503 aquí.
    overall_ready = db_ok
    http_status = fastapi_status.HTTP_200_OK if overall_ready else fastapi_status.HTTP_503_SERVICE_UNAVAILABLE

    response_data = schemas.HealthCheckResponse(
        status="ok" if overall_ready else "error",
        service=settings.PROJECT_NAME,
        ready=overall_ready,
        dependencies={
            "database": db_status,
            "vector_store": milvus_status # Reportar estado de Milvus basado en startup
        }
    )

    # Si la BD falla, lanzamos 503. Si solo Milvus falló en startup, devolvemos 200 OK
    # pero indicando el error en 'dependencies'.
    if not overall_ready:
         log.warning("Health check determined service is not ready (DB connection failed).", dependencies=response_data.dependencies)
         # Usar el detail del response model puede ser muy verboso para un 503 simple
         raise HTTPException(status_code=http_status, detail="Service is unhealthy: Database connection failed.")
    else:
         # Loguear incluso si Milvus tuvo problemas en startup pero la BD está OK
         log.info("Health check successful.", dependencies=response_data.dependencies)
         return response_data


# --- Main execution (for local development) ---
# (Sin cambios)
if __name__ == "__main__":
    log.info(f"Starting Uvicorn server for {settings.PROJECT_NAME} local development...")
    log_level_str = settings.LOG_LEVEL.lower()
    if log_level_str not in logging._nameToLevel:
        log.warning(f"Invalid LOG_LEVEL '{settings.LOG_LEVEL}', defaulting Uvicorn log level to 'info'.")
        log_level_str = "info"

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
        log_level=log_level_str,
    )