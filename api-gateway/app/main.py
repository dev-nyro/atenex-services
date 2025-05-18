# File: app/main.py
# api-gateway/app/main.py
import os
from fastapi import FastAPI, Request, Depends, HTTPException, status
from typing import Optional, List, Set # Importado Set
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import httpx
import structlog
import uvicorn
import time
import uuid
import logging
import re # Mantenemos re por si se usa en otro lado

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

# --- Lifespan Manager (Sin cambios respecto a la versión actual) ---
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
    version="1.1.5", # Nueva versión para reflejar corrección CORS específica del error
    lifespan=lifespan,
)

# --- Middlewares ---

# --- CORRECCIÓN CORS ---
# Usar un set para la construcción inicial para evitar duplicados, luego convertir a lista.
allowed_origins_set: Set[str] = {
    "http://localhost:3000",    # Desarrollo local frontend
    "http://localhost:3001",    # Otro puerto de desarrollo local frontend
    "http://127.0.0.1:3000",    # Desarrollo local frontend
    "http://127.0.0.1:3001",    # Otro puerto de desarrollo local frontend
}

# Añadir el dominio de producción del frontend desde el que se origina el error
PROD_FRONTEND_URL = "https://www.atenex.pe"
allowed_origins_set.add(PROD_FRONTEND_URL)

# Añadir la URL base de Vercel si está configurada en las settings
if settings.VERCEL_FRONTEND_URL:
    allowed_origins_set.add(settings.VERCEL_FRONTEND_URL)

# Añadir la URL de preview de Vercel que estaba en el código original (si sigue siendo relevante)
# Es mejor si esta URL es configurable si cambia o hay múltiples previews.
VERCEL_PREVIEW_URL = "https://atenex-frontend-git-main-devnyro-gmailcoms-projects.vercel.app"
allowed_origins_set.add(VERCEL_PREVIEW_URL)

# Añadir la URL de Ngrok específica que aparece en el log de error del frontend
# Esta es la URL a la que el frontend está intentando conectarse.
NGROK_URL_FROM_ERROR_LOG = "https://1bdb-2001-1388-53a0-ca20-ec59-6cb3-85d5-9c1a.ngrok-free.app"
allowed_origins_set.add(NGROK_URL_FROM_ERROR_LOG)

# La variable NGROK_URL_FROM_LOG (con https://2646-...) del código original
# se puede eliminar o comentar si ya no es relevante, o si se prefiere que la URL de Ngrok
# venga de una variable de entorno (ej. settings.NGROK_URL).
# Por ahora, la omitiremos para enfocarnos en la URL del error.

final_allowed_origins = list(allowed_origins_set)

log.info("Configuring CORS middleware", allowed_origins=final_allowed_origins)

app.add_middleware(CORSMiddleware,
                   allow_origins=final_allowed_origins, # Usar la lista final de orígenes únicos
                   allow_credentials=True,
                   allow_methods=["*"], # Permite GET, POST, OPTIONS, etc.
                   allow_headers=["*"], # Permite todos los headers comunes (incluyendo Content-Type, Authorization, etc.)
                   expose_headers=["X-Request-ID", "X-Process-Time"],
                   max_age=600)
# --- FIN CORRECCIÓN CORS ---


# Request Context/Timing/Logging Middleware (Con mejora en logging de OPTIONS)
@app.middleware("http")
async def add_request_context_timing_logging(request: Request, call_next):
    start_time = time.perf_counter()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = request_id
    user_context = {}
    # No intentar extraer payload aquí, puede que aún no exista
    origin = request.headers.get("origin", "N/A") # Capturar Origin para logs y errores
    request_log = log.bind(request_id=request_id, method=request.method, path=request.url.path,
                           client_ip=request.client.host if request.client else "unknown",
                           origin=origin) # Añadir origin al log inicial

    # Loguear la recepción de OPTIONS a nivel INFO para visibilidad
    if request.method == "OPTIONS":
        request_log.info("OPTIONS preflight request received")
    else:
        # Extraer contexto de usuario si ya está disponible (p.ej., de middleware anterior)
        if hasattr(request.state, 'user') and isinstance(request.state.user, dict):
            user_context['user_id'] = request.state.user.get('sub')
            user_context['company_id'] = request.state.user.get('company_id')
            request_log = request_log.bind(**user_context) # Vincular contexto de usuario
        request_log.info("Request received")

    response = None
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        # Loguear headers de respuesta si es OPTIONS para depurar CORS
        if request.method == "OPTIONS" and response:
            # Asegurar que el middleware CORS ya añadió las cabeceras correctas
            request_log.info("OPTIONS preflight response sending", status_code=response.status_code, headers=dict(response.headers))
    except Exception as e:
        proc_time = (time.perf_counter() - start_time) * 1000
        # En caso de error, añadir contexto de usuario si está disponible
        if hasattr(request.state, 'user') and isinstance(request.state.user, dict):
             user_context['user_id'] = request.state.user.get('sub')
             user_context['company_id'] = request.state.user.get('company_id')
             request_log = request_log.bind(**user_context)
        request_log.exception("Unhandled exception processing request", status_code=500, error=str(e), proc_time=round(proc_time,2))

        # Crear respuesta de error genérica
        response = JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "Internal Server Error"})
        response.headers["X-Request-ID"] = request_id

        # ¡IMPORTANTE! Re-aplicar cabeceras CORS a respuestas de error
        # El middleware CORSMiddleware podría no ejecutarse completamente si la excepción ocurre muy temprano.
        req_origin = request.headers.get("Origin")
        # Usar final_allowed_origins para la comprobación
        if req_origin in final_allowed_origins:
             response.headers["Access-Control-Allow-Origin"] = req_origin
             response.headers["Access-Control-Allow-Credentials"] = "true"
             # Opcional: añadir otros headers CORS si son necesarios para errores
             # response.headers["Access-Control-Allow-Methods"] = "*"
             # response.headers["Access-Control-Allow-Headers"] = "*"
        return response # Devolver la respuesta de error con cabeceras CORS
    finally:
        # Loguear finalización solo para métodos que no sean OPTIONS (ya se logueó al enviar)
        if response and request.method != "OPTIONS":
            proc_time = (time.perf_counter() - start_time) * 1000
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time"] = f"{proc_time:.2f}ms"
            # Añadir contexto de usuario al log final si existe
            if hasattr(request.state, 'user') and isinstance(request.state.user, dict):
                user_context['user_id'] = request.state.user.get('sub')
                user_context['company_id'] = request.state.user.get('company_id')
                request_log = request_log.bind(**user_context)
            log_level = "debug" if request.url.path == "/health" else "info"
            log_func = getattr(request_log.bind(status_code=status_code), log_level)
            log_func("Request completed", proc_time=round(proc_time, 2))

    return response


# --- Include Routers (Sin cambios) ---
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