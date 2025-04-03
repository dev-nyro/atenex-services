# api-gateway/app/main.py
import os
from fastapi import FastAPI, Request, Depends, HTTPException, status
from typing import Optional
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import httpx
import structlog
import uvicorn
import time
import uuid
import logging # <-- Importar logging estándar
from supabase.client import Client as SupabaseClient

# Configurar logging ANTES de importar otros módulos que puedan loguear
from app.core.logging_config import setup_logging
setup_logging()
# Ahora importar el resto
from app.core.config import settings
from app.utils.supabase_admin import get_supabase_admin_client
from app.routers import gateway_router, user_router
# *** CORRECCIÓN: Comentar o eliminar importación de auth_router si ya no se usa ***
# from app.routers import auth_router
# Importar dependencias de autenticación para verificar su carga
from app.auth.auth_middleware import StrictAuth, InitialAuth

log = structlog.get_logger("api_gateway.main") # Logger para el módulo main

# Clientes globales que se inicializarán en el lifespan
proxy_http_client: Optional[httpx.AsyncClient] = None
supabase_admin_client: Optional[SupabaseClient] = None

# --- Lifespan para inicializar/cerrar clientes (Sin cambios) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global proxy_http_client, supabase_admin_client
    log.info("Application startup: Initializing global clients...")
    try:
        limits = httpx.Limits(
            max_keepalive_connections=settings.HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS,
            max_connections=settings.HTTP_CLIENT_MAX_CONNECTIONS
        )
        timeout = httpx.Timeout(settings.HTTP_CLIENT_TIMEOUT, connect=10.0)
        proxy_http_client = httpx.AsyncClient(limits=limits, timeout=timeout, follow_redirects=False, http2=True)
        gateway_router.http_client = proxy_http_client
        log.info("HTTP Client initialized successfully.", limits=str(limits), timeout=str(timeout))
    except Exception as e:
        log.exception("CRITICAL: Failed to initialize HTTP client during startup!", error=str(e))
        proxy_http_client = None; gateway_router.http_client = None
    log.info("Initializing Supabase Admin Client...")
    try:
        supabase_admin_client = get_supabase_admin_client()
        user_router.supabase_admin = supabase_admin_client # Inyección simple, mejor Depends
        log.info("Supabase Admin Client reference obtained (initialized via get_supabase_admin_client).")
    except Exception as e:
        log.exception("CRITICAL: Failed to get Supabase Admin Client during startup!", error=str(e))
        supabase_admin_client = None; user_router.supabase_admin = None
    yield
    log.info("Application shutdown: Closing clients...")
    if proxy_http_client and not proxy_http_client.is_closed:
        try: await proxy_http_client.aclose(); log.info("HTTP Client closed successfully.")
        except Exception as e: log.exception("Error closing HTTP client during shutdown.", error=str(e))
    log.info("Supabase Admin Client shutdown check complete (no explicit close needed).")


# --- Creación de la App FastAPI ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Punto de entrada único y seguro para los microservicios de Nyro. Valida JWTs de Supabase, gestiona asociación inicial de compañía y reenvía tráfico a servicios backend.",
    version="1.0.0",
    lifespan=lifespan,
)

# --- Middlewares ---

# 1. CORS Middleware (Configuración robusta - Sin cambios respecto a tu código)
allowed_origins = []
vercel_url = os.getenv("VERCEL_FRONTEND_URL", "https://atenex-frontend.vercel.app")
allowed_origins.append(vercel_url)
localhost_url = "http://localhost:3000"
allowed_origins.append(localhost_url)
ngrok_url_env = os.getenv("NGROK_URL")
if ngrok_url_env:
    if ngrok_url_env.startswith("https://") and ".ngrok" in ngrok_url_env:
        allowed_origins.append(ngrok_url_env)
ngrok_url_from_logs = "https://1942-2001-1388-53a1-a7c9-241c-4a44-2b12-938f.ngrok-free.app"
if ngrok_url_from_logs not in allowed_origins:
    allowed_origins.append(ngrok_url_from_logs)
allowed_origins = list(set(filter(None, allowed_origins)))
allowed_headers = ["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With", "ngrok-skip-browser-warning", "X-Request-ID"]
allowed_methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
log.info("Final CORS Allowed Origins:", origins=allowed_origins)
log.info("CORS Allowed Headers:", headers=allowed_headers)
log.info("CORS Allowed Methods:", methods=allowed_methods)
app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_credentials=True, allow_methods=allowed_methods, allow_headers=allowed_headers, expose_headers=["X-Request-ID", "X-Process-Time"], max_age=600)

# 2. Middleware Request ID, Timing, Logging (Sin cambios respecto a tu código)
@app.middleware("http")
async def add_request_id_timing_logging(request: Request, call_next):
    start_time = time.time(); request_id = request.headers.get("x-request-id", str(uuid.uuid4())); request.state.request_id = request_id
    with structlog.contextvars.bound_contextvars(request_id=request_id):
        bound_log = structlog.get_logger("api_gateway.requests")
        bound_log.info("Request received", method=request.method, path=request.url.path, client_ip=request.client.host if request.client else "N/A", user_agent=request.headers.get("user-agent", "N/A")[:100])
        response = None
        try:
            response = await call_next(request)
            process_time = time.time() - start_time
            response.headers["X-Process-Time"] = f"{process_time:.4f}"; response.headers["X-Request-ID"] = request_id
            bound_log.info("Request processed successfully", status_code=response.status_code, duration=round(process_time, 4))
        except Exception as e:
            process_time = time.time() - start_time
            bound_log.exception("Unhandled exception during request processing", duration=round(process_time, 4), error_type=type(e).__name__, error=str(e))
            raise e
        finally: pass
        return response

# --- Routers ---
# *** CORRECCIÓN: Comentar o eliminar inclusión de auth_router si ya no se usa o está vacío ***
# app.include_router(auth_router.router) # Ya no necesario si no hay rutas auth en gateway
app.include_router(gateway_router.router) # Proxy principal
app.include_router(user_router.router) # Rutas relacionadas con usuario (si las hay)

# --- Endpoints Básicos (Sin cambios) ---
@app.get("/", tags=["Gateway Status"], summary="Root endpoint", include_in_schema=False)
async def root(): return {"message": f"{settings.PROJECT_NAME} is running!"}
@app.get("/health", tags=["Gateway Status"], summary="Kubernetes Health Check", status_code=status.HTTP_200_OK, response_description="Indicates if the gateway and its core dependencies are healthy.")
async def health_check():
    admin_client_status = "available" if supabase_admin_client else "unavailable"
    http_client_status = "available" if proxy_http_client and not proxy_http_client.is_closed else "unavailable"
    is_healthy = admin_client_status == "available" and http_client_status == "available"
    health_details = {"status": "healthy" if is_healthy else "unhealthy", "service": settings.PROJECT_NAME, "dependencies": {"supabase_admin_client": admin_client_status, "proxy_http_client": http_client_status}}
    if not is_healthy: log.error("Health check failed", details=health_details); raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=health_details)
    log.debug("Health check passed.", details=health_details); return health_details

# --- Manejadores de Excepciones Globales (Sin cambios) ---
@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    req_id = getattr(request.state, 'request_id', 'N/A'); bound_log = log.bind(request_id=req_id)
    log_level_name = "warning" if 400 <= exc.status_code < 500 else "error"; log_method = getattr(bound_log, log_level_name, bound_log.info)
    log_method("HTTP Exception occurred", status_code=exc.status_code, detail=exc.detail, path=request.url.path, exception_headers=exc.headers)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    req_id = getattr(request.state, 'request_id', 'N/A'); bound_log = log.bind(request_id=req_id)
    bound_log.exception("Unhandled internal server error occurred in gateway", path=request.url.path, error_type=type(exc).__name__)
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "An unexpected internal server error occurred."})

# --- Log final de configuración (Sin cambios) ---
log.info(f"'{settings.PROJECT_NAME}' application configured and ready to start.", log_level=settings.LOG_LEVEL, ingest_service=str(settings.INGEST_SERVICE_URL), query_service=str(settings.QUERY_SERVICE_URL), auth_proxy_enabled=bool(settings.AUTH_SERVICE_URL), supabase_url=str(settings.SUPABASE_URL), default_company_id_set=bool(settings.DEFAULT_COMPANY_ID))