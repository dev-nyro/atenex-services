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
├── routers
│   ├── __init__.py
│   ├── gateway_router.py
│   └── user_router.py
└── utils
    └── supabase_admin.py
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

# --- MODIFICACIÓN: Importar verify_token CON el nuevo parámetro ---
from .jwt_handler import verify_token

log = structlog.get_logger(__name__)
bearer_scheme = HTTPBearer(bearerFormat="JWT", auto_error=False)

async def _get_user_payload_internal(
    request: Request,
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)],
    require_company_id: bool # Parámetro interno para controlar la verificación
) -> Optional[Dict[str, Any]]:
    """
    Función interna para obtener y validar el payload, controlando si se requiere company_id.
    """
    request.state.user = None # Resetear estado por defecto

    if authorization is None:
        log.debug("No Authorization Bearer header found.")
        return None

    token = authorization.credentials
    try:
        # --- MODIFICACIÓN: Pasar require_company_id a verify_token ---
        payload = verify_token(token, require_company_id=require_company_id)
        request.state.user = payload
        log_msg = "Token verified" + (" (company_id required)" if require_company_id else " (company_id not required)")
        log.debug(log_msg, subject=payload.get('sub'), company_id=payload.get('company_id'))
        return payload
    except HTTPException as e:
        log.info(f"Token verification failed in dependency: {e.detail}", status_code=e.status_code)
        # Re-lanzar la excepción (401 o 403) para que FastAPI la maneje
        raise e
    except Exception as e:
        # Capturar errores inesperados de verify_token (aunque no debería ocurrir)
        log.exception("Unexpected error during internal payload retrieval", error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal error during authentication check."
        )

# --- Dependencia Estándar (sin cambios en su definición externa) ---
# Intenta obtener el payload, requiere company_id por defecto.
# Devuelve None si no hay token, lanza 401/403 si el token es inválido o falta company_id.
async def get_current_user_payload(
    request: Request,
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]
) -> Optional[Dict[str, Any]]:
    """
    Dependencia FastAPI para intentar validar el token JWT (requiriendo company_id).
    """
    # Llama a la función interna requiriendo company_id
    try:
        return await _get_user_payload_internal(request, authorization, require_company_id=True)
    except HTTPException as e:
        # Si _get_user_payload_internal lanza 401/403, lo relanzamos
        raise e
    except Exception as e:
         # Manejar cualquier otro error inesperado aquí también
         log.exception("Unexpected error in get_current_user_payload wrapper", error=e)
         raise HTTPException(status_code=500, detail="Internal Server Error")


# --- Dependencia que Requiere Usuario (sin cambios) ---
# Falla si get_current_user_payload devuelve None o lanza excepción.
async def require_user(
    user_payload: Annotated[Optional[Dict[str, Any]], Depends(get_current_user_payload)]
) -> Dict[str, Any]:
    """
    Dependencia FastAPI que *asegura* que una ruta requiere un usuario autenticado
    con un token válido Y con company_id asociado.
    """
    if user_payload is None:
        # Esto ocurre si get_current_user_payload devolvió None (sin token)
        log.info("Access denied: Authentication required but no valid token was found or provided.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Si get_current_user_payload lanzó 401/403, la ejecución ya se detuvo.
    log.debug("User requirement met (with company_id check).", subject=user_payload.get('sub'))
    return user_payload

# --- NUEVA Dependencia: Requiere Autenticación pero NO company_id ---
async def require_authenticated_user_no_company_check(
    request: Request,
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]
) -> Dict[str, Any]:
    """
    Dependencia FastAPI que asegura que el usuario está autenticado (token válido)
    pero NO requiere que el company_id esté presente en el token todavía.
    Útil para endpoints como el de asociación de compañía.
    """
    try:
        # Llama a la función interna SIN requerir company_id
        payload = await _get_user_payload_internal(request, authorization, require_company_id=False)

        if payload is None:
            # Si no hay token, lanzar 401
            log.info("Access denied: Authentication required for initial setup but no token provided.")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        # Si había token pero era inválido (firma, exp, aud), _get_user_payload_internal ya lanzó 401.
        log.debug("User requirement met (without company_id check).", subject=payload.get('sub'))
        return payload
    except HTTPException as e:
        # Re-lanzar 401 si el token era inválido
        raise e
    except Exception as e:
        log.exception("Unexpected error in require_authenticated_user_no_company_check", error=e)
        raise HTTPException(status_code=500, detail="Internal Server Error")

# Alias para claridad
InitialAuth = Annotated[Dict[str, Any], Depends(require_authenticated_user_no_company_check)]
StrictAuth = Annotated[Dict[str, Any], Depends(require_user)]
```

## File: `app\auth\jwt_handler.py`
```py
# api-gateway/app/auth/jwt_handler.py
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from jose import JWTError, jwt
from fastapi import HTTPException, status
import structlog

from app.core.config import settings

log = structlog.get_logger(__name__)

SECRET_KEY = settings.JWT_SECRET
ALGORITHM = settings.JWT_ALGORITHM
REQUIRED_CLAIMS = ['sub', 'aud', 'exp']
EXPECTED_AUDIENCE = 'authenticated'

# --- MODIFICACIÓN: Añadir parámetro require_company_id ---
def verify_token(token: str, require_company_id: bool = True) -> Dict[str, Any]:
    """
    Verifica el token JWT usando el secreto y algoritmo de Supabase.
    Valida la firma, expiración, audiencia y claims requeridos.
    Opcionalmente requiere la presencia de 'company_id' en app_metadata.

    Args:
        token: El string del token JWT.
        require_company_id: Si es True (default), falla si 'company_id'
                            no se encuentra en app_metadata. Si es False,
                            permite tokens válidos sin company_id.

    Returns:
        El payload decodificado si el token es válido.

    Raises:
        HTTPException(401): Si el token es inválido, expirado, malformado,
                           le faltan claims, o la audiencia no es correcta.
        HTTPException(403): Si require_company_id es True y falta company_id.
        HTTPException(500): Si ocurre un error inesperado.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer error=\"invalid_token\""},
    )
    forbidden_exception = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="User authenticated, but company association is missing in token.",
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
         raise internal_error_exception

    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            audience=EXPECTED_AUDIENCE,
            options={
                "verify_signature": True,
                "verify_aud": True,
                "verify_exp": True,
            }
        )

        missing_claims = [claim for claim in REQUIRED_CLAIMS if claim not in payload]
        if missing_claims:
            log.warning("Token verification failed: Missing required claims.",
                        missing_claims=missing_claims,
                        token_subject=payload.get('sub'))
            credentials_exception.detail = f"Token missing required claims: {', '.join(missing_claims)}"
            raise credentials_exception

        company_id: Optional[str] = None
        app_metadata = payload.get('app_metadata')
        if isinstance(app_metadata, dict):
            company_id_raw = app_metadata.get('company_id')
            if company_id_raw is not None:
                 company_id = str(company_id_raw) # Asegurar string
                 payload['company_id'] = company_id # Añadir al payload para conveniencia

        # --- MODIFICACIÓN: Aplicar requerimiento de company_id ---
        if require_company_id and company_id is None:
             log.error("Token lacks required 'company_id' in app_metadata.",
                       token_subject=payload.get('sub'),
                       payload_keys=list(payload.keys()))
             raise forbidden_exception # Lanzar 403 si se requiere y falta
        elif company_id:
             log.debug("Token verified, company_id present.",
                       subject=payload.get('sub'), company_id=company_id)
        else: # company_id is None but require_company_id is False
             log.debug("Token verified, company_id not required or not present.",
                       subject=payload.get('sub'))
        # --- FIN MODIFICACIÓN ---

        if 'sub' not in payload:
             log.error("Critical: 'sub' claim missing after initial check.", payload_keys=list(payload.keys()))
             raise credentials_exception

        return payload

    except JWTError as e:
        log.warning(f"JWT Verification Error: {e}", token_provided=True, algorithm=ALGORITHM, audience=EXPECTED_AUDIENCE)
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
        else:
            credentials_exception.detail = f"Token validation failed: {e}"
        raise credentials_exception from e
    except HTTPException as e:
        raise e # Re-lanzar 403 o cualquier otra HTTPException
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

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Lee variables con el prefijo GATEWAY_ desde .env o el entorno
        env_file='.env',
        env_prefix='GATEWAY_',
        case_sensitive=False,
        env_file_encoding='utf-8',
        extra='ignore'
    )

    PROJECT_NAME: str = "Nyro API Gateway"
    API_V1_STR: str = "/api/v1"

    INGEST_SERVICE_URL: str # Se leerá como GATEWAY_INGEST_SERVICE_URL
    QUERY_SERVICE_URL: str  # Se leerá como GATEWAY_QUERY_SERVICE_URL
    AUTH_SERVICE_URL: Optional[str] = None # Se leerá como GATEWAY_AUTH_SERVICE_URL

    # JWT settings
    JWT_SECRET: str # Se leerá como GATEWAY_JWT_SECRET (desde Secret)
    JWT_ALGORITHM: str = "HS256" # Se leerá como GATEWAY_JWT_ALGORITHM

    # Supabase Admin settings
    # --- MODIFICACIÓN: Leer variable específica del backend ---
    # Leerá la variable de entorno GATEWAY_SUPABASE_URL
    SUPABASE_URL: str
    # Leerá la variable de entorno GATEWAY_SUPABASE_SERVICE_ROLE_KEY (desde Secret)
    SUPABASE_SERVICE_ROLE_KEY: str
    # -------------------------------------------------------

    # Leerá la variable de entorno GATEWAY_DEFAULT_COMPANY_ID
    DEFAULT_COMPANY_ID: Optional[str] = None

    LOG_LEVEL: str = "INFO"
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS: int = 20
    HTTP_CLIENT_MAX_CONNECTIONS: int = 100

@lru_cache()
def get_settings() -> Settings:
    log = logging.getLogger(__name__)
    if not log.handlers:
        log.setLevel(logging.INFO)
        log.addHandler(logging.StreamHandler(sys.stdout))

    log.info("Loading Gateway settings...")
    try:
        # Pydantic-settings leerá las variables de entorno correspondientes
        # (GATEWAY_SUPABASE_URL, GATEWAY_JWT_SECRET, etc.)
        settings_instance = Settings()

        log.info("Gateway Settings Loaded:")
        log.info(f"  PROJECT_NAME: {settings_instance.PROJECT_NAME}")
        log.info(f"  INGEST_SERVICE_URL: {settings_instance.INGEST_SERVICE_URL}")
        log.info(f"  QUERY_SERVICE_URL: {settings_instance.QUERY_SERVICE_URL}")
        log.info(f"  AUTH_SERVICE_URL: {settings_instance.AUTH_SERVICE_URL or 'Not Set'}")

        # Verificación JWT Secret (Placeholder)
        if settings_instance.JWT_SECRET == "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME_IN_ENV_OR_SECRET":
            log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            log.critical("! FATAL: GATEWAY_JWT_SECRET is using the default insecure value!")
            log.critical("! Set GATEWAY_JWT_SECRET via env var or K8s Secret.")
            log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        else:
            log.info(f"  JWT_SECRET: *** SET (Loaded from Secret/Env) ***")
        log.info(f"  JWT_ALGORITHM: {settings_instance.JWT_ALGORITHM}")

        # Verificaciones Supabase URL y Service Key
        # --- MODIFICACIÓN: Usar el nombre correcto de la variable ---
        if not settings_instance.SUPABASE_URL:
             # Esto no debería pasar si es obligatorio y no tiene default, pydantic fallaría antes.
             # Pero mantenemos la verificación por si acaso.
             log.critical("! FATAL: GATEWAY_SUPABASE_URL is not configured.")
             sys.exit("FATAL: GATEWAY_SUPABASE_URL not configured.")
        else:
             log.info(f"  SUPABASE_URL: {settings_instance.SUPABASE_URL}")
        # -----------------------------------------------------------

        # Verificación Service Key (Placeholder)
        if settings_instance.SUPABASE_SERVICE_ROLE_KEY == "YOUR_SUPABASE_SERVICE_ROLE_KEY_HERE":
            log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            log.critical("! FATAL: GATEWAY_SUPABASE_SERVICE_ROLE_KEY is using the default placeholder!")
            log.critical("! Set GATEWAY_SUPABASE_SERVICE_ROLE_KEY via env var or K8s Secret.")
            log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        else:
            log.info("  SUPABASE_SERVICE_ROLE_KEY: *** SET (Loaded from Secret/Env) ***")

        # Verificación Default Company ID
        if not settings_instance.DEFAULT_COMPANY_ID:
            log.warning("! WARNING: GATEWAY_DEFAULT_COMPANY_ID is not set. Company association might fail.")
        else:
            log.info(f"  DEFAULT_COMPANY_ID: {settings_instance.DEFAULT_COMPANY_ID}")

        log.info(f"  LOG_LEVEL: {settings_instance.LOG_LEVEL}")
        log.info(f"  HTTP_CLIENT_TIMEOUT: {settings_instance.HTTP_CLIENT_TIMEOUT}")
        return settings_instance
    except Exception as e:
        # Captura errores de validación de Pydantic o cualquier otro error al cargar
        log.exception(f"FATAL: Error loading/validating Gateway settings: {e}")
        sys.exit(f"FATAL: Error loading/validating Gateway settings: {e}")

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
        timeout = httpx.Timeout(settings.HTTP_CLIENT_TIMEOUT, connect=10.0)
        proxy_http_client = httpx.AsyncClient(
             limits=limits,
             timeout=timeout,
             follow_redirects=False,
             http2=True
        )
        gateway_router.http_client = proxy_http_client
        log.info("HTTP Client initialized successfully.", limits=limits, timeout=timeout)
    except Exception as e:
        log.exception("Failed to initialize HTTP client during startup!", error=e)
        proxy_http_client = None
        gateway_router.http_client = None

    log.info("Initializing Supabase Admin Client...")
    try:
        supabase_admin_client = get_supabase_admin_client()
        log.info("Supabase Admin Client initialized successfully.")
    except Exception as e:
        log.exception("Failed to initialize Supabase Admin Client during startup!", error=e)
        supabase_admin_client = None

    yield

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
        log.info("Supabase Admin Client shutdown.")

# --- Creación de la aplicación FastAPI (Sin cambios) ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Punto de entrada único y seguro para los microservicios de Nyro.",
    version="1.0.0",
    lifespan=lifespan,
)

# --- Middlewares ---

# --- CORSMiddleware PRIMERO (Sin cambios respecto a la versión anterior) ---
allowed_origins = []
vercel_url = os.getenv("VERCEL_FRONTEND_URL")
if vercel_url:
    log.info(f"Adding Vercel frontend URL to allowed origins: {vercel_url}")
    allowed_origins.append(vercel_url)
else:
    vercel_fallback_url = "https://atenex-frontend.vercel.app"
    log.warning(f"VERCEL_FRONTEND_URL env var not set. Using fallback: {vercel_fallback_url}")
    allowed_origins.append(vercel_fallback_url)
localhost_url = "http://localhost:3000"
log.info(f"Adding localhost frontend URL to allowed origins: {localhost_url}")
allowed_origins.append(localhost_url)
ngrok_url_from_log = "https://5158-2001-1388-53a1-a7c9-fd46-ef87-59cf-a7f7.ngrok-free.app"
log.info(f"Adding specific Ngrok URL from logs to allowed origins: {ngrok_url_from_log}")
allowed_origins.append(ngrok_url_from_log)
ngrok_url_env = os.getenv("NGROK_URL")
if ngrok_url_env and ngrok_url_env not in allowed_origins:
    if ngrok_url_env.startswith("https://") or ngrok_url_env.startswith("http://"):
        log.info(f"Adding Ngrok URL from NGROK_URL env var to allowed origins: {ngrok_url_env}")
        allowed_origins.append(ngrok_url_env)
    else:
        log.warning(f"NGROK_URL environment variable has an unexpected format: {ngrok_url_env}")
allowed_origins = list(set(filter(None, allowed_origins)))
if not allowed_origins:
    log.critical("CRITICAL: No allowed origins configured for CORS.")
else:
    log.info("Final CORS Allowed Origins:", origins=allowed_origins)
allowed_headers = ["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With", "ngrok-skip-browser-warning"]
log.info("CORS Allowed Headers:", headers=allowed_headers)
allowed_methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
log.info("CORS Allowed Methods:", methods=allowed_methods)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=allowed_methods,
    allow_headers=allowed_headers,
    expose_headers=["X-Request-ID", "X-Process-Time"],
    max_age=600,
)
# --- Fin CORSMiddleware ---


# --- Otros Middlewares (Request ID, Timing) ---
# Añadido DESPUÉS de CORS
@app.middleware("http")
async def add_process_time_header_and_request_id(request: Request, call_next):
    start_time = time.time()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = request_id

    with structlog.contextvars.bind_contextvars(request_id=request_id):
        # --- !!! CORRECCIÓN DE INDENTACIÓN AQUÍ !!! ---
        # El log.info y el bloque try/except deben estar indentados UN NIVEL dentro del 'with'
        log.info("Request received", method=request.method, path=request.url.path, client_ip=request.client.host if request.client else "N/A")

        try:
            response = await call_next(request)
            process_time = time.time() - start_time
            response.headers["X-Process-Time"] = str(process_time)
            response.headers["X-Request-ID"] = request_id
            log.info("Request processed successfully", status_code=response.status_code, duration=round(process_time, 4))
        except Exception as e:
            process_time = time.time() - start_time
            log.exception("Unhandled exception during request processing", duration=round(process_time, 4), error=str(e))
            raise e
        return response
        # --- !!! FIN CORRECCIÓN DE INDENTACIÓN !!! ---
# --- Fin otros Middlewares ---


# --- Routers (Sin cambios) ---
app.include_router(gateway_router.router)
app.include_router(user_router.router)

# --- Endpoints Básicos y Manejadores de Excepciones (Sin cambios) ---
@app.get("/", tags=["Gateway Status"], summary="Root endpoint")
async def root():
    return {"message": f"{settings.PROJECT_NAME} is running!"}

@app.get("/health", tags=["Gateway Status"], summary="Kubernetes Health Check", status_code=status.HTTP_200_OK)
async def health_check():
     admin_client: Optional[SupabaseClient] = supabase_admin_client
     if not admin_client:
         log.error("Health check failed: Supabase Admin Client not available.")
         raise HTTPException(status_code=503, detail="Gateway service dependency unavailable (Admin Client).")
     http_client_check: Optional[httpx.AsyncClient] = proxy_http_client
     if not http_client_check or http_client_check.is_closed:
         log.error("Health check failed: HTTP Client not available or closed.")
         raise HTTPException(status_code=503, detail="Gateway service dependency unavailable (HTTP Client).")
     log.debug("Health check passed.")
     return {"status": "healthy", "service": settings.PROJECT_NAME}

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    req_id = getattr(request.state, 'request_id', 'N/A')
    bound_log = log.bind(request_id=req_id)
    bound_log.warning("HTTP Exception occurred", status_code=exc.status_code, detail=exc.detail, path=request.url.path)
    headers = exc.headers if exc.status_code == 401 else None
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=headers
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    req_id = getattr(request.state, 'request_id', 'N/A')
    bound_log = log.bind(request_id=req_id)
    bound_log.exception("Unhandled internal server error occurred in gateway", path=request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred."}
    )

log.info(f"'{settings.PROJECT_NAME}' application configured and ready to start.", allowed_origins=allowed_origins, allowed_methods=allowed_methods, allowed_headers=allowed_headers)
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

## File: `app\routers\user_router.py`
```py
# api-gateway/app/routers/user_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Body, Request # Añadir Request
from typing import Annotated, Dict, Any, Optional
import structlog
from pydantic import BaseModel, Field

# Comentar temporalmente las dependencias problemáticas para probar OPTIONS
# from app.auth.auth_middleware import InitialAuth
# from app.utils.supabase_admin import get_supabase_admin_client
# from supabase import Client as SupabaseClient
# from gotrue.errors import AuthApiError

from app.core.config import settings # Necesitamos settings para el default company id

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["Users"])

@router.post(
    "/me/ensure-company",
    status_code=status.HTTP_200_OK,
    summary="Ensure User Company Association (Dependencies Temporarily Disabled for OPTIONS Test)", # Modificar summary
    description="Checks if the authenticated user has a company ID associated... (Dependencies Temporarily Disabled)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Default Company ID not configured"},
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication token missing or invalid"},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Failed to update user metadata / Dependencies disabled"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Dependencies temporarily disabled for testing"}, # Añadir 503
    }
)
async def ensure_company_association(
    # --- DEPENDENCIAS COMENTADAS TEMPORALMENTE ---
    request: Request # Inyectar Request para acceder a headers si fuera necesario (aunque no lo usaremos ahora)
    # user_payload: InitialAuth,
    # supabase_admin: Annotated[SupabaseClient, Depends(get_supabase_admin_client)],
    # --- FIN DEPENDENCIAS COMENTADAS ---
):
    # --- LÓGICA TEMPORAL DE PRUEBA ---
    # Devolver un error 503 para indicar que la lógica real está desactivada,
    # pero si llegamos aquí, significa que el OPTIONS (y el POST inicial) pasaron.
    log.warning("ensure_company_association endpoint called, but dependencies are disabled for testing OPTIONS request.")
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Endpoint logic temporarily disabled for CORS testing. OPTIONS request seems to be passing."
    )
    # --- FIN LÓGICA TEMPORAL ---

    """
    # --- LÓGICA ORIGINAL (COMENTADA) ---
    user_id = user_payload.get("sub")
    current_company_id = user_payload.get("company_id")

    log_ctx = structlog.contextvars.get_contextvars()
    log_ctx["user_id"] = user_id
    bound_log = log.bind(**log_ctx)

    bound_log.info("Ensure company association endpoint called.")

    if current_company_id:
        bound_log.info("User already has company ID associated.", company_id=current_company_id)
        return {"message": "Company association already exists."}

    company_id_to_assign = settings.DEFAULT_COMPANY_ID
    if not company_id_to_assign:
        bound_log.error("Cannot associate company: GATEWAY_DEFAULT_COMPANY_ID is not configured.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error: Default company ID not set."
        )

    bound_log.info(f"Attempting to associate user with Company ID: {company_id_to_assign}")

    try:
        bound_log.debug("Fetching existing user data from Supabase Admin...")
        get_user_response = await supabase_admin.auth.admin.get_user_by_id(user_id)
        existing_app_metadata = get_user_response.user.app_metadata or {}
        new_app_metadata = {**existing_app_metadata, "company_id": company_id_to_assign}
        bound_log.debug("Updating user with new app_metadata", new_metadata=new_app_metadata)

        update_response = await supabase_admin.auth.admin.update_user_by_id(
            user_id,
            attributes={'app_metadata': new_app_metadata}
        )
        bound_log.info("Successfully updated user app_metadata with company ID.", company_id=company_id_to_assign)
        return {"message": "Company association successful.", "company_id": company_id_to_assign}

    except AuthApiError as e:
        bound_log.error(f"Supabase Admin API error during user update: {e}", status_code=e.status)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update user data in authentication system: {e.message}"
        )
    except Exception as e:
        bound_log.exception("Unexpected error during company association.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while associating company."
        )
    # --- FIN LÓGICA ORIGINAL ---
    """
```

## File: `app\utils\supabase_admin.py`
```py
# api-gateway/app/utils/supabase_admin.py
from supabase.client import Client, create_client
# from supabase.lib.client_options import ClientOptions # No necesitamos options por ahora
from functools import lru_cache
import structlog

from app.core.config import settings

log = structlog.get_logger(__name__)

@lru_cache()
def get_supabase_admin_client() -> Client:
    """
    Crea y devuelve un cliente Supabase inicializado con la Service Role Key.
    Utiliza caché para devolver la misma instancia.

    Raises:
        ValueError: Si la URL de Supabase o la Service Role Key no están configuradas.

    Returns:
        Instancia del cliente Supabase Admin.
    """
    supabase_url = settings.SUPABASE_URL
    # --- MODIFICACIÓN: Acceder a la clave como string normal ---
    service_key = settings.SUPABASE_SERVICE_ROLE_KEY
    # ----------------------------------------------------------

    if not supabase_url:
        log.critical("Supabase URL is not configured for Admin Client.")
        raise ValueError("Supabase URL not configured in settings.")
    # --- MODIFICACIÓN: Validar el placeholder como string ---
    if not service_key or service_key == "YOUR_SUPABASE_SERVICE_ROLE_KEY_HERE":
        log.critical("Supabase Service Role Key is not configured or using default for Admin Client.")
        raise ValueError("Supabase Service Role Key not configured securely in settings.")
    # -------------------------------------------------------

    log.info("Initializing Supabase Admin Client...")
    try:
        # Pasar la clave como string directamente
        supabase_admin: Client = create_client(supabase_url, service_key)
        log.info("Supabase Admin Client initialized successfully.")
        return supabase_admin
    except Exception as e:
        log.exception("Failed to initialize Supabase Admin Client", error=e)
        raise ValueError(f"Failed to initialize Supabase Admin Client: {e}")

# Instancia global (opcional, pero común con lru_cache)
# supabase_admin_client = get_supabase_admin_client() # Podría llamarse aquí o bajo demanda
```

## File: `pyproject.toml`
```toml
# api-gateway/pyproject.toml
[tool.poetry]
name = "api-gateway"
version = "1.0.0"
description = "API Gateway for Nyro Microservices"
authors = ["Nyro <dev@nyro.com>"]
readme = "README.md"

[tool.poetry.dependencies]
python = "^3.10"
fastapi = "^0.110.0"
uvicorn = {extras = ["standard"], version = "^0.28.0"}
gunicorn = "^21.2.0"
pydantic = {extras = ["email"], version = "^2.6.4"}
pydantic-settings = "^2.2.1"
httpx = "^0.27.0"
python-jose = {extras = ["cryptography"], version = "^3.3.0"}
structlog = "^24.1.0"
tenacity = "^8.2.3"
supabase = "^2.5.0" # Cliente Python para Supabase (incluye gotrue-py)

[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"
pytest-httpx = "^0.29.0"

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
