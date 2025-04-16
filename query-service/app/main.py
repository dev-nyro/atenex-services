# query-service/app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status, Request # <-- Importar Request
from fastapi.exceptions import RequestValidationError # <-- Importar RequestValidationError
from fastapi.responses import JSONResponse, Response, PlainTextResponse # <-- Importar PlainTextResponse
import structlog
import uvicorn
import logging
import sys
import asyncio
import json # Importar json

# Configurar logging primero
from app.core.config import settings
from app.core.logging_config import setup_logging
setup_logging()
log = structlog.get_logger("query_service.main")

# Importar routers y otros módulos
# Asumiendo que los endpoints están directamente en estos módulos (ajustar si endpoints/ está separado)
from app.api.v1.endpoints import query as query_router_module
from app.api.v1.endpoints import chat as chat_router_module
from app.db import postgres_client
from app.pipelines.rag_pipeline import build_rag_pipeline, check_pipeline_dependencies
from app.api.v1 import schemas
# from app.utils import helpers # Descomentar si se usan helpers

# Estado global
SERVICE_READY = False
GLOBAL_RAG_PIPELINE = None # Se construye en startup

# Crear instancia de FastAPI
app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.2.1", # Incrementar versión por cambios
    description="Microservice to handle user queries using a Haystack RAG pipeline with Milvus and Gemini, including chat history management. Explicit HTTP endpoint communication.",
)

# --- Event Handlers (Sin cambios en lógica) ---
@app.on_event("startup")
async def startup_event():
    global SERVICE_READY, GLOBAL_RAG_PIPELINE
    log.info(f"Starting up {settings.PROJECT_NAME}...")
    # (Misma lógica de inicialización de DB y pipeline)
    db_pool_initialized = False
    pipeline_built = False
    milvus_startup_ok = False
    try:
        await postgres_client.get_db_pool()
        db_ready = await postgres_client.check_db_connection()
        if db_ready: log.info("PostgreSQL connection pool initialized and verified."); db_pool_initialized = True
        else: log.critical("CRITICAL: Failed PostgreSQL connection verification."); SERVICE_READY = False; return
    except Exception as e: log.critical("CRITICAL: Failed PostgreSQL pool initialization.", error=str(e)); SERVICE_READY = False; return

    try:
        GLOBAL_RAG_PIPELINE = build_rag_pipeline()
        dep_status = await check_pipeline_dependencies()
        if dep_status.get("milvus_connection", "") == "ok":
            log.info("Haystack RAG pipeline built and Milvus connection verified."); pipeline_built = True; milvus_startup_ok = True
        else:
             log.warning("RAG pipeline built, but Milvus check failed/pending.", status=dep_status.get("milvus_connection")); pipeline_built = True; milvus_startup_ok = False # Pipeline OK, Milvus warn
    except Exception as e: log.error("Failed building Haystack RAG pipeline.", error=str(e)); pipeline_built = False

    if db_pool_initialized and pipeline_built:
        SERVICE_READY = True
        log.info(f"{settings.PROJECT_NAME} READY. Initial Milvus Check: {'OK' if milvus_startup_ok else 'Failed/Warn'}")
    else:
        SERVICE_READY = False
        log.critical(f"{settings.PROJECT_NAME} startup FAILED. DB OK: {db_pool_initialized}, Pipeline OK: {pipeline_built}. Service NOT READY.")


@app.on_event("shutdown")
async def shutdown_event():
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    await postgres_client.close_db_pool()
    log.info("Shutdown complete.")

# --- Exception Handlers (Verificar RequestValidationError) ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    log_level = log.warning if exc.status_code < 500 else log.error
    log_level("HTTP Exception caught", status_code=exc.status_code, detail=exc.detail, path=str(request.url))
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Extraer los detalles de validación del error
    error_details = []
    try:
         # exc.errors() devuelve una lista de diccionarios con 'loc', 'msg', 'type'
         for error in exc.errors():
             error_details.append({
                 "loc": error.get("loc", []),
                 "msg": error.get("msg", "Unknown validation error"),
                 "type": error.get("type", "validation_error")
             })
    except Exception as e:
        log.error("Error formatting validation exception details", internal_error=str(e))
        error_details = [{"loc": [], "msg": "Failed to parse validation errors.", "type": "internal_parsing_error"}]

    # Loguear con el detalle formateado
    log.warning("Request Validation Error",
                path=str(request.url),
                client=request.client.host if request.client else "unknown",
                errors=error_details) # Loguear el detalle formateado

    # Devolver respuesta 422 con el formato esperado por el frontend (list[detail])
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        # FastAPI/Pydantic por defecto envuelve 'detail' así. Frontend parece esperar esto.
        content={"detail": error_details},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled Exception caught", path=str(request.url))
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal Server Error"},
    )


# --- Routers (Usando las instancias de los módulos) ---
# Asegurar prefijo común para todos los endpoints bajo /api/v1/query
app.include_router(query_router_module.router, prefix=settings.API_V1_STR, tags=["Query Interaction"])
app.include_router(chat_router_module.router, prefix=settings.API_V1_STR, tags=["Chat Management"])
log.info("Routers included", prefix=settings.API_V1_STR)

# --- Root Endpoint / Health Check ---
@app.get("/", tags=["Health Check"], summary="Service Liveness/Readiness Check")
async def read_root(request: Request): # Añadir Request para poder loguear X-Request-ID
    # Health check ahora también verifica DB explícitamente
    global SERVICE_READY
    request_id = request.headers.get("x-request-id", "N/A")
    health_log = log.bind(request_id=request_id)
    health_log.debug("Health check endpoint '/' requested")

    if not SERVICE_READY:
         health_log.warning("Health check failed: Service not ready (SERVICE_READY is False).")
         return PlainTextResponse("Service Not Ready", status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE)

    # Verificar DB conexión adicionalmente en cada health check si es rápido
    db_ok = await postgres_client.check_db_connection()
    if db_ok:
        health_log.info("Health check successful (Service Ready, DB connection OK).")
        return PlainTextResponse("OK", status_code=fastapi_status.HTTP_200_OK)
    else:
        health_log.error("Health check failed: Service is READY but DB check FAILED.")
        # Marcar como no saludable si la DB falla, aunque el servicio haya iniciado
        return PlainTextResponse("Service Ready but DB check failed", status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE)


# --- Main execution (for local development) ---
if __name__ == "__main__":
    log_level_str = settings.LOG_LEVEL.lower()
    uvicorn.run("app.main:app", host="0.0.0.0", port=8001, reload=True, log_level=log_level_str)