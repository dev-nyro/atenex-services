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
from app.pipelines import rag_pipeline, check_pipeline_dependencies # Importar builder y check
from app.api.v1 import schemas # Importar schemas para health check

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
@app.on_event("startup")
async def startup_event():
    global SERVICE_READY, GLOBAL_RAG_PIPELINE
    log.info(f"Starting up {settings.PROJECT_NAME}...")
    db_pool_initialized = False
    pipeline_built = False

    # 1. Inicializar Pool de Base de Datos (Supabase)
    try:
        await postgres_client.get_db_pool()
        # Verificar conexión inicial
        db_ready = await postgres_client.check_db_connection()
        if db_ready:
            log.info("PostgreSQL connection pool initialized and connection verified.")
            db_pool_initialized = True
        else:
            log.critical("CRITICAL: Failed to verify PostgreSQL connection on startup.")
    except Exception as e:
        log.critical("CRITICAL: Failed to establish essential PostgreSQL connection pool on startup.", error=str(e), exc_info=True)

    # 2. Construir el Pipeline Haystack (depende de Milvus y Embedder/LLM Keys)
    # Intentar construirlo, pero no marcar el servicio como no listo si falla aquí,
    # podría recuperarse o las claves podrían cargarse más tarde. El health check lo verificará.
    # Opcionalmente, podríamos hacer que el startup falle si el pipeline no se construye.
    if db_pool_initialized: # Solo intentar si la BD está lista (puede que no sea necesario)
        try:
            # Forzar la construcción del pipeline ahora
            GLOBAL_RAG_PIPELINE = rag_pipeline.build_rag_pipeline()
            # Verificar dependencias del pipeline (Milvus)
            dep_status = await check_pipeline_dependencies()
            if dep_status.get("milvus_connection") == "ok":
                 log.info("Haystack RAG pipeline built and Milvus connection verified.")
                 pipeline_built = True
            else:
                 log.warning("Haystack RAG pipeline built, but Milvus check failed.", milvus_status=dep_status.get("milvus_connection"))
                 # Aún así, podríamos considerar el servicio 'listo' si el pipeline se construyó
                 # El health check manejará el estado de Milvus dinámicamente
                 pipeline_built = True # Marcar como construido, pero health check lo refinará
        except Exception as e:
            log.error("Failed to build Haystack RAG pipeline during startup.", error=str(e), exc_info=True)
            # No fallar el startup necesariamente, pero el servicio no estará completamente funcional.


    # Marcar servicio como listo SOLO si las dependencias MÍNIMAS (DB) están OK
    # El estado del pipeline/Milvus se reflejará en el health check detallado
    if db_pool_initialized:
        SERVICE_READY = True
        log.info(f"{settings.PROJECT_NAME} startup sequence completed. Service marked as READY (DB OK). Pipeline status: {'Built' if pipeline_built else 'Build Failed'}")
    else:
        SERVICE_READY = False
        log.critical(f"{settings.PROJECT_NAME} startup failed. Essential DB connection could not be established. Service marked as NOT READY.")
        # Podríamos forzar la salida si la BD es absolutamente esencial para CUALQUIER operación
        # sys.exit(1)


@app.on_event("shutdown")
async def shutdown_event():
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    await postgres_client.close_db_pool()
    log.info("PostgreSQL connection pool closed.")
    # No hay cliente Gemini global explícito para cerrar si usamos la instancia del módulo
    # Si hubiéramos creado un cliente en startup, lo cerraríamos aquí.
    log.info(f"{settings.PROJECT_NAME} shutdown complete.")

# --- Exception Handlers (Reutilizados) ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    # Evitar loguear 404 como warning si es común
    log_level = log.warning if exc.status_code < 500 and exc.status_code != 404 else log.error
    log_level("HTTP Exception caught", status_code=exc.status_code, detail=exc.detail, path=str(request.url))
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    log.warning("Request Validation Error", errors=exc.errors(), path=str(request.url))
    # Simplificar el formato del error para el cliente
    error_details = [{"loc": err.get("loc"), "msg": err.get("msg"), "type": err.get("type")} for err in exc.errors()]
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation Error", "errors": error_details},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    log.exception("Unhandled Exception caught", path=str(request.url)) # Usar log.exception para incluir traceback
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred."},
    )


# --- Routers ---
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
    Health check endpoint. Checks service readiness and critical dependencies like Database and Milvus.
    Returns 503 Service Unavailable if the service is not ready or dependencies are down.
    """
    global SERVICE_READY
    log.debug("Root endpoint accessed (health check)")

    # Verificar estado general del servicio (establecido en startup)
    if not SERVICE_READY:
         log.warning("Health check failed: Service did not start successfully (SERVICE_READY is False).")
         raise HTTPException(
             status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
             detail="Service is not ready, essential connections likely failed during startup."
         )

    # Realizar chequeos activos de dependencias
    db_status = "pending"
    milvus_status = "pending"
    overall_ready = True

    # Chequeo activo de la base de datos
    try:
        db_ok = await postgres_client.check_db_connection()
        if db_ok:
            db_status = "ok"
            log.debug("Health check: DB ping successful.")
        else:
            db_status = "error: Connection failed"
            overall_ready = False
            log.error("Health check failed: DB ping failed.")
    except Exception as db_err:
        db_status = f"error: {type(db_err).__name__}"
        overall_ready = False
        log.error("Health check failed: DB ping error.", error=str(db_err))

    # Chequeo activo de Milvus (a través de la función del pipeline)
    try:
        milvus_dep_status = await check_pipeline_dependencies()
        milvus_status = milvus_dep_status.get("milvus_connection", "error: Check failed")
        if "error" in milvus_status:
            # Considerar si Milvus caído debe marcar el servicio como no listo
            # Depende de si queremos que el servicio responda parcialmente o no
            # overall_ready = False # Descomentar si Milvus es crítico para estar 'ready'
            log.warning("Health check: Milvus check indicates an issue.", status=milvus_status)
        else:
            log.debug("Health check: Milvus check successful.")

    except Exception as milvus_err:
        milvus_status = f"error: {type(milvus_err).__name__}"
        # overall_ready = False # Descomentar si Milvus es crítico
        log.error("Health check failed: Milvus check error.", error=str(milvus_err))


    # Determinar el estado final HTTP
    http_status = fastapi_status.HTTP_200_OK if overall_ready else fastapi_status.HTTP_503_SERVICE_UNAVAILABLE

    response_data = schemas.HealthCheckResponse(
        status="ok" if overall_ready else "error",
        service=settings.PROJECT_NAME,
        ready=overall_ready,
        dependencies={
            "database": db_status,
            "vector_store": milvus_status
            # Añadir chequeo de Gemini/OpenAI si se desea (puede ser costoso/rate limited)
            # "llm_api": "pending",
            # "embedding_api": "pending"
        }
    )

    if not overall_ready:
         log.warning("Health check determined service is not ready.", dependencies=response_data.dependencies)
         # Lanzar excepción para devolver 503, usando el detail del response model
         raise HTTPException(status_code=http_status, detail=response_data.model_dump())
    else:
         log.info("Health check successful.", dependencies=response_data.dependencies)
         return response_data


# --- Main execution (for local development) ---
if __name__ == "__main__":
    # Asegurarse de que el logging esté configurado antes de correr uvicorn desde aquí
    # setup_logging() # Ya se llama al inicio del script
    log.info(f"Starting Uvicorn server for {settings.PROJECT_NAME} local development...")

    # Determinar nivel de log para Uvicorn basado en settings
    log_level_str = settings.LOG_LEVEL.lower()
    if log_level_str not in logging._nameToLevel:
        log.warning(f"Invalid LOG_LEVEL '{settings.LOG_LEVEL}', defaulting Uvicorn log level to 'info'.")
        log_level_str = "info"

    # Correr Uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0", # Escuchar en todas las interfaces
        port=8001,      # Usar un puerto diferente al de ingest-service (e.g., 8001)
        reload=True,    # Habilitar recarga automática para desarrollo
        log_level=log_level_str,
        # Podrías añadir use_colors=True si tu terminal lo soporta y no usas JSON
    )