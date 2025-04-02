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
from supabase.client import Client as SupabaseClient # Importar tipo

# Configuración y Settings
from app.core.config import settings
# Configuración de Logging
from app.core.logging_config import setup_logging
setup_logging() # Configurar logging al inicio

# --- NUEVO: Importar cliente Supabase Admin ---
from app.utils.supabase_admin import get_supabase_admin_client

# Routers
from app.routers import gateway_router
# --- NUEVO: Importar user_router ---
from app.routers import user_router

log = structlog.get_logger("api_gateway.main")

# --- Variables globales para clientes ---
# (Se inicializan en lifespan)
proxy_http_client: Optional[httpx.AsyncClient] = None
supabase_admin_client: Optional[SupabaseClient] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global proxy_http_client, supabase_admin_client # Declarar que modificaremos las globales
    # Startup: Inicializar cliente HTTP global
    log.info("Application startup: Initializing global HTTP client...")
    # ... (código de inicialización de httpx existente) ...
    try:
        # ... (inicialización de httpx.AsyncClient) ...
        proxy_http_client = httpx.AsyncClient( # Asignar a la variable global
             # ... (configuración httpx) ...
             limits = httpx.Limits(
                max_keepalive_connections=settings.HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS,
                max_connections=settings.HTTP_CLIENT_MAX_CONNECTIONS
             ),
             timeout = httpx.Timeout(settings.HTTP_CLIENT_TIMEOUT, connect=10.0),
             follow_redirects=False,
             http2=True
        )
        gateway_router.http_client = proxy_http_client # Asignar también al router si lo usa directamente
        log.info("HTTP Client initialized successfully.")
    except Exception as e:
        log.exception("Failed to initialize HTTP client during startup!", error=e)
        proxy_http_client = None
        gateway_router.http_client = None

    # --- NUEVO: Inicializar Cliente Supabase Admin ---
    log.info("Initializing Supabase Admin Client...")
    try:
        supabase_admin_client = get_supabase_admin_client() # Obtener instancia cachead
        # Podrías añadir una verificación simple aquí si quieres
        # await supabase_admin_client.auth.admin.list_users(limit=1)
        log.info("Supabase Admin Client initialized successfully.")
    except Exception as e:
        log.exception("Failed to initialize Supabase Admin Client during startup!", error=e)
        supabase_admin_client = None # Marcar como no disponible si falla
        # Considera salir si es crítico
        # import sys
        # sys.exit("Failed to initialize Supabase Admin Client")
    # --- FIN NUEVO ---

    yield # La aplicación se ejecuta aquí

    # Shutdown: Cerrar clientes
    log.info("Application shutdown: Closing clients...")
    # Cerrar HTTP Client
    if proxy_http_client and not proxy_http_client.is_closed:
        try:
            await proxy_http_client.aclose()
            log.info("HTTP Client closed successfully.")
        except Exception as e:
            log.exception("Error closing HTTP client during shutdown.", error=e)
    else:
        log.warning("HTTP Client was not initialized or already closed.")

    # Cerrar Supabase Admin Client (actualmente no tiene un método aclose explícito)
    # La librería maneja conexiones subyacentes, pero logueamos que terminamos.
    if supabase_admin_client:
        log.info("Supabase Admin Client shutdown (no explicit close needed).")

# --- Creación de la aplicación FastAPI ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Punto de entrada único y seguro para los microservicios de Nyro.",
    version="1.0.0",
    lifespan=lifespan,
)

# --- Middlewares ---
# ... (Middleware de Request ID/Timing sin cambios) ...
@app.middleware("http")
async def add_process_time_header_and_request_id(request: Request, call_next):
    start_time = time.time()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    with structlog.contextvars.bind_contextvars(request_id=request_id):
        # log.info("Request received", method=request.method, path=request.url.path, client_ip=request.client.host if request.client else "N/A")
        try:
            response = await call_next(request)
            process_time = time.time() - start_time
            response.headers["X-Process-Time"] = str(process_time)
            response.headers["X-Request-ID"] = request_id
            # log.info("Request processed successfully", status_code=response.status_code, duration=round(process_time, 4))
        except Exception as e:
            process_time = time.time() - start_time
            log.exception("Unhandled exception during request processing", duration=round(process_time, 4), error=str(e))
            raise e
        return response

# ... (Configuración CORS sin cambios) ...
VERCEL_FRONTEND_URL = os.getenv("VERCEL_FRONTEND_URL", "https://TU_APP_EN_VERCEL.vercel.app")
NGROK_URL = os.getenv("NGROK_URL", "https://b0c3-2001-1388-53a1-a7c9-8901-65aa-f1fe-6a8.ngrok-free.app")
LOCALHOST_FRONTEND = "http://localhost:3000"
allowed_origins = [LOCALHOST_FRONTEND, VERCEL_FRONTEND_URL]
if NGROK_URL:
    if NGROK_URL.startswith("https://"):
        allowed_origins.append(NGROK_URL)
        # Opcional: permitir http si es necesario para ngrok local
        # allowed_origins.append(NGROK_URL.replace("https://", "http://"))
    else: log.warning(f"NGROK_URL format not recognized or insecure: {NGROK_URL}")
log.info("Configuring CORS", allowed_origins=allowed_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "Authorization", "Content-Type", "X-Requested-With"],
)

# --- Routers ---
app.include_router(gateway_router.router)
# --- NUEVO: Incluir user_router ---
app.include_router(user_router.router)

# --- Endpoints Básicos del Propio Gateway (sin cambios) ---
@app.get("/", tags=["Gateway Status"], summary="Root endpoint")
async def root():
    return {"message": f"{settings.PROJECT_NAME} is running!"}

@app.get("/health", tags=["Gateway Status"], summary="Kubernetes Health Check", status_code=status.HTTP_200_OK)
async def health_check(client: httpx.AsyncClient = Depends(gateway_router.get_client)):
     # Verificar también cliente admin si es crítico
     admin_client: Optional[SupabaseClient] = supabase_admin_client
     if not admin_client:
         log.error("Health check failed: Supabase Admin Client not available.")
         raise HTTPException(status_code=503, detail="Gateway service dependency unavailable (Admin Client).")
     log.debug("Health check passed.")
     return {"status": "healthy", "service": settings.PROJECT_NAME}


# --- Manejadores de Excepciones Globales (sin cambios) ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # ... (código existente) ...
    req_id = structlog.contextvars.get_contextvars().get("request_id", "N/A")
    bound_log = log.bind(request_id=req_id)
    bound_log.warning("HTTP Exception occurred", status_code=exc.status_code, detail=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    # ... (código existente) ...
    req_id = structlog.contextvars.get_contextvars().get("request_id", "N/A")
    bound_log = log.bind(request_id=req_id)
    bound_log.exception("Unhandled internal server error occurred in gateway")
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "An internal server error occurred."})

log.info(f"'{settings.PROJECT_NAME}' application configured and ready to start.") 