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

# Importar cliente Supabase Admin
from app.utils.supabase_admin import get_supabase_admin_client

# Routers
from app.routers import gateway_router
from app.routers import user_router

log = structlog.get_logger("api_gateway.main")

# Variables globales para clientes (inicializadas en lifespan)
proxy_http_client: Optional[httpx.AsyncClient] = None
supabase_admin_client: Optional[SupabaseClient] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global proxy_http_client, supabase_admin_client
    log.info("Application startup: Initializing global clients...")
    try:
        proxy_http_client = httpx.AsyncClient(
             limits = httpx.Limits(
                max_keepalive_connections=settings.HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS,
                max_connections=settings.HTTP_CLIENT_MAX_CONNECTIONS
             ),
             timeout = httpx.Timeout(settings.HTTP_CLIENT_TIMEOUT, connect=10.0),
             follow_redirects=False,
             http2=True # Habilitar HTTP/2 si los servicios downstream lo soportan
        )
        # Asignar al router si es necesario (aunque es mejor usar Depends)
        gateway_router.http_client = proxy_http_client
        log.info("HTTP Client initialized successfully.")
    except Exception as e:
        log.exception("Failed to initialize HTTP client during startup!", error=e)
        proxy_http_client = None
        gateway_router.http_client = None # Asegurar que esté None si falla

    log.info("Initializing Supabase Admin Client...")
    try:
        supabase_admin_client = get_supabase_admin_client() # Obtener instancia cacheada
        # Opcional: Ping simple para verificar conectividad
        # await supabase_admin_client.table('users').select('id', head=True, count='exact').execute() # Ejemplo
        log.info("Supabase Admin Client initialized successfully.")
    except Exception as e:
        log.exception("Failed to initialize Supabase Admin Client during startup!", error=e)
        supabase_admin_client = None
        # Considerar si la app debe fallar al iniciar si el cliente admin es crítico
        # import sys
        # sys.exit("FATAL: Failed to initialize Supabase Admin Client")

    yield # La aplicación se ejecuta aquí

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
        log.info("Supabase Admin Client shutdown.") # No hay método aclose() explícito

# Creación de la aplicación FastAPI
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Punto de entrada único y seguro para los microservicios de Nyro.",
    version="1.0.0",
    lifespan=lifespan,
)

# --- Middlewares ---

# Request ID y Timing (Sin cambios)
@app.middleware("http")
async def add_process_time_header_and_request_id(request: Request, call_next):
    start_time = time.time()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = request_id # Store request_id in state
    with structlog.contextvars.bind_contextvars(request_id=request_id):
        log.info("Request received", method=request.method, path=request.url.path, client_ip=request.client.host if request.client else "N/A")
        try:
            response = await call_next(request)
            process_time = time.time() - start_time
            response.headers["X-Process-Time"] = str(process_time)
            response.headers["X-Request-ID"] = request_id # Ensure it's always added
            log.info("Request processed successfully", status_code=response.status_code, duration=round(process_time, 4))
        except Exception as e:
            process_time = time.time() - start_time
            log.exception("Unhandled exception during request processing", duration=round(process_time, 4), error=str(e))
            # Re-raise para que los exception handlers de FastAPI actúen
            raise e
        return response

# --- Configuración CORS (CORREGIDA) ---
# Leer las URLs permitidas desde variables de entorno.
# Es CRÍTICO que estas variables estén configuradas en el entorno de despliegue (Kubernetes).
# Usar los nombres de variable definidos en config.py (que tienen prefijo GATEWAY_)
# Pydantic-settings ya las habrá cargado en el objeto `settings`.

# Leer URLs desde las settings (que a su vez leen de env vars con prefijo GATEWAY_)
# Usaremos VERCEL_FRONTEND_URL y NGROK_URL directamente si están en `settings`,
# o las leeremos del entorno si no están definidas explícitamente en `settings`.

# Define los orígenes permitidos. Prioriza las variables de entorno específicas.
allowed_origins = []

# 1. Frontend en Vercel (¡LA MÁS IMPORTANTE!)
#    Asegúrate de tener GATEWAY_VERCEL_FRONTEND_URL="https://atenex-frontend.vercel.app" en tu ConfigMap/Secrets.
vercel_url = os.getenv("GATEWAY_VERCEL_FRONTEND_URL") # Leer directamente del entorno
if vercel_url:
    log.info(f"Adding Vercel frontend URL to allowed origins: {vercel_url}")
    allowed_origins.append(vercel_url)
else:
    log.warning("GATEWAY_VERCEL_FRONTEND_URL environment variable not set. CORS might block Vercel frontend.")

# 2. Frontend Localhost (para desarrollo)
localhost_url = "http://localhost:3000"
log.info(f"Adding localhost frontend URL to allowed origins: {localhost_url}")
allowed_origins.append(localhost_url)

# 3. Ngrok URL (para pruebas/desarrollo con túnel)
#    Asegúrate de tener GATEWAY_NGROK_URL="https://tú-url.ngrok-free.app" si la usas.
ngrok_url = os.getenv("GATEWAY_NGROK_URL") # Leer directamente del entorno
if ngrok_url:
    if ngrok_url.startswith("https://") or ngrok_url.startswith("http://"):
        log.info(f"Adding Ngrok URL to allowed origins: {ngrok_url}")
        allowed_origins.append(ngrok_url)
    else:
        log.warning(f"GATEWAY_NGROK_URL has an unexpected format: {ngrok_url}")

# Eliminar duplicados y asegurar que no haya orígenes vacíos
allowed_origins = list(set(filter(None, allowed_origins)))

if not allowed_origins:
    log.critical("CRITICAL: No allowed origins configured for CORS. Frontend will likely be blocked.")
    # Podrías decidir salir si no hay orígenes configurados
    # import sys
    # sys.exit("FATAL: No CORS origins configured.")
else:
    log.info("Final CORS Allowed Origins:", origins=allowed_origins)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins, # Usar la lista construida
    allow_credentials=True, # Necesario para enviar cookies o headers de Auth
    allow_methods=["*"], # Permitir todos los métodos estándar
    allow_headers=["*", "Authorization", "Content-Type", "X-Requested-With", "ngrok-skip-browser-warning"], # Permitir headers comunes + Auth + ngrok bypass
    expose_headers=["X-Request-ID", "X-Process-Time"], # Exponer headers custom si es necesario
)
# --- FIN Configuración CORS ---


# --- Routers ---
app.include_router(gateway_router.router)
app.include_router(user_router.router)

# --- Endpoints Básicos del Propio Gateway ---
@app.get("/", tags=["Gateway Status"], summary="Root endpoint")
async def root():
    return {"message": f"{settings.PROJECT_NAME} is running!"}

@app.get("/health", tags=["Gateway Status"], summary="Kubernetes Health Check", status_code=status.HTTP_200_OK)
async def health_check(): # No necesita el cliente HTTP aquí si solo chequea el admin
     admin_client: Optional[SupabaseClient] = supabase_admin_client
     if not admin_client:
         log.error("Health check failed: Supabase Admin Client not available.")
         raise HTTPException(status_code=503, detail="Gateway service dependency unavailable (Admin Client).")

     # Opcional: Añadir chequeo del cliente HTTP si es necesario
     http_client_check: Optional[httpx.AsyncClient] = proxy_http_client
     if not http_client_check or http_client_check.is_closed:
         log.error("Health check failed: HTTP Client not available or closed.")
         raise HTTPException(status_code=503, detail="Gateway service dependency unavailable (HTTP Client).")

     log.debug("Health check passed.")
     return {"status": "healthy", "service": settings.PROJECT_NAME}


# --- Manejadores de Excepciones Globales ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    req_id = getattr(request.state, 'request_id', 'N/A') # Get from state if available
    bound_log = log.bind(request_id=req_id)
    bound_log.warning("HTTP Exception occurred", status_code=exc.status_code, detail=exc.detail, path=request.url.path)
    # Evitar devolver headers WWW-Authenticate por defecto en algunos errores para no confundir al browser
    headers = exc.headers if exc.status_code == 401 else None # Solo para 401
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

log.info(f"'{settings.PROJECT_NAME}' application configured and ready to start.")

# --- Para ejecución local (opcional) ---
# if __name__ == "__main__":
#     uvicorn.run(app, host="0.0.0.0", port=8080, log_level=settings.LOG_LEVEL.lower())