from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import structlog
import uvicorn

from app.api.v1.endpoints import ingest
from app.core.config import settings
from app.core.logging_config import setup_logging
from app.db import postgres_client, milvus_client

# Configurar logging ANTES de importar cualquier otra cosa que loguee
setup_logging()
log = structlog.get_logger(__name__)

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.1.0",
    description="Microservice for document ingestion and preprocessing pipeline.",
)

# --- Event Handlers ---
@app.on_event("startup")
async def startup_event():
    log.info("Starting up Ingest Service...")
    try:
        await postgres_client.get_db_pool() # Establece el pool de PG
        milvus_client.connect_milvus()      # Conecta a Milvus
        log.info("Database connections established.")
    except Exception as e:
        log.critical("Failed to establish database connections on startup", error=e, exc_info=True)
        # Podríamos decidir salir si las conexiones son críticas
        # raise SystemExit("Could not connect to databases") from e
    log.info("Ingest Service startup complete.")

@app.on_event("shutdown")
async def shutdown_event():
    log.info("Shutting down Ingest Service...")
    await postgres_client.close_db_pool()
    milvus_client.disconnect_milvus()
    # Aquí podrías cerrar también los clientes HTTP si los creaste globalmente
    log.info("Database connections closed.")
    log.info("Ingest Service shutdown complete.")

# --- Exception Handlers ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    log.warning("HTTP Exception caught", status_code=exc.status_code, detail=exc.detail, path=request.url.path)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    log.warning("Request Validation Error", errors=exc.errors(), path=request.url.path)
    # Formatear errores para que sean más legibles
    error_details = []
    for error in exc.errors():
        field = " -> ".join(map(str, error["loc"]))
        error_details.append({"field": field, "message": error["msg"]})
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation Error", "errors": error_details},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    log.error("Unhandled Exception caught", error=exc, path=request.url.path, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred."},
    )

# --- Routers ---
app.include_router(ingest.router, prefix=settings.API_V1_STR, tags=["Ingestion"])

# --- Root Endpoint ---
@app.get("/", tags=["Health Check"])
async def read_root():
    log.debug("Root endpoint accessed (health check)")
    return {"status": "ok", "service": settings.PROJECT_NAME}

# --- Main execution (for local development) ---
if __name__ == "__main__":
    log.info("Starting Uvicorn server for local development...")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000, # Puerto para el Ingest Service
        reload=True, # Activar reload para desarrollo
        log_level=settings.LOG_LEVEL.lower()
    )