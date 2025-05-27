# Estructura de la Codebase

```
app/
├── __init__.py
├── auth
│   ├── __init__.py
│   ├── auth_middleware.py
│   └── auth_service.py
├── core
│   ├── __init__.py
│   ├── config.py
│   └── logging_config.py
├── db
│   └── postgres_client.py
├── main.py
├── models
│   ├── __init__.py
│   ├── admin_models.py
│   └── document_stats_models.py
├── routers
│   ├── __init__.py
│   ├── admin_router.py
│   ├── auth_router.py
│   ├── gateway_router.py
│   └── user_router.py
└── utils
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
# File: app/auth/auth_middleware.py
# api-gateway/app/auth/auth_middleware.py
from fastapi import Request, HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional, Annotated, Dict, Any, List # Añadido List
import structlog
import httpx

from app.auth.auth_service import verify_token

log = structlog.get_logger(__name__)

bearer_scheme = HTTPBearer(bearerFormat="JWT", auto_error=False)

async def _get_user_payload_internal(
    request: Request,
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)],
    require_company_id: bool
) -> Optional[Dict[str, Any]]:
    if hasattr(request.state, 'user'):
        del request.state.user
    if authorization is None:
        log.debug("No Authorization Bearer header found.")
        return None
    token = authorization.credentials
    try:
        # verify_token ahora puede incluir 'roles' actualizados desde la DB
        payload = await verify_token(token, require_company_id=require_company_id)
        request.state.user = payload
        log_msg = "Token verified successfully via internal getter"
        if require_company_id:
            log_msg += " (company_id required and present)"
        else:
            log_msg += " (company_id check passed or not required)"
        log.debug(log_msg, subject=payload.get('sub'), company_id=payload.get('company_id'), roles=payload.get('roles'))
        return payload
    except HTTPException as e:
        log_detail = getattr(e, 'detail', 'No detail provided')
        # Extraer user_id del payload si está disponible en la excepción
        user_id_from_exception = getattr(e, 'user_id', None)
        log.info(f"Token verification failed in dependency: {log_detail}",
                 status_code=e.status_code,
                 user_id=user_id_from_exception)
        raise e
    except Exception as e:
        log.exception("Unexpected error during internal payload retrieval", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal error during authentication check."
        )

async def get_current_user_payload(
    request: Request,
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]
) -> Optional[Dict[str, Any]]:
    try:
        return await _get_user_payload_internal(request, authorization, require_company_id=True)
    except HTTPException as e:
        raise e
    except Exception as e:
         log.exception("Unexpected error in get_current_user_payload wrapper", error=str(e))
         raise HTTPException(status_code=500, detail="Internal Server Error in auth wrapper")

async def get_initial_user_payload(
    request: Request,
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]
) -> Optional[Dict[str, Any]]:
    try:
        # No requiere company_id, pero sí valida firma, exp, usuario existe y activo
        return await _get_user_payload_internal(request, authorization, require_company_id=False)
    except HTTPException as e:
        raise e
    except Exception as e:
         log.exception("Unexpected error in get_initial_user_payload wrapper", error=str(e))
         raise HTTPException(status_code=500, detail="Internal Server Error in auth wrapper")

async def require_user(
    user_payload: Annotated[Optional[Dict[str, Any]], Depends(get_current_user_payload)]
) -> Dict[str, Any]:
    if user_payload is None:
        log.info("Access denied by require_user: Authentication required but no token was provided.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Las excepciones 401 (inválido) o 403 (sin company_id) ya fueron lanzadas por get_current_user_payload
    return user_payload

async def require_initial_user(
    user_payload: Annotated[Optional[Dict[str, Any]], Depends(get_initial_user_payload)]
) -> Dict[str, Any]:
    if user_payload is None:
        log.info("Access denied by require_initial_user: Authentication required but no token was provided.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # La excepción 401 (inválido, no existe, inactivo) ya fue lanzada por get_initial_user_payload
    return user_payload

# --- NUEVA DEPENDENCIA: Require Admin User ---
async def require_admin_user(
    # Depende de require_initial_user para la validación base del token
    # (firma, exp, usuario existe y activo). No requiere company_id en este punto.
    user_payload: Annotated[Dict[str, Any], Depends(require_initial_user)]
) -> Dict[str, Any]:
    """
    Dependencia FastAPI que asegura que el usuario autenticado tiene el rol 'admin'.
    Relanza 401 si el token base es inválido (gestionado por require_initial_user).
    Lanza 403 si el token es válido pero el usuario no tiene el rol 'admin'.
    """
    # user_payload ya está validado como perteneciente a un usuario existente y activo.
    # Ahora verificamos el rol específico de admin.
    # Usamos los roles del payload, que verify_token actualizó desde la DB.
    roles: Optional[List[str]] = user_payload.get("roles")

    if not roles or "admin" not in roles:
        log.warning("Admin access denied: User does not have 'admin' role.",
                   user_id=user_payload.get("sub"),
                   roles=roles)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions. Administrator role required.",
        )

    # Si llegamos aquí, el usuario es admin
    log.info("Admin access granted.", user_id=user_payload.get("sub"), roles=roles)
    return user_payload # Devuelve el payload con toda la info del admin


# --- Tipos anotados para usar en las rutas ---
StrictAuth = Annotated[Dict[str, Any], Depends(require_user)]
InitialAuth = Annotated[Dict[str, Any], Depends(require_initial_user)]
AdminAuth = Annotated[Dict[str, Any], Depends(require_admin_user)] # <-- NUEVO: Tipo para Admin
```

## File: `app\auth\auth_service.py`
```py
# File: app/auth/auth_service.py
# api-gateway/app/auth/auth_service.py
import uuid
import time
from typing import Dict, Any, Optional, List # Añadido List
from datetime import datetime, timezone, timedelta
from jose import jwt, JWTError, ExpiredSignatureError, JOSEError
import structlog
from passlib.context import CryptContext
from fastapi import HTTPException, status

from app.core.config import settings
from app.db import postgres_client # Importar cliente DB

log = structlog.get_logger(__name__)

# Contexto Passlib para Bcrypt (recomendado)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- Verificación y Hashing de Contraseñas ---

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica una contraseña en texto plano contra un hash almacenado."""
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except Exception as e:
        # Podría haber errores si el hash no es válido o tiene un formato inesperado
        log.error("Password verification failed", error=str(e), hash_used=hashed_password[:10]+"...") # No loguear hash completo
        return False

def get_password_hash(password: str) -> str:
    """Genera un hash bcrypt para una contraseña en texto plano."""
    return pwd_context.hash(password)

# --- Autenticación de Usuario ---

async def authenticate_user(email: str, password: str) -> Optional[Dict[str, Any]]:
    """
    Autentica un usuario por email y contraseña contra la base de datos.
    Retorna el diccionario del usuario si es válido y activo, None en caso contrario.
    Incluye 'roles' si existe.
    """
    log.debug("Attempting to authenticate user", email=email)
    # postgres_client.get_user_by_email ya fue modificado para devolver 'roles'
    user_data = await postgres_client.get_user_by_email(email)

    if not user_data:
        log.info("Authentication failed: User not found", email=email)
        return None

    if not user_data.get('is_active', False): # Verificar si el usuario está activo
        log.warning("Authentication failed: User is inactive", email=email, user_id=str(user_data.get('id')))
        return None

    hashed_password = user_data.get('hashed_password')
    if not hashed_password or not verify_password(password, hashed_password):
        log.warning("Authentication failed: Invalid password", email=email, user_id=str(user_data.get('id')))
        return None

    # Autenticación exitosa
    log.info("User authenticated successfully", email=email, user_id=str(user_data.get('id')), roles=user_data.get('roles'))
    # Eliminar hash de contraseña antes de devolver los datos
    user_data.pop('hashed_password', None)
    return user_data

# --- Creación de Tokens JWT ---

def create_access_token(
    user_id: uuid.UUID,
    email: str,
    company_id: Optional[uuid.UUID] = None,
    roles: Optional[List[str]] = None, # <-- AÑADIDO: Parámetro roles
    # full_name: Optional[str] = None,
    expires_delta: timedelta = timedelta(days=1) # Tiempo de expiración configurable
) -> str:
    """
    Crea un token JWT para el usuario autenticado.
    Incluye 'sub', 'exp', 'iat', 'email', opcionalmente 'company_id' y 'roles'.
    """
    expire = datetime.now(timezone.utc) + expires_delta
    issued_at = datetime.now(timezone.utc)

    to_encode: Dict[str, Any] = {
        "sub": str(user_id),         # Subject (ID de usuario)
        "exp": expire,               # Expiration Time
        "iat": issued_at,            # Issued At
        "email": email,              # Email del usuario
        # "aud": "authenticated",
        # "iss": "AtenexAuth",
    }

    # Añadir company_id si está presente
    if company_id:
        to_encode["company_id"] = str(company_id)

    # --- AÑADIDO: Añadir roles si están presentes ---
    if roles:
        # Asegurarse de que es una lista, aunque la llamada debería pasarla así
        to_encode["roles"] = roles if isinstance(roles, list) else [roles]

    # Codificar el token usando el secreto y algoritmo de la configuración
    try:
        encoded_jwt = jwt.encode(
            to_encode,
            settings.JWT_SECRET.get_secret_value(), # Obtener valor del SecretStr
            algorithm=settings.JWT_ALGORITHM
        )
        log.debug("Access token created", user_id=str(user_id), expires_at=expire.isoformat(), claims_keys=list(to_encode.keys()))
        return encoded_jwt
    except JOSEError as e:
        log.exception("Failed to encode JWT", error=str(e), user_id=str(user_id))
        raise HTTPException(status_code=500, detail="Could not create access token")


# --- Verificación de Tokens JWT ---

async def verify_token(token: str, require_company_id: bool = True) -> Dict[str, Any]:
    """
    Verifica la validez de un token JWT (firma, expiración, claims básicos).
    Opcionalmente requiere la presencia de un 'company_id' válido en el payload.
    Verifica que el usuario ('sub') exista y esté activo en la base de datos.
    Devuelve el payload completo, incluyendo 'roles' si existen en el token.

    Returns:
        El payload decodificado si el token es válido.

    Raises:
        HTTPException(401): Si el token es inválido (firma, exp, formato) o el usuario no existe/inactivo.
        HTTPException(403): Si 'require_company_id' es True y falta un 'company_id' válido.
        HTTPException(500): Error interno.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer error=\"invalid_token\""},
    )
    forbidden_exception = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="User authenticated, but required company association is missing or invalid in token.",
        headers={"WWW-Authenticate": "Bearer error=\"insufficient_scope\""}, # Indica falta de permisos/scope
    )
    user_inactive_exception = HTTPException( # Excepción específica para inactivos
        status_code=status.HTTP_401_UNAUTHORIZED, # Sigue siendo 401
        detail="User associated with token is inactive.",
        headers={"WWW-Authenticate": "Bearer error=\"invalid_token\", error_description=\"User inactive\""},
    )
    internal_error_exception = HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="An internal error occurred during token verification.",
    )

    if not settings.JWT_SECRET or settings.JWT_SECRET.get_secret_value() == "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME_IN_ENV_OR_SECRET":
         log.critical("FATAL: Attempting JWT verification with default or missing GATEWAY_JWT_SECRET!")
         raise internal_error_exception

    payload = {} # Inicializar payload
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True, # Verificar Issued At si está presente
                "verify_nbf": True, # Verificar Not Before si está presente
            }
        )
        log.debug("Token decoded successfully", payload_keys=list(payload.keys()))

        user_id_str = payload.get("sub")
        if not user_id_str:
            log.warning("Token verification failed: 'sub' claim missing.")
            credentials_exception.detail = "Token missing 'sub' (user ID) claim."
            raise credentials_exception

        try:
            user_id = uuid.UUID(user_id_str)
        except ValueError:
            log.warning("Token verification failed: 'sub' claim is not a valid UUID.", sub_value=user_id_str)
            credentials_exception.detail = "Invalid user ID format in token."
            raise credentials_exception

        # Verificar existencia y estado del usuario en DB
        user_db_data = await postgres_client.get_user_by_id(user_id)
        if not user_db_data:
            log.warning("Token verification failed: User specified in 'sub' not found in DB.", user_id=user_id_str)
            credentials_exception.detail = "User associated with token not found."
            raise credentials_exception
        if not user_db_data.get('is_active', False):
            log.warning("Token verification failed: User specified in 'sub' is inactive.", user_id=user_id_str)
            # Usar la excepción específica para inactivos
            raise user_inactive_exception

        # Actualizar payload con roles de la DB (más seguro que confiar sólo en el token)
        # Esto asegura que los roles están actualizados incluso si el token es viejo.
        db_roles = user_db_data.get('roles')
        if db_roles:
             payload['roles'] = db_roles # Sobreescribir/añadir roles desde la DB
             log.debug("Updated payload with roles from database", user_id=user_id_str, db_roles=db_roles)
        elif 'roles' in payload:
             # Si la DB no tiene roles pero el token sí, podría ser un estado inconsistente.
             # Por seguridad, eliminamos los roles del token si no están en DB.
             log.warning("Roles found in token but not in DB, removing from payload for consistency.", user_id=user_id_str, token_roles=payload.get('roles'))
             payload.pop('roles', None)


        # Validar 'company_id' si es requerido
        company_id_str: Optional[str] = payload.get("company_id")
        valid_company_id_present = False
        if company_id_str:
            try:
                uuid.UUID(company_id_str) # Validar formato UUID
                valid_company_id_present = True
            except ValueError:
                log.warning("Token verification: 'company_id' claim present but not a valid UUID.",
                           company_id_value=company_id_str, user_id=user_id_str)
                credentials_exception.detail = "Invalid company ID format in token."
                raise credentials_exception

        if require_company_id and not valid_company_id_present:
            log.info("Token verification failed: Required 'company_id' is missing.", user_id=user_id_str)
            # Pasar el user_id a la excepción para posible logging en la dependencia
            forbidden_exception.user_id = user_id_str # type: ignore
            raise forbidden_exception # Falla porque se requiere y no está

        log.info("Token verified successfully", user_id=user_id_str, company_id=company_id_str or "N/A", roles=payload.get('roles', 'N/A'))
        return payload # Devolver el payload completo y actualizado

    except ExpiredSignatureError:
        log.info("Token verification failed: Token has expired.")
        credentials_exception.detail = "Token has expired."
        credentials_exception.headers["WWW-Authenticate"] = 'Bearer error="invalid_token", error_description="The token has expired"'
        raise credentials_exception
    except JWTError as e:
        log.warning(f"JWT Verification Error: {e}", token_provided=True)
        credentials_exception.detail = f"Token validation failed: {e}"
        raise credentials_exception
    except HTTPException as e:
        # Re-lanzar excepciones HTTP ya manejadas (como 403 por company_id o 401 por inactivo)
        raise e
    except Exception as e:
        # Capturar cualquier otro error inesperado
        user_id_from_payload = payload.get('sub', 'unknown') if payload else 'unknown'
        log.exception(f"Unexpected error during token verification: {e}", user_id=user_id_from_payload)
        raise internal_error_exception
```

## File: `app\core\__init__.py`
```py

```

## File: `app\core\config.py`
```py
# File: app/core/config.py
# api-gateway/app/core/config.py
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, validator, ValidationError, HttpUrl, SecretStr
from functools import lru_cache
import sys
import logging
from typing import Optional, List
import uuid # Para validar UUID

# URLs por defecto si no se especifican en el entorno (usando el namespace 'nyro-develop')
K8S_INGEST_SVC_URL_DEFAULT = "http://ingest-api-service.nyro-develop.svc.cluster.local:80"
K8S_QUERY_SVC_URL_DEFAULT = "http://query-service.atenex-develop.svc.cluster.local:80"
K8S_AUTH_SVC_URL_DEFAULT = None # No hay auth service por defecto

# PostgreSQL Kubernetes Default (usando el namespace 'atenex-develop')
POSTGRES_K8S_HOST_DEFAULT = "postgresql.atenex-develop.svc.cluster.local" # <-- K8s Service Name
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex" # <-- Nombre de DB (asegúrate que coincida)
POSTGRES_K8S_USER_DEFAULT = "postgres" # <-- Usuario DB (asegúrate que coincida)

class Settings(BaseSettings):
    # Configuración de Pydantic-Settings
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='GATEWAY_',
        case_sensitive=False,
        env_file_encoding='utf-8',
        extra='ignore'
    )

    # Información del Proyecto
    PROJECT_NAME: str = "Atenex API Gateway" # <-- Nombre actualizado
    API_V1_STR: str = "/api/v1"

    # URLs de Servicios Backend
    INGEST_SERVICE_URL: HttpUrl = K8S_INGEST_SVC_URL_DEFAULT
    QUERY_SERVICE_URL: HttpUrl = K8S_QUERY_SVC_URL_DEFAULT
    AUTH_SERVICE_URL: Optional[HttpUrl] = K8S_AUTH_SVC_URL_DEFAULT

    # Configuración JWT (Obligatoria)
    JWT_SECRET: SecretStr # Obligatorio, usar SecretStr para seguridad
    JWT_ALGORITHM: str = "HS256"

    # Configuración PostgreSQL (Obligatoria)
    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr # Obligatorio desde Secrets
    POSTGRES_SERVER: str = POSTGRES_K8S_HOST_DEFAULT
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    # Configuración de Asociación de Compañía
    DEFAULT_COMPANY_ID: Optional[str] = None # UUID de compañía por defecto

    # Configuración General
    LOG_LEVEL: str = "INFO"
    HTTP_CLIENT_TIMEOUT: int = 60
    # Corregido: KEEPALIVE en lugar de KEEPALIAS
    HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS: int = 100
    HTTP_CLIENT_MAX_CONNECTIONS: int = 200

    # CORS (Opcional - URLs de ejemplo, ajusta según necesites)
    VERCEL_FRONTEND_URL: Optional[str] = "https://atenex-frontend.vercel.app"
    # NGROK_URL: Optional[str] = None

    # Validadores Pydantic
    @validator('JWT_SECRET')
    def check_jwt_secret(cls, v):
        # get_secret_value() se usa al *usar* el secreto, aquí solo verificamos que no esté vacío
        if not v or v.get_secret_value() == "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME_IN_ENV_OR_SECRET":
            raise ValueError("GATEWAY_JWT_SECRET is not set or uses an insecure default value.")
        return v

    @validator('DEFAULT_COMPANY_ID', always=True)
    def check_default_company_id_format(cls, v):
        if v is not None:
            try:
                uuid.UUID(str(v))
            except ValueError:
                raise ValueError(f"GATEWAY_DEFAULT_COMPANY_ID ('{v}') is not a valid UUID.")
        return v

    @validator('LOG_LEVEL')
    def check_log_level(cls, v):
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        normalized_v = v.upper()
        if normalized_v not in valid_levels:
            raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return normalized_v

# Usar lru_cache para asegurar que las settings se cargan una sola vez
@lru_cache()
def get_settings() -> Settings:
    temp_log = logging.getLogger("atenex_api_gateway.config.loader")
    if not temp_log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter('%(levelname)s: %(message)s')
        handler.setFormatter(formatter)
        temp_log.addHandler(handler)
        temp_log.setLevel(logging.INFO)

    temp_log.info("Loading Atenex Gateway settings...")
    try:
        settings_instance = Settings()

        # Loguear valores cargados (excepto secretos)
        temp_log.info("Atenex Gateway Settings Loaded Successfully:")
        temp_log.info(f"  PROJECT_NAME: {settings_instance.PROJECT_NAME}")
        temp_log.info(f"  INGEST_SERVICE_URL: {str(settings_instance.INGEST_SERVICE_URL)}")
        temp_log.info(f"  QUERY_SERVICE_URL: {str(settings_instance.QUERY_SERVICE_URL)}")
        temp_log.info(f"  AUTH_SERVICE_URL: {str(settings_instance.AUTH_SERVICE_URL) if settings_instance.AUTH_SERVICE_URL else 'Not Set'}")
        temp_log.info(f"  JWT_SECRET: *** SET (Validated) ***")
        temp_log.info(f"  JWT_ALGORITHM: {settings_instance.JWT_ALGORITHM}")
        temp_log.info(f"  POSTGRES_SERVER: {settings_instance.POSTGRES_SERVER}")
        temp_log.info(f"  POSTGRES_PORT: {settings_instance.POSTGRES_PORT}")
        temp_log.info(f"  POSTGRES_DB: {settings_instance.POSTGRES_DB}")
        temp_log.info(f"  POSTGRES_USER: {settings_instance.POSTGRES_USER}")
        temp_log.info(f"  POSTGRES_PASSWORD: *** SET ***")
        if settings_instance.DEFAULT_COMPANY_ID:
            temp_log.info(f"  DEFAULT_COMPANY_ID: {settings_instance.DEFAULT_COMPANY_ID} (Validated as UUID if set)")
        else:
            temp_log.warning("  DEFAULT_COMPANY_ID: Not Set (Ensure-company endpoint requires this)")
        temp_log.info(f"  LOG_LEVEL: {settings_instance.LOG_LEVEL}")
        temp_log.info(f"  HTTP_CLIENT_TIMEOUT: {settings_instance.HTTP_CLIENT_TIMEOUT}")
        temp_log.info(f"  HTTP_CLIENT_MAX_CONNECTIONS: {settings_instance.HTTP_CLIENT_MAX_CONNECTIONS}")
        temp_log.info(f"  HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS: {settings_instance.HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS}")
        temp_log.info(f"  VERCEL_FRONTEND_URL: {settings_instance.VERCEL_FRONTEND_URL or 'Not Set'}")
        # temp_log.info(f"  NGROK_URL: {settings_instance.NGROK_URL or 'Not Set'}")

        return settings_instance

    except ValidationError as e:
        temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        temp_log.critical("! FATAL: Error validating Atenex Gateway settings:")
        for error in e.errors():
            loc = " -> ".join(map(str, error['loc'])) if error.get('loc') else 'N/A'
            temp_log.critical(f"!  - {loc}: {error['msg']}")
        temp_log.critical("! Check your Kubernetes ConfigMap/Secrets or .env file.")
        temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        sys.exit("FATAL: Invalid Atenex Gateway configuration. Check logs.")
    except Exception as e:
        temp_log.exception(f"FATAL: Unexpected error loading Atenex Gateway settings: {e}")
        sys.exit(f"FATAL: Unexpected error loading Atenex Gateway settings: {e}")

# Crear instancia global de settings
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

## File: `app\db\postgres_client.py`
```py
# File: app/db/postgres_client.py
# api-gateway/app/db/postgres_client.py
import uuid
from typing import Any, Optional, Dict, List
import asyncpg
import structlog
import json
from datetime import datetime # Añadido para tipos

from app.core.config import settings

log = structlog.get_logger(__name__)

_pool: Optional[asyncpg.Pool] = None

async def get_db_pool() -> asyncpg.Pool:
    """
    Obtiene o crea el pool de conexiones a la base de datos PostgreSQL.
    """
    global _pool
    if _pool is None or _pool._closed: # Recrea el pool si está cerrado
        try:
            log.info("Creating PostgreSQL connection pool...",
                     host=settings.POSTGRES_SERVER,
                     port=settings.POSTGRES_PORT,
                     user=settings.POSTGRES_USER,
                     database=settings.POSTGRES_DB)

            # Asegúrate de que el codec jsonb esté registrado si usas JSONB
            def _json_encoder(value):
                return json.dumps(value)
            def _json_decoder(value):
                return json.loads(value)

            async def init_connection(conn):
                 # Codec para JSONB
                 await conn.set_type_codec(
                     'jsonb',
                     encoder=_json_encoder,
                     decoder=_json_decoder,
                     schema='pg_catalog',
                     format='text' # Asegurar formato texto para JSONB
                 )
                 # Codec para JSON estándar
                 await conn.set_type_codec(
                      'json',
                      encoder=_json_encoder,
                      decoder=_json_decoder,
                      schema='pg_catalog',
                      format='text' # Asegurar formato texto para JSON
                  )
                 # --- CORRECCIÓN: ELIMINADO EL BLOQUE PARA text[] ---
                 # asyncpg maneja text[] automáticamente. No es necesario (y causa error)
                 # registrar un codec explícito para él.
                 # await conn.set_type_codec(
                 #     'text[]',
                 #     encoder=lambda x: x,
                 #     decoder=lambda x: x,
                 #     schema='pg_catalog',
                 #     format='text'
                 # )
                 # --- FIN CORRECCIÓN ---


            _pool = await asyncpg.create_pool(
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(), # Obtener valor del SecretStr
                database=settings.POSTGRES_DB,
                host=settings.POSTGRES_SERVER,
                port=settings.POSTGRES_PORT,
                min_size=5,   # Ajusta según necesidad
                max_size=20,  # Ajusta según necesidad
                statement_cache_size=0, # Deshabilitar caché para evitar problemas con tipos dinámicos
                init=init_connection # Añadir inicializador para codecs
            )
            log.info("PostgreSQL connection pool created successfully.")
        except Exception as e:
            log.error("Failed to create PostgreSQL connection pool",
                      error=str(e), error_type=type(e).__name__,
                      host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT,
                      db=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
                      exc_info=True)
            _pool = None # Asegurar que el pool es None si falla la creación
            raise # Relanzar para que el inicio de la app falle si no hay DB
    return _pool

async def close_db_pool():
    """Cierra el pool de conexiones a la base de datos."""
    global _pool
    if _pool and not _pool._closed:
        log.info("Closing PostgreSQL connection pool...")
        await _pool.close()
        _pool = None # Resetear la variable global
        log.info("PostgreSQL connection pool closed successfully.")
    elif _pool and _pool._closed:
        log.warning("Attempted to close an already closed PostgreSQL pool.")
        _pool = None
    else:
        log.info("No active PostgreSQL connection pool to close.")


async def check_db_connection() -> bool:
    """Verifica que la conexión a la base de datos esté funcionando."""
    pool = None # Asegurar inicialización
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            result = await conn.fetchval("SELECT 1")
        return result == 1
    except Exception as e:
        log.error("Database connection check failed", error=str(e))
        return False
    # No cerrar el pool aquí, solo verificar

# --- Métodos específicos para la tabla USERS ---

async def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """
    Recupera un usuario por su email.
    Devuelve un diccionario con los datos del usuario o None si no se encuentra.
    Alineado con el esquema USERS (con 'roles').
    """
    pool = await get_db_pool()
    # Selecciona 'roles' en lugar de 'role'
    query = """
        SELECT id, company_id, email, hashed_password, full_name, roles,
               created_at, last_login, is_active
        FROM users
        WHERE lower(email) = lower($1)
    """
    log.debug("Executing get_user_by_email query", email=email)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, email)
        if row:
            log.debug("User found by email", user_id=str(row['id']))
            # asyncpg devuelve 'roles' como una lista de Python automáticamente
            return dict(row)
        else:
            log.debug("User not found by email", email=email)
            return None
    except Exception as e:
        log.error("Error getting user by email", error=str(e), email=email, exc_info=True)
        raise

async def get_user_by_id(user_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """
    Recupera un usuario por su ID (UUID).
    Devuelve un diccionario con los datos del usuario o None si no se encuentra.
    Alineado con el esquema USERS (con 'roles'). Excluye la contraseña hash por seguridad.
    """
    pool = await get_db_pool()
    # Selecciona 'roles' en lugar de 'role'
    query = """
        SELECT id, company_id, email, full_name, roles,
               created_at, last_login, is_active
        FROM users
        WHERE id = $1
    """
    log.debug("Executing get_user_by_id query", user_id=str(user_id))
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, user_id)
        if row:
            log.debug("User found by ID", user_id=str(user_id))
            # asyncpg devuelve 'roles' como una lista de Python automáticamente
            return dict(row)
        else:
            log.debug("User not found by ID", user_id=str(user_id))
            return None
    except Exception as e:
        log.error("Error getting user by ID", error=str(e), user_id=str(user_id), exc_info=True)
        raise

async def update_user_company(user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    """
    Actualiza el company_id para un usuario específico y actualiza updated_at (si existiera).
    NOTA: La tabla users según el esquema no tiene updated_at.
    Devuelve True si la actualización fue exitosa, False en caso contrario.
    """
    pool = await get_db_pool()
    # Quitamos updated_at = NOW() porque la columna no existe
    query = """
        UPDATE users
        SET company_id = $2 -- , updated_at = NOW() -- Columna no existe
        WHERE id = $1
        RETURNING id -- Devolvemos el ID para confirmar la actualización
    """
    log.debug("Executing update_user_company query", user_id=str(user_id), company_id=str(company_id))
    try:
        async with pool.acquire() as conn:
            result = await conn.fetchval(query, user_id, company_id)
        if result is not None:
            log.info("User company updated successfully", user_id=str(user_id), new_company_id=str(company_id))
            return True
        else:
            log.warning("Update user company command executed but no rows were affected.", user_id=str(user_id))
            return False
    except Exception as e:
        log.error("Error updating user company", error=str(e), user_id=str(user_id), company_id=str(company_id), exc_info=True)
        raise

# --- NUEVAS FUNCIONES PARA ADMIN ---
# (Las nuevas funciones no se modifican, ya eran correctas respecto a 'roles')

async def create_company(name: str) -> Dict[str, Any]:
    """Crea una nueva compañía."""
    pool = await get_db_pool()
    query = """
        INSERT INTO companies (name)
        VALUES ($1)
        RETURNING id, name, created_at, is_active;
    """
    log.debug("Executing create_company query", name=name)
    try:
        async with pool.acquire() as conn:
            new_company = await conn.fetchrow(query, name)
            if new_company:
                 log.info("Company created successfully", company_id=str(new_company['id']), name=new_company['name'])
                 return dict(new_company)
            else:
                 log.error("Company creation query executed but no data returned.")
                 raise Exception("Failed to retrieve created company data.")
    except asyncpg.UniqueViolationError:
        log.warning("Failed to create company: Name might already exist.", name=name)
        raise
    except Exception as e:
        log.error("Error creating company", error=str(e), name=name, exc_info=True)
        raise

async def get_active_companies_select() -> List[Dict[str, Any]]:
    """Obtiene una lista de IDs y nombres de compañías activas para selectores."""
    pool = await get_db_pool()
    query = """
        SELECT id, name
        FROM companies
        WHERE is_active = TRUE
        ORDER BY name;
    """
    log.debug("Executing get_active_companies_select query")
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query)
        log.info(f"Retrieved {len(rows)} active companies for select.")
        return [dict(row) for row in rows]
    except Exception as e:
        log.error("Error getting active companies for select", error=str(e), exc_info=True)
        raise

async def create_user(
    email: str,
    hashed_password: str,
    name: Optional[str],
    company_id: uuid.UUID,
    roles: List[str]
) -> Dict[str, Any]:
    """Crea un nuevo usuario asociado a una compañía."""
    pool = await get_db_pool()
    # Query corregido sin updated_at
    query = """
        INSERT INTO users (email, hashed_password, full_name, company_id, roles, is_active, created_at)
        VALUES ($1, $2, $3, $4, $5, TRUE, NOW())
        RETURNING id, email, full_name, company_id, roles, is_active, created_at;
    """
    db_roles = roles if isinstance(roles, list) else [roles]
    log.debug("Executing create_user query", email=email, company_id=str(company_id), roles=db_roles)
    try:
        async with pool.acquire() as conn:
            new_user = await conn.fetchrow(query, email, hashed_password, name, company_id, db_roles)
            if new_user:
                log.info("User created successfully", user_id=str(new_user['id']), email=new_user['email'])
                user_data = dict(new_user)
                # hashed_password no está en el RETURNING, así que no hace falta pop
                return user_data
            else:
                log.error("User creation query executed but no data returned.")
                raise Exception("Failed to retrieve created user data.")
    except asyncpg.UniqueViolationError as e:
         log.warning("Failed to create user: Email likely already exists.", email=email, pg_error=str(e))
         raise
    except asyncpg.ForeignKeyViolationError as e:
         log.warning("Failed to create user: Company ID likely does not exist.", company_id=str(company_id), pg_error=str(e))
         raise
    except Exception as e:
        log.error("Error creating user", error=str(e), email=email, exc_info=True)
        raise

async def count_active_companies() -> int:
    """Cuenta el número total de compañías activas."""
    pool = await get_db_pool()
    query = "SELECT COUNT(*) FROM companies WHERE is_active = TRUE;"
    log.debug("Executing count_active_companies query")
    try:
        async with pool.acquire() as conn:
            count = await conn.fetchval(query)
        log.info(f"Found {count} active companies.")
        return count or 0
    except Exception as e:
        log.error("Error counting active companies", error=str(e), exc_info=True)
        raise

async def count_active_users_per_active_company() -> List[Dict[str, Any]]:
    """Cuenta usuarios activos por cada compañía activa."""
    pool = await get_db_pool()
    query = """
        SELECT c.id as company_id, c.name, COUNT(u.id) AS user_count
        FROM companies c
        LEFT JOIN users u ON c.id = u.company_id AND u.is_active = TRUE
        WHERE c.is_active = TRUE
        GROUP BY c.id, c.name
        ORDER BY c.name;
    """
    log.debug("Executing count_active_users_per_active_company query")
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query)
        log.info(f"Retrieved user counts for {len(rows)} active companies.")
        return [dict(row) for row in rows]
    except Exception as e:
        log.error("Error counting users per company", error=str(e), exc_info=True)
        raise

async def get_company_by_id(company_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Obtiene datos de una compañía por su ID."""
    pool = await get_db_pool()
    # Query corregido para incluir updated_at de la tabla companies
    query = """
        SELECT id, name, email, created_at, updated_at, is_active
        FROM companies
        WHERE id = $1;
    """
    log.debug("Executing get_company_by_id query", company_id=str(company_id))
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, company_id)
        if row:
            log.debug("Company found by ID", company_id=str(company_id))
            return dict(row)
        else:
            log.debug("Company not found by ID", company_id=str(company_id))
            return None
    except Exception as e:
        log.error("Error getting company by ID", error=str(e), company_id=str(company_id), exc_info=True)
        raise

async def check_email_exists(email: str) -> bool:
    """Verifica si un email ya existe en la tabla users."""
    pool = await get_db_pool()
    query = "SELECT EXISTS (SELECT 1 FROM users WHERE lower(email) = lower($1));"
    log.debug("Executing check_email_exists query", email=email)
    try:
        async with pool.acquire() as conn:
            exists = await conn.fetchval(query, email)
        log.debug(f"Email '{email}' exists check result: {exists}")
        return exists or False
    except Exception as e:
        log.error("Error checking email existence", error=str(e), email=email, exc_info=True)
        raise

async def get_users_by_company_id_paginated(
    company_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """
    Recupera usuarios por company_id con paginación.
    Devuelve una lista de diccionarios con datos de usuarios o una lista vacía.
    Los campos seleccionados coinciden con el modelo UserResponse.
    """
    pool = await get_db_pool()
    query = """
        SELECT id, email, full_name AS name, company_id, is_active, created_at, roles
        FROM users
        WHERE company_id = $1
        ORDER BY created_at DESC, id
        LIMIT $2 OFFSET $3;
    """
    db_log = log.bind(target_company_id=str(company_id), limit=limit, offset=offset)
    db_log.debug("Executing get_users_by_company_id_paginated query")
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, company_id, limit, offset)
        db_log.info(f"Retrieved {len(rows)} users for company.")
        # asyncpg maneja la conversión de TEXT[] a List[str] para roles
        return [dict(row) for row in rows]
    except Exception as e:
        db_log.error("Error getting users by company ID", error=str(e), exc_info=True)
        # Relanzar para que el router lo maneje como un error 500
        raise
```

## File: `app\main.py`
```py
# File: app/main.py
# api-gateway/app/main.py
import os
from fastapi import FastAPI, Request, Depends, HTTPException, status
from typing import Optional, List, Set # Importado Set
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import httpx
import structlog
import uvicorn
import time
import uuid
import logging
import re # Mantenemos re por si se usa en otro lado

# --- Configuración de Logging PRIMERO ---
from app.core.logging_config import setup_logging
setup_logging()

# --- Importaciones Core y DB ---
from app.core.config import settings
from app.db import postgres_client

# --- Importar Routers ---
from app.routers.gateway_router import router as gateway_router_instance
from app.routers.user_router import router as user_router_instance
from app.routers.admin_router import router as admin_router_instance

log = structlog.get_logger("atenex_api_gateway.main")

# --- Lifespan Manager (Sin cambios respecto a la versión actual) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Application startup sequence initiated...")
    http_client_instance: Optional[httpx.AsyncClient] = None
    db_pool_ok = False
    try:
        log.info("Initializing HTTPX client for application state...")
        limits = httpx.Limits(max_keepalive_connections=settings.HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS, max_connections=settings.HTTP_CLIENT_MAX_CONNECTIONS)
        timeout = httpx.Timeout(settings.HTTP_CLIENT_TIMEOUT, connect=15.0)
        http_client_instance = httpx.AsyncClient(limits=limits, timeout=timeout, follow_redirects=False, http2=True)
        app.state.http_client = http_client_instance
        log.info("HTTPX client initialized and attached to app.state successfully.")
    except Exception as e:
        log.exception("CRITICAL: Failed to initialize HTTPX client during startup!", error=str(e))
        app.state.http_client = None
    log.info("Initializing and verifying PostgreSQL connection pool...")
    try:
        pool = await postgres_client.get_db_pool()
        if pool:
            db_pool_ok = await postgres_client.check_db_connection()
            if db_pool_ok: log.info("PostgreSQL connection pool initialized and connection verified.")
            else: log.critical("PostgreSQL pool initialized BUT connection check failed!"); await postgres_client.close_db_pool()
        else: log.critical("PostgreSQL connection pool initialization returned None!")
    except Exception as e: log.exception("CRITICAL: Failed to initialize or verify PostgreSQL connection!", error=str(e)); db_pool_ok = False
    if getattr(app.state, 'http_client', None) and db_pool_ok: log.info("Application startup sequence complete. Dependencies ready.")
    else: log.error("Application startup sequence FAILED.", http_client_ready=bool(getattr(app.state, 'http_client', None)), db_ready=db_pool_ok)
    yield
    log.info("Application shutdown sequence initiated...")
    client_to_close = getattr(app.state, 'http_client', None)
    if client_to_close and not client_to_close.is_closed:
        log.info("Closing HTTPX client from app.state...")
        try:
            await client_to_close.aclose()
            log.info("HTTPX client closed.")
        except Exception as e:
            log.exception("Error closing HTTPX client.", error=str(e))
    else:
        log.info("HTTPX client was not initialized or already closed.")
    log.info("Closing PostgreSQL connection pool...")
    try:
        await postgres_client.close_db_pool()
    except Exception as e:
        log.exception("Error closing PostgreSQL pool.", error=str(e))
    log.info("Application shutdown complete.")


# --- Create FastAPI App Instance ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Atenex API Gateway: Single entry point, JWT auth, routing via explicit HTTP calls, Admin API.",
    version="1.1.5", # Nueva versión para reflejar corrección CORS específica del error
    lifespan=lifespan,
)

# --- Middlewares ---

# --- CORRECCIÓN CORS ---
# Usar un set para la construcción inicial para evitar duplicados, luego convertir a lista.
allowed_origins_set: Set[str] = {
    "http://localhost:3000",    # Desarrollo local frontend
    "http://localhost:3001",    # Otro puerto de desarrollo local frontend
    "http://127.0.0.1:3000",    # Desarrollo local frontend
    "http://127.0.0.1:3001",    # Otro puerto de desarrollo local frontend
}

# Añadir el dominio de producción del frontend desde el que se origina el error
PROD_FRONTEND_URL = "https://www.atenex.pe"
allowed_origins_set.add(PROD_FRONTEND_URL)

# Añadir la URL base de Vercel si está configurada en las settings
if settings.VERCEL_FRONTEND_URL:
    allowed_origins_set.add(settings.VERCEL_FRONTEND_URL)

# Añadir la URL de preview de Vercel que estaba en el código original (si sigue siendo relevante)
# Es mejor si esta URL es configurable si cambia o hay múltiples previews.
VERCEL_PREVIEW_URL = "https://atenex-frontend-git-main-devnyro-gmailcoms-projects.vercel.app"
allowed_origins_set.add(VERCEL_PREVIEW_URL)

# Añadir la URL de Ngrok específica que aparece en el log de error del frontend
# Esta es la URL a la que el frontend está intentando conectarse.
NGROK_URL_FROM_ERROR_LOG = "https://1bdb-2001-1388-53a0-ca20-ec59-6cb3-85d5-9c1a.ngrok-free.app"
allowed_origins_set.add(NGROK_URL_FROM_ERROR_LOG)

# La variable NGROK_URL_FROM_LOG (con https://2646-...) del código original
# se puede eliminar o comentar si ya no es relevante, o si se prefiere que la URL de Ngrok
# venga de una variable de entorno (ej. settings.NGROK_URL).
# Por ahora, la omitiremos para enfocarnos en la URL del error.

final_allowed_origins = list(allowed_origins_set)

log.info("Configuring CORS middleware", allowed_origins=final_allowed_origins)

app.add_middleware(CORSMiddleware,
                   allow_origins=final_allowed_origins, # Usar la lista final de orígenes únicos
                   allow_credentials=True,
                   allow_methods=["*"], # Permite GET, POST, OPTIONS, etc.
                   allow_headers=["*"], # Permite todos los headers comunes (incluyendo Content-Type, Authorization, etc.)
                   expose_headers=["X-Request-ID", "X-Process-Time"],
                   max_age=600)
# --- FIN CORRECCIÓN CORS ---


# Request Context/Timing/Logging Middleware (Con mejora en logging de OPTIONS)
@app.middleware("http")
async def add_request_context_timing_logging(request: Request, call_next):
    start_time = time.perf_counter()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = request_id
    user_context = {}
    # No intentar extraer payload aquí, puede que aún no exista
    origin = request.headers.get("origin", "N/A") # Capturar Origin para logs y errores
    request_log = log.bind(request_id=request_id, method=request.method, path=request.url.path,
                           client_ip=request.client.host if request.client else "unknown",
                           origin=origin) # Añadir origin al log inicial

    # Loguear la recepción de OPTIONS a nivel INFO para visibilidad
    if request.method == "OPTIONS":
        request_log.info("OPTIONS preflight request received")
    else:
        # Extraer contexto de usuario si ya está disponible (p.ej., de middleware anterior)
        if hasattr(request.state, 'user') and isinstance(request.state.user, dict):
            user_context['user_id'] = request.state.user.get('sub')
            user_context['company_id'] = request.state.user.get('company_id')
            request_log = request_log.bind(**user_context) # Vincular contexto de usuario
        request_log.info("Request received")

    response = None
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        # Loguear headers de respuesta si es OPTIONS para depurar CORS
        if request.method == "OPTIONS" and response:
            # Asegurar que el middleware CORS ya añadió las cabeceras correctas
            request_log.info("OPTIONS preflight response sending", status_code=response.status_code, headers=dict(response.headers))
    except Exception as e:
        proc_time = (time.perf_counter() - start_time) * 1000
        # En caso de error, añadir contexto de usuario si está disponible
        if hasattr(request.state, 'user') and isinstance(request.state.user, dict):
             user_context['user_id'] = request.state.user.get('sub')
             user_context['company_id'] = request.state.user.get('company_id')
             request_log = request_log.bind(**user_context)
        request_log.exception("Unhandled exception processing request", status_code=500, error=str(e), proc_time=round(proc_time,2))

        # Crear respuesta de error genérica
        response = JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "Internal Server Error"})
        response.headers["X-Request-ID"] = request_id

        # ¡IMPORTANTE! Re-aplicar cabeceras CORS a respuestas de error
        # El middleware CORSMiddleware podría no ejecutarse completamente si la excepción ocurre muy temprano.
        req_origin = request.headers.get("Origin")
        # Usar final_allowed_origins para la comprobación
        if req_origin in final_allowed_origins:
             response.headers["Access-Control-Allow-Origin"] = req_origin
             response.headers["Access-Control-Allow-Credentials"] = "true"
             # Opcional: añadir otros headers CORS si son necesarios para errores
             # response.headers["Access-Control-Allow-Methods"] = "*"
             # response.headers["Access-Control-Allow-Headers"] = "*"
        return response # Devolver la respuesta de error con cabeceras CORS
    finally:
        # Loguear finalización solo para métodos que no sean OPTIONS (ya se logueó al enviar)
        if response and request.method != "OPTIONS":
            proc_time = (time.perf_counter() - start_time) * 1000
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time"] = f"{proc_time:.2f}ms"
            # Añadir contexto de usuario al log final si existe
            if hasattr(request.state, 'user') and isinstance(request.state.user, dict):
                user_context['user_id'] = request.state.user.get('sub')
                user_context['company_id'] = request.state.user.get('company_id')
                request_log = request_log.bind(**user_context)
            log_level = "debug" if request.url.path == "/health" else "info"
            log_func = getattr(request_log.bind(status_code=status_code), log_level)
            log_func("Request completed", proc_time=round(proc_time, 2))

    return response


# --- Include Routers (Sin cambios) ---
log.info("Including application routers...")
app.include_router(user_router_instance, prefix="/api/v1/users", tags=["Users & Authentication"])
app.include_router(admin_router_instance, prefix="/api/v1/admin", tags=["Admin"])
app.include_router(gateway_router_instance, prefix="/api/v1") # Proxy general va último
log.info("Routers included successfully.")

# --- Root & Health Endpoints (Sin cambios) ---
@app.get("/", tags=["General"], summary="Root endpoint", include_in_schema=False)
async def read_root():
    return {"message": f"{settings.PROJECT_NAME} is running!"}

@app.get("/health", tags=["Health"], summary="Health check endpoint")
async def health_check(request: Request):
    health_status = {"status": "healthy", "service": settings.PROJECT_NAME, "checks": {}}
    db_ok = await postgres_client.check_db_connection()
    health_status["checks"]["database_connection"] = "ok" if db_ok else "failed"
    http_client = getattr(request.app.state, 'http_client', None)
    http_client_ok = http_client is not None and not http_client.is_closed
    health_status["checks"]["http_client"] = "ok" if http_client_ok else "failed"
    if not db_ok or not http_client_ok:
        health_status["status"] = "unhealthy"
        log.warning("Health check determined service unhealthy", checks=health_status["checks"])
        return JSONResponse(content=health_status, status_code=503)
    log.debug("Health check successful", checks=health_status["checks"])
    return health_status

# --- Main Execution (Sin cambios) ---
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    host = os.getenv("HOST", "0.0.0.0")
    reload_flag = os.getenv("UVICORN_RELOAD", "false").lower() == "true"
    log_level_uvicorn = settings.LOG_LEVEL.lower()

    print(f"Starting {settings.PROJECT_NAME} using Uvicorn...")
    print(f" Host: {host}")
    print(f" Port: {port}")
    print(f" Reload: {reload_flag}")
    print(f" Log Level: {log_level_uvicorn}")

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload_flag,
        log_level=log_level_uvicorn
    )
```

## File: `app\models\__init__.py`
```py

```

## File: `app\models\admin_models.py`
```py
# File: app/models/admin_models.py
# api-gateway/app/models/admin_models.py
import uuid
from pydantic import BaseModel, EmailStr, Field, validator
from typing import List, Optional
from datetime import datetime

# --- Modelos para Compañías ---

class CompanyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, description="Nombre de la nueva compañía.")

class CompanyResponse(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    # Podríamos añadir is_active si fuera relevante en la respuesta
    # is_active: bool

    model_config = { # Anteriormente Config
        "from_attributes": True # Anteriormente orm_mode
    }

class CompanySelectItem(BaseModel):
    id: uuid.UUID
    name: str

    model_config = {
        "from_attributes": True
    }

# No necesitamos un modelo wrapper CompanyListSelectResponse, FastAPI puede devolver List[CompanySelectItem] directamente.

# --- Modelos para Usuarios ---

class UserCreateRequest(BaseModel):
    email: EmailStr = Field(..., description="Correo electrónico único del nuevo usuario.")
    password: str = Field(..., min_length=8, description="Contraseña del nuevo usuario (mínimo 8 caracteres).")
    name: Optional[str] = Field(None, description="Nombre completo del usuario (opcional).")
    company_id: uuid.UUID = Field(..., description="ID de la compañía a la que pertenece el usuario.")
    roles: List[str] = Field(default=['user'], description="Lista de roles asignados al usuario (ej. ['user', 'admin']).")

    @validator('roles', each_item=True)
    def check_role_values(cls, v):
        # Opcional: Validar que los roles sean de un conjunto permitido si es necesario
        allowed_roles = {'user', 'admin'} # Ejemplo
        if v not in allowed_roles:
             raise ValueError(f"Rol inválido: '{v}'. Roles permitidos: {allowed_roles}")
        return v

class UserResponse(BaseModel):
    id: uuid.UUID
    email: EmailStr
    name: Optional[str] = None
    company_id: uuid.UUID
    roles: List[str]
    is_active: bool
    created_at: datetime

    model_config = {
        "from_attributes": True
    }


# --- Modelos para Estadísticas ---

class UsersPerCompanyStat(BaseModel):
    company_id: uuid.UUID
    name: str
    user_count: int

    model_config = {
        "from_attributes": True
    }

class AdminStatsResponse(BaseModel):
    company_count: int = Field(..., description="Número total de compañías activas.")
    users_per_company: List[UsersPerCompanyStat] = Field(..., description="Lista de compañías activas con su conteo de usuarios activos.")
```

## File: `app\models\document_stats_models.py`
```py
# File: app/models/document_stats_models.py
# NUEVO ARCHIVO
import uuid
from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from datetime import datetime

class DocumentStatsByStatus(BaseModel):
    processed: int = 0
    processing: int = 0
    uploaded: int = 0
    error: int = 0

class DocumentStatsByType(BaseModel):
    pdf: int = 0
    docx: int = 0
    txt: int = 0
    # Puedes añadir más tipos si son relevantes
    # उदाहरण के लिए: md: int = 0, html: int = 0
    other: int = 0 # Para tipos no listados explícitamente

class DocumentStatsByUser(BaseModel):
    user_id: str # Podría ser UUID, pero el ejemplo usa 'u1'
    name: Optional[str] = None # Nombre del usuario, idealmente a obtener de la tabla users
    count: int

class DocumentRecentActivity(BaseModel):
    date: str # Podría ser datetime.date o str ISO8601
    uploaded: int = 0
    processed: int = 0
    error: int = 0

class DocumentStatsResponse(BaseModel):
    total_documents: int
    total_chunks: int
    by_status: DocumentStatsByStatus
    by_type: DocumentStatsByType
    by_user: List[DocumentStatsByUser]
    recent_activity: List[DocumentRecentActivity]
    oldest_document_date: Optional[datetime] = None
    newest_document_date: Optional[datetime] = None

    model_config = {
        "from_attributes": True
    }
```

## File: `app\routers\__init__.py`
```py

```

## File: `app\routers\admin_router.py`
```py
# File: app/routers/admin_router.py
# api-gateway/app/routers/admin_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Request, Path, Query
from typing import List, Dict, Any
import structlog
import uuid
import asyncpg # Para capturar errores específicos de DB

# --- Dependencias y Servicios ---
from app.auth.auth_middleware import AdminAuth # Usar la dependencia AdminAuth
from app.db import postgres_client
from app.auth.auth_service import get_password_hash

# --- Modelos Pydantic ---
from app.models.admin_models import (
    CompanyCreateRequest, CompanyResponse, CompanySelectItem,
    UserCreateRequest, UserResponse, # UserResponse será usado por el nuevo endpoint
    UsersPerCompanyStat, AdminStatsResponse
)

log = structlog.get_logger(__name__)
router = APIRouter() # No añadir prefijo aquí, se añade en main.py

# --- Endpoints de Administración ---

@router.get(
    "/stats",
    response_model=AdminStatsResponse,
    summary="Obtener estadísticas generales",
    description="Devuelve el número total de compañías activas y el número de usuarios activos por compañía activa."
)
async def get_admin_stats(
    request: Request,
    admin_user_payload: AdminAuth # Protegido: Solo admins pueden acceder
):
    admin_id = admin_user_payload.get("sub")
    req_id = getattr(request.state, 'request_id', 'N/A')
    stats_log = log.bind(request_id=req_id, admin_user_id=admin_id)
    stats_log.info("Admin requested platform statistics.")

    try:
        company_count = await postgres_client.count_active_companies()
        users_per_company = await postgres_client.count_active_users_per_active_company()

        stats_log.info("Statistics retrieved successfully.", company_count=company_count, companies_with_users=len(users_per_company))
        return AdminStatsResponse(
            company_count=company_count,
            users_per_company=users_per_company
        )
    except Exception as e:
        stats_log.exception("Error retrieving admin statistics", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve statistics.")


@router.post(
    "/companies",
    response_model=CompanyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear una nueva compañía",
    description="Crea un nuevo registro de compañía en la base de datos."
)
async def create_new_company(
    request: Request,
    company_data: CompanyCreateRequest,
    admin_user_payload: AdminAuth # Protegido
):
    admin_id = admin_user_payload.get("sub")
    req_id = getattr(request.state, 'request_id', 'N/A')
    company_log = log.bind(request_id=req_id, admin_user_id=admin_id, company_name=company_data.name)
    company_log.info("Admin attempting to create a new company.")

    try:
        new_company = await postgres_client.create_company(name=company_data.name)
        company_log.info("Company created successfully", new_company_id=str(new_company['id']))
        return CompanyResponse.model_validate(new_company)
    except asyncpg.UniqueViolationError:
         company_log.warning("Failed to create company: Name likely already exists.")
         raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Company name '{company_data.name}' may already exist.")
    except Exception as e:
        company_log.exception("Error creating company", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create company.")


@router.get(
    "/companies/select",
    response_model=List[CompanySelectItem],
    summary="Listar compañías para selector",
    description="Devuelve una lista simplificada (ID, Nombre) de compañías activas, ordenadas por nombre."
)
async def list_companies_for_select(
    request: Request,
    admin_user_payload: AdminAuth # Protegido
):
    admin_id = admin_user_payload.get("sub")
    req_id = getattr(request.state, 'request_id', 'N/A')
    list_log = log.bind(request_id=req_id, admin_user_id=admin_id)
    list_log.info("Admin requested list of active companies for select.")

    try:
        companies = await postgres_client.get_active_companies_select()
        list_log.info(f"Retrieved {len(companies)} companies for select.")
        return companies
    except Exception as e:
        list_log.exception("Error retrieving companies for select", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve company list.")


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear un nuevo usuario",
    description="Crea un nuevo usuario, lo asocia a una compañía y le asigna roles."
)
async def create_new_user(
    request: Request,
    user_data: UserCreateRequest,
    admin_user_payload: AdminAuth # Protegido
):
    admin_id = admin_user_payload.get("sub")
    req_id = getattr(request.state, 'request_id', 'N/A')
    user_log = log.bind(request_id=req_id, admin_user_id=admin_id, new_user_email=user_data.email, target_company_id=str(user_data.company_id))
    user_log.info("Admin attempting to create a new user.")

    try:
        email_exists = await postgres_client.check_email_exists(user_data.email)
        if email_exists:
            user_log.warning("User creation failed: Email already exists.")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Email '{user_data.email}' already registered.")
    except Exception as e:
        user_log.exception("Error checking email existence during user creation", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error checking email availability.")

    try:
        company = await postgres_client.get_company_by_id(user_data.company_id)
        if not company:
            user_log.warning("User creation failed: Target company not found.", company_id=str(user_data.company_id))
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Company with ID '{user_data.company_id}' not found.")
        if not company.get('is_active', False):
             user_log.warning("User creation failed: Target company is inactive.", company_id=str(user_data.company_id))
             raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cannot assign user to inactive company '{user_data.company_id}'.")
    except Exception as e:
        user_log.exception("Error checking company existence during user creation", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error verifying target company.")

    try:
        hashed_password = get_password_hash(user_data.password)
        new_user = await postgres_client.create_user(
            email=user_data.email,
            hashed_password=hashed_password,
            name=user_data.name,
            company_id=user_data.company_id,
            roles=user_data.roles
        )
        user_log.info("User created successfully", new_user_id=str(new_user['id']))
        return UserResponse.model_validate(new_user)

    except asyncpg.UniqueViolationError:
         user_log.warning("User creation conflict (likely email).")
         raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Email '{user_data.email}' already registered.")
    except asyncpg.ForeignKeyViolationError:
         user_log.warning("User creation failed due to non-existent company ID.")
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Company with ID '{user_data.company_id}' not found.")
    except Exception as e:
        user_log.exception("Error creating user in database", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create user.")

# --- NUEVO ENDPOINT ---
@router.get(
    "/users/by_company/{companyId}",
    response_model=List[UserResponse],
    summary="Listar usuarios por ID de compañía",
    description="Obtiene una lista paginada de usuarios pertenecientes a una compañía específica. Requiere rol de administrador."
)
async def list_users_by_company(
    request: Request,
    admin_user_payload: AdminAuth, # Moved before parameters with default values
    companyId: uuid.UUID = Path(..., description="ID de la compañía cuyos usuarios se desean listar."),
    limit: int = Query(50, ge=1, le=200, description="Número máximo de usuarios a devolver."),
    offset: int = Query(0, ge=0, description="Offset para paginación.")
):
    admin_id_from_token = admin_user_payload.get("sub")
    # X-Company-ID del admin (opcional para auditoría), viene del token si existe.
    admin_company_id_for_audit = admin_user_payload.get("company_id")
    # X-User-ID header es el ID del admin, que es admin_id_from_token
    # X-Company-ID header (opcional para auditoria) es admin_company_id_for_audit

    req_id = getattr(request.state, 'request_id', 'N/A')

    endpoint_log = log.bind(
        request_id=req_id,
        admin_user_id=admin_id_from_token,
        admin_company_id_for_audit=str(admin_company_id_for_audit) if admin_company_id_for_audit else "N/A",
        target_company_id_path=str(companyId),
        limit_query=limit,
        offset_query=offset
    )
    endpoint_log.info("Admin request to list users by company.")

    # 1. Verificar que la compañía (del path param) exista
    target_company = await postgres_client.get_company_by_id(companyId)
    if not target_company:
        endpoint_log.warning("Target company specified in path not found.", company_id_path=str(companyId))
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Company with ID '{companyId}' not found."
        )
    
    # (Opcional) Podrías querer verificar si la compañía está activa si es un requisito.
    # if not target_company.get('is_active', False):
    #     endpoint_log.warning("Target company is not active.")
    #     raise HTTPException(
    #         status_code=status.HTTP_400_BAD_REQUEST,
    #         detail=f"Company with ID '{companyId}' is not active. Cannot list users for inactive company."
    #     )

    # 2. Obtener usuarios de la compañía especificada en el path
    try:
        users_data = await postgres_client.get_users_by_company_id_paginated(
            company_id=companyId, # Usar el companyId del path
            limit=limit,
            offset=offset
        )
        endpoint_log.info(f"Successfully retrieved {len(users_data)} users from DB for target company.")
        
        # FastAPI se encargará de validar cada item de la lista contra UserResponse
        # y serializarlo correctamente.
        return users_data
        
    except Exception as e:
        endpoint_log.exception("Error retrieving users by company from database.", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve users for the company."
        )
```

## File: `app\routers\auth_router.py`
```py
# File: app/routers/auth_router.py
# api-gateway/app/routers/auth_router.py
from fastapi import APIRouter
import structlog

log = structlog.get_logger(__name__)

# Este router ya no define rutas activas.
# El login está en user_router.
# El proxy /api/v1/auth/* se define en gateway_router si settings.AUTH_SERVICE_URL está configurado.
router = APIRouter()

log.info("Auth router loaded (currently defines no active endpoints).")

# Puedes eliminar este archivo si no planeas añadir rutas específicas bajo /api/v1/auth
# que NO sean proxy al AUTH_SERVICE_URL. Si lo eliminas, asegúrate de quitar
# la línea 'app.include_router(auth_router.router)' en app/main.py.
```

## File: `app\routers\gateway_router.py`
```py
# File: app/routers/gateway_router.py
# api-gateway/app/routers/gateway_router.py
from fastapi import APIRouter, Request, Response, Depends, HTTPException, status, Path, Query
from fastapi.responses import StreamingResponse
from typing import Optional, Annotated, Dict, Any
import httpx
import structlog
import asyncio
import uuid
import re
from datetime import date

from app.core.config import settings
from app.auth.auth_middleware import StrictAuth, InitialAuth
from app.models.document_stats_models import DocumentStatsResponse


log = structlog.get_logger("atenex_api_gateway.router.gateway")
dep_log = structlog.get_logger("atenex_api_gateway.dependency.client")
auth_dep_log = structlog.get_logger("atenex_api_gateway.dependency.auth")
router = APIRouter()
http_client: Optional[httpx.AsyncClient] = None

HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "content-encoding", "content-length"
}

def get_client(request: Request) -> httpx.AsyncClient:
    client = getattr(request.app.state, "http_client", None)
    if client is None or client.is_closed:
        dep_log.error("Gateway HTTP client dependency check failed: Client not available or closed.")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Gateway service dependency unavailable (HTTP Client).")
    return client

async def logged_strict_auth(user_payload: StrictAuth) -> Dict[str, Any]:
    log.info("logged_strict_auth called", user_payload=user_payload, type=str(type(user_payload)))
    return user_payload

LoggedStrictAuth = Annotated[Dict[str, Any], Depends(logged_strict_auth)]

async def _proxy_request(
    request: Request,
    target_service_base_url_str: str,
    client: httpx.AsyncClient,
    user_payload: Optional[Dict[str, Any]],
    backend_service_path: str,
):
    method = request.method
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    proxy_log = log.bind(
        request_id=request_id, method=method, original_path=request.url.path,
        target_service=target_service_base_url_str, target_path=backend_service_path
    )
    proxy_log.info("Initiating proxy request")
    try:
        target_base_url = httpx.URL(target_service_base_url_str)
        if not backend_service_path.startswith("/"):
            backend_service_path = "/" + backend_service_path
        target_url = target_base_url.copy_with(path=backend_service_path, query=request.url.query.encode("utf-8"))
        proxy_log.debug("Target URL constructed", url=str(target_url))
    except Exception as e:
        proxy_log.error("Failed to construct target URL", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal gateway configuration error.")

    headers_to_forward = {}
    client_host = request.client.host if request.client else "unknown"
    x_forwarded_for = request.headers.get("x-forwarded-for", client_host)
    headers_to_forward["X-Forwarded-For"] = x_forwarded_for
    headers_to_forward["X-Forwarded-Proto"] = request.url.scheme
    if "host" in request.headers: headers_to_forward["X-Forwarded-Host"] = request.headers["host"]
    headers_to_forward["X-Request-ID"] = request_id

    for name, value in request.headers.items():
        lower_name = name.lower()
        if lower_name not in HOP_BY_HOP_HEADERS and lower_name != "host":
            headers_to_forward[name] = value
        elif lower_name == "content-type":
            headers_to_forward[name] = value

    log_context_headers = {}
    if user_payload:
        user_id = user_payload.get('sub')
        company_id = user_payload.get('company_id')
        user_email = user_payload.get('email')
        if not user_id or not company_id:
             proxy_log.critical("Payload missing required fields post-auth!", payload_keys=list(user_payload.keys()))
             raise HTTPException(status_code=500, detail="Internal authentication context error.")
        headers_to_forward['X-User-ID'] = str(user_id)
        headers_to_forward['X-Company-ID'] = str(company_id)
        headers_to_forward['x-user-id'] = str(user_id) 
        headers_to_forward['x-company-id'] = str(company_id) 
        log_context_headers['user_id'] = str(user_id)
        log_context_headers['company_id'] = str(company_id)
        if user_email:
            headers_to_forward['X-User-Email'] = str(user_email)
            log_context_headers['user_email'] = str(user_email)
        proxy_log = proxy_log.bind(**log_context_headers)
        proxy_log.debug("Context headers prepared", headers_added=list(log_context_headers.keys()))

    method_allows_body = method.upper() in ["POST", "PUT", "PATCH", "DELETE"]
    request_body_bytes: Optional[bytes] = None
    if method_allows_body:
        try:
            request_body_bytes = await request.body()
            proxy_log.debug(f"Read request body ({len(request_body_bytes)} bytes)")
            if request_body_bytes:
                headers_to_forward['content-length'] = str(len(request_body_bytes))
            else:
                headers_to_forward.pop('content-length', None) 
            headers_to_forward.pop('transfer-encoding', None) 
        except Exception as e:
            proxy_log.error("Failed to read request body", error=str(e), exc_info=True)
            raise HTTPException(status_code=400, detail="Could not read request body.")
    else:
        headers_to_forward.pop('content-length', None) 
        proxy_log.debug("No request body expected or present.")

    backend_response: Optional[httpx.Response] = None
    try:
        proxy_log.debug(f"Sending {method} request to {target_url}", headers=list(headers_to_forward.keys()))
        req = client.build_request(method=method, url=target_url, headers=headers_to_forward, content=request_body_bytes)
        backend_response = await client.send(req, stream=True)
        proxy_log.info(f"Received response from backend", status_code=backend_response.status_code, content_type=backend_response.headers.get("content-type"))
        response_headers = {}
        for name, value in backend_response.headers.items():
            if name.lower() not in HOP_BY_HOP_HEADERS:
                 response_headers[name] = value
        response_headers["X-Request-ID"] = request_id

        return StreamingResponse(
            backend_response.aiter_raw(),
            status_code=backend_response.status_code,
            headers=response_headers,
            media_type=backend_response.headers.get("content-type"),
            background=backend_response.aclose 
        )
    except httpx.TimeoutException as exc:
        proxy_log.error(f"Proxy request timed out waiting for backend", target=str(target_url), error=str(exc), exc_info=False)
        if backend_response: await backend_response.aclose()
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Upstream service timed out.")
    except httpx.ConnectError as exc:
        proxy_log.error(f"Proxy connection error: Could not connect to backend", target=str(target_url), error=str(exc), exc_info=False)
        if backend_response: await backend_response.aclose()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not connect to upstream service.")
    except httpx.RequestError as exc:
        proxy_log.error(f"Proxy request error communicating with backend", target=str(target_url), error=str(exc), exc_info=True)
        if backend_response: await backend_response.aclose()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Error communicating with upstream service: {exc}")
    except Exception as exc:
        proxy_log.exception(f"Unexpected error occurred during proxy request", target=str(target_url))
        if backend_response: await backend_response.aclose()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred in the gateway.")


@router.options("/ingest/{endpoint_path:path}", tags=["CORS", "Proxy - Ingest Service"], include_in_schema=False)
async def options_proxy_ingest_service_generic(endpoint_path: str = Path(...)): return Response(status_code=200)

@router.options("/query/ask", tags=["CORS", "Proxy - Query Service"], include_in_schema=False)
async def options_query_ask(): return Response(status_code=200)

@router.options("/query/chats", tags=["CORS", "Proxy - Query Service"], include_in_schema=False)
async def options_query_chats(): return Response(status_code=200)

@router.options("/query/chats/{chat_id}/messages", tags=["CORS", "Proxy - Query Service"], include_in_schema=False)
async def options_chat_messages(chat_id: uuid.UUID = Path(...)): return Response(status_code=200)

@router.options("/query/chats/{chat_id}", tags=["CORS", "Proxy - Query Service"], include_in_schema=False)
async def options_delete_chat(chat_id: uuid.UUID = Path(...)): return Response(status_code=200)

if settings.AUTH_SERVICE_URL:
    @router.options("/auth/{endpoint_path:path}", tags=["CORS", "Proxy - Auth Service (Optional)"], include_in_schema=False)
    async def options_proxy_auth_service_generic(endpoint_path: str = Path(...)): return Response(status_code=200)

@router.options("/documents/stats", tags=["CORS", "Proxy - Document Stats"], include_in_schema=False)
async def options_document_stats():
    return Response(status_code=200)

@router.get(
    "/query/chats",
    tags=["Proxy - Query Service"],
    summary="List user's chats (Proxied)"
)
async def proxy_get_chats(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: LoggedStrictAuth,
):
    log.info("proxy_get_chats called", headers=dict(request.headers), user_payload=user_payload)
    backend_path = f"{settings.API_V1_STR}/chats" 
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

@router.post(
    "/query/ask",
    tags=["Proxy - Query Service"],
    summary="Submit a query or message to a chat (Proxied)"
)
async def proxy_post_query(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: LoggedStrictAuth,
):
    try:
        body = await request.body()
        log.info("proxy_post_query called", headers=dict(request.headers), user_payload=user_payload, body_length=len(body))
    except Exception as e:
        log.error("Error reading request body in proxy_post_query", error=str(e))
    backend_path = f"{settings.API_V1_STR}/ask"
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

@router.get(
    "/query/chats/{chat_id}/messages",
    tags=["Proxy - Query Service"],
    summary="Get messages for a specific chat (Proxied)"
)
async def proxy_get_chat_messages(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: LoggedStrictAuth,
    chat_id: uuid.UUID = Path(...),
):
    backend_path = f"{settings.API_V1_STR}/chats/{chat_id}/messages"
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

@router.delete(
    "/query/chats/{chat_id}",
    tags=["Proxy - Query Service"],
    summary="Delete a specific chat (Proxied)"
)
async def proxy_delete_chat(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: LoggedStrictAuth,
    chat_id: uuid.UUID = Path(...),
):
    backend_path = f"{settings.API_V1_STR}/chats/{chat_id}"
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)


@router.get(
    "/documents/stats",
    response_model=DocumentStatsResponse, 
    tags=["Proxy - Document Stats", "Proxy - Ingest Service"],
    summary="Get aggregated document statistics (Proxied to Ingest Service)"
)
async def proxy_get_document_stats(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: LoggedStrictAuth,
    from_date: Optional[date] = Query(None, description="Filter from this date (ISO8601)."),
    to_date: Optional[date] = Query(None, description="Filter up to this date (ISO8601)."),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by specific status (processed, processing, uploaded, error)."),
    group_by: Optional[str] = Query(None, description="Group activity by (day, week, month, user).")
):
    # El prefijo del ingest-service es /api/v1/ingest, y el endpoint es /stats
    # Por lo tanto, el path completo en el ingest-service es /api/v1/ingest/stats
    backend_path = f"{settings.API_V1_STR}/ingest/stats" 
    
    log.info("Proxying request for document stats",
             original_path=request.url.path,
             target_backend_path=backend_path,
             ingest_service_url=str(settings.INGEST_SERVICE_URL))

    return await _proxy_request(
        request=request,
        target_service_base_url_str=str(settings.INGEST_SERVICE_URL),
        client=client,
        user_payload=user_payload,
        backend_service_path=backend_path
    )

@router.api_route(
    "/ingest/{endpoint_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    tags=["Proxy - Ingest Service"],
    summary="Generic proxy for Ingest Service endpoints (Authenticated)"
)
async def proxy_ingest_service_generic(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: LoggedStrictAuth,
    endpoint_path: str = Path(...),
):
    # Asume que los endpoints en ingest-service están directamente bajo /api/v1/ingest/...
    # y que el settings.API_V1_STR del ingest-service es "/api/v1/ingest"
    # Entonces, si endpoint_path es "upload", el backend_path será "/api/v1/ingest/upload"
    backend_path = f"{settings.API_V1_STR}/ingest/{endpoint_path}"
    return await _proxy_request(
        request=request,
        target_service_base_url_str=str(settings.INGEST_SERVICE_URL),
        client=client,
        user_payload=user_payload,
        backend_service_path=backend_path
    )

if settings.AUTH_SERVICE_URL:
    # Lógica para el proxy de auth_service si está configurado
    pass
else:
    log.info("Auth service proxy is disabled (GATEWAY_AUTH_SERVICE_URL not set).")
```

## File: `app\routers\user_router.py`
```py
# File: app/routers/user_router.py
# api-gateway/app/routers/user_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Request, Body
from typing import Dict, Any, Optional, Annotated, List # Añadido List
import structlog
import uuid

from app.auth.auth_middleware import InitialAuth
from app.auth.auth_service import authenticate_user, create_access_token
from app.db import postgres_client
from app.core.config import settings
from pydantic import BaseModel, EmailStr, Field, validator

log = structlog.get_logger(__name__)

# El prefijo /api/v1/users se define en main.py al incluir el router
router = APIRouter()

# --- Modelos Pydantic ---
class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=6)

# --- MODIFICADO: Añadir roles a LoginResponse ---
class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: EmailStr
    full_name: Optional[str] = None
    roles: Optional[List[str]] = None # Añadido campo roles
    company_id: Optional[str] = None

class EnsureCompanyRequest(BaseModel):
    company_id: Optional[str] = None
    @validator('company_id')
    def validate_company_id_format(cls, v):
        if v is not None:
            try: uuid.UUID(v)
            except ValueError: raise ValueError("Provided company_id is not a valid UUID")
        return v

class EnsureCompanyResponse(BaseModel):
    user_id: str
    company_id: str
    message: str
    new_access_token: str
    token_type: str = "bearer"
    # Añadir también roles aquí si el frontend lo necesita tras asegurar compañía
    roles: Optional[List[str]] = None

# --- Endpoints ---
# El path "/login" se resuelve como "/api/v1/users/login" debido al prefijo en main.py
@router.post("/login", response_model=LoginResponse, tags=["Users & Authentication"])
async def login_for_access_token(login_data: LoginRequest):
    log.info("Login attempt initiated", email=login_data.email)
    user = await authenticate_user(login_data.email, login_data.password)
    if not user:
        log.warning("Login failed", email=login_data.email)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password", headers={"WWW-Authenticate": "Bearer"})

    user_id = user.get("id")
    company_id = user.get("company_id")
    email = user.get("email")
    full_name = user.get("full_name")
    roles = user.get("roles") # Obtener roles de la DB

    if not user_id or not email:
        log.error("Authenticated user data missing critical fields (id or email)", keys=list(user.keys()) if user else None)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal login error: Missing user data.")

    # --- MODIFICADO: Pasar roles a create_access_token ---
    access_token = create_access_token(
        user_id=user_id,
        email=email,
        company_id=company_id,
        roles=roles # Pasar la lista de roles
    )

    log.info("Login successful", user_id=str(user_id), company_id=str(company_id) if company_id else "None", roles=roles)

    # --- MODIFICADO: Incluir roles en la respuesta ---
    return LoginResponse(
        access_token=access_token,
        user_id=str(user_id),
        email=email,
        full_name=full_name,
        roles=roles, # Devolver roles al frontend
        company_id=str(company_id) if company_id else None
    )

# El path "/me/ensure-company" se resuelve como "/api/v1/users/me/ensure-company"
@router.post("/me/ensure-company", response_model=EnsureCompanyResponse, tags=["Users & Authentication"])
async def ensure_company_association(
    request: Request,
    user_payload: InitialAuth, # Requiere token válido (firma, exp, user activo), no company_id
    ensure_request: Optional[EnsureCompanyRequest] = Body(None)
):
    user_id_str = user_payload.get("sub")
    req_id = getattr(request.state, 'request_id', 'N/A')
    log_ctx = log.bind(request_id=req_id, user_id=user_id_str)
    log_ctx.info("Ensure company association requested.")

    if not user_id_str:
        log_ctx.error("Ensure company failed: User ID ('sub') missing from token payload.");
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User ID not found in token.")
    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        log_ctx.error("Ensure company failed: Invalid User ID format in token.", sub_value=user_id_str)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user ID format.")

    # Obtener datos actuales del usuario (incluyendo roles actuales)
    current_user_data = await postgres_client.get_user_by_id(user_id)
    if not current_user_data:
        log_ctx.error("Ensure company failed: User from token not found in database.");
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.") # 404 es más apropiado

    current_company_id = current_user_data.get("company_id")
    current_roles = current_user_data.get("roles") # Obtener roles actuales
    user_email = current_user_data.get("email") # Obtener email para el nuevo token

    if not user_email:
        log_ctx.error("Ensure company failed: Email missing for user, cannot generate new token.", user_id=user_id_str)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal error: Missing user email.")

    target_company_id_str: Optional[str] = None
    action_taken = "none" # 'updated', 'confirmed', 'failed'

    # Determinar target company ID
    if ensure_request and ensure_request.company_id:
        target_company_id_str = ensure_request.company_id
        log_ctx.info("Attempting to use company_id from request body.", target_id=target_company_id_str)
    elif current_company_id:
        target_company_id_str = str(current_company_id)
        log_ctx.info("Using user's current company_id.", current_id=target_company_id_str)
    elif settings.DEFAULT_COMPANY_ID:
        target_company_id_str = settings.DEFAULT_COMPANY_ID
        log_ctx.info("Using default company_id from settings.", default_id=target_company_id_str)
    else:
        log_ctx.error("Ensure company failed: Cannot determine target company ID. None provided, user has no current ID, and no default is set.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot associate company: No specific ID provided and no default available for this user."
        )

    # Validar formato del target company ID
    try:
        target_company_id = uuid.UUID(target_company_id_str)
    except (ValueError, TypeError):
        log_ctx.error("Ensure company failed: Invalid target company ID format.", target_value=target_company_id_str)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid target company ID format.")

    # Verificar si la compañía objetivo existe (opcional pero recomendado)
    # try:
    #     target_company = await postgres_client.get_company_by_id(target_company_id)
    #     if not target_company:
    #          log_ctx.warning("Ensure company failed: Target company ID does not exist in DB.", target_id=str(target_company_id))
    #          raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Target company {target_company_id} not found.")
    # except Exception as e:
    #      log_ctx.exception("Ensure company failed: Error verifying target company existence.", error=str(e))
    #      raise HTTPException(status_code=500, detail="Error verifying target company.")


    # Actualizar DB si es necesario
    if target_company_id != current_company_id:
        log_ctx.info("Target company differs from current. Attempting database update.",
                     current_id=str(current_company_id), new_id=str(target_company_id))
        try:
            updated = await postgres_client.update_user_company(user_id, target_company_id)
            if not updated:
                # Esto podría ocurrir si el user_id ya no existe, aunque get_user_by_id lo verificó antes.
                log_ctx.error("Ensure company failed: Database update command affected 0 rows.", target_company_id=str(target_company_id))
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update user company association.")
            action_taken = "updated"
            log_ctx.info("User company association updated in database successfully.")
        except Exception as e:
            log_ctx.exception("Ensure company failed: Error during database update.", error=str(e))
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database error during company update.")
    else:
        action_taken = "confirmed"
        log_ctx.info("Target company matches current. No database update needed.", company_id=str(target_company_id))

    # Generar nuevo token con la compañía final y los roles actuales
    new_access_token = create_access_token(
        user_id=user_id,
        email=user_email,
        company_id=target_company_id, # Usar la compañía final
        roles=current_roles # Incluir los roles actuales en el nuevo token
    )
    log_ctx.info("New access token generated successfully.", final_company_id=str(target_company_id), roles=current_roles)

    if action_taken == "updated":
        message = f"Company association successfully updated to {target_company_id}."
    else: # action_taken == "confirmed"
        message = f"Company association confirmed as {target_company_id}."

    return EnsureCompanyResponse(
        user_id=str(user_id),
        company_id=str(target_company_id),
        message=message,
        new_access_token=new_access_token,
        roles=current_roles # Devolver roles en la respuesta
    )
```

## File: `pyproject.toml`
```toml
# File: pyproject.toml
# api-gateway/pyproject.toml
[tool.poetry]
name = "atenex-api-gateway"
version = "1.1.4" # Incrementar versión para reflejar correcciones
description = "API Gateway for Atenex Microservices"
authors = ["Atenex Team <dev@atenex.com>"]
readme = "README.md"

[tool.poetry.dependencies]
python = "^3.10"

# Core FastAPI y servidor ASGI
fastapi = "^0.110.0"
uvicorn = {extras = ["standard"], version = "^0.28.0"}
gunicorn = "^21.2.0"

# Configuración y validación
pydantic = {extras = ["email"], version = "^2.6.4"}
pydantic-settings = "^2.2.1"

# Cliente HTTP asíncrono (versión con extras es la correcta)
httpx = {extras = ["http2"], version = "^0.27.0"}

# Manejo de JWT
python-jose = {extras = ["cryptography"], version = "^3.3.0"}

# Logging estructurado
structlog = "^24.1.0"

# Cliente PostgreSQL Asíncrono
asyncpg = "^0.29.0"

# Hashing de Contraseñas
passlib = {extras = ["bcrypt"], version = "^1.7.4"}

# Dependencia necesaria para httpx[http2]
h2 = "^4.1.0"

[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"
pytest-httpx = "^0.29.0"
# black = "^24.3.0"
# ruff = "^0.3.4"
# mypy = "^1.9.0"

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
