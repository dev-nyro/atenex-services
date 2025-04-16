# query-service/app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status, Request # <-- Importar Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError # <-- Importar ResponseValidationError
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
    # No establecer openapi_url aquí si el Gateway lo expone
    # openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.2.2", # Incrementar versión por correcciones
    description="Microservice to handle user queries using a Haystack RAG pipeline with Milvus and Gemini, including chat history management. Routes are relative (e.g., /ask, /chats).",
)

# --- Event Handlers (Sin cambios en lógica interna) ---
@app.on_event("startup")
async def startup_event():
    global SERVICE_READY, GLOBAL_RAG_PIPELINE
    log.info(f"Starting up {settings.PROJECT_NAME}...")
    db_pool_initialized = False
    pipeline_built = False
    milvus_startup_ok = False
    # Intenta inicializar la BD
    try:
        await postgres_client.get_db_pool()
        db_ready = await postgres_client.check_db_connection()
        if db_ready:
            log.info("PostgreSQL connection pool initialized and verified.")
            db_pool_initialized = True
        else:
            log.critical("CRITICAL: Failed PostgreSQL connection verification during startup.")
            # No marcar como listo, pero no salir inmediatamente, podría recuperarse
            db_pool_initialized = False
    except Exception as e:
        log.critical("CRITICAL: Failed PostgreSQL pool initialization during startup.", error=str(e), exc_info=True)
        db_pool_initialized = False
        # Podríamos decidir salir si la DB es crítica: sys.exit(1)

    # Intenta construir el pipeline RAG
    try:
        GLOBAL_RAG_PIPELINE = build_rag_pipeline()
        dep_status = await check_pipeline_dependencies()
        milvus_status = dep_status.get("milvus_connection", "error: check failed")
        if "ok" in milvus_status: # Aceptar 'ok' u 'ok (collection not found yet)'
            log.info("Haystack RAG pipeline built and Milvus connection status is OK.", status=milvus_status)
            pipeline_built = True
            milvus_startup_ok = True
        else:
             log.warning("Haystack RAG pipeline built, but initial Milvus check failed or pending.", status=milvus_status)
             pipeline_built = True # El pipeline se construyó, pero Milvus podría tener problemas
             milvus_startup_ok = False
    except Exception as e:
        log.error("Failed building Haystack RAG pipeline during startup.", error=str(e), exc_info=True)
        pipeline_built = False

    # Marcar como listo solo si las dependencias críticas (DB, Pipeline) están OK
    # Considerar si Milvus es estrictamente necesario para estar 'ready'
    if db_pool_initialized and pipeline_built: # Milvus puede recuperarse, pero DB y pipeline son esenciales
        SERVICE_READY = True
        log.info(f"{settings.PROJECT_NAME} service components initialized. SERVICE READY. Initial Milvus Check: {'OK' if milvus_startup_ok else 'WARN/FAIL'}")
    else:
        SERVICE_READY = False
        log.critical(f"{settings.PROJECT_NAME} startup FAILED critical components. DB OK: {db_pool_initialized}, Pipeline Built: {pipeline_built}. Service NOT READY.")


@app.on_event("shutdown")
async def shutdown_event():
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    await postgres_client.close_db_pool()
    log.info("Shutdown complete.")

# --- Exception Handlers ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # Log detallado incluyendo request ID si está disponible
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
         error_details = exc.errors() # Pydantic v2 / FastAPI >= 0.100
    except Exception:
         error_details = [{"loc": [], "msg": "Failed to parse validation errors.", "type": "internal_parsing_error"}]

    log.warning("Request Validation Error",
                path=str(request.url),
                method=request.method,
                client=request.client.host if request.client else "unknown",
                errors=error_details, # Loguear el detalle formateado
                request_id=request_id)

    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": error_details}, # FastAPI espera 'detail' como clave
    )

# *** AÑADIDO: Handler específico para ResponseValidationError ***
@app.exception_handler(ResponseValidationError)
async def response_validation_exception_handler(request: Request, exc: ResponseValidationError):
    request_id = request.headers.get("x-request-id", "N/A")
    # Loguear el error de validación de RESPUESTA - esto es un error del SERVIDOR
    log.error("Response Validation Error - Server failed to construct valid response",
              path=str(request.url),
              method=request.method,
              errors=exc.errors(), # Loguear los detalles del error de validación
              request_id=request_id,
              exc_info=True) # Incluir traceback

    # Devolver un error 500 genérico al cliente, porque es un problema del backend
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
# *** CORRECCIÓN: Eliminar el prefijo API_V1_STR ***
# El API Gateway maneja el prefijo /api/v1/query
app.include_router(query_router_module.router, tags=["Query Interaction"]) # Ruta expuesta: /ask
app.include_router(chat_router_module.router, tags=["Chat Management"])   # Rutas expuestas: /chats, /chats/{id}/messages, etc.
log.info("Routers included without prefix.")

# --- Root Endpoint / Health Check ---
@app.get("/", tags=["Health Check"], summary="Service Liveness/Readiness Check")
async def read_root(request: Request): # Añadir Request para poder loguear X-Request-ID
    global SERVICE_READY
    request_id = request.headers.get("x-request-id", "N/A")
    health_log = log.bind(request_id=request_id)
    health_log.debug("Health check endpoint '/' requested")

    if not SERVICE_READY:
         health_log.warning("Health check failed: Service not ready (SERVICE_READY is False).")
         # Devolver 503 Service Unavailable
         return PlainTextResponse("Service Not Ready", status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE)

    # Si el servicio se marcó como listo, verificar conexión DB como chequeo adicional
    db_ok = await postgres_client.check_db_connection()
    if db_ok:
        health_log.info("Health check successful (Service Ready, DB connection OK).")
        return PlainTextResponse("OK", status_code=fastapi_status.HTTP_200_OK)
    else:
        health_log.error("Health check failed: Service is READY but DB check FAILED.")
        # Marcar como no saludable si la DB falla ahora, aunque el servicio haya iniciado
        return PlainTextResponse("Service Ready but DB check failed", status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE)


# --- Main execution (for local development, sin cambios) ---
if __name__ == "__main__":
    # Nota: el puerto 8001 podría colisionar con ingest-service si corren localmente.
    # Cambiar a 8002 como indica el README del gateway para desarrollo local.
    port = 8002 # Usar puerto 8002 para query-service localmente
    log_level_str = settings.LOG_LEVEL.lower()
    print(f"----- Starting Query Service locally on port {port} -----")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True, log_level=log_level_str)