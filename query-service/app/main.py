# ./app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status
from fastapi.exceptions import RequestValidationError
# *** CORRECCIÓN: Importar Response para health check simple ***
from fastapi.responses import JSONResponse, Response
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
from app.pipelines.rag_pipeline import build_rag_pipeline, check_pipeline_dependencies
from app.api.v1 import schemas

# Estado global para verificar dependencias críticas al inicio
SERVICE_READY = False
# Almacenar el pipeline construido globalmente si se decide inicializar en startup
GLOBAL_RAG_PIPELINE = None

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.1.0", # Considerar actualizar versión si haces cambios significativos
    description="Microservice to handle user queries using a Haystack RAG pipeline with Milvus and Gemini.",
    # Desactivar docs si no se necesitan expuestos directamente en el microservicio
    # (asumiendo que se accede via API Gateway que tendrá su propia doc)
    # docs_url=None,
    # redoc_url=None,
)

# --- Event Handlers ---
# (Sin cambios en la lógica de startup/shutdown respecto a la versión anterior)
@app.on_event("startup")
async def startup_event():
    global SERVICE_READY, GLOBAL_RAG_PIPELINE
    log.info(f"Starting up {settings.PROJECT_NAME}...")
    db_pool_initialized = False
    pipeline_built = False
    milvus_startup_ok = False

    try:
        await postgres_client.get_db_pool()
        db_ready = await postgres_client.check_db_connection()
        if db_ready:
            log.info("PostgreSQL connection pool initialized and connection verified.")
            db_pool_initialized = True
        else:
            log.critical("CRITICAL: Failed to verify PostgreSQL connection on startup.")
            SERVICE_READY = False
            log.critical(f"{settings.PROJECT_NAME} startup failed. Essential DB connection could not be established. Service marked as NOT READY.")
            return
    except Exception as e:
        log.critical("CRITICAL: Failed to establish essential PostgreSQL connection pool on startup.", error=str(e), exc_info=True)
        SERVICE_READY = False
        log.critical(f"{settings.PROJECT_NAME} startup failed during DB init. Service marked as NOT READY.")
        return

    try:
        GLOBAL_RAG_PIPELINE = build_rag_pipeline()
        dep_status = await check_pipeline_dependencies()
        if dep_status.get("milvus_connection") == "ok":
             log.info("Haystack RAG pipeline built and Milvus connection verified during startup.")
             pipeline_built = True
             milvus_startup_ok = True
        else:
             log.warning("Haystack RAG pipeline built, but Milvus connection check failed during startup.", milvus_status=dep_status.get("milvus_connection"))
             pipeline_built = True
             milvus_startup_ok = False
    except Exception as e:
        log.error("Failed to build Haystack RAG pipeline during startup.", error=str(e), exc_info=True)
        pipeline_built = False
        milvus_startup_ok = False

    if db_pool_initialized and pipeline_built:
        SERVICE_READY = True
        # MILVUS_STARTUP_OK ya no se necesita globalmente con el health check simplificado
        log.info(f"{settings.PROJECT_NAME} startup sequence completed. Service marked as READY. Initial Milvus Check: {'OK' if milvus_startup_ok else 'Failed'}")
    else:
        SERVICE_READY = False
        if db_pool_initialized and not pipeline_built:
            log.critical(f"{settings.PROJECT_NAME} startup failed because RAG pipeline could not be built. Service marked as NOT READY.")


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
    # *** CORRECCIÓN: Eliminar response_model y devolver Response simple ***
    # response_model=schemas.HealthCheckResponse,
    summary="Service Liveness/Readiness Check",
    # Descripción más técnica para el probe
    description="Basic Kubernetes probe endpoint. Returns 200 OK with 'OK' text if the service started successfully, otherwise 503."
)
async def read_root():
    """
    Basic health check endpoint. Returns 200 OK if SERVICE_READY is True,
    otherwise 503. Designed for Kubernetes probes.
    """
    global SERVICE_READY
    log.debug("Root endpoint accessed (health check)")

    if SERVICE_READY:
        log.debug("Health check successful (service ready).")
        # Devolver respuesta mínima y rápida
        return Response(content="OK", status_code=fastapi_status.HTTP_200_OK, media_type="text/plain")
    else:
        log.warning("Health check failed: Service not ready (SERVICE_READY is False).")
        # Devolver 503 explícito
        # No lanzar HTTPException aquí necesariamente, mejor devolver la respuesta 503 directamente
        # para evitar logs de error innecesarios por los probes fallidos antes de estar listo.
        # Aunque lanzar la excepción también funciona y es como estaba antes. Mantenemos la excepción por ahora.
        raise HTTPException(
            status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service is not ready or failed during startup."
        )


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