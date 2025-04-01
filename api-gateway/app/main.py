# api-gateway/app/main.py
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import httpx
import structlog
import uvicorn # Para ejecución local si es necesario
import time # Para medir tiempo de respuesta

# Configuración y Settings (Asegúrate que carga bien ahora)
from app.core.config import settings
# Configuración de Logging (structlog) ANTES de importar otros módulos que logueen
from app.core.logging_config import setup_logging
setup_logging() # Configurar logging al inicio

# Routers (Solo el gateway)
from app.routers import gateway_router

log = structlog.get_logger("api_gateway.main")

# --- Ciclo de vida de la aplicación para gestionar el cliente HTTP ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Inicializar cliente HTTP global
    log.info("Application startup: Initializing global HTTP client...",
             timeout=settings.HTTP_CLIENT_TIMEOUT,
             max_connections=settings.HTTP_CLIENT_MAX_CONNECTIONS,
             max_keepalive=settings.HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS)
    limits = httpx.Limits(
        max_keepalive_connections=settings.HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS,
        max_connections=settings.HTTP_CLIENT_MAX_CONNECTIONS
    )
    timeout = httpx.Timeout(settings.HTTP_CLIENT_TIMEOUT, connect=10.0) # Timeout general y de conexión
    # Podrías añadir reintentos con un transport personalizado si httpx no lo hace por defecto
    # transport = httpx.AsyncHTTPTransport(retries=2) # Requiere instalar httpcore[http2] o similar
    try:
        gateway_router.http_client = httpx.AsyncClient(
            # transport=transport,
            limits=limits,
            timeout=timeout,
            follow_redirects=False, # El gateway no debe seguir redirects automáticamente
            http2=True # Habilitar HTTP/2 si los backends lo soportan
        )
        log.info("HTTP Client initialized successfully.")
        # Realizar una prueba de conexión simple (opcional)
        # await gateway_router.http_client.get("http://google.com", timeout=5.0)
        # log.info("HTTP client test connection successful.")
    except Exception as e:
        log.exception("Failed to initialize HTTP client during startup!", error=e)
        # Podrías decidir salir si el cliente es esencial y falla al inicio
        # import sys
        # sys.exit("Failed to initialize HTTP client")
        gateway_router.http_client = None # Marcar como no disponible

    yield # La aplicación se ejecuta aquí

    # Shutdown: Cerrar cliente HTTP global
    log.info("Application shutdown: Closing HTTP client...")
    if gateway_router.http_client and not gateway_router.http_client.is_closed:
        try:
            await gateway_router.http_client.aclose()
            log.info("HTTP Client closed successfully.")
        except Exception as e:
            log.exception("Error closing HTTP client during shutdown.", error=e)
    else:
        log.warning("HTTP Client was not initialized or already closed.")


# --- Creación de la aplicación FastAPI ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Punto de entrada único y seguro para los microservicios de Nyro. Gestiona autenticación y enrutamiento.",
    version="1.0.0", # Ajusta la versión
    lifespan=lifespan, # Usar el gestor de ciclo de vida para el cliente HTTP
    # openapi_url=f"{settings.API_V1_STR}/openapi.json" # Opcional: ruta para spec OpenAPI
)

# --- Middlewares ---

# Middleware para añadir Request ID y medir tiempo de respuesta
@app.middleware("http")
async def add_process_time_header_and_request_id(request: Request, call_next):
    start_time = time.time()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))

    # Añadir request_id al contexto de structlog para todos los logs de esta petición
    with structlog.contextvars.bind_contextvars(request_id=request_id):
        log.info("Request received", method=request.method, path=request.url.path, client_ip=request.client.host)
        try:
            response = await call_next(request)
            process_time = time.time() - start_time
            response.headers["X-Process-Time"] = str(process_time)
            response.headers["X-Request-ID"] = request_id # Devolver ID al cliente
            log.info("Request processed successfully", status_code=response.status_code, duration=round(process_time, 4))
        except Exception as e:
             process_time = time.time() - start_time
             log.exception("Unhandled exception during request processing", duration=round(process_time, 4), error=str(e))
             # Re-lanzar para que el exception handler global lo capture
             raise e
        return response


# CORS (Configurar adecuadamente para producción)
# Orígenes permitidos deberían ser la URL de tu frontend
ALLOWED_ORIGINS = ["*"] # CAMBIAR EN PRODUCCIÓN a algo como ["https://tu-frontend.com"]
if os.getenv("ENVIRONMENT") == "production": # Ejemplo de cómo cambiar en prod
    ALLOWED_ORIGINS = ["https://your-production-frontend.com"] # Reemplaza con tu URL real

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True, # Permite cookies/auth headers
    allow_methods=["*"],    # O especifica métodos: ["GET", "POST", "PUT", "DELETE"]
    allow_headers=["*"],    # O especifica headers necesarios: ["Authorization", "Content-Type", "X-Company-ID"]
)


# Podrías añadir otros middlewares aquí (ej: tracing con OpenTelemetry)

# --- Routers ---
# Incluir el router principal del gateway
app.include_router(gateway_router.router)

# --- Endpoints Básicos del Propio Gateway ---
@app.get("/", tags=["Gateway Status"], summary="Root endpoint")
async def root():
    """Endpoint raíz para verificar que el Gateway está activo."""
    return {"message": f"{settings.PROJECT_NAME} is running!"}

@app.get("/health",
         tags=["Gateway Status"],
         summary="Kubernetes Health Check",
         response_description="Returns 'healthy' if the gateway is operational.",
         status_code=status.HTTP_200_OK,
         responses={
             status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Gateway HTTP client is not ready"}
         })
async def health_check(
    # Usar la dependencia para asegurar que el cliente está listo
    client: httpx.AsyncClient = Depends(gateway_router.get_client)
):
    """
    Endpoint de health check para Kubernetes Liveness/Readiness probes.
    Verifica si el cliente HTTP interno está inicializado y no cerrado.
    Si get_client() falla, devolverá 503 automáticamente.
    """
    log.debug("Health check endpoint called, client is available.")
    # Podrías añadir chequeos pasivos adicionales si es necesario (ej: config cargada)
    return {"status": "healthy", "service": settings.PROJECT_NAME}

# --- Manejador de Excepciones Global (personalizado) ---
# Captura las HTTPException y las loguea de forma estructurada
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # Usar el logger vinculado con request_id si existe
    req_id = structlog.contextvars.get_contextvars().get("request_id", "N/A")
    bound_log = log.bind(request_id=req_id)

    bound_log.warning(
        "HTTP Exception occurred",
        status_code=exc.status_code,
        detail=exc.detail,
        method=request.method,
        path=request.url.path,
        client_ip=request.client.host if request.client else "N/A",
        headers=dict(exc.headers) if exc.headers else None # Loguear headers de la excepción (ej: WWW-Authenticate)
    )
    # Devolver la respuesta JSON estándar para HTTPException
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers, # Preservar headers como WWW-Authenticate
    )

# Captura cualquier otra excepción no manejada y devuelve un 500 genérico
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    req_id = structlog.contextvars.get_contextvars().get("request_id", "N/A")
    bound_log = log.bind(request_id=req_id)

    bound_log.exception(
        "Unhandled internal server error occurred in gateway",
        method=request.method,
        path=request.url.path,
        client_ip=request.client.host if request.client else "N/A",
        error_type=type(exc).__name__,
        error=str(exc)
    )
    # Devolver una respuesta genérica 500 para no exponer detalles internos
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred."},
    )

log.info(f"'{settings.PROJECT_NAME}' application configured and ready to start.")

# --- Ejecución Local (Opcional, para desarrollo) ---
# if __name__ == "__main__":
#     print(f"--- Starting {settings.PROJECT_NAME} locally with Uvicorn ---")
#     uvicorn.run(
#         "app.main:app",
#         host="0.0.0.0",
#         port=8080, # Puerto del Gateway, diferente a los microservicios
#         reload=True, # Activar reload para desarrollo (¡cuidado con el cliente HTTP!)
#         log_level=settings.LOG_LEVEL.lower(),
#         # Considera usar el loop uvloop para mejor rendimiento si está instalado
#         # loop="uvloop",
#         # http="httptools" # También puede mejorar rendimiento
#     )