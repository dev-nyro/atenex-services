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
import re # Importar el módulo de expresiones regulares

# --- Configuración de Logging PRIMERO ---
from app.core.logging_config import setup_logging
setup_logging()

# --- Importaciones Core y DB ---
from app.core.config import settings
from app.db import postgres_client # Importar cliente DB

# --- !!! Definir la variable global ANTES de importar routers !!! ---
proxy_http_client: Optional[httpx.AsyncClient] = None

# --- Importar Routers (Ahora es seguro) ---
from app.routers import gateway_router, user_router

log = structlog.get_logger("atenex_api_gateway.main")


# --- Lifespan Manager (Startup y Shutdown) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global proxy_http_client # Indicar que modificaremos la variable global
    log.info("Application startup sequence initiated...")

    # 1. Inicializar Cliente HTTP (usando la variable global)
    try:
        log.info("Initializing global HTTPX client...")
        limits = httpx.Limits(
            max_keepalive_connections=settings.HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS,
            max_connections=settings.HTTP_CLIENT_MAX_CONNECTIONS
        )
        timeout = httpx.Timeout(settings.HTTP_CLIENT_TIMEOUT, connect=10.0)
        proxy_http_client = httpx.AsyncClient( # Asigna a la variable global
            limits=limits,
            timeout=timeout,
            follow_redirects=False,
            http2=True
        )
        log.info("HTTPX client initialized successfully.", limits=str(limits), timeout=str(timeout))
    except Exception as e:
        log.exception("CRITICAL: Failed to initialize HTTPX client during startup!", error=str(e))
        proxy_http_client = None # Asegurarse que es None si falla

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
                  await postgres_client.close_db_pool() # Intentar cerrar si falló la verificación
        else:
             log.critical("PostgreSQL connection pool initialization returned None!")

    except Exception as e:
        log.exception("CRITICAL: Failed to initialize or verify PostgreSQL connection during startup!", error=str(e))

    # Verificación final antes de ceder el control
    if not proxy_http_client:
        log.error("Startup check failed: HTTP client is not available.")
        # Podrías lanzar una excepción aquí para detener el inicio si el cliente es crucial
        # raise RuntimeError("HTTP Client could not be initialized.")
    if not db_pool_ok:
        log.error("Startup check failed: PostgreSQL connection is not available.")
        # Podrías lanzar una excepción aquí
        # raise RuntimeError("Database connection failed.")

    log.info("Application startup sequence complete. Ready to serve requests (pending checks).")
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
    else:
        log.info("HTTPX client was not initialized or already closed.")


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
    description="Atenex API Gateway: Single entry point, JWT auth, routing to backend microservices (Ingest, Query).",
    version="1.0.2", # Version bump
    lifespan=lifespan,
)

# --- Middlewares ---

# 1. CORS Middleware (Lógica sin cambios)
vercel_pattern = ""
if settings.VERCEL_FRONTEND_URL:
    base_vercel_url = settings.VERCEL_FRONTEND_URL
    base_vercel_url = re.sub(r"(-git-[a-z0-9-]+)?(-[a-z0-9]+)?\.vercel\.app", ".vercel.app", base_vercel_url)
    escaped_base = re.escape(base_vercel_url).replace(r"\.vercel\.app", "")
    vercel_pattern = rf"({escaped_base}(-[a-z0-9-]+)*\.vercel\.app)"
else:
    log.warning("VERCEL_FRONTEND_URL not set, CORS for Vercel previews might not work.")

localhost_pattern = r"(http://localhost:300[0-9])"
allowed_origin_patterns = [localhost_pattern]
if vercel_pattern:
    allowed_origin_patterns.append(vercel_pattern)
final_regex = rf"^{ '|'.join(allowed_origin_patterns) }$"

log.info("Configuring CORS middleware", allow_origin_regex=final_regex)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=final_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-Process-Time"],
    max_age=600,
)

# 2. Middleware para Request ID, Timing y Logging (Lógica sin cambios)
@app.middleware("http")
async def add_request_context_timing_logging(request: Request, call_next):
    start_time = time.perf_counter()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = request_id

    request_log = log.bind(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        client_ip=request.client.host if request.client else "unknown",
        origin=request.headers.get("origin", "N/A")
    )

    if request.method == "OPTIONS": request_log.info("OPTIONS preflight request received")
    else: request_log.info("Request received")

    response = None
    status_code = 500
    process_time_ms = 0

    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception as e:
        process_time_ms = (time.perf_counter() - start_time) * 1000
        request_log.exception("Unhandled exception during request processing", error=str(e), status_code=status_code, process_time_ms=round(process_time_ms, 2))
        response = JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal Server Error"}
        )
        origin = request.headers.get("Origin")
        if origin and re.match(final_regex, origin):
             response.headers["Access-Control-Allow-Origin"] = origin
             response.headers["Access-Control-Allow-Credentials"] = "true"
        else: request_log.warning("Origin not allowed/missing for error response CORS", origin=origin)
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        if response:
            process_time_ms = (time.perf_counter() - start_time) * 1000
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time"] = f"{process_time_ms:.2f}ms"
            log_level = "debug" if request.url.path == "/health" else "info"
            log_func = getattr(request_log.bind(status_code=status_code), log_level)
            if request.method != "OPTIONS": log_func("Request completed", process_time_ms=round(process_time_ms, 2))

    return response


# --- Incluir Routers ---
log.info("Including application routers...")
app.include_router(user_router.router) # Tags definidos en el router
app.include_router(gateway_router.router) # Tags definidos en el router
# Nota: auth_router no se incluye si fue eliminado o no tiene rutas
log.info("Routers included successfully.")

# --- Endpoint Raíz y Health Check ---
@app.get("/", tags=["General"], summary="Root endpoint indicating service is running")
async def read_root():
    return {"message": f"{settings.PROJECT_NAME} is running!"}

@app.get("/health", tags=["Health"], summary="Basic health check endpoint")
async def health_check():
    # Health check podría verificar estado del cliente httpx y db
    health_status = {"status": "healthy", "service": settings.PROJECT_NAME, "checks": {}}
    db_ok = await postgres_client.check_db_connection()
    health_status["checks"]["database_connection"] = "ok" if db_ok else "failed"

    # Verificar cliente HTTPX
    global proxy_http_client
    http_client_ok = proxy_http_client is not None and not proxy_http_client.is_closed
    health_status["checks"]["http_client"] = "ok" if http_client_ok else "failed"

    if not db_ok or not http_client_ok:
        health_status["status"] = "unhealthy"
        log.warning("Health check failed", checks=health_status["checks"])
        # No lanzar excepción aquí directamente, dejar que K8s maneje el estado unhealthy
        # raise HTTPException(status_code=503, detail=health_status)
        return JSONResponse(content=health_status, status_code=503)

    return health_status


# --- Ejecución (para desarrollo local) ---
if __name__ == "__main__":
    print(f"Starting {settings.PROJECT_NAME} using Uvicorn...")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8080)),
        reload=True,
        log_level=settings.LOG_LEVEL.lower(),
    )