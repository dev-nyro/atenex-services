# Estructura de la Codebase

```
app/
├── __init__.py
├── auth
│   ├── __init__.py
│   ├── auth_middleware.py
│   └── jwt_handler.py
├── core
│   ├── __init__.py
│   ├── config.py
│   └── logging_config.py
├── main.py
└── routers
    ├── __init__.py
    └── gateway_router.py
```

# Codebase: `app`

## File: `app\__init__.py`
```py
# ...existing code or leave empty...

```

## File: `app\auth\__init__.py`
```py

```

## File: `app\auth\auth_middleware.py`
```py
# api-gateway/app/auth/auth_middleware.py
from fastapi import Request, HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional, Annotated, Dict, Any
import structlog

from .jwt_handler import verify_token

log = structlog.get_logger(__name__)

# Define el esquema de seguridad para documentación OpenAPI y extracción de token.
# auto_error=False significa que manejaremos el error si falta el token nosotros mismos.
bearer_scheme = HTTPBearer(bearerFormat="JWT", auto_error=False)

async def get_current_user_payload(
    request: Request,
    # Usa Annotated y Depends para inyectar el resultado de bearer_scheme
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]
) -> Optional[Dict[str, Any]]:
    """
    Dependencia FastAPI para intentar validar el token JWT si está presente.

    - Si el token es válido, devuelve el payload decodificado.
    - Si el token es inválido o expirado, lanza la HTTPException de verify_token (401).
    - Si no hay token (authorization is None o no es Bearer), devuelve None.

    Almacena el payload (o None) en request.state.user para posible uso posterior.
    Esta dependencia NO fuerza la autenticación, solo la intenta si hay token.
    """
    if authorization is None:
        # No hay header Authorization o no es Bearer.
        log.debug("No Authorization Bearer header found. Proceeding as anonymous.")
        request.state.user = None # Marcar que no hay usuario autenticado
        return None

    token = authorization.credentials
    try:
        payload = verify_token(token) # Lanza HTTPException(401) o (500) si es inválido
        request.state.user = payload # Almacenar payload para otros middlewares/endpoints
        log.debug("Token verified in dependency. User payload set in request.state",
                  subject=payload.get('sub'),
                  company_id=payload.get('company_id'))
        return payload
    except HTTPException as e:
        # Propaga la excepción HTTP generada por verify_token (normalmente 401)
        log.info(f"Token verification failed in dependency: {e.detail}", status_code=e.status_code)
        request.state.user = None # Asegurar que no hay payload en estado
        # IMPORTANTE: Re-lanzamos la excepción para que FastAPI la maneje
        # y la ruta que depende de 'require_user' no se ejecute.
        raise e
    # No es necesario capturar Exception genérica aquí, verify_token ya lo hace

async def require_user(
    # Usar Annotated y Depends para obtener el resultado de get_current_user_payload
    # FastAPI primero ejecutará get_current_user_payload. Si esa dependencia
    # lanza una excepción (ej: 401 por token inválido), la ejecución se detiene
    # y esta dependencia 'require_user' ni siquiera se completa.
    user_payload: Annotated[Optional[Dict[str, Any]], Depends(get_current_user_payload)]
) -> Dict[str, Any]:
    """
    Dependencia FastAPI que *asegura* que una ruta requiere un usuario autenticado
    y con un token válido.

    Reutiliza get_current_user_payload. Si get_current_user_payload:
    - Devuelve un payload: Esta dependencia devuelve ese payload.
    - Devuelve None (sin token): Esta dependencia lanza un 401 explícito.
    - Lanza una excepción (token inválido): FastAPI ya habrá detenido la ejecución.

    Returns:
        El payload del usuario si la autenticación fue exitosa.

    Raises:
        HTTPException(401): Si no se proporcionó un token válido.
    """
    if user_payload is None:
        # Esto ocurre si get_current_user_payload devolvió None (porque no había token).
        # Si había un token pero era inválido, get_current_user_payload ya lanzó 401.
        log.info("Access denied: Authentication required but no valid token was found or provided.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"}, # Indica que se requiere Bearer token
        )
    # Si llegamos aquí, user_payload es un diccionario válido
    log.debug("User requirement met.", subject=user_payload.get('sub'))
    return user_payload

# No usaremos un Middleware global para JWT, aplicaremos 'require_user' por ruta.
```

## File: `app\auth\jwt_handler.py`
```py
# api-gateway/app/auth/jwt_handler.py
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional # Añadir Optional y List
from jose import JWTError, jwt
from fastapi import HTTPException, status
import structlog

from app.core.config import settings

log = structlog.get_logger(__name__)

SECRET_KEY = settings.JWT_SECRET
ALGORITHM = settings.JWT_ALGORITHM

# --- Claims Requeridos de un Token Supabase VÁLIDO ---
# Ajusta esto según lo que REALMENTE necesites y lo que Supabase incluya.
# 'sub' (Subject = User ID), 'aud' (Audience), 'exp' (Expiration) son estándar.
# Necesitas verificar si 'company_id' está directamente o dentro de app_metadata/user_metadata.
# Si está en metadata, la validación aquí solo asegura que el token es válido,
# y la extracción del company_id se haría después.
REQUIRED_CLAIMS = ['sub', 'aud', 'exp'] # Mínimo requerido estándar

# --- Audiencia Esperada (IMPORTANTE) ---
# Los tokens JWT de Supabase suelen tener 'authenticated' como audiencia para usuarios logueados.
# Verifica esto en un token real de tu proyecto Supabase.
EXPECTED_AUDIENCE = 'authenticated'

def verify_token(token: str) -> Dict[str, Any]:
    """
    Verifica el token JWT usando el secreto y algoritmo de Supabase.
    Valida la firma, expiración, audiencia y claims requeridos.

    Args:
        token: El string del token JWT.

    Returns:
        El payload decodificado si el token es válido.

    Raises:
        HTTPException(401): Si el token es inválido, expirado, malformado,
                           le faltan claims, o la audiencia no es correcta.
        HTTPException(500): Si ocurre un error inesperado.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer error=\"invalid_token\""},
    )
    internal_error_exception = HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="An error occurred during token verification",
    )

    if not token:
        log.warning("Attempted verification with empty token string.")
        credentials_exception.detail = "Authentication token was not provided."
        raise credentials_exception

    if SECRET_KEY == "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME_IN_ENV_OR_SECRET":
         log.critical("FATAL: Attempting JWT verification with default insecure secret!")
         raise internal_error_exception # No permitir operación con secreto por defecto

    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            audience=EXPECTED_AUDIENCE, # <-- VALIDACIÓN DE AUDIENCIA
            options={
                "verify_signature": True,
                "verify_aud": True, # <-- HABILITAR VALIDACIÓN DE AUDIENCIA
                "verify_exp": True,
                # "require": REQUIRED_CLAIMS # 'require' puede ser muy estricto, validamos manualmente
            }
        )

        # Validación manual de claims requeridos (más flexible que 'require')
        missing_claims = [claim for claim in REQUIRED_CLAIMS if claim not in payload]
        if missing_claims:
            log.warning("Token verification failed: Missing required claims.",
                        missing_claims=missing_claims,
                        token_subject=payload.get('sub'))
            credentials_exception.detail = f"Token missing required claims: {', '.join(missing_claims)}"
            raise credentials_exception

        # --- EXTRACCIÓN DE COMPANY_ID (¡IMPORTANTE!) ---
        # Supabase a menudo almacena datos personalizados en 'app_metadata' o 'user_metadata'.
        # NECESITAS VERIFICAR DÓNDE ESTÁ 'company_id' en tus tokens reales.
        company_id: Optional[str] = None
        # Opción 1: Directamente en el payload (menos común para Supabase)
        # company_id = payload.get('company_id')

        # Opción 2: Dentro de app_metadata (más común para datos relacionados con la app)
        app_metadata = payload.get('app_metadata')
        if isinstance(app_metadata, dict):
            company_id = app_metadata.get('company_id')
            # Podrías tener otros datos aquí: provider, roles, etc.
            # log.debug("Extracted app_metadata", data=app_metadata)

        # Opción 3: Dentro de user_metadata (más común para preferencias del usuario)
        # user_metadata = payload.get('user_metadata')
        # if isinstance(user_metadata, dict) and not company_id: # Solo si no se encontró en app_metadata
        #     company_id = user_metadata.get('company_id')

        # --- FIN EXTRACCIÓN COMPANY_ID ---

        # Validar que company_id se encontró (si es requerido por tu lógica)
        if company_id is None:
             log.error("Token verification successful, BUT 'company_id' not found in expected claims (app_metadata?).",
                       token_subject=payload.get('sub'),
                       payload_keys=list(payload.keys()))
             # Lanzar 403 Forbidden porque el usuario está autenticado pero no autorizado para proceder
             # sin company_id en este contexto. O podrías devolver el payload y manejarlo en el router.
             raise HTTPException(
                 status_code=status.HTTP_403_FORBIDDEN,
                 detail="User authenticated, but company association is missing in token.",
             )
        else:
             # Añadir company_id al payload devuelto para fácil acceso
             payload['company_id'] = str(company_id) # Asegurar que sea string
             log.debug("Token verified successfully and company_id found.",
                       subject=payload.get('sub'),
                       company_id=payload['company_id'])


        # 'sub' (user_id) ya está validado por REQUIRED_CLAIMS
        if 'sub' not in payload:
             log.error("Critical: 'sub' claim missing after initial check.", payload_keys=list(payload.keys()))
             raise credentials_exception # Debería haber fallado antes

        return payload

    except JWTError as e:
        log.warning(f"JWT Verification Error: {e}", token_provided=True, algorithm=ALGORITHM, audience=EXPECTED_AUDIENCE)
        # Ajustar el mensaje según el tipo de error
        error_desc = str(e)
        if "Signature verification failed" in error_desc:
            credentials_exception.detail = "Invalid token signature."
            credentials_exception.headers["WWW-Authenticate"] = "Bearer error=\"invalid_token\", error_description=\"Invalid signature\""
        elif "Token is expired" in error_desc:
            credentials_exception.detail = "Token has expired."
            credentials_exception.headers["WWW-Authenticate"] = "Bearer error=\"invalid_token\", error_description=\"The token has expired\""
        elif "Audience verification failed" in error_desc:
             credentials_exception.detail = "Invalid token audience."
             credentials_exception.headers["WWW-Authenticate"] = f"Bearer error=\"invalid_token\", error_description=\"Invalid audience, expected '{EXPECTED_AUDIENCE}'\""
        elif "required claim" in error_desc.lower():
             # Esto no debería pasar con la validación manual, pero por si acaso
             credentials_exception.detail = f"Token missing required claim: {e}"
             credentials_exception.headers["WWW-Authenticate"] = "Bearer error=\"invalid_token\", error_description=\"Missing required claim\""
        else:
            credentials_exception.detail = f"Token validation failed: {e}"

        raise credentials_exception from e
    except HTTPException as e:
        # Re-lanzar HTTPException (como la 403 por falta de company_id)
        raise e
    except Exception as e:
        log.exception(f"Unexpected error during token verification: {e}")
        raise internal_error_exception from e
```

## File: `app\core\__init__.py`
```py

```

## File: `app\core\config.py`
```py
# api-gateway/app/core/config.py
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
import sys
import logging
from typing import Optional

K8S_INGEST_SVC_URL_DEFAULT = "http://ingest-api-service.nyro-develop.svc.cluster.local:80"
K8S_QUERY_SVC_URL_DEFAULT = "http://query-service.nyro-develop.svc.cluster.local:80"
# K8S_AUTH_SVC_URL_DEFAULT = "http://auth-service.nyro-develop.svc.cluster.local:80" # Si aplica

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='GATEWAY_',
        case_sensitive=False,
        env_file_encoding='utf-8',
        extra='ignore'
    )

    PROJECT_NAME: str = "Nyro API Gateway"
    API_V1_STR: str = "/api/v1"

    INGEST_SERVICE_URL: str = os.getenv("GATEWAY_INGEST_SERVICE_URL", K8S_INGEST_SVC_URL_DEFAULT)
    QUERY_SERVICE_URL: str = os.getenv("GATEWAY_QUERY_SERVICE_URL", K8S_QUERY_SVC_URL_DEFAULT)
    AUTH_SERVICE_URL: Optional[str] = os.getenv("GATEWAY_AUTH_SERVICE_URL") # Para proxy de auth

    # JWT settings - Leído desde Secret K8s o .env
    # IMPORTANTE: El valor por defecto aquí SÓLO debe usarse para desarrollo local
    # NUNCA debe ser el valor real en producción/k8s.
    JWT_SECRET: str = "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME_IN_ENV_OR_SECRET"
    JWT_ALGORITHM: str = "HS256"

    LOG_LEVEL: str = "INFO"
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS: int = 20
    HTTP_CLIENT_MAX_CONNECTIONS: int = 100

@lru_cache()
def get_settings() -> Settings:
    log = logging.getLogger(__name__)
    log.setLevel(logging.INFO)
    log.addHandler(logging.StreamHandler(sys.stdout))
    log.info("Loading Gateway settings...")
    try:
        settings_instance = Settings()
        log.info("Gateway Settings Loaded:")
        log.info(f"  PROJECT_NAME: {settings_instance.PROJECT_NAME}")
        log.info(f"  INGEST_SERVICE_URL: {settings_instance.INGEST_SERVICE_URL}")
        log.info(f"  QUERY_SERVICE_URL: {settings_instance.QUERY_SERVICE_URL}")
        log.info(f"  AUTH_SERVICE_URL: {settings_instance.AUTH_SERVICE_URL or 'Not Set'}")
        # *** VERIFICACIÓN CRÍTICA DEL SECRETO JWT ***
        if settings_instance.JWT_SECRET == "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME_IN_ENV_OR_SECRET":
            log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            log.critical("! FATAL: GATEWAY_JWT_SECRET is using the default insecure value!")
            log.critical("! Set GATEWAY_JWT_SECRET via env var or K8s Secret.")
            log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            # Considera salir si es crítico en producción:
            # if os.getenv("ENVIRONMENT") == "production":
            #     sys.exit("FATAL: GATEWAY_JWT_SECRET not configured securely.")
        else:
            log.info(f"  JWT_SECRET: *** SET (Loaded from Secret/Env) ***")
        log.info(f"  JWT_ALGORITHM: {settings_instance.JWT_ALGORITHM}")
        log.info(f"  LOG_LEVEL: {settings_instance.LOG_LEVEL}")
        log.info(f"  HTTP_CLIENT_TIMEOUT: {settings_instance.HTTP_CLIENT_TIMEOUT}")
        return settings_instance
    except Exception as e:
        log.exception(f"FATAL: Error loading Gateway settings: {e}")
        sys.exit(f"FATAL: Error loading Gateway settings: {e}")

settings = get_settings()
```

## File: `app\core\logging_config.py`
```py
# api-gateway/app/core/logging_config.py
import logging
import sys
import structlog
import os
from app.core.config import settings # Importar settings ya parseadas

def setup_logging():
    """Configura el logging estructurado con structlog."""

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True), # Usar UTC
        structlog.processors.StackInfoRenderer(),
        # Añadir info de proceso/thread si es útil
        # structlog.processors.ProcessInfoProcessor(),
    ]

    # Add caller info only in debug mode for performance
    if settings.LOG_LEVEL.upper() == "DEBUG":
         shared_processors.append(structlog.processors.CallsiteParameterAdder(
             {
                 structlog.processors.CallsiteParameter.FILENAME,
                 structlog.processors.CallsiteParameter.LINENO,
             }
         ))

    # Configure structlog processors for eventual output
    structlog.configure(
        processors=shared_processors + [
            # Prepara el evento para el formateador stdlib
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure the formatter for stdlib logging handler
    formatter = structlog.stdlib.ProcessorFormatter(
        # Procesadores que se ejecutan en el diccionario final antes de renderizar
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            # Renderiza como JSON
            structlog.processors.JSONRenderer(),
            # O usa ConsoleRenderer para logs más legibles en desarrollo local:
            # structlog.dev.ConsoleRenderer(colors=True), # Requiere 'colorama'
        ],
        # Procesadores que se ejecutan ANTES del formateo (ya definidos en configure)
        # foreign_pre_chain=shared_processors, # No es necesario si se usa wrap_for_formatter
    )

    # Configure root logger handler (StreamHandler a stdout)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger() # Obtener root logger

    # Evitar añadir handlers duplicados si la función se llama más de una vez
    if not any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, structlog.stdlib.ProcessorFormatter) for h in root_logger.handlers):
        # Limpiar handlers existentes (opcional, puede interferir con otros)
        # root_logger.handlers.clear()
        root_logger.addHandler(handler)

    # Establecer nivel en el root logger
    try:
        root_logger.setLevel(settings.LOG_LEVEL.upper())
    except ValueError:
        root_logger.setLevel(logging.INFO)
        logging.warning(f"Invalid LOG_LEVEL '{settings.LOG_LEVEL}'. Defaulting to INFO.")


    # Silenciar librerías verbosas (ajustar niveles según necesidad)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING) # O INFO si quieres logs de acceso
    logging.getLogger("gunicorn.error").setLevel(logging.INFO)
    logging.getLogger("gunicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("jose").setLevel(logging.INFO)

    log = structlog.get_logger("api_gateway.config") # Logger específico
    log.info("Structlog logging configured", log_level=settings.LOG_LEVEL.upper())
```

## File: `app\main.py`
```py
# api-gateway/app/main.py
# --- Añadir la importación que faltaba ---
import os
# --- Fin de la corrección ---
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import httpx
import structlog
import uvicorn # Para ejecución local si es necesario
import time # Para medir tiempo de respuesta
import uuid # Para generar request ID

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
    # Generar un nuevo UUID para cada request si no viene en header
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))

    # Añadir request_id al contexto de structlog para todos los logs de esta petición
    with structlog.contextvars.bind_contextvars(request_id=request_id):
        log.info("Request received", method=request.method, path=request.url.path, client_ip=request.client.host if request.client else "N/A")
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


# --- Configuración CORS ---
# Orígenes permitidos: Tu URL de Vercel, tu URL de ngrok, y localhost para desarrollo local del frontend
# ¡IMPORTANTE! Reemplaza con tus URLs reales.
VERCEL_FRONTEND_URL = os.getenv("VERCEL_FRONTEND_URL", "https://TU_APP_EN_VERCEL.vercel.app") # Lee desde env o usa un placeholder
NGROK_URL = os.getenv("NGROK_URL", "https://b0c3-2001-1388-53a1-a7c9-8901-65aa-f1fe-6a8.ngrok-free.app") # URL de ngrok proporcionada
LOCALHOST_FRONTEND = "http://localhost:3000"

allowed_origins = [
    LOCALHOST_FRONTEND,
    VERCEL_FRONTEND_URL,
]

# Añadir ngrok URL si está definida
if NGROK_URL:
    # ngrok puede dar http y https, permitir ambos si es necesario, pero prefiere https
    if NGROK_URL.startswith("https://"):
        allowed_origins.append(NGROK_URL)
        allowed_origins.append(NGROK_URL.replace("https://", "http://")) # Permitir http también si es necesario
    elif NGROK_URL.startswith("http://"):
         allowed_origins.append(NGROK_URL)
         allowed_origins.append(NGROK_URL.replace("http://", "https://"))
    else:
         log.warning(f"NGROK_URL format not recognized: {NGROK_URL}")


# En producción estricta, podrías quitar localhost y ngrok
# if os.getenv("ENVIRONMENT") == "production":
#     allowed_origins = [VERCEL_FRONTEND_URL]

log.info("Configuring CORS", allowed_origins=allowed_origins)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins, # Lista de orígenes permitidos
    allow_credentials=True,        # Permite cookies/headers de auth
    allow_methods=["*"],           # Métodos permitidos (GET, POST, etc.)
    allow_headers=["*", "Authorization", "Content-Type", "X-Requested-With"], # Headers permitidos
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


```

## File: `app\routers\__init__.py`
```py

```

## File: `app\routers\gateway_router.py`
```py
# api-gateway/app/routers/gateway_router.py
from fastapi import APIRouter, Request, Response, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from typing import Optional, Annotated, Dict, Any
import httpx
import structlog
import asyncio # Para timeouts específicos

from app.core.config import settings
from app.auth.auth_middleware import require_user # Dependencia para proteger rutas

log = structlog.get_logger(__name__)
router = APIRouter()

# Cliente HTTP global reutilizable (se inicializará/cerrará en main.py lifespan)
http_client: Optional[httpx.AsyncClient] = None

# Headers que no deben pasarse directamente downstream ni upstream
# Añadir otros si es necesario (e.g., server, x-powered-by)
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host", # Host siempre debe ser el del servicio destino
    "content-length", # Será recalculado por httpx o el servidor destino
}

def get_client() -> httpx.AsyncClient:
    """Dependencia para obtener el cliente HTTP inicializado."""
    if http_client is None or http_client.is_closed:
        log.error("Gateway HTTP client is not available or closed.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway service is temporarily unavailable (client error)."
        )
    return http_client

# api-gateway/app/routers/gateway_router.py
# ... (importaciones y código existente) ...

async def _proxy_request(
    request: Request,
    target_url: str,
    client: httpx.AsyncClient,
    # user_payload es el diccionario devuelto por verify_token (via require_user)
    user_payload: Optional[Dict[str, Any]] # Puede ser None para rutas no protegidas
):
    """Función interna para realizar el proxy de la petición."""
    method = request.method
    downstream_url = httpx.URL(target_url)
    log_context = {} # Para añadir al log

    # 1. Preparar Headers
    headers_to_forward = {}
    # ... (código para copiar headers y quitar hop-by-hop) ...

    # 2. Inyectar Headers basados en el Payload del Token (SI EXISTE)
    if user_payload:
        # Extraer user_id (del claim 'sub')
        user_id = user_payload.get('sub')
        if user_id:
            headers_to_forward['X-User-ID'] = str(user_id)
            log_context['user_id'] = user_id
        else:
            log.warning("User payload present but 'sub' (user_id) claim missing!", payload_keys=list(user_payload.keys()))
            # Decide si esto es un error fatal (403) o si puedes continuar

        # Extraer company_id (que añadimos en verify_token)
        company_id = user_payload.get('company_id')
        if company_id:
            headers_to_forward['X-Company-ID'] = str(company_id) # Asegurar string
            log_context['company_id'] = company_id
        else:
            # Esto no debería ocurrir si verify_token lo requiere y lo añade,
            # pero es una verificación de seguridad adicional.
            log.error("CRITICAL: Valid user payload is missing 'company_id'!", payload_info=user_payload)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal configuration error: Company ID missing after auth.")

        # Podrías extraer y añadir otros headers como X-User-Email, X-User-Roles etc.
        user_email = user_payload.get('email')
        if user_email:
             headers_to_forward['X-User-Email'] = str(user_email)
             log_context['user_email'] = user_email

    # Vincular contexto al logger para esta petición
    log_with_context = log.bind(**log_context)

    # ... (código para preparar query params y body) ...
    query_params = request.query_params
    request_body_bytes = request.stream()

    # 3. Realizar la petición downstream
    log_with_context.info(f"Proxying request", method=method, path=request.url.path, target=str(downstream_url))

    try:
        req = client.build_request(
            method=method,
            url=downstream_url,
            headers=headers_to_forward,
            params=query_params,
            content=request_body_bytes
        )
        rp = await client.send(req, stream=True)

        # ... (código para procesar y devolver la respuesta) ...
        log_with_context.info(f"Received response from downstream", status_code=rp.status_code, target=str(downstream_url))
        # ... (filtrar headers de respuesta) ...
        response_headers = {k: v for k, v in rp.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}

        return StreamingResponse(
            rp.aiter_raw(),
            status_code=rp.status_code,
            headers=response_headers,
            media_type=rp.headers.get("content-type"),
        )

    # ... (manejo de excepciones httpx) ...
    except httpx.TimeoutException as exc:
         log_with_context.error(f"Request timed out", target=str(downstream_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=f"Upstream service timeout.")
    except httpx.ConnectError as exc:
         log_with_context.error(f"Connection error", target=str(downstream_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Upstream service unavailable.")
    except Exception as exc:
        log_with_context.exception(f"Unexpected error during proxy", target=str(downstream_url))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal gateway error.")
    finally:
        # Cerrar respuesta si es necesario
        if 'rp' in locals() and rp and hasattr(rp, 'aclose') and callable(rp.aclose):
             await rp.aclose()


# --- Rutas Proxy ---
# Asegúrate que las rutas que requieren autenticación tengan `Depends(require_user)`

@router.api_route(
    "/api/v1/ingest/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    dependencies=[Depends(require_user)], # <-- REQUIERE AUTH
    tags=["Proxy - Ingest"],
    summary="Proxy to Ingest Service (Auth Required)",
)
async def proxy_ingest_service(
    request: Request,
    path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    # Inyecta el payload validado por require_user
    user_payload: Annotated[Dict[str, Any], Depends(require_user)]
):
    base_url = settings.INGEST_SERVICE_URL.rstrip('/')
    target_url = f"{base_url}/api/v1/ingest/{path}" # Reconstruir URL destino
    if request.url.query: target_url += f"?{request.url.query}"
    return await _proxy_request(request, target_url, client, user_payload)

@router.api_route(
    "/api/v1/query/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    dependencies=[Depends(require_user)], # <-- REQUIERE AUTH
    tags=["Proxy - Query"],
    summary="Proxy to Query Service (Auth Required)",
)
async def proxy_query_service(
    request: Request,
    path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: Annotated[Dict[str, Any], Depends(require_user)]
):
    base_url = settings.QUERY_SERVICE_URL.rstrip('/')
    target_url = f"{base_url}/api/v1/query/{path}" # Reconstruir URL destino
    if request.url.query: target_url += f"?{request.url.query}"
    return await _proxy_request(request, target_url, client, user_payload)


# --- Proxy para Auth Service (OPCIONAL - SIN require_user) ---
# Si tienes un microservicio de Auth o quieres proxyficar llamadas a Supabase Auth
if settings.AUTH_SERVICE_URL:
    log.info(f"Auth service proxy enabled for: {settings.AUTH_SERVICE_URL}")
    @router.api_route(
        "/api/v1/auth/{path:path}", # Usar prefijo /api/v1 consistentemente?
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        tags=["Proxy - Auth"],
        summary="Proxy to Authentication Service (No Gateway Auth)",
    )
    async def proxy_auth_service(
        request: Request,
        path: str,
        client: Annotated[httpx.AsyncClient, Depends(get_client)],
        # NO hay dependencia require_user aquí
    ):
        """Proxy genérico para el servicio de autenticación."""
        base_url = settings.AUTH_SERVICE_URL.rstrip('/')
        # Construir URL destino, asumiendo que el Auth service espera la ruta completa
        # Ejemplo: /api/v1/auth/login -> http://auth-service/api/v1/auth/login
        target_url = f"{base_url}/api/v1/auth/{path}"
        if request.url.query: target_url += f"?{request.url.query}"

        # Pasar user_payload=None ya que estas rutas no requieren token *validado por el gateway*
        # (el token podría pasarse para operaciones como 'refresh' o 'get user info')
        return await _proxy_request(request, target_url, client, user_payload=None)
else:
     log.warning("Auth service proxy is not configured (GATEWAY_AUTH_SERVICE_URL not set).")
     # Podrías añadir una ruta aquí para devolver un 501 Not Implemented si se llama a /api/v1/auth/*
     @router.api_route("/api/v1/auth/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], include_in_schema=False)
     async def auth_not_configured(path: str):
         raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Authentication endpoint proxy not configured in the gateway.")
```

## File: `pyproject.toml`
```toml
# api-gateway/pyproject.toml
[tool.poetry]
name = "api-gateway"
version = "1.0.0" # Puedes ajustar la versión
description = "API Gateway for Nyro Microservices"
authors = ["Nyro <dev@nyro.com>"] # Ajusta el autor
readme = "README.md" # Asumiendo que tendrás un README

[tool.poetry.dependencies]
python = "^3.10"
fastapi = "^0.110.0"
uvicorn = {extras = ["standard"], version = "^0.28.0"}
gunicorn = "^21.2.0" # O la versión que prefieras
pydantic = {extras = ["email"], version = "^2.6.4"}
pydantic-settings = "^2.2.1"
httpx = "^0.27.0" # Cliente HTTP async crucial para el proxy
python-jose = {extras = ["cryptography"], version = "^3.3.0"} # Para manejo de JWT
structlog = "^24.1.0" # Para logging estructurado
tenacity = "^8.2.3" # Para reintentos (útil con httpx)

[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"
pytest-httpx = "^0.29.0" # Para mockear llamadas HTTP en tests

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
