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

# Obtener logger principal
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
        # Timeout más granular (ej. 5s para conectar, 30s para leer/escribir/pool)
        timeout = httpx.Timeout(
            settings.HTTP_CLIENT_TIMEOUT, # Tiempo total de lectura
            connect=10.0,
            write=settings.HTTP_CLIENT_TIMEOUT,
            pool=settings.HTTP_CLIENT_TIMEOUT
        )
        proxy_http_client = httpx.AsyncClient(
             limits=limits,
             timeout=timeout,
             follow_redirects=False, # Importante para un proxy
             http2=True # Habilitar HTTP/2 si los backends lo soportan
        )
        # Inyectar el cliente en el router que lo necesita
        gateway_router.http_client = proxy_http_client
        log.info("HTTP Client initialized successfully.", limits=str(limits), timeout=str(timeout))
    except Exception as e:
        log.exception("Failed to initialize HTTP client during startup!", error=str(e))
        proxy_http_client = None
        gateway_router.http_client = None # Asegurar que es None en el router también

    log.info("Initializing Supabase Admin Client...")
    try:
        # Usar el getter que maneja la inicialización y caché
        supabase_admin_client = get_supabase_admin_client()
        # Asegurarse que la dependencia en user_router lo obtiene
        user_router.supabase_admin = supabase_admin_client # Inyectar directamente si user_router lo necesita como global (menos ideal)
                                                            # Es mejor usar Depends(get_supabase_admin_client) en la ruta.
        log.info("Supabase Admin Client reference obtained (initialized via get_supabase_admin_client).")
    except Exception as e:
        # get_supabase_admin_client ya loguea y podría salir, pero capturamos por si acaso
        log.exception("Failed to get Supabase Admin Client during startup!", error=str(e))
        supabase_admin_client = None

    yield # La aplicación se ejecuta aquí

    # --- Shutdown ---
    log.info("Application shutdown: Closing clients...")
    if proxy_http_client and not proxy_http_client.is_closed:
        try:
            await proxy_http_client.aclose()
            log.info("HTTP Client closed successfully.")
        except Exception as e:
            log.exception("Error closing HTTP client during shutdown.", error=str(e))
    else:
        log.warning("HTTP Client was not available or already closed during shutdown.")
    # El cliente Supabase de supabase-py no necesita aclose explícito
    log.info("Supabase Admin Client shutdown check complete (no explicit close needed).")

# --- Creación de la aplicación FastAPI (Sin cambios) ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Punto de entrada único y seguro para los microservicios de Nyro.",
    version="1.0.0",
    lifespan=lifespan,
    # Deshabilitar docs en producción si se desea
    # docs_url=None if os.getenv("ENVIRONMENT") == "production" else "/docs",
    # redoc_url=None if os.getenv("ENVIRONMENT") == "production" else "/redoc",
)

# --- Middlewares ---

# --- CORSMiddleware PRIMERO (Ajustado) ---
allowed_origins = []

# 1. Frontend Vercel URL (desde env var o fallback)
vercel_url = os.getenv("VERCEL_FRONTEND_URL")
if vercel_url:
    log.info(f"Adding Vercel frontend URL from env var to allowed origins: {vercel_url}")
    allowed_origins.append(vercel_url)
else:
    # Usar el fallback que SÍ se ve en los logs del frontend
    vercel_fallback_url = "https://atenex-frontend.vercel.app"
    log.warning(f"VERCEL_FRONTEND_URL env var not set. Using fallback: {vercel_fallback_url}")
    allowed_origins.append(vercel_fallback_url)

# 2. Localhost para desarrollo
localhost_url = "http://localhost:3000"
log.info(f"Adding localhost frontend URL to allowed origins: {localhost_url}")
allowed_origins.append(localhost_url)

# 3. Ngrok URL (La que USA el frontend, NO la de los logs antiguos del backend)
#    ¡¡¡IMPORTANTE!!! Usa la URL de los logs del NAVEGADOR.
ngrok_url_from_frontend_logs = "https://1942-2001-1388-53a1-a7c9-241c-4a44-2b12-938f.ngrok-free.app"
log.info(f"Adding specific Ngrok URL observed in frontend logs: {ngrok_url_from_frontend_logs}")
allowed_origins.append(ngrok_url_from_frontend_logs)

# 4. Opcional: Ngrok URL desde variable de entorno (si quieres que sea configurable)
ngrok_url_env = os.getenv("NGROK_URL")
if ngrok_url_env and ngrok_url_env not in allowed_origins:
    if ngrok_url_env.startswith("https://") or ngrok_url_env.startswith("http://"):
        log.info(f"Adding Ngrok URL from NGROK_URL env var to allowed origins: {ngrok_url_env}")
        allowed_origins.append(ngrok_url_env)
    else:
        log.warning(f"NGROK_URL environment variable has an unexpected format: {ngrok_url_env}. Ignoring.")

# Limpiar duplicados y None/empty strings
allowed_origins = list(set(filter(None, allowed_origins)))

if not allowed_origins:
    log.critical("CRITICAL: No allowed origins configured for CORS. Frontend will be blocked.")
else:
    log.info("Final CORS Allowed Origins:", origins=allowed_origins)

# Cabeceras permitidas: Asegurarse que 'Authorization', 'Content-Type', y la de ngrok estén.
allowed_headers = [
    "Authorization",
    "Content-Type",
    "Accept",
    "Origin",
    "X-Requested-With",
    "ngrok-skip-browser-warning", # Para saltar la advertencia de Ngrok
    # Puedes añadir otras si son necesarias, como 'X-Client-Info', 'apikey' (si Supabase lo usa)
]
log.info("CORS Allowed Headers:", headers=allowed_headers)

# Métodos permitidos: Incluir OPTIONS es crucial para preflight requests
allowed_methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
log.info("CORS Allowed Methods:", methods=allowed_methods)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins, # Lista de orígenes permitidos
    allow_credentials=True, # Permite cookies/auth headers desde los orígenes permitidos
    allow_methods=allowed_methods, # Métodos HTTP permitidos
    allow_headers=allowed_headers, # Cabeceras HTTP permitidas
    expose_headers=["X-Request-ID", "X-Process-Time"], # Cabeceras expuestas al frontend
    max_age=600, # Tiempo en segundos que el navegador puede cachear la respuesta preflight
)
# --- Fin CORSMiddleware ---


# --- Middleware de Request ID y Timing (CORREGIDO) ---
@app.middleware("http")
async def add_request_id_timing_logging(request: Request, call_next):
    start_time = time.time()
    # Generar request_id si no viene en la cabecera
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    # Guardar request_id en el estado para posible uso en exception handlers
    request.state.request_id = request_id

    # *** CORRECCIÓN DEL AttributeError ***
    # Usar el context manager devuelto por bound_contextvars
    with structlog.contextvars.bound_contextvars(request_id=request_id):
        bound_log = structlog.get_logger("api_gateway.requests") # Logger específico para requests
        bound_log.info(
            "Request received",
            method=request.method,
            path=request.url.path,
            client_ip=request.client.host if request.client else "N/A",
            user_agent=request.headers.get("user-agent", "N/A")[:100] # Limitar longitud
        )

        try:
            # Procesar la solicitud DENTRO del contexto del logger
            response = await call_next(request)
            # Calcular tiempo después de obtener la respuesta
            process_time = time.time() - start_time
            # Añadir cabeceras a la respuesta ANTES de devolverla
            response.headers["X-Process-Time"] = f"{process_time:.4f}" # Formatear tiempo
            response.headers["X-Request-ID"] = request_id

            # Loguear respuesta exitosa
            bound_log.info(
                "Request processed successfully",
                status_code=response.status_code,
                duration=round(process_time, 4)
            )
        except Exception as e:
            # Loggear excepción no manejada ANTES de relanzarla
            process_time = time.time() - start_time
            # Usar logger ya vinculado con request_id
            bound_log.exception(
                "Unhandled exception during request processing",
                duration=round(process_time, 4),
                error_type=type(e).__name__,
                error=str(e) # Incluir mensaje de error
            )
            # Relanzar la excepción para que los exception_handlers de FastAPI la capturen
            raise e
        # Devolver la respuesta
        return response
# --- Fin middleware corregido ---


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

     health_details = {
         "status": "healthy" if is_healthy else "unhealthy",
         "service": settings.PROJECT_NAME,
         "dependencies": {
             "supabase_admin_client": admin_client_status,
             "proxy_http_client": http_client_status,
         }
     }

     if not is_healthy:
         log.error("Health check failed", details=health_details)
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=health_details)

     log.debug("Health check passed.", details=health_details)
     return health_details

# --- Manejadores de Excepciones (Mejorados) ---
@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    req_id = getattr(request.state, 'request_id', 'N/A')
    # Usar un logger vinculado
    bound_log = log.bind(request_id=req_id)
    log_level = "warning" if 400 <= exc.status_code < 500 else "error"
    bound_log.log(log_level, "HTTP Exception occurred",
                  status_code=exc.status_code,
                  detail=exc.detail,
                  path=request.url.path)
    # Incluir cabeceras WWW-Authenticate si son relevantes (ej. 401)
    headers = getattr(exc, "headers", None)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}, # Mantener formato FastAPI
        headers=headers # Pasar las cabeceras de la excepción original si existen
    )

# Handler genérico para errores 500 no esperados
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    req_id = getattr(request.state, 'request_id', 'N/A')
    # Usar un logger vinculado y loguear con traceback completo
    bound_log = log.bind(request_id=req_id)
    bound_log.exception("Unhandled internal server error occurred in gateway",
                        path=request.url.path,
                        error_type=type(exc).__name__) # Usar logger.exception para incluir traceback

    # Devolver respuesta genérica al cliente
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected internal server error occurred."}
    )

# Log final antes de iniciar Uvicorn (si se ejecuta directamente)
log.info(f"'{settings.PROJECT_NAME}' application configured and ready to start.",
         allowed_origins=allowed_origins,
         allowed_methods=allowed_methods,
         allowed_headers=allowed_headers)

# --- Punto de entrada para ejecución directa (ej. uvicorn app.main:app) ---
# No se necesita código adicional aquí si se usa Gunicorn/Uvicorn como en los logs