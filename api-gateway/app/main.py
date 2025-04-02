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
        # Usar límites definidos en settings
        limits = httpx.Limits(
            max_keepalive_connections=settings.HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS,
            max_connections=settings.HTTP_CLIENT_MAX_CONNECTIONS
        )
        timeout = httpx.Timeout(settings.HTTP_CLIENT_TIMEOUT, connect=10.0)

        proxy_http_client = httpx.AsyncClient(
             limits=limits,
             timeout=timeout,
             follow_redirects=False,
             http2=True # Habilitar HTTP/2 si los servicios downstream lo soportan
        )
        # Asignar al router si es necesario (aunque es mejor usar Depends)
        # Puede que no sea necesario si gateway_router.py usa Depends(get_client)
        # gateway_router.http_client = proxy_http_client
        log.info("HTTP Client initialized successfully.", limits=limits, timeout=timeout)
    except Exception as e:
        log.exception("Failed to initialize HTTP client during startup!", error=e)
        proxy_http_client = None
        # gateway_router.http_client = None # Asegurar que esté None si falla

    log.info("Initializing Supabase Admin Client...")
    try:
        supabase_admin_client = get_supabase_admin_client() # Obtener instancia cacheada
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
    # Opcional: Deshabilitar docs en producción si se desea
    # docs_url=None if os.getenv("ENVIRONMENT") == "production" else "/docs",
    # redoc_url=None if os.getenv("ENVIRONMENT") == "production" else "/redoc",
)

# --- Middlewares ---

# Request ID y Timing (Sin cambios)
@app.middleware("http")
async def add_process_time_header_and_request_id(request: Request, call_next):
    start_time = time.time()
    # Usar X-Request-ID si viene de un balanceador/ingress, sino generar uno
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = request_id # Store request_id in state

    # Vincular request_id al contexto de logging para todas las logs de esta request
    with structlog.contextvars.bind_contextvars(request_id=request_id):
        # Log inicial de la petición
        log.info("Request received", method=request.method, path=request.url.path, client_ip=request.client.host if request.client else "N/A", user_agent=request.headers.get("user-agent", "N/A"))

        try:
            response = await call_next(request)
            process_time = time.time() - start_time
            # Añadir headers a la respuesta
            response.headers["X-Process-Time"] = str(process_time)
            response.headers["X-Request-ID"] = request_id # Asegurar que siempre esté
            log.info("Request processed successfully", status_code=response.status_code, duration=round(process_time, 4))
        except Exception as e:
            process_time = time.time() - start_time
            # Loguear la excepción no manejada ANTES de relanzarla
            log.exception("Unhandled exception during request processing", duration=round(process_time, 4), error=str(e), exc_info=True)
            # Relanzar para que los exception handlers de FastAPI actúen
            raise e
        return response

# --- Configuración CORS (CORREGIDA Y MEJORADA) ---
# Lee las URLs permitidas desde variables de entorno.
# Es CRÍTICO que estas variables estén configuradas en el entorno de despliegue (Kubernetes ConfigMap/Secrets).

allowed_origins = []

# 1. Frontend en Vercel (¡LA MÁS IMPORTANTE!)
#    Asegúrate de tener VERCEL_FRONTEND_URL="https://atenex-frontend.vercel.app" en tu ConfigMap/Secrets.
#    Usamos directamente os.getenv aquí para permitir flexibilidad en el despliegue,
#    en lugar de forzar el prefijo GATEWAY_.
vercel_url = os.getenv("VERCEL_FRONTEND_URL")
if vercel_url:
    log.info(f"Adding Vercel frontend URL to allowed origins: {vercel_url}")
    allowed_origins.append(vercel_url)
else:
    log.warning("VERCEL_FRONTEND_URL environment variable not set. CORS might block Vercel frontend.")

# 2. Frontend Localhost (para desarrollo)
#    Siempre útil tenerlo para pruebas locales.
localhost_url = "http://localhost:3000"
log.info(f"Adding localhost frontend URL to allowed origins: {localhost_url}")
allowed_origins.append(localhost_url)

# 3. Ngrok URL (para pruebas/desarrollo con túnel)
#    Asegúrate de tener NGROK_URL="https://TU_URL.ngrok-free.app" si la usas y
#    el frontend llama *directamente* a la URL de ngrok.
#    *IMPORTANTE*: Si el frontend llama a la URL de Vercel y Vercel hace proxy (no es tu caso),
#    entonces solo necesitas la URL de Vercel. Pero si el frontend en Vercel llama
#    directamente a la URL de ngrok (como indican tus logs), necesitas añadir la URL de ngrok
#    *O* (mejor) configurar ngrok para que reescriba la cabecera Host y usar la URL de Vercel.
#    Dado el error, añadiremos la URL de ngrok leída del entorno por ahora.
ngrok_url_env = os.getenv("NGROK_URL")
# Extraer la URL de ngrok de los logs del frontend para asegurar que es la correcta
ngrok_url_from_log = "https://5158-2001-1388-53a1-a7c9-fd46-ef87-59cf-a7f7.ngrok-free.app" # Extraída de tus logs
if ngrok_url_from_log:
     log.info(f"Adding Ngrok URL from logs to allowed origins: {ngrok_url_from_log}")
     allowed_origins.append(ngrok_url_from_log)
elif ngrok_url_env:
    if ngrok_url_env.startswith("https://") or ngrok_url_env.startswith("http://"):
        log.info(f"Adding Ngrok URL from NGROK_URL env var to allowed origins: {ngrok_url_env}")
        allowed_origins.append(ngrok_url_env)
    else:
        log.warning(f"NGROK_URL environment variable has an unexpected format: {ngrok_url_env}")
else:
     log.warning("NGROK_URL environment variable not set. If frontend calls ngrok directly, CORS might fail.")


# Eliminar duplicados y asegurar que no haya orígenes vacíos o nulos
allowed_origins = list(set(filter(None, allowed_origins)))

if not allowed_origins:
    log.critical("CRITICAL: No allowed origins configured for CORS. Frontend will likely be blocked.")
    # Podrías decidir salir si no hay orígenes configurados en producción
    # import sys
    # if os.getenv("ENVIRONMENT") == "production": sys.exit("FATAL: No CORS origins configured.")
else:
    log.info("Final CORS Allowed Origins:", origins=allowed_origins)

# Cabeceras permitidas: Incluir Authorization, Content-Type y la cabecera de bypass de ngrok.
# Es importante permitir las cabeceras que tu frontend envía.
allowed_headers = ["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With", "ngrok-skip-browser-warning"]
log.info("CORS Allowed Headers:", headers=allowed_headers)

# Métodos permitidos: Incluir OPTIONS además de los métodos estándar.
allowed_methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
log.info("CORS Allowed Methods:", methods=allowed_methods)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins, # Usar la lista construida
    allow_credentials=True, # Necesario para enviar cookies o headers de Auth
    allow_methods=allowed_methods, # Permitir métodos incluyendo OPTIONS
    allow_headers=allowed_headers, # Permitir cabeceras necesarias
    expose_headers=["X-Request-ID", "X-Process-Time"], # Exponer cabeceras custom si es necesario
    max_age=600, # Cache preflight response por 10 minutos
)
# --- FIN Configuración CORS ---


# --- Routers ---
# Asegúrate que los routers se incluyen DESPUÉS del middleware CORS
app.include_router(gateway_router.router)
app.include_router(user_router.router)

# --- Endpoints Básicos del Propio Gateway ---
@app.get("/", tags=["Gateway Status"], summary="Root endpoint")
async def root():
    """Endpoint raíz que indica que el gateway está en funcionamiento."""
    return {"message": f"{settings.PROJECT_NAME} is running!"}

@app.get("/health", tags=["Gateway Status"], summary="Kubernetes Health Check", status_code=status.HTTP_200_OK)
async def health_check():
    """
    Verifica la salud del servicio y sus dependencias críticas (Cliente HTTP, Cliente Supabase Admin).
    Usado por Kubernetes Liveness/Readiness Probes.
    """
    admin_client: Optional[SupabaseClient] = supabase_admin_client
    http_client_check: Optional[httpx.AsyncClient] = proxy_http_client

    # Verificar Cliente Supabase Admin
    if not admin_client:
        log.error("Health check failed: Supabase Admin Client not available.")
        raise HTTPException(status_code=503, detail="Gateway service dependency unavailable (Admin Client).")
    # Opcional: Añadir un ping real a Supabase si es necesario, pero puede añadir latencia
    # try:
    #    await admin_client.table('users').select('id', head=True, count='exact').execute()
    # except Exception as admin_err:
    #    log.error("Health check failed: Supabase Admin Client ping failed.", error=admin_err)
    #    raise HTTPException(status_code=503, detail="Gateway dependency check failed (Admin Client connection).")

    # Verificar Cliente HTTP
    if not http_client_check or http_client_check.is_closed:
        log.error("Health check failed: HTTP Client not available or closed.")
        raise HTTPException(status_code=503, detail="Gateway service dependency unavailable (HTTP Client).")
    # Opcional: Añadir un ping real a un servicio downstream si es necesario
    # try:
    #    await http_client_check.get(f"{settings.INGEST_SERVICE_URL}/health", timeout=5) # Asumiendo que los servicios tienen /health
    # except Exception as http_err:
    #    log.error("Health check failed: Downstream service ping failed.", error=http_err)
    #    raise HTTPException(status_code=503, detail="Gateway dependency check failed (Downstream connection).")


    log.debug("Health check passed.")
    return {"status": "healthy", "service": settings.PROJECT_NAME}


# --- Manejadores de Excepciones Globales ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Manejador para excepciones HTTP controladas (lanzadas por nosotros o FastAPI)."""
    req_id = getattr(request.state, 'request_id', 'N/A') # Get from state if available
    bound_log = log.bind(request_id=req_id)
    # Loguear la excepción HTTP con nivel warning
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
    """Manejador para cualquier excepción no controlada (errores 500)."""
    req_id = getattr(request.state, 'request_id', 'N/A')
    bound_log = log.bind(request_id=req_id)
    # Loguear la excepción no manejada con nivel error y stack trace
    bound_log.exception("Unhandled internal server error occurred in gateway", path=request.url.path, error=str(exc), exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred. Please try again later or contact support."}
    )

log.info(f"'{settings.PROJECT_NAME}' application configured and ready to start.", allowed_origins=allowed_origins, allowed_methods=allowed_methods, allowed_headers=allowed_headers)

# --- Para ejecución local (opcional, útil para depuración) ---
# if __name__ == "__main__":
#     print("Starting Uvicorn locally...")
#     uvicorn.run(
#         "app.main:app",
#         host="0.0.0.0",
#         port=8080, # Puerto estándar para el gateway
#         log_level=settings.LOG_LEVEL.lower(),
#         reload=True # Habilitar reload para desarrollo
#     )