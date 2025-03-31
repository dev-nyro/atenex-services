# api-gateway/app/main.py
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import httpx
import structlog
import uvicorn # Para ejecución local

# Configuración y Settings (Asegúrate que carga bien ahora)
from app.core.config import settings
# Configuración de Logging (structlog)
from app.core.logging_config import setup_logging

# Routers (Solo el gateway)
from app.routers import gateway_router

# Configurar logging ANTES de instanciar FastAPI o importar routers que logueen
setup_logging()
log = structlog.get_logger("api_gateway.main")

# --- Ciclo de vida de la aplicación para gestionar el cliente HTTP ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Inicializar cliente HTTP global
    log.info("Initializing global HTTP client for downstream services...")
    limits = httpx.Limits(
        max_keepalive_connections=settings.HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS,
        max_connections=settings.HTTP_CLIENT_MAX_CONNECTIONS
    )
    timeout = httpx.Timeout(settings.HTTP_CLIENT_TIMEOUT, connect=10.0) # Timeout general y de conexión
    # Podrías añadir reintentos con un transport personalizado si httpx no lo hace por defecto
    # transport = httpx.AsyncHTTPTransport(retries=2)
    gateway_router.http_client = httpx.AsyncClient(
        # transport=transport,
        limits=limits,
        timeout=timeout,
        follow_redirects=False # El gateway no debe seguir redirects
    )
    log.info(f"HTTP Client initialized. Timeout: {settings.HTTP_CLIENT_TIMEOUT}s.")
    yield
    # Shutdown: Cerrar cliente HTTP global
    log.info("Shutting down API Gateway... Closing HTTP client.")
    if gateway_router.http_client:
        await gateway_router.http_client.aclose()
        log.info("HTTP Client closed.")

# --- Creación de la aplicación FastAPI ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Punto de entrada único y seguro para los microservicios de Nyro.",
    version="1.1.0", # Incrementar versión
    lifespan=lifespan # Usar el gestor de ciclo de vida
)

# --- Middlewares ---
# CORS (Configurar adecuadamente para producción)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Cambiar a orígenes específicos en producción
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Podrías añadir otros middlewares aquí (ej: tracing, métricas)

# --- Routers ---
# Incluir el router principal del gateway
app.include_router(gateway_router.router)

# --- Endpoints Básicos ---
@app.get("/", tags=["Health Check"], summary="Root endpoint")
async def root():
    """Endpoint raíz para verificar que el Gateway está activo."""
    return {"message": f"{settings.PROJECT_NAME} is running!"}

@app.get("/health", tags=["Health Check"], summary="Kubernetes Health Check")
async def health_check(client: httpx.AsyncClient = Depends(gateway_router.get_client)):
    """
    Endpoint de health check para Kubernetes probes.
    Verifica si el cliente HTTP está inicializado y no cerrado.
    """
    # get_client ya lanza 503 si no está listo
    log.debug("Health check endpoint called, client seems ok.")
    return {"status": "healthy", "service": settings.PROJECT_NAME}

# --- Manejador de Excepciones Global (personalizado) ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # Loguear la excepción HTTP con structlog
    log.warning(
        "HTTP Exception occurred",
        status_code=exc.status_code,
        detail=exc.detail,
        method=request.method,
        path=request.url.path,
        client_ip=request.client.host if request.client else "N/A",
        # No loguear headers por defecto para evitar info sensible, excepto quizás request-id
        request_id=request.headers.get("x-request-id", "N/A")
    )
    # Devolver la respuesta JSON estándar para HTTPException
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers, # Preservar headers como WWW-Authenticate
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    # Loguear excepciones no manejadas con structlog
    log.exception(
        "Unhandled exception occurred",
        method=request.method,
        path=request.url.path,
        client_ip=request.client.host if request.client else "N/A",
        request_id=request.headers.get("x-request-id", "N/A")
    )
    # Devolver una respuesta genérica 500
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred in the gateway."},
    )

log.info(f"{settings.PROJECT_NAME} application configured and ready.")

# --- Ejecución Local (Opcional, para desarrollo) ---
if __name__ == "__main__":
    print(f"Starting {settings.PROJECT_NAME} locally with Uvicorn...")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8080, # Puerto diferente a los microservicios
        reload=True, # Activar reload para desarrollo
        log_level=settings.LOG_LEVEL.lower()
    )