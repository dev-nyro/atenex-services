# ./app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status
# --- CORRECCIÓN: Importar RequestValidationError ---
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
import structlog
import uvicorn
import logging
import sys
import asyncio

# Configurar logging primero
from app.core.config import settings
from app.core.logging_config import setup_logging
setup_logging()
log = structlog.get_logger(__name__)

# Importar routers y otros módulos
from app.api.v1.endpoints import query
from app.api.v1.endpoints import chat as chat_router
from app.db import postgres_client
from app.pipelines.rag_pipeline import build_rag_pipeline, check_pipeline_dependencies
from app.api.v1 import schemas
from app.utils import helpers # Si se usan helpers globales

# Estado global
SERVICE_READY = False
GLOBAL_RAG_PIPELINE = None

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.2.0", # Mantener versión incrementada
    description="Microservice to handle user queries using a Haystack RAG pipeline with Milvus and Gemini, including chat history management.",
)

# --- Event Handlers (Sin cambios en lógica) ---
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
        if db_ready: log.info("PostgreSQL connection pool initialized and connection verified."); db_pool_initialized = True
        else: log.critical("CRITICAL: Failed to verify PostgreSQL connection on startup."); SERVICE_READY = False; return
    except Exception as e:
        log.critical("CRITICAL: Failed to establish essential PostgreSQL connection pool on startup.", error=str(e), exc_info=True); SERVICE_READY = False; return
    try:
        GLOBAL_RAG_PIPELINE = build_rag_pipeline()
        dep_status = await check_pipeline_dependencies()
        if dep_status.get("milvus_connection") == "ok": log.info("Haystack RAG pipeline built and Milvus connection verified during startup."); pipeline_built = True; milvus_startup_ok = True
        else: log.warning("Haystack RAG pipeline built, but Milvus connection check failed during startup.", milvus_status=dep_status.get("milvus_connection")); pipeline_built = True; milvus_startup_ok = False
    except Exception as e:
        log.error("Failed to build Haystack RAG pipeline during startup.", error=str(e), exc_info=True); pipeline_built = False; milvus_startup_ok = False
    if db_pool_initialized and pipeline_built:
        SERVICE_READY = True
        log.info(f"{settings.PROJECT_NAME} startup sequence completed. Service marked as READY. Initial Milvus Check: {'OK' if milvus_startup_ok else 'Failed'}")
    else:
        SERVICE_READY = False
        if db_pool_initialized and not pipeline_built: log.critical(f"{settings.PROJECT_NAME} startup failed because RAG pipeline could not be built. Service marked as NOT READY.")


@app.on_event("shutdown")
async def shutdown_event():
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    await postgres_client.close_db_pool()
    log.info("PostgreSQL connection pool closed.")
    log.info(f"{settings.PROJECT_NAME} shutdown complete.")

# --- Exception Handlers ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    log_level = log.warning if exc.status_code < 500 and exc.status_code != 404 else log.error
    log_level("HTTP Exception caught", status_code=exc.status_code, detail=exc.detail, path=str(request.url))
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

# --- Manejador donde ocurría el error ---
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError): # Añadir tipo para claridad
    # Usar exc.errors() para obtener detalles específicos
    log.warning("Request Validation Error", errors=exc.errors(), path=str(request.url))
    # Formatear los errores para la respuesta JSON
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
app.include_router(query.router, prefix=settings.API_V1_STR, tags=["Query & Chat Interaction"])
app.include_router(chat_router.router, prefix=settings.API_V1_STR, tags=["Chat Management"])


# --- Root Endpoint / Health Check (Sin cambios) ---
@app.get("/", tags=["Health Check"], summary="Service Liveness/Readiness Check", description="Basic Kubernetes probe endpoint. Returns 200 OK with 'OK' text if the service started successfully, otherwise 503.")
async def read_root():
    global SERVICE_READY
    log.debug("Root endpoint accessed (health check)")
    if SERVICE_READY:
        log.debug("Health check successful (service ready).")
        return Response(content="OK", status_code=fastapi_status.HTTP_200_OK, media_type="text/plain")
    else:
        log.warning("Health check failed: Service not ready (SERVICE_READY is False).")
        raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service is not ready or failed during startup.")


# --- Main execution (for local development) (Sin cambios) ---
if __name__ == "__main__":
    log.info(f"Starting Uvicorn server for {settings.PROJECT_NAME} local development...")
    log_level_str = settings.LOG_LEVEL.lower()
    if log_level_str not in logging._nameToLevel: log.warning(f"Invalid LOG_LEVEL '{settings.LOG_LEVEL}', defaulting Uvicorn log level to 'info'."); log_level_str = "info"
    uvicorn.run("app.main:app", host="0.0.0.0", port=8001, reload=True, log_level=log_level_str)