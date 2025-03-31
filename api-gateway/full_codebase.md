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
from typing import Optional, Annotated
import structlog

from .jwt_handler import verify_token

log = structlog.get_logger(__name__)

# Define el esquema de seguridad para documentación OpenAPI y extracción de token
# auto_error=False -> Nosotros manejamos explícitamente el error si falta el token en require_user
bearer_scheme = HTTPBearer(bearerFormat="JWT", auto_error=False)

async def get_current_user_payload(
    request: Request,
    # Usar Annotated y Depends para inyectar el resultado de bearer_scheme
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]
) -> Optional[dict]:
    """
    Dependencia FastAPI para validar el token JWT si está presente.
    - Si el token es válido, devuelve el payload decodificado.
    - Si el token es inválido, lanza la HTTPException de verify_token (401 o 500).
    - Si no hay token (authorization is None), devuelve None.
    Almacena el payload (o None) en request.state.user para uso posterior.
    """
    if authorization is None:
        # No hay header Authorization o no es Bearer.
        log.debug("No valid Authorization Bearer header found.")
        request.state.user = None # Marcar que no hay usuario autenticado
        return None

    token = authorization.credentials
    try:
        payload = verify_token(token) # Lanza excepción si es inválido
        request.state.user = payload # Almacenar payload para otros middlewares/endpoints
        log.debug("Token verified. User payload set in request.state", subject=payload.get('sub'))
        return payload
    except HTTPException as e:
        # Propaga la excepción HTTP generada por verify_token (401 o 500)
        log.warning(f"Token verification failed in dependency: {e.detail}")
        request.state.user = None # Asegurar que no hay payload en estado
        raise e # FastAPI manejará esta excepción
    # No es necesario capturar Exception genérica aquí, verify_token ya lo hace

async def require_user(
    # Usar Annotated y Depends para obtener el resultado de get_current_user_payload
    user_payload: Annotated[Optional[dict], Depends(get_current_user_payload)]
) -> dict:
    """
    Dependencia FastAPI que *asegura* que una ruta requiere un usuario autenticado.
    Reutiliza get_current_user_payload y levanta 401 si no se pudo obtener un payload válido.
    """
    if user_payload is None:
        # Esto ocurre si get_current_user_payload devolvió None (sin token)
        # o si lanzó una excepción (que ya fue manejada por FastAPI antes de llegar aquí,
        # pero por seguridad, lo comprobamos).
        log.info("Access denied: Authentication required but no valid token found.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Si llegamos aquí, user_payload es un diccionario válido
    return user_payload

# No usaremos el Middleware global por ahora, aplicaremos 'require_user' por ruta.
# class JWTMiddleware(BaseHTTPMiddleware): ... (Código eliminado)
```

## File: `app\auth\jwt_handler.py`
```py
# api-gateway/app/auth/jwt_handler.py
from datetime import datetime
from typing import Optional, Dict, Any
from jose import JWTError, jwt
from fastapi import HTTPException, status
import structlog

# Importar settings refactorizadas
from app.core.config import settings

log = structlog.get_logger(__name__)

SECRET_KEY = settings.JWT_SECRET
ALGORITHM = settings.JWT_ALGORITHM
# ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES # No necesario aquí

# --- create_access_token function removed ---
# The Gateway should *verify* tokens, not create them.

def verify_token(token: str) -> Dict[str, Any]:
    """
    Verifica el token JWT y devuelve el payload si es válido.
    Lanza HTTPException en caso de error.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    internal_error_exception = HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="An error occurred during token verification",
    )

    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            options={"verify_aud": False} # Ajustar si se usa audiencia (aud claim)
        )

        # Validar claims mínimos necesarios para el routing/forwarding
        # 'sub' (subject, a menudo email o user id), 'company_id' son cruciales
        required_claims = ['sub', 'company_id'] # Añade otros si son necesarios ('user_id', 'role')
        if not all(key in payload for key in required_claims):
             missing = [key for key in required_claims if key not in payload]
             log.warning("Token missing required claims", missing_claims=missing, token_payload=payload)
             raise JWTError(f"Token missing required claims: {', '.join(missing)}")

        # Validar expiración (aunque jose-jwt suele hacerlo)
        exp = payload.get("exp")
        if exp is None:
            log.warning("Token has no expiration claim (exp)", token_payload=payload)
            raise JWTError("Token has no expiration")
        if datetime.utcnow() > datetime.utcfromtimestamp(exp):
            log.info("Token has expired", token_exp=exp, current_time=datetime.utcnow())
            raise JWTError("Token has expired")

        # Log de éxito (opcional, puede ser verboso)
        # log.debug("Token verified successfully", subject=payload.get('sub'), company_id=payload.get('company_id'))

        return payload

    except JWTError as e:
        log.warning(f"JWT Verification Error: {e}", token_provided=bool(token))
        # Añadir detalles específicos al error 401 si es posible
        credentials_exception.detail = f"Could not validate credentials: {e}"
        raise credentials_exception from e
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

# Usar nombres de servicio DNS de Kubernetes como defaults
# Asegúrate que los nombres ('ingest-api-service', 'query-service') y namespace ('nyro-develop') son correctos
K8S_INGEST_SVC_URL = "http://ingest-api-service.nyro-develop.svc.cluster.local:80"
K8S_QUERY_SVC_URL = "http://query-service.nyro-develop.svc.cluster.local:80"
# K8S_AUTH_SVC_URL = "http://auth-service.nyro-develop.svc.cluster.local:80" # Si tuvieras un servicio de Auth separado

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='GATEWAY_', # Prefijo para variables de entorno del Gateway
        case_sensitive=False,
        env_file_encoding='utf-8',
        extra='ignore'
    )

    PROJECT_NAME: str = "Nyro API Gateway"

    # URLs de los servicios downstream (usar defaults de K8s)
    INGEST_SERVICE_URL: str = os.getenv("GATEWAY_INGEST_SERVICE_URL", K8S_INGEST_SVC_URL)
    QUERY_SERVICE_URL: str = os.getenv("GATEWAY_QUERY_SERVICE_URL", K8S_QUERY_SVC_URL)
    # AUTH_SERVICE_URL: Optional[str] = os.getenv("GATEWAY_AUTH_SERVICE_URL") # Descomentar si hay Auth Service

    # JWT settings (Debe ser el mismo secreto que usan los microservicios o el servicio de Auth)
    JWT_SECRET: str = "YOUR_JWT_SECRET_KEY_NEEDS_TO_BE_SET_IN_ENV_OR_SECRET" # Obligatorio
    JWT_ALGORITHM: str = "HS256"
    # ACCESS_TOKEN_EXPIRE_MINUTES: int = 30 # No necesario si el Gateway no CREA tokens

    # Logging level
    LOG_LEVEL: str = "INFO"

    # HTTP Client settings para llamadas downstream
    HTTP_CLIENT_TIMEOUT: int = 30 # Timeout en segundos
    HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS: int = 100
    HTTP_CLIENT_MAX_CONNECTIONS: int = 200

# --- Instancia Global ---
@lru_cache()
def get_settings() -> Settings:
    log = logging.getLogger(__name__) # Usar logger estándar antes de configurar structlog
    log.setLevel(logging.INFO)
    log.addHandler(logging.StreamHandler(sys.stdout))
    log.info("Loading Gateway settings...")
    try:
        settings_instance = Settings()
        # Ocultar secreto en logs
        log.info(f"Gateway Settings Loaded:")
        log.info(f"  INGEST_SERVICE_URL: {settings_instance.INGEST_SERVICE_URL}")
        log.info(f"  QUERY_SERVICE_URL: {settings_instance.QUERY_SERVICE_URL}")
        # log.info(f"  AUTH_SERVICE_URL: {settings_instance.AUTH_SERVICE_URL}") # Si existe
        log.info(f"  JWT_SECRET: {'*** SET ***' if settings_instance.JWT_SECRET != 'YOUR_JWT_SECRET_KEY_NEEDS_TO_BE_SET_IN_ENV_OR_SECRET' else '!!! NOT SET - USING DEFAULT PLACEHOLDER !!!'}")
        log.info(f"  JWT_ALGORITHM: {settings_instance.JWT_ALGORITHM}")
        log.info(f"  LOG_LEVEL: {settings_instance.LOG_LEVEL}")

        if settings_instance.JWT_SECRET == "YOUR_JWT_SECRET_KEY_NEEDS_TO_BE_SET_IN_ENV_OR_SECRET":
            log.critical("FATAL: GATEWAY_JWT_SECRET is not set. Please configure it via environment variables or a .env file.")
            sys.exit("FATAL: GATEWAY_JWT_SECRET is not set.")

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
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        # Añadir info de proceso/thread si es útil
        # structlog.processors.ProcessInfoProcessor(),
        # structlog.processors.ThreadLocalContextProcessor(),
    ]

    # Add caller info only in debug mode for performance
    if settings.LOG_LEVEL.upper() == "DEBUG":
         shared_processors.append(structlog.processors.CallsiteParameterAdder(
             {
                 structlog.processors.CallsiteParameter.FILENAME,
                 structlog.processors.CallsiteParameter.LINENO,
                 structlog.processors.CallsiteParameter.FUNC_NAME,
             }
         ))

    # Configure structlog processors for eventual output
    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure the formatter for stdlib logging handler
    formatter = structlog.stdlib.ProcessorFormatter(
        # These run ONCE per log event before formatting
        foreign_pre_chain=shared_processors,
        # These run on the final structured dict before rendering
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(), # Render logs as JSON
        ],
    )

    # Configure root logger handler (usually StreamHandler to stdout)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger() # Obtener root logger

    # Evitar añadir handlers duplicados
    if not any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, structlog.stdlib.ProcessorFormatter) for h in root_logger.handlers):
        # Limpiar handlers existentes si es la primera vez que configuramos (opcional, cuidado)
        # root_logger.handlers.clear()
        root_logger.addHandler(handler)

    # Establecer nivel en el root logger
    root_logger.setLevel(settings.LOG_LEVEL.upper())

    # Silenciar librerías verbosas
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("gunicorn").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    log = structlog.get_logger("api_gateway") # Logger específico para el gateway
    log.info("Structlog logging configured for API Gateway", log_level=settings.LOG_LEVEL)
```

## File: `app\main.py`
```py
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

# V 0.0.1
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

async def _proxy_request(
    request: Request,
    target_url: str,
    client: httpx.AsyncClient,
    user_payload: Optional[dict] # Payload del usuario autenticado (viene de require_user)
):
    """Función interna para realizar el proxy de la petición de forma segura."""
    method = request.method
    downstream_url = httpx.URL(target_url)

    # 1. Preparar Headers para downstream
    headers_to_forward = {}
    # Añadir X-Request-ID o similar para tracing si no existe
    # request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    # headers_to_forward["x-request-id"] = request_id
    # log = log.bind(request_id=request_id) # Vincular ID a logs

    for name, value in request.headers.items():
        if name.lower() not in HOP_BY_HOP_HEADERS:
            headers_to_forward[name] = value

    # Añadir/Sobrescribir X-Company-ID basado en el token verificado
    if user_payload and 'company_id' in user_payload:
        company_id = str(user_payload['company_id'])
        headers_to_forward['X-Company-ID'] = company_id
        log = log.bind(company_id=company_id) # Vincular company_id a logs
    else:
        # Si una ruta protegida llegó aquí sin company_id en el token, es un error de configuración/token
        log.error("Protected route reached without company_id in user payload!", user_payload=user_payload)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing company identifier.")

    # Pasar información del usuario si es necesario (con prefijo para evitar colisiones)
    # if user_payload and 'user_id' in user_payload:
    #     headers_to_forward['X-User-ID'] = str(user_payload['user_id'])
    # if user_payload and 'role' in user_payload:
    #     headers_to_forward['X-User-Role'] = str(user_payload['role'])

    # 2. Preparar Query Params y Body
    query_params = request.query_params
    # Leer el body como stream para evitar cargarlo en memoria si es grande
    request_body_bytes = request.stream()

    # 3. Realizar la petición downstream
    log.info(f"Proxying request", method=method, path=request.url.path, target=str(downstream_url))
    # log.debug(f"Forwarding Headers", headers=headers_to_forward) # Puede ser verboso

    try:
        # Construir el request para httpx
        req = client.build_request(
            method=method,
            url=downstream_url,
            headers=headers_to_forward,
            params=query_params,
            content=request_body_bytes # Pasar el stream directamente
        )
        # Enviar el request y obtener la respuesta como stream
        rp = await client.send(req, stream=True) # stream=True es clave

        # 4. Preparar y devolver la respuesta al cliente original
        # Loguear la respuesta del downstream
        log.info(f"Received response from downstream", status_code=rp.status_code, target=str(downstream_url))

        # Filtrar hop-by-hop headers de la respuesta
        response_headers = {}
        for name, value in rp.headers.items():
            if name.lower() not in HOP_BY_HOP_HEADERS:
                response_headers[name] = value

        # Devolver como StreamingResponse para eficiencia
        return StreamingResponse(
            rp.aiter_raw(), # Stream de bytes de la respuesta
            status_code=rp.status_code,
            headers=response_headers,
            media_type=rp.headers.get("content-type"),
        )

    except httpx.TimeoutException as exc:
        log.error(f"Request to downstream service timed out", target=str(downstream_url), timeout=settings.HTTP_CLIENT_TIMEOUT, error=str(exc))
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=f"Upstream service at {downstream_url.host} timed out.")
    except httpx.ConnectError as exc:
        log.error(f"Could not connect to downstream service", target=str(downstream_url), error=str(exc))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Upstream service at {downstream_url.host} is unavailable.")
    except httpx.RequestError as exc: # Otros errores de request (SSL, etc.)
        log.error(f"Error during request to downstream service", target=str(downstream_url), error=str(exc), exc_info=True)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Error communicating with upstream service: {type(exc).__name__}")
    except Exception as exc:
        log.exception(f"Unexpected error during proxy request", target=str(downstream_url))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An internal gateway error occurred.")
    finally:
        # Asegurarse de cerrar la respuesta si no se usa StreamingResponse o si hay error antes
        if 'rp' in locals() and rp and not rp.is_closed:
             await rp.aclose()


# --- Rutas Proxy Específicas ---

# Usamos un path parameter genérico '{path:path}' para capturar todo después del prefijo
# Aplicamos la dependencia 'require_user' a todas las rutas proxificadas
@router.api_route(
    "/api/v1/ingest/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"], # Excluir OPTIONS/HEAD si no se manejan explícitamente
    dependencies=[Depends(require_user)], # Proteger rutas de ingesta
    tags=["Proxy"],
    summary="Proxy to Ingest Service",
    # include_in_schema=False # Ocultar de OpenAPI para no duplicar? Depende de la estrategia
)
async def proxy_ingest_service(
    request: Request,
    path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user: Annotated[dict, Depends(require_user)] # Inyectar payload validado
):
    """Proxy genérico para todas las rutas bajo /api/v1/ingest/"""
    base_url = settings.INGEST_SERVICE_URL.rstrip('/')
    # Construir la URL completa del servicio downstream
    target_url = f"{base_url}{request.url.path}" # Usar path completo original
    if request.url.query:
        target_url += f"?{request.url.query}"

    return await _proxy_request(request, target_url, client, user)

@router.api_route(
    "/api/v1/query/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    dependencies=[Depends(require_user)], # Proteger rutas de consulta
    tags=["Proxy"],
    summary="Proxy to Query Service",
    # include_in_schema=False
)
async def proxy_query_service(
    request: Request,
    path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user: Annotated[dict, Depends(require_user)]
):
    """Proxy genérico para todas las rutas bajo /api/v1/query/"""
    base_url = settings.QUERY_SERVICE_URL.rstrip('/')
    target_url = f"{base_url}{request.url.path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    return await _proxy_request(request, target_url, client, user)


# --- Proxy para Auth Service (Opcional) ---
# Si tienes un servicio de Auth separado para /login, /register, etc.
# Estas rutas NO estarían protegidas por 'require_user'
# if settings.AUTH_SERVICE_URL:
#     @router.api_route(
#         "/api/auth/{path:path}",
#         methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
#         tags=["Proxy"],
#         summary="Proxy to Authentication Service",
#         # include_in_schema=False
#     )
#     async def proxy_auth_service(
#         request: Request,
#         path: str,
#         client: Annotated[httpx.AsyncClient, Depends(get_client)],
#         # NO hay dependencia 'require_user' aquí
#     ):
#         """Proxy genérico para el servicio de autenticación."""
#         base_url = settings.AUTH_SERVICE_URL.rstrip('/')
#         target_url = f"{base_url}{request.url.path}"
#         if request.url.query:
#             target_url += f"?{request.url.query}"

#         # Pasar user_payload=None ya que estas rutas no requieren autenticación previa
#         return await _proxy_request(request, target_url, client, user_payload=None)
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
fastapi = "^0.110.0" # Actualizado
uvicorn = {extras = ["standard"], version = "^0.28.0"} # Actualizado
gunicorn = "^21.2.0" # Para despliegue
pydantic = {extras = ["email"], version = "^2.6.4"} # Actualizado a v2
pydantic-settings = "^2.2.1" # Para configuración
httpx = "^0.27.0" # Cliente HTTP async
python-jose = {extras = ["cryptography"], version = "^3.3.0"} # Para JWT
structlog = "^24.1.0" # Para logging estructurado
tenacity = "^8.2.3" # Para reintentos en httpx (opcional pero bueno)

[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"
pytest-httpx = "^0.29.0" # Para mockear llamadas HTTP en tests

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
