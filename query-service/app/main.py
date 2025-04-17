# query-service/app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse, Response, PlainTextResponse
import structlog
import uvicorn
import logging
import sys
import asyncio
import json

# Configurar logging primero
from app.core.config import settings
from app.core.logging_config import setup_logging
setup_logging()
log = structlog.get_logger("query_service.main")

# Importar routers y otros módulos
from app.api.v1.endpoints import query as query_router_module
from app.api.v1.endpoints import chat as chat_router_module
from app.db import postgres_client
from app.pipelines.rag_pipeline import run_rag_pipeline
from app.api.v1 import schemas
# from app.utils import helpers

# Estado global
SERVICE_READY = False
GLOBAL_RAG_PIPELINE = None

# Crear instancia de FastAPI
app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json", # Restaurar openapi_url con prefijo
    version="0.2.3", # Incrementar versión por corrección de prefijo
    description="Microservice to handle user queries using a Haystack RAG pipeline with Milvus and Gemini, including chat history management. Expects /api/v1 prefix.", # Descripción actualizada
)

# --- Event Handlers (Sin cambios en lógica interna) ---
@app.on_event("startup")
async def startup_event():
    global SERVICE_READY, GLOBAL_RAG_PIPELINE
    log.info(f"Starting up {settings.PROJECT_NAME}...")
    db_pool_initialized = False
    # Intenta inicializar la BD
    try:
        await postgres_client.get_db_pool()
        db_ready = await postgres_client.check_db_connection()
        if db_ready:
            log.info("PostgreSQL connection pool initialized and verified.")
            db_pool_initialized = True
        else:
            log.critical("CRITICAL: Failed PostgreSQL connection verification during startup.")
            db_pool_initialized = False
    except Exception as e:
        log.critical("CRITICAL: Failed PostgreSQL pool initialization during startup.", error=str(e), exc_info=True)
        db_pool_initialized = False

    # Marcar como listo si la BD está lista
    if db_pool_initialized:
        SERVICE_READY = True
        log.info(f"{settings.PROJECT_NAME} service components initialized. SERVICE READY.")
    else:
        SERVICE_READY = False
        log.critical(f"{settings.PROJECT_NAME} startup FAILED. DB OK: {db_pool_initialized}. Service NOT READY.")


@app.on_event("shutdown")
async def shutdown_event():
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    await postgres_client.close_db_pool()
    log.info("Shutdown complete.")

# --- Exception Handlers (Sin cambios) ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    request_id = request.headers.get("x-request-id", "N/A")
    log_level = log.warning if exc.status_code < 500 else log.error
    log_level("HTTP Exception caught by handler",
              status_code=exc.status_code,
              detail=exc.detail,
              path=str(request.url),
              method=request.method,
              client=request.client.host if request.client else "unknown",
              request_id=request_id)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    request_id = request.headers.get("x-request-id", "N/A")
    error_details = []
    try:
         error_details = exc.errors()
    except Exception:
         error_details = [{"loc": [], "msg": "Failed to parse validation errors.", "type": "internal_parsing_error"}]

    log.warning("Request Validation Error",
                path=str(request.url),
                method=request.method,
                client=request.client.host if request.client else "unknown",
                errors=error_details,
                request_id=request_id)

    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": error_details},
    )

@app.exception_handler(ResponseValidationError)
async def response_validation_exception_handler(request: Request, exc: ResponseValidationError):
    request_id = request.headers.get("x-request-id", "N/A")
    log.error("Response Validation Error - Server failed to construct valid response",
              path=str(request.url),
              method=request.method,
              errors=exc.errors(),
              request_id=request_id,
              exc_info=True)

    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal Server Error: Failed to serialize response."},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    request_id = request.headers.get("x-request-id", "N/A")
    log.exception("Unhandled Exception caught by generic handler",
                  path=str(request.url),
                  method=request.method,
                  client=request.client.host if request.client else "unknown",
                  request_id=request_id)
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal Server Error"},
    )


# --- Routers ---
# *** REVERSIÓN: Volver a añadir el prefijo API_V1_STR ***
# El API Gateway reenvía la ruta completa, por lo que el servicio debe esperarla.
app.include_router(query_router_module.router, prefix=settings.API_V1_STR, tags=["Query Interaction"]) # Ruta esperada: /api/v1/ask
app.include_router(chat_router_module.router, prefix=settings.API_V1_STR, tags=["Chat Management"])   # Rutas esperadas: /api/v1/chats, etc.
log.info("Routers included with prefix", prefix=settings.API_V1_STR) # Actualizar log

# --- Root Endpoint / Health Check (Sin cambios) ---
@app.get("/", tags=["Health Check"], summary="Service Liveness/Readiness Check")
async def read_root(request: Request):
    global SERVICE_READY
    request_id = request.headers.get("x-request-id", "N/A")
    health_log = log.bind(request_id=request_id)
    health_log.debug("Health check endpoint '/' requested")

    if not SERVICE_READY:
         health_log.warning("Health check failed: Service not ready (SERVICE_READY is False).")
         return PlainTextResponse("Service Not Ready", status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE)

    db_ok = await postgres_client.check_db_connection()
    if db_ok:
        health_log.info("Health check successful (Service Ready, DB connection OK).")
        return PlainTextResponse("OK", status_code=fastapi_status.HTTP_200_OK)
    else:
        health_log.error("Health check failed: Service is READY but DB check FAILED.")
        return PlainTextResponse("Service Ready but DB check failed", status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE)


# --- Main execution (for local development, sin cambios) ---
if __name__ == "__main__":
    port = 8002
    log_level_str = settings.LOG_LEVEL.lower()
    print(f"----- Starting Query Service locally on port {port} -----")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True, log_level=log_level_str)