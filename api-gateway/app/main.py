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
from app.routers import gateway_router, user_router, auth_router

from app.core.config import settings
from app.core.logging_config import setup_logging
# Configurar logging ANTES de importar otros módulos que puedan loguear
setup_logging()
# Ahora importar el resto
from app.utils.supabase_admin import get_supabase_admin_client
from app.routers import gateway_router, user_router
# Importar dependencias de autenticación para verificar su carga
from app.auth.auth_middleware import StrictAuth, InitialAuth

log = structlog.get_logger("api_gateway.main") # Logger para el módulo main

# Clientes globales que se inicializarán en el lifespan
proxy_http_client: Optional[httpx.AsyncClient] = None
supabase_admin_client: Optional[SupabaseClient] = None

# --- Lifespan para inicializar/cerrar clientes ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global proxy_http_client, supabase_admin_client
    log.info("Application startup: Initializing global clients...")

    # Inicializar cliente HTTPX para proxy
    try:
        # Configurar límites y timeouts desde settings
        limits = httpx.Limits(
            max_keepalive_connections=settings.HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS,
            max_connections=settings.HTTP_CLIENT_MAX_CONNECTIONS
        )
        # Usar timeout general, se puede sobreescribir por request si es necesario
        # Añadir connect timeout más corto
        timeout = httpx.Timeout(
            settings.HTTP_CLIENT_TIMEOUT, connect=10.0, # Timeout de conexión más corto
            # read, write, pool usan el general
        )
        proxy_http_client = httpx.AsyncClient(
             limits=limits,
             timeout=timeout,
             follow_redirects=False, # No seguir redirects automáticamente en el proxy
             http2=True # Habilitar HTTP/2 si los servicios backend lo soportan
        )
        # Inyectar el cliente en el módulo del router (alternativa a pasarlo en cada request)
        gateway_router.http_client = proxy_http_client
        log.info("HTTP Client initialized successfully.", limits=str(limits), timeout=str(timeout))
    except Exception as e:
        log.exception("CRITICAL: Failed to initialize HTTP client during startup!", error=str(e))
        # Podríamos decidir si la app puede arrancar sin cliente HTTP
        # sys.exit("Failed to initialize HTTP client.") # Opcional: Salir si es crítico
        proxy_http_client = None
        gateway_router.http_client = None # Asegurar que el router lo vea como None

    # Inicializar cliente Supabase Admin
    log.info("Initializing Supabase Admin Client...")
    try:
        # get_supabase_admin_client usa lru_cache y maneja errores internos
        supabase_admin_client = get_supabase_admin_client()
        # Inyectar en el router de usuario (menos ideal, mejor usar Depends en la ruta)
        user_router.supabase_admin = supabase_admin_client
        log.info("Supabase Admin Client reference obtained (initialized via get_supabase_admin_client).")
        # Podríamos hacer un test rápido aquí si fuera necesario, pero get_client lo hace al primer uso
    except Exception as e:
        # get_supabase_admin_client ya loguea el error crítico
        log.exception("CRITICAL: Failed to get Supabase Admin Client during startup!", error=str(e))
        # Podríamos decidir si salir o continuar sin cliente admin
        # sys.exit("Failed to initialize Supabase Admin client.") # Opcional
        supabase_admin_client = None
        user_router.supabase_admin = None # Asegurar que el router lo vea como None

    # Punto donde la aplicación está lista para recibir requests
    yield
    # --- Shutdown ---
    log.info("Application shutdown: Closing clients...")

    # Cerrar cliente HTTPX
    if proxy_http_client and not proxy_http_client.is_closed:
        try:
            await proxy_http_client.aclose()
            log.info("HTTP Client closed successfully.")
        except Exception as e:
            log.exception("Error closing HTTP client during shutdown.", error=str(e))
    elif proxy_http_client is None:
        log.warning("HTTP Client was not initialized during startup.")
    else: # Estaba inicializado pero ya cerrado
        log.info("HTTP Client was already closed.")

    # Cliente Supabase (supabase-py) no requiere cierre explícito عادةً
    log.info("Supabase Admin Client shutdown check complete (no explicit close needed).")


# --- Creación de la App FastAPI ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Punto de entrada único y seguro para los microservicios de Nyro. Valida JWTs de Supabase, gestiona asociación inicial de compañía y reenvía tráfico a servicios backend.",
    version="1.0.0",
    lifespan=lifespan, # Usar el lifespan definido arriba
    # Se pueden añadir otros parámetros como openapi_url, docs_url, redoc_url
)

# --- Middlewares ---

# 1. CORS Middleware (Configuración más robusta)
allowed_origins = []
# Orígenes desde variables de entorno o configuración
vercel_url = os.getenv("VERCEL_FRONTEND_URL", "https://atenex-frontend.vercel.app") # Usar fallback
log.info(f"Adding Vercel frontend URL to allowed origins: {vercel_url}")
allowed_origins.append(vercel_url)

localhost_url = "http://localhost:3000" # Frontend local estándar
log.info(f"Adding localhost frontend URL to allowed origins: {localhost_url}")
allowed_origins.append(localhost_url)

# Añadir Ngrok URL de forma dinámica o desde env var
ngrok_url_env = os.getenv("NGROK_URL")
if ngrok_url_env:
    if ngrok_url_env.startswith("https://") and ".ngrok" in ngrok_url_env:
        log.info(f"Adding Ngrok URL from NGROK_URL env var: {ngrok_url_env}")
        allowed_origins.append(ngrok_url_env)
    else:
        log.warning(f"NGROK_URL environment variable ('{ngrok_url_env}') doesn't look like a valid https Ngrok URL. Ignoring.")
# También añadir la URL específica observada en logs si es diferente y no estaba en env
ngrok_url_from_logs = "https://1942-2001-1388-53a1-a7c9-241c-4a44-2b12-938f.ngrok-free.app"
if ngrok_url_from_logs not in allowed_origins:
    log.info(f"Adding specific Ngrok URL observed in logs: {ngrok_url_from_logs}")
    allowed_origins.append(ngrok_url_from_logs)

# Eliminar duplicados y None
allowed_origins = list(set(filter(None, allowed_origins)))
if not allowed_origins:
    log.critical("CRITICAL: No allowed origins configured for CORS. Frontend requests will likely fail.")
else:
    log.info("Final CORS Allowed Origins:", origins=allowed_origins)

# Headers permitidos (incluir estándar y específicos como ngrok)
allowed_headers = [
    "Authorization", "Content-Type", "Accept", "Origin",
    "X-Requested-With", "ngrok-skip-browser-warning", "X-Request-ID" # Permitir pasar X-Request-ID
]
log.info("CORS Allowed Headers:", headers=allowed_headers)

# Métodos permitidos
allowed_methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
log.info("CORS Allowed Methods:", methods=allowed_methods)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True, # Importante para pasar cookies/auth headers
    allow_methods=allowed_methods,
    allow_headers=allowed_headers,
    expose_headers=["X-Request-ID", "X-Process-Time"], # Exponer headers custom
    max_age=600, # Cachear respuesta preflight OPTIONS por 10 mins
)

# 2. Middleware para Request ID, Timing y Logging Estructurado
@app.middleware("http")
async def add_request_id_timing_logging(request: Request, call_next):
    start_time = time.time()
    # Usar X-Request-ID del header si existe, sino generar uno nuevo
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    # Guardar en el estado para acceso posterior
    request.state.request_id = request_id

    # Vincular request_id al contexto de structlog para todos los logs de esta request
    with structlog.contextvars.bound_contextvars(request_id=request_id):
        # Logger específico para requests entrantes/salientes
        bound_log = structlog.get_logger("api_gateway.requests")
        # Loggear inicio de request
        bound_log.info("Request received",
                       method=request.method,
                       path=request.url.path,
                       client_ip=request.client.host if request.client else "N/A",
                       user_agent=request.headers.get("user-agent", "N/A")[:100]) # Limitar longitud UA

        response = None
        try:
            response = await call_next(request)
            # Calcular duración después de obtener la respuesta
            process_time = time.time() - start_time
            # Añadir headers custom a la respuesta
            response.headers["X-Process-Time"] = f"{process_time:.4f}"
            response.headers["X-Request-ID"] = request_id
            # Loggear fin de request exitosa
            bound_log.info("Request processed successfully",
                           status_code=response.status_code,
                           duration=round(process_time, 4))
        except Exception as e:
            # Loggear excepción no manejada ANTES de re-lanzarla
            process_time = time.time() - start_time
            # Usar logger del middleware para excepciones no capturadas por handlers específicos
            bound_log.exception("Unhandled exception during request processing",
                                duration=round(process_time, 4),
                                error_type=type(e).__name__,
                                error=str(e))
            # Re-lanzar para que los exception_handlers de FastAPI la capturen
            raise e
        finally:
            # Este bloque se ejecuta siempre, incluso si hay return o raise
            # Asegurar que el contexto de structlog se limpie (aunque contextvars debería hacerlo)
            pass

        return response

# --- Routers ---
# Incluir los routers definidos en otros módulos
app.include_router(auth_router.router)
app.include_router(gateway_router.router)
app.include_router(user_router.router)

# --- Endpoints Básicos ---
@app.get("/", tags=["Gateway Status"], summary="Root endpoint", include_in_schema=False)
async def root():
    """Endpoint raíz simple para verificar que el gateway está corriendo."""
    return {"message": f"{settings.PROJECT_NAME} is running!"}

@app.get("/health",
         tags=["Gateway Status"],
         summary="Kubernetes Health Check",
         status_code=status.HTTP_200_OK,
         response_description="Indicates if the gateway and its core dependencies are healthy.",
         responses={
             status.HTTP_200_OK: {"description": "Gateway is healthy."},
             status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Gateway is unhealthy due to dependency issues."}
         })
async def health_check():
     """
     Verifica el estado del gateway y sus dependencias críticas (Cliente HTTP, Cliente Supabase Admin).
     Usado por Kubernetes Liveness/Readiness Probes.
     """
     admin_client_status = "available" if supabase_admin_client else "unavailable"
     http_client_status = "available" if proxy_http_client and not proxy_http_client.is_closed else "unavailable"

     # Considerar la app saludable sólo si AMBOS clientes están listos
     is_healthy = admin_client_status == "available" and http_client_status == "available"

     health_details = {
         "status": "healthy" if is_healthy else "unhealthy",
         "service": settings.PROJECT_NAME,
         "dependencies": {
             "supabase_admin_client": admin_client_status,
             "proxy_http_client": http_client_status
         }
     }

     if not is_healthy:
         log.error("Health check failed", details=health_details)
         # Levantar 503 si no está saludable
         raise HTTPException(
             status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
             detail=health_details
         )

     log.debug("Health check passed.", details=health_details)
     return health_details

# --- Manejadores de Excepciones Globales ---
# Captura excepciones HTTP que ocurren en las rutas o dependencias
@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    req_id = getattr(request.state, 'request_id', 'N/A')
    # Usar el logger principal o uno específico para excepciones
    bound_log = log.bind(request_id=req_id)

    # Determinar nivel de log basado en status code
    # Errores de cliente (4xx) son WARNING, errores de servidor (5xx) son ERROR
    log_level_name = "warning" if 400 <= exc.status_code < 500 else "error"
    # *** CORRECCIÓN: Usar getattr para llamar al método de log correcto ***
    log_method = getattr(bound_log, log_level_name, bound_log.info) # Fallback a info si es un nivel desconocido

    log_method("HTTP Exception occurred",
               status_code=exc.status_code,
               detail=exc.detail,
               path=request.url.path,
               # Incluir headers de la excepción si existen (ej. WWW-Authenticate)
               exception_headers=exc.headers)

    # Devolver respuesta JSON estándar para HTTPExceptions
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers # Pasar headers de la excepción a la respuesta
    )

# Captura cualquier otra excepción no manejada (errores 500 inesperados)
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    req_id = getattr(request.state, 'request_id', 'N/A')
    bound_log = log.bind(request_id=req_id)

    # Loguear la excepción completa para diagnóstico
    bound_log.exception("Unhandled internal server error occurred in gateway",
                        path=request.url.path,
                        error_type=type(exc).__name__) # Incluir tipo de error

    # Devolver respuesta 500 genérica al cliente
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected internal server error occurred."}
    )

# --- Log final de configuración (opcional) ---
# Este log se ejecutará sólo una vez cuando el módulo se cargue
log.info(f"'{settings.PROJECT_NAME}' application configured and ready to start.",
         log_level=settings.LOG_LEVEL,
         ingest_service=str(settings.INGEST_SERVICE_URL),
         query_service=str(settings.QUERY_SERVICE_URL),
         auth_proxy_enabled=bool(settings.AUTH_SERVICE_URL),
         supabase_url=str(settings.SUPABASE_URL),
         default_company_id_set=bool(settings.DEFAULT_COMPANY_ID)
         )

# --- Ejecución con Uvicorn (para desarrollo local) ---
# Esto normalmente no se incluye si usas Gunicorn en producción
# if __name__ == "__main__":
#     uvicorn.run(
#         "app.main:app",
#         host="0.0.0.0",
#         port=8080, # Puerto estándar interno
#         log_level=settings.LOG_LEVEL.lower(), # Pasar nivel de log a uvicorn
#         reload=True # Habilitar reload para desarrollo
#         # Añadir --log-config app/logging.yaml si usas config de logging externa
#     )