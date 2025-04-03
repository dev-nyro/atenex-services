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
from supabase.client import Client as SupabaseClient

# Configuración y Settings
from app.core.config import settings
# Configuración de Logging
from app.core.logging_config import setup_logging
setup_logging() # Configurar logging al inicio

# Importar cliente Supabase Admin
from app.utils.supabase_admin import get_supabase_admin_client

# Routers
from app.routers import gateway_router
from app.routers import user_router

log = structlog.get_logger("api_gateway.main")

# Variables globales para clientes (inicializadas en lifespan)
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
        timeout = httpx.Timeout(settings.HTTP_CLIENT_TIMEOUT, connect=10.0)
        proxy_http_client = httpx.AsyncClient(
             limits=limits,
             timeout=timeout,
             follow_redirects=False,
             http2=True
        )
        gateway_router.http_client = proxy_http_client
        log.info("HTTP Client initialized successfully.", limits=limits, timeout=timeout)
    except Exception as e:
        log.exception("Failed to initialize HTTP client during startup!", error=e)
        proxy_http_client = None
        gateway_router.http_client = None

    log.info("Initializing Supabase Admin Client...")
    try:
        supabase_admin_client = get_supabase_admin_client()
        log.info("Supabase Admin Client initialized successfully.")
    except Exception as e:
        log.exception("Failed to initialize Supabase Admin Client during startup!", error=e)
        supabase_admin_client = None

    yield

    log.info("Application shutdown: Closing clients...")
    if proxy_http_client and not proxy_http_client.is_closed:
        try:
            await proxy_http_client.aclose()
            log.info("HTTP Client closed successfully.")
        except Exception as e:
            log.exception("Error closing HTTP client during shutdown.", error=e)
    else:
        log.warning("HTTP Client was not available or already closed.")
    if supabase_admin_client:
        log.info("Supabase Admin Client shutdown.")

# --- Creación de la aplicación FastAPI (Sin cambios) ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Punto de entrada único y seguro para los microservicios de Nyro.",
    version="1.0.0",
    lifespan=lifespan,
)

# --- Middlewares ---

# --- CORSMiddleware PRIMERO (Sin cambios respecto a la versión anterior) ---
allowed_origins = []
vercel_url = os.getenv("VERCEL_FRONTEND_URL")
if vercel_url:
    log.info(f"Adding Vercel frontend URL to allowed origins: {vercel_url}")
    allowed_origins.append(vercel_url)
else:
    vercel_fallback_url = "https://atenex-frontend.vercel.app"
    log.warning(f"VERCEL_FRONTEND_URL env var not set. Using fallback: {vercel_fallback_url}")
    allowed_origins.append(vercel_fallback_url)
localhost_url = "http://localhost:3000"
log.info(f"Adding localhost frontend URL to allowed origins: {localhost_url}")
allowed_origins.append(localhost_url)
ngrok_url_from_log = "https://5158-2001-1388-53a1-a7c9-fd46-ef87-59cf-a7f7.ngrok-free.app"
log.info(f"Adding specific Ngrok URL from logs to allowed origins: {ngrok_url_from_log}")
allowed_origins.append(ngrok_url_from_log)
ngrok_url_env = os.getenv("NGROK_URL")
if ngrok_url_env and ngrok_url_env not in allowed_origins:
    if ngrok_url_env.startswith("https://") or ngrok_url_env.startswith("http://"):
        log.info(f"Adding Ngrok URL from NGROK_URL env var to allowed origins: {ngrok_url_env}")
        allowed_origins.append(ngrok_url_env)
    else:
        log.warning(f"NGROK_URL environment variable has an unexpected format: {ngrok_url_env}")
allowed_origins = list(set(filter(None, allowed_origins)))
if not allowed_origins:
    log.critical("CRITICAL: No allowed origins configured for CORS.")
else:
    log.info("Final CORS Allowed Origins:", origins=allowed_origins)
allowed_headers = ["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With", "ngrok-skip-browser-warning"]
log.info("CORS Allowed Headers:", headers=allowed_headers)
allowed_methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
log.info("CORS Allowed Methods:", methods=allowed_methods)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=allowed_methods,
    allow_headers=allowed_headers,
    expose_headers=["X-Request-ID", "X-Process-Time"],
    max_age=600,
)
# --- Fin CORSMiddleware ---


# --- Otros Middlewares (Request ID, Timing) ---
# Añadido DESPUÉS de CORS
@app.middleware("http")
async def add_process_time_header_and_request_id(request: Request, call_next):
    start_time = time.time()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = request_id

    with structlog.contextvars.bind_contextvars(request_id=request_id):
        # --- !!! CORRECCIÓN DE INDENTACIÓN AQUÍ !!! ---
        # El log.info y el bloque try/except deben estar indentados UN NIVEL dentro del 'with'
        log.info("Request received", method=request.method, path=request.url.path, client_ip=request.client.host if request.client else "N/A")

        try:
            response = await call_next(request)
            process_time = time.time() - start_time
            response.headers["X-Process-Time"] = str(process_time)
            response.headers["X-Request-ID"] = request_id
            log.info("Request processed successfully", status_code=response.status_code, duration=round(process_time, 4))
        except Exception as e:
            process_time = time.time() - start_time
            log.exception("Unhandled exception during request processing", duration=round(process_time, 4), error=str(e))
            raise e
        return response
        # --- !!! FIN CORRECCIÓN DE INDENTACIÓN !!! ---
# --- Fin otros Middlewares ---


# --- Routers (Sin cambios) ---
app.include_router(gateway_router.router)
app.include_router(user_router.router)

# --- Endpoints Básicos y Manejadores de Excepciones (Sin cambios) ---
@app.get("/", tags=["Gateway Status"], summary="Root endpoint")
async def root():
    return {"message": f"{settings.PROJECT_NAME} is running!"}

@app.get("/health", tags=["Gateway Status"], summary="Kubernetes Health Check", status_code=status.HTTP_200_OK)
async def health_check():
     admin_client: Optional[SupabaseClient] = supabase_admin_client
     if not admin_client:
         log.error("Health check failed: Supabase Admin Client not available.")
         raise HTTPException(status_code=503, detail="Gateway service dependency unavailable (Admin Client).")
     http_client_check: Optional[httpx.AsyncClient] = proxy_http_client
     if not http_client_check or http_client_check.is_closed:
         log.error("Health check failed: HTTP Client not available or closed.")
         raise HTTPException(status_code=503, detail="Gateway service dependency unavailable (HTTP Client).")
     log.debug("Health check passed.")
     return {"status": "healthy", "service": settings.PROJECT_NAME}

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    req_id = getattr(request.state, 'request_id', 'N/A')
    bound_log = log.bind(request_id=req_id)
    bound_log.warning("HTTP Exception occurred", status_code=exc.status_code, detail=exc.detail, path=request.url.path)
    headers = exc.headers if exc.status_code == 401 else None
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=headers
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    req_id = getattr(request.state, 'request_id', 'N/A')
    bound_log = log.bind(request_id=req_id)
    bound_log.exception("Unhandled internal server error occurred in gateway", path=request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred."}
    )

log.info(f"'{settings.PROJECT_NAME}' application configured and ready to start.", allowed_origins=allowed_origins, allowed_methods=allowed_methods, allowed_headers=allowed_headers)