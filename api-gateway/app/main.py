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
import logging # Importar logging estándar para configuración inicial

# --- Configuración de Logging PRIMERO ---
# Asegura que el logging esté listo antes de importar otros módulos
from app.core.logging_config import setup_logging
setup_logging()

# --- Importaciones Post-Logging ---
from app.core.config import settings
from app.db import postgres_client # Importar cliente DB
from app.routers import gateway_router, user_router # Importar routers
# (Opcional) Importar dependencias para verificar carga si es necesario
# from app.auth.auth_middleware import StrictAuth, InitialAuth

# Logger principal para este módulo
log = structlog.get_logger("atenex_api_gateway.main")

# --- Clientes Globales (Inicializados en Lifespan) ---
proxy_http_client: Optional[httpx.AsyncClient] = None
# No necesitamos una variable global para el pool de DB, usamos postgres_client.get_db_pool()

# --- Lifespan Manager (Startup y Shutdown) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global proxy_http_client
    log.info("Application startup sequence initiated...")

    # 1. Inicializar Cliente HTTP para Proxying
    try:
        log.info("Initializing global HTTPX client for proxying...")
        limits = httpx.Limits(
            max_keepalive_connections=settings.HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS,
            max_connections=settings.HTTP_CLIENT_MAX_CONNECTIONS
        )
        timeout = httpx.Timeout(settings.HTTP_CLIENT_TIMEOUT, connect=10.0) # Timeout global
        proxy_http_client = httpx.AsyncClient(
            limits=limits,
            timeout=timeout,
            follow_redirects=False, # El gateway no debe seguir redirecciones
            http2=True # Habilitar HTTP/2 si los backends lo soportan
        )
        # Inyectar el cliente en el router del gateway para que lo use
        gateway_router.http_client = proxy_http_client
        log.info("HTTPX client initialized successfully.", limits=str(limits), timeout=str(timeout))
    except Exception as e:
        log.exception("CRITICAL: Failed to initialize HTTPX client during startup!", error=str(e))
        # Si el cliente HTTP falla, el proxy no funcionará. Podríamos decidir salir.
        proxy_http_client = None
        gateway_router.http_client = None
        # raise RuntimeError("Failed to initialize HTTP client, cannot start gateway.") from e

    # 2. Inicializar y Verificar Conexión a PostgreSQL
    log.info("Initializing and verifying PostgreSQL connection pool...")
    db_pool_ok = False
    try:
        # get_db_pool() creará el pool si no existe
        pool = await postgres_client.get_db_pool()
        if pool:
             # Verificar conexión real
             db_pool_ok = await postgres_client.check_db_connection()
             if db_pool_ok:
                 log.info("PostgreSQL connection pool initialized and connection verified.")
             else:
                  log.critical("PostgreSQL pool initialized BUT connection check failed!")
                  # Podríamos cerrar el pool recién creado si la verificación falla
                  await postgres_client.close_db_pool()
        else:
             log.critical("PostgreSQL connection pool initialization returned None!")

    except Exception as e:
        log.exception("CRITICAL: Failed to initialize or verify PostgreSQL connection during startup!", error=str(e))
        # La app puede seguir, pero las funciones DB fallarán. get_db_pool relanzará si hay error.

    # Si alguna dependencia crítica falló, podríamos loguearlo de nuevo aquí
    if not proxy_http_client:
        log.warning("Startup warning: HTTP client is not available.")
    if not db_pool_ok:
        log.warning("Startup warning: PostgreSQL connection is not available.")

    # --- Aplicación Lista para Recibir Tráfico ---
    log.info("Application startup sequence complete. Ready to serve requests.")
    yield # <--- La aplicación se ejecuta aquí

    # --- Shutdown Sequence ---
    log.info("Application shutdown sequence initiated...")

    # 1. Cerrar Cliente HTTP
    if proxy_http_client and not proxy_http_client.is_closed:
        log.info("Closing global HTTPX client...")
        try:
            await proxy_http_client.aclose()
            log.info("HTTPX client closed successfully.")
        except Exception as e:
            log.exception("Error closing HTTPX client during shutdown.", error=str(e))

    # 2. Cerrar Pool de PostgreSQL
    log.info("Closing PostgreSQL connection pool...")
    try:
        await postgres_client.close_db_pool()
        # El log de éxito/info ya está en close_db_pool()
    except Exception as e:
        log.exception("Error closing PostgreSQL connection pool during shutdown.", error=str(e))

    log.info("Application shutdown sequence complete.")


# --- Creación de la App FastAPI ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Atenex API Gateway: Punto de entrada único, autenticación JWT, enrutamiento a microservicios backend (Ingest, Query).",
    version="1.0.1", # O la versión que uses
    lifespan=lifespan, # Usar el gestor de contexto lifespan
    # openapi_url=f"{settings.API_V1_STR}/openapi.json" # Opcional: ruta para spec OpenAPI
)

# --- Middlewares ---

# 1. CORS Middleware
# Construir lista de orígenes permitidos dinámicamente
allowed_origins: List[str] = []
if settings.VERCEL_FRONTEND_URL:
    allowed_origins.append(settings.VERCEL_FRONTEND_URL)
# Añadir localhost para desarrollo local del frontend
allowed_origins.append("http://localhost:3000")
allowed_origins.append("http://localhost:3001") # Puerto alternativo común

# Permitir NGROK si está configurado (útil para demos/webhooks)
# ngrok_url = os.getenv("NGROK_URL") # Leer directamente de env o desde settings
# if ngrok_url and ngrok_url.startswith("https://") and ".ngrok" in ngrok_url:
#     allowed_origins.append(ngrok_url)

# Eliminar duplicados y None/empty strings
allowed_origins = list(filter(None, set(allowed_origins)))

log.info("Configuring CORS middleware", allowed_origins=allowed_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins if allowed_origins else ["*"], # Permitir todo si no hay orígenes específicos
    allow_credentials=True, # Importante para cookies/auth headers
    allow_methods=["*"],   # Permitir todos los métodos estándar
    allow_headers=["*"],   # Permitir todos los headers (incluye Authorization, Content-Type, X-Request-ID, etc.)
    expose_headers=["X-Request-ID", "X-Process-Time"], # Headers que el frontend puede leer
    max_age=600, # Tiempo en segundos que el navegador cachea la respuesta preflight OPTIONS
)

# 2. Middleware para Request ID, Timing y Logging Estructurado
@app.middleware("http")
async def add_request_context_timing_logging(request: Request, call_next):
    start_time = time.perf_counter() # Usar perf_counter para mejor precisión
    # Obtener o generar Request ID
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    # Poner request_id en el estado para acceso fácil en otros lugares
    request.state.request_id = request_id

    # Crear un logger contextual para esta request
    request_log = log.bind(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        client_ip=request.client.host if request.client else "unknown"
    )

    request_log.info("Request received")

    response = None
    status_code = 500 # Default en caso de error no manejado antes de la respuesta
    process_time_ms = 0

    try:
        # Llamar al siguiente middleware o a la ruta
        response = await call_next(request)
        status_code = response.status_code

    except Exception as e:
        # Capturar excepciones no manejadas por los handlers de FastAPI o rutas
        process_time_ms = (time.perf_counter() - start_time) * 1000
        request_log.exception("Unhandled exception during request processing",
                              error=str(e), status_code=status_code, # status_code puede ser 500 aquí
                              process_time_ms=round(process_time_ms, 2))

        # Crear una respuesta de error genérica 500
        response = JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal Server Error"}
        )
        # Añadir request ID al header de error también
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time"] = f"{process_time_ms:.2f}ms"
        # Devolver la respuesta de error
        return response
    finally:
        # Calcular tiempo de procesamiento final (incluso si hubo excepción antes de la respuesta)
        # Si la respuesta se creó en el 'except', este cálculo es redundante, pero inofensivo
        if response: # Asegurar que response existe
            process_time_ms = (time.perf_counter() - start_time) * 1000
            # Añadir headers de Request ID y Timing a la respuesta final
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time"] = f"{process_time_ms:.2f}ms"

            # Log final con estado y tiempo
            # No loguear health checks en INFO para no llenar logs
            log_level = "debug" if request.url.path == "/health" else "info"
            log_func = getattr(request_log, log_level)
            log_func("Request completed",
                     status_code=status_code,
                     process_time_ms=round(process_time_ms, 2))

    return response


# --- Incluir Routers ---
log.info("Including application routers...")
# Rutas de usuario (login, ensure-company)
app.include_router(user_router.router, tags=["Users & Authentication"])
# Rutas de gateway (proxy)
app.include_router(gateway_router.router) # Tags definidos dentro del router
# El router de auth (/api/v1/auth/*) está deshabilitado o manejado por gateway_router si AUTH_SERVICE_URL está seteado
# app.include_router(auth_router.router) # Comentado/Eliminado

log.info("Routers included successfully.")

# --- Endpoint Raíz y Health Check ---
@app.get("/", tags=["General"], summary="Root endpoint indicating service is running")
async def read_root():
    """Endpoint raíz simple para indicar que el gateway está activo."""
    return {"message": f"{settings.PROJECT_NAME} is running!"}

@app.get("/health", tags=["Health"], summary="Basic health check endpoint")
async def health_check():
    """
    Verifica la salud básica del servicio.
    Podría extenderse para verificar dependencias (DB, HTTP Client).
    """
    # Estado básico
    health_status = {"status": "healthy", "service": settings.PROJECT_NAME}
    # Opcional: Verificar dependencias (puede añadir latencia)
    # http_ok = gateway_router.http_client and not gateway_router.http_client.is_closed
    # db_ok = await postgres_client.check_db_connection()
    # if not http_ok or not db_ok:
    #     health_status["status"] = "unhealthy"
    #     health_status["checks"] = {"http_client": http_ok, "database": db_ok}
    #     return JSONResponse(content=health_status, status_code=503)

    return health_status

# --- Verificación de Dependencias en Startup (Opcional pero útil) ---
# Se ejecuta después de que el lifespan 'yield' ha ocurrido pero antes de aceptar tráfico.
# @app.on_event("startup")
# async def verify_lifespan_dependencies_post_yield():
#     """Verificación adicional de dependencias críticas después de la inicialización."""
#     log.info("Performing post-lifespan dependency verification...")
#     if not gateway_router.http_client:
#         log.critical("POST-STARTUP CHECK FAILED: HTTP Client is not available!")
#     # Verificar conexión a PostgreSQL de nuevo
#     try:
#         db_connected = await postgres_client.check_db_connection()
#         if not db_connected:
#             log.critical("POST-STARTUP CHECK FAILED: PostgreSQL connection check failed!")
#     except Exception as e:
#         log.critical("POST-STARTUP CHECK FAILED: PostgreSQL connection check error!", error=str(e))
#     log.info("Post-lifespan dependency verification complete.")


# --- Ejecución (para desarrollo local o si no se usa Gunicorn/Uvicorn directo) ---
if __name__ == "__main__":
    # Esto se usa generalmente para desarrollo local con --reload
    # En producción, Gunicorn + Uvicorn workers se encargan de ejecutar la app
    print(f"Starting {settings.PROJECT_NAME} using Uvicorn...")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8080, # Puerto estándar para contenedores
        reload=True, # Habilitar reload solo para desarrollo local
        log_level=settings.LOG_LEVEL.lower(), # Usar nivel de log de config
        # workers=1 # Uvicorn en modo reload usualmente usa 1 worker
    )