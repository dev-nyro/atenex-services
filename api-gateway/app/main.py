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
import re # <-- AÑADIDO: Importar el módulo de expresiones regulares

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
# (Sin cambios en lifespan)
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
        proxy_http_client = None
        gateway_router.http_client = None

    # 2. Inicializar y Verificar Conexión a PostgreSQL
    log.info("Initializing and verifying PostgreSQL connection pool...")
    db_pool_ok = False
    try:
        pool = await postgres_client.get_db_pool()
        if pool:
             db_pool_ok = await postgres_client.check_db_connection()
             if db_pool_ok:
                 log.info("PostgreSQL connection pool initialized and connection verified.")
             else:
                  log.critical("PostgreSQL pool initialized BUT connection check failed!")
                  await postgres_client.close_db_pool()
        else:
             log.critical("PostgreSQL connection pool initialization returned None!")

    except Exception as e:
        log.exception("CRITICAL: Failed to initialize or verify PostgreSQL connection during startup!", error=str(e))

    if not proxy_http_client:
        log.warning("Startup warning: HTTP client is not available.")
    if not db_pool_ok:
        log.warning("Startup warning: PostgreSQL connection is not available.")

    log.info("Application startup sequence complete. Ready to serve requests.")
    yield # <--- La aplicación se ejecuta aquí

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
    except Exception as e:
        log.exception("Error closing PostgreSQL connection pool during shutdown.", error=str(e))

    log.info("Application shutdown sequence complete.")


# --- Creación de la App FastAPI ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Atenex API Gateway: Punto de entrada único, autenticación JWT, enrutamiento a microservicios backend (Ingest, Query).",
    version="1.0.1",
    lifespan=lifespan,
)

# --- Middlewares ---

# 1. CORS Middleware
# --- *** CORRECCIÓN: Usar allow_origin_regex para Vercel y localhost *** ---
# Construir expresión regular para permitir localhost, URL Vercel principal y previews/branches
# Asumimos que settings.VERCEL_FRONTEND_URL contiene la URL BASE DE PRODUCCIÓN o similar
# y que las previews siguen un patrón predecible. Ajusta si es necesario.
vercel_pattern = ""
if settings.VERCEL_FRONTEND_URL:
    # Ejemplo: VERCEL_FRONTEND_URL="https://atenex-frontend.vercel.app"
    # El regex permitirá "https://atenex-frontend.vercel.app" y "https://atenex-frontend-*.vercel.app"
    # O si VERCEL_FRONTEND_URL="https://atenex-frontend-git-main-....vercel.app"
    # Este regex intentará adaptarse:
    base_vercel_url = settings.VERCEL_FRONTEND_URL
    # Quita el posible hash/branch para generalizar (esto es una heurística, puede necesitar ajuste)
    base_vercel_url = re.sub(r"(-git-[a-z0-9-]+)?(-[a-z0-9]+)?\.vercel\.app", ".vercel.app", base_vercel_url)
    escaped_base = re.escape(base_vercel_url).replace(r"\.vercel\.app", "")
    # Permitir la URL base exacta O con partes adicionales antes de .vercel.app
    vercel_pattern = rf"({escaped_base}(-[a-z0-9-]+)*\.vercel\.app)"
    log.info("Derived Vercel CORS pattern", pattern=vercel_pattern, original_url=settings.VERCEL_FRONTEND_URL)
else:
    log.warning("VERCEL_FRONTEND_URL not set in config, CORS for Vercel might not work correctly.")

# Patrón para localhost en puertos 3000 a 3009 (común para desarrollo frontend)
localhost_pattern = r"(http://localhost:300[0-9])"

# Combinar patrones: localhost Y Vercel (si está definido)
allowed_origin_patterns = [localhost_pattern]
if vercel_pattern:
    allowed_origin_patterns.append(vercel_pattern)

# Crear la regex final (inicio de línea ^, patrón1 | patrón2, fin de línea $)
final_regex = rf"^{ '|'.join(allowed_origin_patterns) }$"

log.info("Configuring CORS middleware", allow_origin_regex=final_regex)
app.add_middleware(
    CORSMiddleware,
    # allow_origins=allowed_origins if allowed_origins else ["*"], # <-- REEMPLAZADO POR REGEX
    allow_origin_regex=final_regex, # <-- USAR REGEX CONSTRUIDA
    allow_credentials=True, # Necesario para enviar/recibir cookies o cabeceras Authorization
    allow_methods=["*"], # Permite GET, POST, OPTIONS, DELETE, PUT, etc.
    allow_headers=["*"], # Permite 'Content-Type', 'Authorization', 'X-Request-ID', etc.
    expose_headers=["X-Request-ID", "X-Process-Time"], # Cabeceras expuestas al frontend
    max_age=600, # Tiempo en segundos que el resultado preflight puede ser cacheado
)
# --- *** FIN DE LA CORRECCIÓN CORS *** ---


# 2. Middleware para Request ID, Timing y Logging Estructurado
# (Sin cambios en este middleware)
@app.middleware("http")
async def add_request_context_timing_logging(request: Request, call_next):
    start_time = time.perf_counter()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = request_id

    # Bind inicial para el logger de la request
    request_log = log.bind(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        client_ip=request.client.host if request.client else "unknown",
        origin=request.headers.get("origin", "N/A") # <-- Añadir origin para debugging CORS
    )

    # Si es una solicitud OPTIONS, loguearla de forma diferente
    if request.method == "OPTIONS":
        request_log.info("OPTIONS preflight request received")
        # El middleware CORS debería manejar la respuesta
        # Dejamos que continúe para que CORSMiddleware actúe
    else:
        request_log.info("Request received") # Log para otras solicitudes

    response = None
    status_code = 500
    process_time_ms = 0

    try:
        response = await call_next(request)
        status_code = response.status_code

    except Exception as e:
        process_time_ms = (time.perf_counter() - start_time) * 1000
        request_log.exception("Unhandled exception during request processing",
                              error=str(e), status_code=status_code,
                              process_time_ms=round(process_time_ms, 2))
        # Crear respuesta de error estándar
        response = JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal Server Error"}
        )
        # Importante: Añadir cabeceras CORS también a las respuestas de error generadas aquí
        origin = request.headers.get("Origin")
        if origin and re.match(final_regex, origin): # Si el origen es permitido por nuestra regex
             response.headers["Access-Control-Allow-Origin"] = origin
             response.headers["Access-Control-Allow-Credentials"] = "true"
        else:
             request_log.warning("Origin not allowed or missing for error response CORS headers", origin=origin)

        response.headers["X-Request-ID"] = request_id
        # No añadir X-Process-Time a la respuesta de error necesariamente
        return response

    finally:
        # Código que se ejecuta después de que la solicitud se procesó (incluso si hubo excepción NO CAPTURADA POR EL TRY)
        # Si la respuesta fue generada por el call_next() normal:
        if response:
            process_time_ms = (time.perf_counter() - start_time) * 1000
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time"] = f"{process_time_ms:.2f}ms"

            # Logging final (evitar loguear /health en INFO)
            log_level = "debug" if request.url.path == "/health" else "info"
            log_func = getattr(request_log, log_level)

            # Añadir status_code al contexto del logger antes del mensaje final
            request_log = request_log.bind(status_code=status_code)

            if request.method != "OPTIONS": # No loguear completado para OPTIONS si ya logueamos "received"
                log_func("Request completed",
                         process_time_ms=round(process_time_ms, 2))

    return response


# --- Incluir Routers ---
log.info("Including application routers...")
app.include_router(user_router.router) # Tags ya definidos en el router
app.include_router(gateway_router.router) # Tags ya definidos en el router
log.info("Routers included successfully.")

# --- Endpoint Raíz y Health Check ---
# (Sin cambios aquí)
@app.get("/", tags=["General"], summary="Root endpoint indicating service is running")
async def read_root():
    return {"message": f"{settings.PROJECT_NAME} is running!"}

@app.get("/health", tags=["Health"], summary="Basic health check endpoint")
async def health_check():
    # Comprobación básica, se podría extender para verificar DB, etc.
    health_status = {"status": "healthy", "service": settings.PROJECT_NAME}
    db_ok = await postgres_client.check_db_connection()
    if not db_ok:
       log.warning("Health check warning: Database connection failed")
       # No cambiar status a unhealthy por ahora, solo loguear
       # health_status["status"] = "unhealthy"
       # health_status["checks"] = { "database_connection": "failed" }
       # return JSONResponse(content=health_status, status_code=503) # Podría causar problemas en K8s probes
       health_status["checks"] = { "database_connection": "failed (warning)" }
    else:
       health_status["checks"] = { "database_connection": "ok" }

    return health_status


# --- Ejecución (para desarrollo local o si no se usa Gunicorn/Uvicorn directo) ---
if __name__ == "__main__":
    print(f"Starting {settings.PROJECT_NAME} using Uvicorn...")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8080)), # Usar PORT de env var si existe, default 8080
        reload=True, # Desactivar reload en producción o al usar Gunicorn con workers
        log_level=settings.LOG_LEVEL.lower(),
    ) 