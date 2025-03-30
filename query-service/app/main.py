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
# Importación corregida (ya estaba bien)
from app.api.v1.endpoints import query
from app.db import postgres_client
from app.pipelines.rag_pipeline import build_rag_pipeline, check_pipeline_dependencies
from app.api.v1 import schemas

# Estado global para verificar dependencias críticas al inicio
SERVICE_READY = False
# Almacenar el pipeline construido globalmente si se decide inicializar en startup
GLOBAL_RAG_PIPELINE = None

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.1.0",
    description="Microservice to handle user queries using a Haystack RAG pipeline with Milvus and Gemini.",
)

# --- Event Handlers ---
# (Sin cambios aquí respecto a la versión funcional anterior)
@app.on_event("startup")
async def startup_event():
    global SERVICE_READY, GLOBAL_RAG_PIPELINE
    log.info(f"Starting up {settings.PROJECT_NAME}...")
    db_pool_initialized = False
    pipeline_built = False

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

    # 2. Construir el Pipeline Haystack
    if db_pool_initialized:
        try:
            GLOBAL_RAG_PIPELINE = build_rag_pipeline()
            dep_status = await check_pipeline_dependencies()
            if dep_status.get("milvus_connection") == "ok":
                 log.info("Haystack RAG pipeline built and Milvus connection verified.")
                 pipeline_built = True
            else:
                 log.warning("Haystack RAG pipeline built, but Milvus check failed.", milvus_status=dep_status.get("milvus_connection"))
                 pipeline_built = True
        except Exception as e:
            log.error("Failed to build Haystack RAG pipeline during startup.", error=str(e), exc_info=True)

    # Marcar servicio como listo SOLO si las dependencias MÍNIMAS (DB) están OK
    if db_pool_initialized:
        SERVICE_READY = True
        log.info(f"{settings.PROJECT_NAME} startup sequence completed. Service marked as READY (DB OK). Pipeline status: {'Built' if pipeline_built else 'Build Failed'}")
    else:
        SERVICE_READY = False
        log.critical(f"{settings.PROJECT_NAME} startup failed. Essential DB connection could not be established. Service marked as NOT READY.")
        # sys.exit(1)


@app.on_event("shutdown")
async def shutdown_event():
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    await postgres_client.close_db_pool()
    log.info("PostgreSQL connection pool closed.")
    log.info(f"{settings.PROJECT_NAME} shutdown complete.")

# --- Exception Handlers (Sin cambios) ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    log_level = log.warning if exc.status_code < 500 and exc.status_code != 404 else log.error
    log_level("HTTP Exception caught", status_code=exc.status_code, detail=exc.detail, path=str(request.url))
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    log.warning("Request Validation Error", errors=exc.errors(), path=str(request.url))
    error_details = [{"loc": err.get("loc"), "msg": err.get("msg"), "type": err.get("type")} for err in exc.errors()]
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation Error", "errors": error_details},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    log.exception("Unhandled Exception caught", path=str(request.url))
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred."},
    )


# --- Routers ---
# *** CORRECCIÓN: Cambiar 'ingest.router' a 'query.router' y actualizar tags ***
app.include_router(query.router, prefix=settings.API_V1_STR, tags=["Query"])


# --- Root Endpoint / Health Check ---
# (Sin cambios aquí respecto a la versión funcional anterior)
@app.get(
    "/",
    tags=["Health Check"],
    response_model=schemas.HealthCheckResponse,
    summary="Service Health Check"
)
async def read_root():
    """
    Health check endpoint. Checks service readiness and critical dependencies like Database and Milvus.
    Returns 503 Service Unavailable if the service is not ready or dependencies are down.
    """
    global SERVICE_READY
    log.debug("Root endpoint accessed (health check)")

    if not SERVICE_READY:
         log.warning("Health check failed: Service did not start successfully (SERVICE_READY is False).")
         raise HTTPException(
             status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
             detail="Service is not ready, essential connections likely failed during startup."
         )

    db_status = "pending"
    milvus_status = "pending"
    overall_ready = True

    # Chequeo activo de la base de datos
    try:
        db_ok = await postgres_client.check_db_connection()
        db_status = "ok" if db_ok else "error: Connection failed"
        if not db_ok: overall_ready = False; log.error("Health check failed: DB ping failed.")
    except Exception as db_err:
        db_status = f"error: {type(db_err).__name__}"
        overall_ready = False
        log.error("Health check failed: DB ping error.", error=str(db_err))

    # Chequeo activo de Milvus
    try:
        milvus_dep_status = await check_pipeline_dependencies()
        milvus_status = milvus_dep_status.get("milvus_connection", "error: Check failed")
        if "error" in milvus_status:
            log.warning("Health check: Milvus check indicates an issue.", status=milvus_status)
            # overall_ready = False # Considerar si Milvus es crítico
    except Exception as milvus_err:
        milvus_status = f"error: {type(milvus_err).__name__}"
        log.error("Health check failed: Milvus check error.", error=str(milvus_err))
        # overall_ready = False

    http_status = fastapi_status.HTTP_200_OK if overall_ready else fastapi_status.HTTP_503_SERVICE_UNAVAILABLE

    response_data = schemas.HealthCheckResponse(
        status="ok" if overall_ready else "error",
        service=settings.PROJECT_NAME,
        ready=overall_ready,
        dependencies={
            "database": db_status,
            "vector_store": milvus_status
        }
    )

    if not overall_ready:
         log.warning("Health check determined service is not ready.", dependencies=response_data.dependencies)
         raise HTTPException(status_code=http_status, detail=response_data.model_dump())
    else:
         log.info("Health check successful.", dependencies=response_data.dependencies)
         return response_data


# --- Main execution (for local development) ---
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