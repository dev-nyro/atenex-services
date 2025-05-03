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
import re # Mantenemos re por si se usa en otro lado, pero no para CORS aquí

# --- Configuración de Logging PRIMERO ---
from app.core.logging_config import setup_logging
setup_logging()

# --- Importaciones Core y DB ---
from app.core.config import settings
from app.db import postgres_client

# --- Importar Routers ---
from app.routers.gateway_router import router as gateway_router_instance
from app.routers.user_router import router as user_router_instance
from app.routers.admin_router import router as admin_router_instance

log = structlog.get_logger("atenex_api_gateway.main")

# --- Lifespan Manager (Sin cambios) ---
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
    version="1.1.3", # Nueva versión para reflejar corrección CORS
    lifespan=lifespan,
)

# --- Middlewares ---

# --- CORRECCIÓN CORS: Usar allow_origins en lugar de regex ---
allowed_origins = [
    "http://localhost:3000", # Puerto común de React/Next.js dev
    "http://localhost:3001", # Otro puerto posible
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
]

# Añadir la URL base de Vercel si está configurada
if settings.VERCEL_FRONTEND_URL:
    allowed_origins.append(settings.VERCEL_FRONTEND_URL)
    # ¡IMPORTANTE! Para que las PREVIEWS de Vercel funcionen, necesitas permitir
    # sus URLs específicas o usar un patrón regex más permisivo (que puede ser
    # menos seguro). Otra opción es añadir URLs de preview específicas aquí
    # temporalmente durante el desarrollo o usar una variable de entorno separada.
    # Ejemplo añadiendo la URL específica del log (NO RECOMENDADO para producción):
    allowed_origins.append("https://atenex-frontend-git-main-devnyro-gmailcoms-projects.vercel.app")

# Añadir la URL de Ngrok del log (considera hacerla configurable vía env var)
# ¡CAMBIA ESTO SI TU URL DE NGROK CAMBIA!
NGROK_URL_FROM_LOG = "https://2646-2001-1388-53a1-bd93-5941-79e3-d98a-2e11.ngrok-free.app"
allowed_origins.append(NGROK_URL_FROM_LOG)

log.info("Configuring CORS middleware", allowed_origins=allowed_origins)
app.add_middleware(CORSMiddleware,
                   allow_origins=allowed_origins, # Usar la lista de orígenes
                   allow_credentials=True,
                   allow_methods=["*"], # Permite GET, POST, OPTIONS, etc.
                   allow_headers=["*"], # Permite todos los headers comunes
                   expose_headers=["X-Request-ID", "X-Process-Time"],
                   max_age=600)
# --- FIN CORRECCIÓN CORS ---


# Request Context/Timing/Logging Middleware (Sin cambios)
@app.middleware("http")
async def add_request_context_timing_logging(request: Request, call_next):
    start_time = time.perf_counter()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = request_id
    user_context = {}
    if hasattr(request.state, 'user') and isinstance(request.state.user, dict):
        user_context['user_id'] = request.state.user.get('sub')
        user_context['company_id'] = request.state.user.get('company_id')
    # Incluir origin en el log inicial
    origin = request.headers.get("origin", "N/A")
    request_log = log.bind(request_id=request_id, method=request.method, path=request.url.path,
                           client_ip=request.client.host if request.client else "unknown",
                           origin=origin, **user_context)

    # Loguear la recepción de OPTIONS de forma diferente para depurar CORS
    if request.method == "OPTIONS":
        request_log.info("OPTIONS preflight request received") # Cambiado a INFO para visibilidad
    else:
        request_log.info("Request received")

    response = None
    status_code = 500
    try:
        response = await call_next(request); status_code = response.status_code
        # Loguear headers de respuesta si es OPTIONS para depurar CORS
        if request.method == "OPTIONS" and response:
            request_log.info("OPTIONS preflight response sent", status_code=response.status_code, headers=dict(response.headers))
    except Exception as e:
        proc_time = (time.perf_counter() - start_time) * 1000
        request_log.exception("Unhandled exception", status_code=500, error=str(e), proc_time=round(proc_time,2))
        # ¡Importante! Asegurar que las respuestas de error también incluyan cabeceras CORS
        response = JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
        response.headers["X-Request-ID"] = request_id
        # Re-aplicar CORS a respuestas de error (el middleware puede no hacerlo si la excepción ocurre antes)
        if origin in allowed_origins: # Comprobar si el origen está permitido
             response.headers["Access-Control-Allow-Origin"] = origin
             response.headers["Access-Control-Allow-Credentials"] = "true"
        return response
    finally:
        if response and request.method != "OPTIONS": # No loguear completion de OPTIONS si ya se logueó la respuesta
            proc_time = (time.perf_counter() - start_time) * 1000
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time"] = f"{proc_time:.2f}ms"
            log_level = "debug" if request.url.path == "/health" else "info"
            log_func = getattr(request_log.bind(status_code=status_code), log_level)
            log_func("Request completed", proc_time=round(proc_time, 2))
    return response

# --- Include Routers ---
log.info("Including application routers...")
app.include_router(user_router_instance, prefix="/api/v1/users", tags=["Users & Authentication"])
app.include_router(admin_router_instance, prefix="/api/v1/admin", tags=["Admin"])
app.include_router(gateway_router_instance, prefix="/api/v1") # Proxy general va último
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