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
import logging # <-- Importar logging
from supabase.client import Client as SupabaseClient

# ... (resto de imports y código inicial sin cambios) ...
from app.core.config import settings
from app.core.logging_config import setup_logging
setup_logging()
from app.utils.supabase_admin import get_supabase_admin_client
from app.routers import gateway_router, user_router

log = structlog.get_logger("api_gateway.main")
proxy_http_client: Optional[httpx.AsyncClient] = None
supabase_admin_client: Optional[SupabaseClient] = None

# --- Lifespan (Sin cambios) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global proxy_http_client, supabase_admin_client
    log.info("Application startup: Initializing global clients...")
    try:
        limits = httpx.Limits(
            max_keepalive_connections=settings.HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS,
            max_connections=settings.HTTP_CLIENT_MAX_CONNECTIONS
        )
        timeout = httpx.Timeout(
            settings.HTTP_CLIENT_TIMEOUT, connect=10.0,
            write=settings.HTTP_CLIENT_TIMEOUT, pool=settings.HTTP_CLIENT_TIMEOUT
        )
        proxy_http_client = httpx.AsyncClient(
             limits=limits, timeout=timeout, follow_redirects=False, http2=True
        )
        gateway_router.http_client = proxy_http_client
        log.info("HTTP Client initialized successfully.", limits=str(limits), timeout=str(timeout))
    except Exception as e:
        log.exception("Failed to initialize HTTP client during startup!", error=str(e))
        proxy_http_client = None
        gateway_router.http_client = None

    log.info("Initializing Supabase Admin Client...")
    try:
        supabase_admin_client = get_supabase_admin_client()
        user_router.supabase_admin = supabase_admin_client # Inyección menos ideal
        log.info("Supabase Admin Client reference obtained (initialized via get_supabase_admin_client).")
    except Exception as e:
        log.exception("Failed to get Supabase Admin Client during startup!", error=str(e))
        supabase_admin_client = None

    yield # Application runs here

    log.info("Application shutdown: Closing clients...")
    if proxy_http_client and not proxy_http_client.is_closed:
        try:
            await proxy_http_client.aclose()
            log.info("HTTP Client closed successfully.")
        except Exception as e:
            log.exception("Error closing HTTP client during shutdown.", error=str(e))
    else:
        log.warning("HTTP Client was not available or already closed during shutdown.")
    log.info("Supabase Admin Client shutdown check complete (no explicit close needed).")


# --- FastAPI App Creation (Sin cambios) ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Punto de entrada único y seguro para los microservicios de Nyro.",
    version="1.0.0",
    lifespan=lifespan,
)

# --- Middlewares (CORS y Request ID/Timing - Sin cambios respecto a la versión anterior) ---
# ... (Código de configuración CORS y middleware add_request_id_timing_logging sin cambios) ...
allowed_origins = []
# ... (lógica para añadir Vercel, localhost, Ngrok a allowed_origins) ...
vercel_url = os.getenv("VERCEL_FRONTEND_URL")
if vercel_url:
    log.info(f"Adding Vercel frontend URL from env var to allowed origins: {vercel_url}")
    allowed_origins.append(vercel_url)
else:
    vercel_fallback_url = "https://atenex-frontend.vercel.app"
    log.warning(f"VERCEL_FRONTEND_URL env var not set. Using fallback: {vercel_fallback_url}")
    allowed_origins.append(vercel_fallback_url)
localhost_url = "http://localhost:3000"
log.info(f"Adding localhost frontend URL to allowed origins: {localhost_url}")
allowed_origins.append(localhost_url)
ngrok_url_from_frontend_logs = "https://1942-2001-1388-53a1-a7c9-241c-4a44-2b12-938f.ngrok-free.app"
log.info(f"Adding specific Ngrok URL observed in frontend logs: {ngrok_url_from_frontend_logs}")
allowed_origins.append(ngrok_url_from_frontend_logs)
ngrok_url_env = os.getenv("NGROK_URL")
if ngrok_url_env and ngrok_url_env not in allowed_origins:
    if ngrok_url_env.startswith("https://") or ngrok_url_env.startswith("http://"):
        log.info(f"Adding Ngrok URL from NGROK_URL env var to allowed origins: {ngrok_url_env}")
        allowed_origins.append(ngrok_url_env)
    else:
        log.warning(f"NGROK_URL environment variable has an unexpected format: {ngrok_url_env}. Ignoring.")
allowed_origins = list(set(filter(None, allowed_origins)))
if not allowed_origins: log.critical("CRITICAL: No allowed origins configured for CORS.")
else: log.info("Final CORS Allowed Origins:", origins=allowed_origins)
allowed_headers = ["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With", "ngrok-skip-browser-warning"]
log.info("CORS Allowed Headers:", headers=allowed_headers)
allowed_methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
log.info("CORS Allowed Methods:", methods=allowed_methods)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins, allow_credentials=True, allow_methods=allowed_methods,
    allow_headers=allowed_headers, expose_headers=["X-Request-ID", "X-Process-Time"], max_age=600,
)
@app.middleware("http")
async def add_request_id_timing_logging(request: Request, call_next):
    start_time = time.time()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = request_id
    with structlog.contextvars.bound_contextvars(request_id=request_id):
        bound_log = structlog.get_logger("api_gateway.requests")
        bound_log.info("Request received", method=request.method, path=request.url.path, client_ip=request.client.host if request.client else "N/A", user_agent=request.headers.get("user-agent", "N/A")[:100])
        try:
            response = await call_next(request)
            process_time = time.time() - start_time
            response.headers["X-Process-Time"] = f"{process_time:.4f}"
            response.headers["X-Request-ID"] = request_id
            bound_log.info("Request processed successfully", status_code=response.status_code, duration=round(process_time, 4))
        except Exception as e:
            process_time = time.time() - start_time
            bound_log.exception("Unhandled exception during request processing", duration=round(process_time, 4), error_type=type(e).__name__, error=str(e))
            raise e
        return response

# --- Routers (Sin cambios) ---
app.include_router(gateway_router.router)
app.include_router(user_router.router)

# --- Endpoints Básicos (Sin cambios) ---
@app.get("/", tags=["Gateway Status"], summary="Root endpoint", include_in_schema=False)
async def root():
    return {"message": f"{settings.PROJECT_NAME} is running!"}

@app.get("/health", tags=["Gateway Status"], summary="Kubernetes Health Check", status_code=status.HTTP_200_OK)
async def health_check():
     admin_client_status = "available" if supabase_admin_client else "unavailable"
     http_client_status = "available" if proxy_http_client and not proxy_http_client.is_closed else "unavailable"
     is_healthy = admin_client_status == "available" and http_client_status == "available"
     health_details = {"status": "healthy" if is_healthy else "unhealthy", "service": settings.PROJECT_NAME,"dependencies": {"supabase_admin_client": admin_client_status,"proxy_http_client": http_client_status}}
     if not is_healthy:
         log.error("Health check failed", details=health_details)
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=health_details)
     log.debug("Health check passed.", details=health_details)
     return health_details

# --- Manejadores de Excepciones (CORREGIDO) ---
@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    req_id = getattr(request.state, 'request_id', 'N/A')
    bound_log = log.bind(request_id=req_id)

    # *** CORRECCIÓN: Usar niveles numéricos de logging ***
    log_level_int = logging.WARNING if 400 <= exc.status_code < 500 else logging.ERROR
    log_level_name = logging.getLevelName(log_level_int).lower() # Obtener nombre ('warning', 'error')

    # Usar el método específico (warning, error) en lugar de .log() para evitar KeyError
    log_method = getattr(bound_log, log_level_name, bound_log.info) # Fallback a info si el nivel no existe
    log_method("HTTP Exception occurred",
               status_code=exc.status_code,
               detail=exc.detail,
               path=request.url.path)

    headers = getattr(exc, "headers", None)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=headers
    )

# Handler genérico para errores 500 no esperados (Sin cambios, ya usaba exception)
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    req_id = getattr(request.state, 'request_id', 'N/A')
    bound_log = log.bind(request_id=req_id)
    bound_log.exception("Unhandled internal server error occurred in gateway",
                        path=request.url.path,
                        error_type=type(exc).__name__)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected internal server error occurred."}
    )

# Log final (Sin cambios)
log.info(f"'{settings.PROJECT_NAME}' application configured and ready to start.",
         allowed_origins=allowed_origins,
         allowed_methods=allowed_methods,
         allowed_headers=allowed_headers)