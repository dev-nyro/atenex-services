# ingest-service/app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response # Importar Response
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
from app.api.v1.endpoints import ingest # Importar el router directamente
from app.db import postgres_client

SERVICE_READY = False

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.1.0",
    description="Microservice for document ingestion and preprocessing using Haystack.",
)

# --- Event Handlers (Sin cambios) ---
@app.on_event("startup")
async def startup_event():
    global SERVICE_READY
    log.info("Starting up Ingest Service...")
    db_pool_initialized = False
    try:
        await postgres_client.get_db_pool()
        pool = await postgres_client.get_db_pool()
        async with pool.acquire() as conn:
            await asyncio.wait_for(conn.execute("SELECT 1"), timeout=10.0)
        log.info("PostgreSQL connection pool initialized and verified.")
        db_pool_initialized = True
    except Exception as e:
        log.critical("CRITICAL: Failed PostgreSQL connection on startup.", error=str(e), exc_info=True)
    if db_pool_initialized:
        SERVICE_READY = True
        log.info("Ingest Service startup sequence completed. READY.")
    else:
        SERVICE_READY = False
        log.warning("Ingest Service startup completed but DB connection failed. NOT READY.")

@app.on_event("shutdown")
async def shutdown_event():
    log.info("Shutting down Ingest Service..."); await postgres_client.close_db_pool(); log.info("Shutdown complete.")

# --- Exception Handlers (Sin cambios) ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc): log.warning("HTTP Exception", s=exc.status_code, d=exc.detail); return JSONResponse(s=exc.status_code, c={"detail": exc.detail})
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc): log.warning("Validation Error", e=exc.errors()); return JSONResponse(s=422, c={"detail": "Validation Error", "errors": exc.errors()})
@app.exception_handler(Exception)
async def generic_exception_handler(request, exc): log.exception("Unhandled Exception"); return JSONResponse(s=500, c={"detail": "Internal Server Error"})

# --- Router ---
# *** CORRECCIÓN: Usar el prefijo correcto ***
app.include_router(ingest.router, prefix="/api/v1/ingest", tags=["Ingestion"])
# Asegúrate que el prefijo aquí + la ruta en el endpoint coincidan con lo que llama el gateway
# Gateway llama a /api/v1/ingest/status -> Debe mapear a GET /status aquí
# Gateway llama a /api/v1/ingest/status/{id} -> Debe mapear a GET /status/{id} aquí
# Gateway llama a /api/v1/ingest/upload -> Debe mapear a POST /ingest aquí (porque el endpoint usa "/ingest")
# El prefijo parece correcto si las rutas en ingest.py son "/ingest", "/status", "/status/{id}"

# --- Root Endpoint / Health Check (Sin cambios) ---
@app.get("/", tags=["Health Check"], status_code=fastapi_status.HTTP_200_OK)
async def read_root():
    global SERVICE_READY; health_log = log.bind(check="liveness/readiness")
    if not SERVICE_READY: health_log.warning("Health check failed: Not Ready"); raise HTTPException(status_code=503, detail="Service not ready")
    try:
        pool = await postgres_client.get_db_pool()
        async with pool.acquire() as conn:
            await asyncio.wait_for(conn.execute("SELECT 1"), timeout=5.0)
        health_log.debug("Health check: DB ping successful.")
    except Exception as db_ping_err: health_log.error("Health check failed: DB ping error", e=db_ping_err); SERVICE_READY = False; raise HTTPException(status_code=503, detail="DB connection error")
    return Response(content="OK", status_code=fastapi_status.HTTP_200_OK, media_type="text/plain") # Cambiado a  respuesta simple OK