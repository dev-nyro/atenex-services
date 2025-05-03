# File: app/main.py
# api-gateway/app/main.py
import os
from fastapi import FastAPI, Request, Depends, HTTPException, status
from typing import Optional, List, Set
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import httpx
import structlog
import uvicorn
import time
import uuid
import logging
import re

# --- Configuración de Logging PRIMERO ---
from app.core.logging_config import setup_logging
setup_logging()

# --- Importaciones Core y DB ---
from app.core.config import settings
from app.db import postgres_client

# --- Importar Routers ---
from app.routers.gateway_router import router as gateway_router_instance
from app.routers.user_router import router as user_router_instance
# --- AÑADIDO: Importar el nuevo router de admin ---
from app.routers.admin_router import router as admin_router_instance
# from app.routers.auth_router import router as auth_router_instance # Probablemente no necesario

log = structlog.get_logger("atenex_api_gateway.main")

# --- Lifespan Manager (Sin cambios respecto a la versión anterior) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Application startup sequence initiated...")
    http_client_instance: Optional[httpx.AsyncClient] = None
    db_pool_ok = False
    try:
        log.info("Initializing HTTPX client for application state...")
        limits = httpx.Limits(max_keepalive_connections=settings.HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS, max_connections=settings.HTTP_CLIENT_MAX_CONNECTIONS)
        timeout = httpx.Timeout(settings.HTTP_CLIENT_TIMEOUT, connect=15.0)
        http_client_instance = httpx.AsyncClient(limits=limits, timeout=timeout, follow_redirects=False, http2=True)
        app.state.http_client = http_client_instance
        log.info("HTTPX client initialized and attached to app.state successfully.")
    except Exception as e:
        log.exception("CRITICAL: Failed to initialize HTTPX client during startup!", error=str(e))
        app.state.http_client = None
    log.info("Initializing and verifying PostgreSQL connection pool...")
    try:
        pool = await postgres_client.get_db_pool()
        if pool:
            db_pool_ok = await postgres_client.check_db_connection()
            if db_pool_ok: log.info("PostgreSQL connection pool initialized and connection verified.")
            else: log.critical("PostgreSQL pool initialized BUT connection check failed!"); await postgres_client.close_db_pool()
        else: log.critical("PostgreSQL connection pool initialization returned None!")
    except Exception as e: log.exception("CRITICAL: Failed to initialize or verify PostgreSQL connection!", error=str(e)); db_pool_ok = False
    if getattr(app.state, 'http_client', None) and db_pool_ok: log.info("Application startup sequence complete. Dependencies ready.")
    else: log.error("Application startup sequence FAILED.", http_client_ready=bool(getattr(app.state, 'http_client', None)), db_ready=db_pool_ok)
    yield
    log.info("Application shutdown sequence initiated...")
    client_to_close = getattr(app.state, 'http_client', None)
    if client_to_close and not client_to_close.is_closed:
        log.info("Closing HTTPX client from app.state...")
        try:
            await client_to_close.aclose()
            log.info("HTTPX client closed.")
        except Exception as e:
            log.exception("Error closing HTTPX client.", error=str(e))
    else:
        log.info("HTTPX client was not initialized or already closed.")
    log.info("Closing PostgreSQL connection pool...")
    try:
        await postgres_client.close_db_pool()
    except Exception as e:
        log.exception("Error closing PostgreSQL pool.", error=str(e))
    log.info("Application shutdown complete.")

# --- Create FastAPI App Instance ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Atenex API Gateway: Single entry point, JWT auth, routing via explicit HTTP calls, Admin API.",
    version="1.1.0", # Version bump para reflejar cambios admin
    lifespan=lifespan,
)

# --- Middlewares ---
# CORS (Configuración sin cambios, asumiendo que 'final_regex' está definido correctamente antes)
vercel_pattern = ""
if settings.VERCEL_FRONTEND_URL:
    base_vercel_url = settings.VERCEL_FRONTEND_URL.split("://")[1]
    base_vercel_url = re.sub(r"(-git-[a-z0-9-]+)?(-[a-z0-9]+)?\.vercel\.app", ".vercel.app", base_vercel_url)
    escaped_base = re.escape(base_vercel_url).replace(r"\.vercel\.app", "")
    vercel_pattern = rf"(https://{escaped_base}(-[a-z0-9-]+)*\.vercel\.app)"
else: log.warning("VERCEL_FRONTEND_URL not set for CORS.")
localhost_pattern = r"(http://localhost:300[0-9]|http://127.0.0.1:300[0-9])" # Añadido 127.0.0.1
allowed_origin_patterns = [localhost_pattern];
if vercel_pattern: allowed_origin_patterns.append(vercel_pattern)
final_regex = rf"^{ '|'.join(allowed_origin_patterns) }$" if allowed_origin_patterns else "" # Manejar caso sin patrones
if final_regex:
    log.info("Configuring CORS middleware", allow_origin_regex=final_regex)
    app.add_middleware(CORSMiddleware, allow_origin_regex=final_regex, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"],
                       expose_headers=["X-Request-ID", "X-Process-Time"], max_age=600)
else:
    log.warning("No CORS origins configured. CORS middleware not added.")

# Request Context/Timing/Logging Middleware (Sin cambios)
@app.middleware("http")
async def add_request_context_timing_logging(request: Request, call_next):
    start_time = time.perf_counter()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = request_id
    # Añadir user_id/company_id al log si están en el estado (puestos por auth middleware)
    user_context = {}
    if hasattr(request.state, 'user') and isinstance(request.state.user, dict):
        user_context['user_id'] = request.state.user.get('sub')
        user_context['company_id'] = request.state.user.get('company_id')
    request_log = log.bind(request_id=request_id, method=request.method, path=request.url.path,
                           client_ip=request.client.host if request.client else "unknown",
                           origin=request.headers.get("origin", "N/A"), **user_context)
    if request.method == "OPTIONS": request_log.debug("OPTIONS preflight request received")
    else: request_log.info("Request received")
    response = None; status_code = 500
    try:
        response = await call_next(request); status_code = response.status_code
    except Exception as e:
        proc_time = (time.perf_counter() - start_time) * 1000
        request_log.exception("Unhandled exception", status_code=500, error=str(e), proc_time=round(proc_time,2))
        response = JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
        origin = request.headers.get("Origin");
        if final_regex and origin and re.match(final_regex, origin):
             response.headers["Access-Control-Allow-Origin"] = origin
             response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        if response:
            proc_time = (time.perf_counter() - start_time) * 1000
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time"] = f"{proc_time:.2f}ms"
            log_level = "debug" if request.url.path == "/health" else "info"
            log_func = getattr(request_log.bind(status_code=status_code), log_level)
            if request.method != "OPTIONS": log_func("Request completed", proc_time=round(proc_time, 2))
    return response

# --- Include Routers ---
log.info("Including application routers...")
# User router (prefix /api/v1/users)
app.include_router(user_router_instance, prefix="/api/v1", tags=["Users & Authentication"]) # Mantenemos prefix /api/v1 aquí para que coincida con paths internos
# Admin router (prefix /api/v1/admin)
app.include_router(admin_router_instance, prefix="/api/v1/admin", tags=["Admin"]) # Añadir el router admin
# Gateway router (prefix /api/v1) - Debe ir DESPUÉS de los más específicos si hay solapamiento
app.include_router(gateway_router_instance, prefix="/api/v1")
log.info("Routers included successfully.")

# --- Root & Health Endpoints (Sin cambios) ---
@app.get("/", tags=["General"], summary="Root endpoint", include_in_schema=False)
async def read_root():
    return {"message": f"{settings.PROJECT_NAME} is running!"}

@app.get("/health", tags=["Health"], summary="Health check endpoint")
async def health_check(request: Request):
    health_status = {"status": "healthy", "service": settings.PROJECT_NAME, "checks": {}}
    db_ok = await postgres_client.check_db_connection()
    health_status["checks"]["database_connection"] = "ok" if db_ok else "failed"
    http_client = getattr(request.app.state, 'http_client', None)
    http_client_ok = http_client is not None and not http_client.is_closed
    health_status["checks"]["http_client"] = "ok" if http_client_ok else "failed"
    if not db_ok or not http_client_ok:
        health_status["status"] = "unhealthy"
        log.warning("Health check determined service unhealthy", checks=health_status["checks"])
        return JSONResponse(content=health_status, status_code=503)
    log.debug("Health check successful", checks=health_status["checks"])
    return health_status

# --- Main Execution (Sin cambios) ---
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    host = os.getenv("HOST", "0.0.0.0")
    reload_flag = os.getenv("UVICORN_RELOAD", "false").lower() == "true"
    log_level_uvicorn = settings.LOG_LEVEL.lower()

    print(f"Starting {settings.PROJECT_NAME} using Uvicorn...")
    print(f" Host: {host}")
    print(f" Port: {port}")
    print(f" Reload: {reload_flag}")
    print(f" Log Level: {log_level_uvicorn}")

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload_flag,
        log_level=log_level_uvicorn
    )