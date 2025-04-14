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
from app.core.logging_config import setup_logging
setup_logging()

# --- Importaciones Post-Logging ---
from app.core.config import settings
from app.db import postgres_client # Importar cliente DB
from app.routers import gateway_router, user_router # Importar routers

# Logger principal para este módulo
log = structlog.get_logger("atenex_api_gateway.main")

# --- Clientes Globales (Inicializados en Lifespan) ---
proxy_http_client: Optional[httpx.AsyncClient] = None

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

        # *** CORRECCIÓN: Quitar http2=True para evitar error de dependencia 'h2' ***
        proxy_http_client = httpx.AsyncClient(
            limits=limits,
            timeout=timeout,
            follow_redirects=False,
            # http2=True # <-- QUITAR O ASEGURAR QUE 'h2' ESTÁ INSTALADO
        )
        # Inyectar el cliente en el router del gateway para que lo use
        gateway_router.http_client = proxy_http_client
        log.info("HTTPX client initialized successfully.", limits=str(limits), timeout=str(timeout))
    except Exception as e:
        # Si ocurre otro error al inicializar httpx
        log.exception("CRITICAL: Failed to initialize HTTPX client during startup!", error=str(e))
        proxy_http_client = None
        gateway_router.http_client = None
        # Considera salir si el proxy es esencial:
        # raise RuntimeError("Failed to initialize HTTP client, cannot start gateway.") from e

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

    # Loguear estado final de dependencias
    if not proxy_http_client:
        log.warning("Startup complete but HTTP client is NOT available.")
    if not db_pool_ok:
        log.warning("Startup complete but PostgreSQL connection is NOT available.")

    # --- Aplicación Lista para Recibir Tráfico ---
    log.info("Application startup sequence complete. Ready to serve requests.")
    yield # <--- La aplicación se ejecuta aquí

    # --- Shutdown Sequence ---
    log.info("Application shutdown sequence initiated...")
    # (Shutdown sin cambios)
    if proxy_http_client and not proxy_http_client.is_closed:
        log.info("Closing global HTTPX client...")
        try:
            await proxy_http_client.aclose()
            log.info("HTTPX client closed successfully.")
        except Exception as e:
            log.exception("Error closing HTTPX client during shutdown.", error=str(e))

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
# (La configuración existente con la lista dinámica parece correcta,
# siempre que VERCEL_FRONTEND_URL esté bien configurado)
allowed_origins = []
if settings.VERCEL_FRONTEND_URL:
    allowed_origins.append(settings.VERCEL_FRONTEND_URL)
allowed_origins.append("http://localhost:3000")
allowed_origins.append("http://localhost:3001")
# Si usas ngrok persistentemente, añádelo aquí o permite '*' si es para pruebas temporales
# O lee una variable de entorno NGROK_URL
# allowed_origins.append("https://be91-2001-1388-53a0-7b8e-ccc7-b326-c1fe-c3e.ngrok-free.app") # Ejemplo ngrok

allowed_origins = list(filter(None, set(allowed_origins)))
log.info("Configuring CORS middleware", allowed_origins=allowed_origins if allowed_origins else ["*"])

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins if allowed_origins else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-Process-Time"],
    max_age=600,
)

# 2. Middleware para Request ID, Timing y Logging Estructurado
# (Middleware sin cambios)
@app.middleware("http")
async def add_request_context_timing_logging(request: Request, call_next):
    start_time = time.perf_counter()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = request_id
    request_log = log.bind(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        client_ip=request.client.host if request.client else "unknown"
    )
    request_log.info("Request received")
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
        response = JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal Server Error"}
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time"] = f"{process_time_ms:.2f}ms"
        return response
    finally:
        if response:
            process_time_ms = (time.perf_counter() - start_time) * 1000
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time"] = f"{process_time_ms:.2f}ms"
            log_level = "debug" if request.url.path == "/health" else "info"
            log_func = getattr(request_log, log_level)
            log_func("Request completed",
                     status_code=status_code,
                     process_time_ms=round(process_time_ms, 2))
    return response


# --- Incluir Routers ---
# (Sin cambios)
log.info("Including application routers...")
app.include_router(user_router.router, tags=["Users & Authentication"])
app.include_router(gateway_router.router)
log.info("Routers included successfully.")

# --- Endpoint Raíz y Health Check ---
# (Sin cambios)
@app.get("/", tags=["General"], summary="Root endpoint indicating service is running")
async def read_root():
    return {"message": f"{settings.PROJECT_NAME} is running!"}

@app.get("/health", tags=["Health"], summary="Basic health check endpoint")
async def health_check():
    health_status = {"status": "healthy", "service": settings.PROJECT_NAME}
    # Aquí podrías añadir chequeos de dependencias si es necesario
    # http_ok = gateway_router.http_client and not gateway_router.http_client.is_closed
    # db_ok = await postgres_client.check_db_connection()
    # if not http_ok or not db_ok:
    #    ... (código para marcar como unhealthy) ...
    return health_status

# --- Ejecución ---
# (Sin cambios)
if __name__ == "__main__":
    print(f"Starting {settings.PROJECT_NAME} using Uvicorn...")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8080,
        reload=True,
        log_level=settings.LOG_LEVEL.lower(),
    )