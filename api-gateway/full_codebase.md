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
├── dependencies.py
├── main.py
├── routers
│   ├── __init__.py
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
from typing import Optional, Annotated, Dict, Any
import structlog
import httpx

# Importar verify_token del nuevo servicio de autenticación
from app.auth.auth_service import verify_token # <-- Asegúrate que la importación es correcta

# Importar get_client desde el módulo de dependencias centralizado
from app.dependencies import get_client

log = structlog.get_logger(__name__)

# Instancia del scheme HTTPBearer. auto_error=False para manejar manualmente la ausencia de token.
bearer_scheme = HTTPBearer(bearerFormat="JWT", auto_error=False)

async def _get_user_payload_internal(
    request: Request,
    # Usar Annotated para combinar Depends y el tipo
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)],
    require_company_id: bool # Parámetro interno para controlar la verificación
) -> Optional[Dict[str, Any]]:
    """
    Función interna para obtener y validar el payload JWT.
    Controla si se requiere company_id según el parámetro.
    Devuelve el payload si el token es válido y cumple el requisito de company_id.
    Devuelve None si no se proporciona token (cabecera Authorization ausente).
    Lanza HTTPException si el token existe pero es inválido (firma, exp, usuario no existe)
    o si falta company_id cuando es requerido (403).
    """
    # Limpiar estado previo si existiera para evitar contaminación entre requests
    if hasattr(request.state, 'user'):
        del request.state.user

    if authorization is None:
        # No hay cabecera Authorization: Bearer
        log.debug("No Authorization Bearer header found.")
        # No es un error aún, la dependencia que lo use decidirá si lo requiere
        return None

    token = authorization.credentials
    try:
        # Llamar a la función de verificación centralizada
        # Esta función ya maneja las excepciones 401 y 403 internamente
        payload = await verify_token(token, require_company_id=require_company_id)

        # Guardar payload en el estado de la request para posible uso posterior (ej. logging middleware)
        request.state.user = payload

        # Log de éxito más descriptivo
        log_msg = "Token verified successfully via internal getter"
        if require_company_id:
            log_msg += " (company_id required and present)"
        else:
            log_msg += " (company_id check passed or not required)"
        log.debug(log_msg, subject=payload.get('sub'), company_id=payload.get('company_id'))

        return payload
    except HTTPException as e:
        # verify_token lanza 401 (inválido) o 403 (falta company_id requerido)
        # Loguear el error específico antes de relanzar
        log_detail = getattr(e, 'detail', 'No detail provided')
        log.info(f"Token verification failed in dependency: {log_detail}",
                 status_code=e.status_code,
                 user_id=getattr(e, 'user_id', None)) # Loguear user_id si está disponible
        # Re-lanzar la excepción para que FastAPI la maneje y devuelva la respuesta HTTP correcta
        raise e
    except Exception as e:
        # Capturar errores inesperados *dentro* de verify_token que no sean HTTPException
        log.exception("Unexpected error during internal payload retrieval", error=str(e))
        # Devolver un error 500 genérico para no exponer detalles internos
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal error during authentication check."
        )

# --- Dependencia Estándar (Intenta obtener payload, REQUIERE company_id) ---
async def get_current_user_payload(
    request: Request,
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]
) -> Optional[Dict[str, Any]]:
    """
    Dependencia FastAPI para intentar validar el token JWT requiriendo company_id.
    - Devuelve el payload si el token es válido Y tiene company_id.
    - Devuelve None si no se proporciona token.
    - Lanza 401 si el token es inválido (firma, exp, usuario no existe).
    - Lanza 403 si el token es válido pero falta company_id.
    """
    try:
        # Llama a la función interna requiriendo company_id
        return await _get_user_payload_internal(request, authorization, require_company_id=True)
    except HTTPException as e:
        # Si _get_user_payload_internal lanza 401 o 403, simplemente relanzamos
        raise e
    except Exception as e:
         # Manejar cualquier otro error inesperado aquí también
         log.exception("Unexpected error in get_current_user_payload wrapper", error=str(e))
         raise HTTPException(status_code=500, detail="Internal Server Error in auth wrapper")

# --- Dependencia para la Asociación Inicial (NO requiere company_id) ---
async def get_initial_user_payload(
    request: Request,
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]
) -> Optional[Dict[str, Any]]:
    """
    Dependencia FastAPI para validar el token JWT sin requerir company_id.
    Útil para la ruta de asociación inicial de company_id.
    - Devuelve el payload si el token es válido (firma, exp, usuario existe), incluso sin company_id.
    - Devuelve None si no se proporciona token.
    - Lanza 401 si el token es inválido (firma, exp, usuario no existe).
    """
    try:
        # Llama a la función interna sin requerir company_id
        return await _get_user_payload_internal(request, authorization, require_company_id=False)
    except HTTPException as e:
        # Si _get_user_payload_internal lanza 401 (o 403, aunque no debería aquí), lo relanzamos
        raise e
    except Exception as e:
         # Manejar cualquier otro error inesperado aquí también
         log.exception("Unexpected error in get_initial_user_payload wrapper", error=str(e))
         raise HTTPException(status_code=500, detail="Internal Server Error in auth wrapper")

# --- Dependencia que Requiere Usuario Estricto (Falla si no hay token válido CON company_id) ---
async def require_user(
    # Depende de la función anterior (get_current_user_payload).
    # Si esa función devuelve None (sin token) o lanza una excepción (401/403),
    # esta dependencia manejará el caso o no se ejecutará.
    user_payload: Annotated[Optional[Dict[str, Any]], Depends(get_current_user_payload)]
) -> Dict[str, Any]:
    """
    Dependencia FastAPI que *asegura* que una ruta requiere un usuario autenticado
    con un token válido Y con company_id asociado.
    - Lanza 401 si no hay token o es inválido (lo hace la dependencia `get_current_user_payload`).
    - Lanza 403 si el token es válido pero falta company_id (lo hace la dependencia `get_current_user_payload`).
    - Si `get_current_user_payload` devuelve `None` (porque no se envió token), esta función lanzará 401.
    """
    if user_payload is None:
        # Esto ocurre si get_current_user_payload devolvió None (sin cabecera Authorization)
        log.info("Access denied by require_user: Authentication required but no token was provided.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"}, # Indica al cliente cómo autenticarse
        )
    # Si user_payload no es None, significa que get_current_user_payload tuvo éxito
    # (el token era válido y tenía company_id), así que simplemente lo devolvemos.
    # Las excepciones 401/403 ya habrían sido lanzadas por la dependencia anterior.
    return user_payload

# --- Dependencia que Requiere Usuario Inicial (Falla si no hay token válido, pero NO requiere company_id) ---
async def require_initial_user(
    # Depende de get_initial_user_payload.
    # Si esa función devuelve None (sin token) o lanza una excepción (401),
    # esta dependencia manejará el caso o no se ejecutará.
    user_payload: Annotated[Optional[Dict[str, Any]], Depends(get_initial_user_payload)]
) -> Dict[str, Any]:
    """
    Dependencia FastAPI que *asegura* que una ruta requiere un usuario autenticado
    con un token válido (firma, exp, usuario existe), pero NO requiere company_id.
    Útil para la ruta de asociación inicial de company_id.
    - Lanza 401 si no hay token o es inválido (lo hace la dependencia `get_initial_user_payload`).
    - Si `get_initial_user_payload` devuelve `None` (sin token), esta función lanzará 401.
    """
    if user_payload is None:
        # Esto ocurre si get_initial_user_payload devolvió None (sin cabecera Authorization)
        log.info("Access denied by require_initial_user: Authentication required but no token was provided.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Si user_payload no es None, significa que get_initial_user_payload tuvo éxito
    # (el token era válido), así que simplemente lo devolvemos.
    return user_payload

# --- Tipos anotados para usar en las rutas y mejorar la legibilidad ---
# StrictAuth: Requiere token válido con company_id
StrictAuth = Annotated[Dict[str, Any], Depends(require_user)]

# InitialAuth: Requiere token válido, pero company_id no es obligatorio
InitialAuth = Annotated[Dict[str, Any], Depends(require_initial_user)]
```

## File: `app\auth\auth_service.py`
```py
# File: app/auth/auth_service.py
# api-gateway/app/auth/auth_service.py
import uuid
import time
from typing import Dict, Any, Optional
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
    """
    log.debug("Attempting to authenticate user", email=email)
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
    log.info("User authenticated successfully", email=email, user_id=str(user_data.get('id')))
    # Eliminar hash de contraseña antes de devolver los datos
    user_data.pop('hashed_password', None)
    return user_data

# --- Creación de Tokens JWT ---

def create_access_token(
    user_id: uuid.UUID,
    email: str,
    company_id: Optional[uuid.UUID] = None,
    # full_name: Optional[str] = None, # Podrías añadir más claims si los necesitas
    # role: Optional[str] = None,
    expires_delta: timedelta = timedelta(days=1) # Tiempo de expiración configurable
) -> str:
    """
    Crea un token JWT para el usuario autenticado.
    Incluye 'sub' (user_id), 'exp', 'iat', 'email' y opcionalmente 'company_id'.
    """
    expire = datetime.now(timezone.utc) + expires_delta
    # Usar datetime.now(timezone.utc) para iat también
    issued_at = datetime.now(timezone.utc)

    to_encode: Dict[str, Any] = {
        "sub": str(user_id),         # Subject (ID de usuario)
        "exp": expire,               # Expiration Time
        "iat": issued_at,            # Issued At
        "email": email,              # Email del usuario
        # "aud": "authenticated",    # Audiencia (opcional, si tus servicios la verifican)
        # "iss": "AtenexAuth",       # Emisor (opcional)
    }

    # Añadir company_id si está presente
    if company_id:
        to_encode["company_id"] = str(company_id)

    # Codificar el token usando el secreto y algoritmo de la configuración
    try:
        encoded_jwt = jwt.encode(
            to_encode,
            settings.JWT_SECRET.get_secret_value(), # Obtener valor del SecretStr
            algorithm=settings.JWT_ALGORITHM
        )
        log.debug("Access token created", user_id=str(user_id), expires_at=expire.isoformat())
        return encoded_jwt
    except JOSEError as e:
        log.exception("Failed to encode JWT", error=str(e), user_id=str(user_id))
        # En un caso real, esto es un error interno grave
        raise HTTPException(status_code=500, detail="Could not create access token")


# --- Verificación de Tokens JWT ---

async def verify_token(token: str, require_company_id: bool = True) -> Dict[str, Any]:
    """
    Verifica la validez de un token JWT (firma, expiración, claims básicos).
    Opcionalmente requiere la presencia de un 'company_id' válido en el payload.
    Verifica que el usuario ('sub') exista en la base de datos.

    Returns:
        El payload decodificado si el token es válido.

    Raises:
        HTTPException(401): Si el token es inválido (firma, exp, formato) o el usuario no existe.
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
    internal_error_exception = HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="An internal error occurred during token verification.",
    )

    # Verificación de seguridad del secreto (ya hecha en config, pero doble check no daña)
    if not settings.JWT_SECRET or settings.JWT_SECRET.get_secret_value() == "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME_IN_ENV_OR_SECRET":
         log.critical("FATAL: Attempting JWT verification with default or missing GATEWAY_JWT_SECRET!")
         raise internal_error_exception

    try:
        # Decodificar el token (valida firma, exp)
        payload = jwt.decode(
            token,
            settings.JWT_SECRET.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_nbf": True, # Si usas 'not before'
                # "verify_aud": False # No verificamos audiencia específica por defecto
            }
        )
        log.debug("Token decoded successfully", payload_keys=list(payload.keys()))

        # Validar 'sub' (user_id)
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

        # Verificar que el usuario existe y está activo en la DB
        # Esto previene usar tokens de usuarios eliminados o desactivados
        user_db_data = await postgres_client.get_user_by_id(user_id)
        if not user_db_data:
            log.warning("Token verification failed: User specified in 'sub' not found in DB.", user_id=user_id_str)
            credentials_exception.detail = "User associated with token not found."
            raise credentials_exception
        if not user_db_data.get('is_active', False):
            log.warning("Token verification failed: User specified in 'sub' is inactive.", user_id=user_id_str)
            credentials_exception.detail = "User associated with token is inactive."
            raise credentials_exception

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
                # Considerar si un company_id inválido debe ser 401 o 403
                # Vamos a tratarlo como inválido (401), ya que el token está malformado en ese aspecto
                credentials_exception.detail = "Invalid company ID format in token."
                raise credentials_exception

        if require_company_id and not valid_company_id_present:
            log.info("Token verification failed: Required 'company_id' is missing.", user_id=user_id_str)
            raise forbidden_exception # Falla porque se requiere y no está

        # Si pasa todas las verificaciones, el token es válido
        log.info("Token verified successfully", user_id=user_id_str, company_id=company_id_str or "N/A")
        return payload # Devolver el payload completo

    except ExpiredSignatureError:
        log.info("Token verification failed: Token has expired.")
        credentials_exception.detail = "Token has expired."
        credentials_exception.headers["WWW-Authenticate"] = 'Bearer error="invalid_token", error_description="The token has expired"'
        raise credentials_exception
    except JWTError as e:
        # Otros errores de JWT (firma inválida, formato incorrecto, etc.)
        log.warning(f"JWT Verification Error: {e}", token_provided=True)
        credentials_exception.detail = f"Token validation failed: {e}"
        raise credentials_exception
    except HTTPException as e:
        # Re-lanzar excepciones HTTP ya manejadas (como 403 por company_id)
        raise e
    except Exception as e:
        # Capturar cualquier otro error inesperado (ej. error de DB en get_user_by_id)
        log.exception(f"Unexpected error during token verification: {e}", user_id=payload.get('sub') if 'payload' in locals() else 'unknown')
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

# URLs por defecto si no se especifican en el entorno (usando el namespace 'atenex-develop')
K8S_INGEST_SVC_URL_DEFAULT = "http://ingest-api-service.atenex-develop.svc.cluster.local:80"
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
                 await conn.set_type_codec(
                     'jsonb',
                     encoder=_json_encoder,
                     decoder=_json_decoder,
                     schema='pg_catalog'
                 )
                 await conn.set_type_codec(
                      'json',
                      encoder=_json_encoder,
                      decoder=_json_decoder,
                      schema='pg_catalog'
                  )

            _pool = await asyncpg.create_pool(
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(), # Obtener valor del SecretStr
                database=settings.POSTGRES_DB,
                host=settings.POSTGRES_SERVER,
                port=settings.POSTGRES_PORT,
                min_size=5,   # Ajusta según necesidad
                max_size=20,  # Ajusta según necesidad
                statement_cache_size=0, # Deshabilitar caché para evitar problemas con tipos dinámicos
                init=init_connection # Añadir inicializador para codecs JSON/JSONB
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
    Alineado con el esquema USERS.
    """
    pool = await get_db_pool()
    query = """
        SELECT id, company_id, email, hashed_password, full_name, role,
               created_at, last_login, is_active
        FROM users
        WHERE lower(email) = lower($1)
    """
    # No filtramos por is_active aquí, la lógica de autenticación puede decidir
    log.debug("Executing get_user_by_email query", email=email)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, email)
        if row:
            log.debug("User found by email", user_id=str(row['id']))
            return dict(row) # Convertir asyncpg.Record a dict
        else:
            log.debug("User not found by email", email=email)
            return None
    except Exception as e:
        log.error("Error getting user by email", error=str(e), email=email, exc_info=True)
        raise # Relanzar para manejo de errores superior

async def get_user_by_id(user_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """
    Recupera un usuario por su ID (UUID).
    Devuelve un diccionario con los datos del usuario o None si no se encuentra.
    Alineado con el esquema USERS. Excluye la contraseña hash por seguridad.
    """
    pool = await get_db_pool()
    query = """
        SELECT id, company_id, email, full_name, role,
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
            return dict(row)
        else:
            log.debug("User not found by ID", user_id=str(user_id))
            return None
    except Exception as e:
        log.error("Error getting user by ID", error=str(e), user_id=str(user_id), exc_info=True)
        raise

async def update_user_company(user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    """
    Actualiza el company_id para un usuario específico y actualiza last_login.
    Devuelve True si la actualización fue exitosa (al menos una fila afectada), False en caso contrario.
    Alineado con el esquema USERS.
    """
    pool = await get_db_pool()
    query = """
        UPDATE users
        SET company_id = $2, updated_at = NOW()
        WHERE id = $1
        RETURNING id -- Devolvemos el ID para confirmar la actualización
    """
    log.debug("Executing update_user_company query", user_id=str(user_id), company_id=str(company_id))
    try:
        async with pool.acquire() as conn:
            # Usar fetchval para obtener el ID devuelto o None
            result = await conn.fetchval(query, user_id, company_id)
        if result is not None:
            log.info("User company updated successfully", user_id=str(user_id), new_company_id=str(company_id))
            return True
        else:
            # Esto podría suceder si el user_id no existe
            log.warning("Update user company command executed but no rows were affected.", user_id=str(user_id))
            return False
    except Exception as e:
        log.error("Error updating user company", error=str(e), user_id=str(user_id), company_id=str(company_id), exc_info=True)
        raise

```

## File: `app\dependencies.py`
```py
# app/dependencies.py
import httpx
from fastapi import HTTPException, status
import structlog
from typing import Optional

# Importar el cliente global desde main
from app.main import proxy_http_client

log = structlog.get_logger("atenex_api_gateway.dependencies")

def get_client() -> httpx.AsyncClient:
    """Devuelve el cliente HTTPX global inicializado en lifespan."""
    if proxy_http_client is None or proxy_http_client.is_closed:
        log.error("API Gateway HTTP client is not available or closed.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal gateway dependency (HTTP Client) unavailable."
        )
    return proxy_http_client

```

## File: `app\main.py`
```py
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
        log.info("HTTPX client initialized successfully.", limits=str(limits), timeout=str(timeout))
    except Exception as e:
        log.exception("CRITICAL: Failed to initialize HTTPX client during startup!", error=str(e))
        proxy_http_client = None

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
```

## File: `app\routers\__init__.py`
```py

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
# api-gateway/app/routers/gateway_router.py
from fastapi import APIRouter, Request, Response, Depends, HTTPException, status, Path
from fastapi.responses import JSONResponse, StreamingResponse
from typing import Optional, Annotated, Dict, Any, List, Union
import httpx
import structlog
import asyncio
import uuid
import json

from app.core.config import settings
# Usar las dependencias de autenticación directamente donde se necesiten
from app.auth.auth_middleware import StrictAuth
from app.dependencies import get_client

log = structlog.get_logger("atenex_api_gateway.router.gateway")

# Inyectar httpx client vía dependencia
HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_client)]

# Instancia del Router
router = APIRouter(prefix="/api/v1", tags=["Gateway Proxy (Explicit Calls)"]) # Cambiar tags

# --- Helper para construir headers comunes ---
def _prepare_forwarded_headers(request: Request, user_payload: Dict[str, Any]) -> Dict[str, str]:
    """Prepara los headers para reenviar, incluyendo X-User-ID y X-Company-ID."""
    headers_to_forward = {}
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))

    # Headers de contexto de proxy estándar
    client_host = request.client.host if request.client else "unknown"
    x_forwarded_for = request.headers.get("x-forwarded-for", client_host)
    headers_to_forward["X-Forwarded-For"] = x_forwarded_for
    headers_to_forward["X-Forwarded-Proto"] = request.url.scheme
    if "host" in request.headers:
        headers_to_forward["X-Forwarded-Host"] = request.headers["host"]
    headers_to_forward["X-Request-ID"] = request_id

    # Añadir Headers de Autenticación/Contexto
    user_id = user_payload.get('sub')
    company_id = user_payload.get('company_id')
    user_email = user_payload.get('email')

    if not user_id or not company_id:
        log.critical("Gateway Internal Error: User payload missing sub or company_id after auth!", payload_keys=list(user_payload.keys()))
        raise HTTPException(status_code=500, detail="Internal context error")

    headers_to_forward['X-User-ID'] = str(user_id)
    headers_to_forward['X-Company-ID'] = str(company_id)
    if user_email:
        headers_to_forward['X-User-Email'] = str(user_email)

    # Añadir Accept header si existe
    if "accept" in request.headers:
        headers_to_forward["Accept"] = request.headers["accept"]

    # **NO** añadir Content-Type aquí por defecto, se maneja por endpoint
    return headers_to_forward

# --- Helper para manejar la respuesta del backend ---
async def _handle_backend_response(backend_response: httpx.Response, request_id: str) -> Response:
    """Procesa la respuesta del backend y la convierte en una respuesta FastAPI."""
    response_log = log.bind(request_id=request_id, backend_status=backend_response.status_code)

    content_type = backend_response.headers.get("content-type", "application/octet-stream")
    response_log.debug("Handling backend response", content_type=content_type)

    # Preparar headers de respuesta
    response_headers = {
        name: value for name, value in backend_response.headers.items()
        # Excluir hop-by-hop headers de la respuesta del backend
        if name.lower() not in [
            "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
            "te", "trailers", "transfer-encoding", "upgrade", "content-encoding" # Excluir content-length aquí también
        ]
    }
    response_headers["X-Request-ID"] = request_id # Propagar Request ID

    # Leer contenido - NO USAR STREAMINGRESPONSE a menos que sea necesario y se maneje
    try:
        # Leer el cuerpo completo. Ideal para JSON/texto/binarios pequeños.
        content_bytes = await backend_response.aread()
        # Actualizar content-length basado en lo leído
        response_headers["Content-Length"] = str(len(content_bytes))
        response_log.debug("Backend response body read", body_length=len(content_bytes))

    except Exception as read_err:
        response_log.error("Failed to read backend response body", error=str(read_err))
        # Devolver error genérico si no podemos leer la respuesta
        return JSONResponse(
             content={"detail": "Error reading response from upstream service"},
             status_code=status.HTTP_502_BAD_GATEWAY,
             headers={"X-Request-ID": request_id}
        )
    finally:
        # Siempre cerrar la respuesta del backend
        await backend_response.aclose()


    return Response(
        content=content_bytes,
        status_code=backend_response.status_code,
        headers=response_headers,
        media_type=content_type
    )

# --- Helper para manejar errores HTTPX ---
def _handle_httpx_error(exc: Exception, target_url: str, request_id: str):
    error_log = log.bind(request_id=request_id, target_url=target_url)
    if isinstance(exc, httpx.TimeoutException):
        error_log.error("Gateway timeout calling backend service", exc_info=False)
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Upstream service timed out.")
    elif isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        error_log.error("Gateway connection error calling backend service", error=str(exc), exc_info=False)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not connect to upstream service.")
    elif isinstance(exc, httpx.HTTPStatusError): # Errores 4xx/5xx devueltos por el backend
        error_log.warning("Backend service returned error status", status_code=exc.response.status_code, response_preview=exc.response.text[:200])
        # Reintentar devolver la respuesta original si es posible
        try:
            content = exc.response.json()
        except json.JSONDecodeError:
            content = {"detail": exc.response.text or f"Backend returned status {exc.response.status_code}"}
        raise HTTPException(status_code=exc.response.status_code, detail=content)
    else: # Otros errores httpx o inesperados
        error_log.exception("Unexpected error during backend call")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="An unexpected error occurred communicating with upstream service.")

# --- Endpoints Query Service (Llamadas Explícitas) ---

@router.post("/query/ask", summary="Ask a question (Query Service)", description="Forwards query to Query Service via POST")
async def query_service_ask(
    request: Request,
    user_payload: StrictAuth,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
):
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    endpoint_log = log.bind(request_id=request_id)
    endpoint_log.info("Forwarding request to Query Service POST /ask")

    target_url = f"{settings.QUERY_SERVICE_URL}{request.url.path}" # request.url.path incluye /api/v1/query/ask
    headers = _prepare_forwarded_headers(request, user_payload)

    try:
        request_body = await request.json()
        headers["Content-Type"] = "application/json" # Establecer content type
        endpoint_log.debug("Request body parsed for forwarding", body=request_body)
    except json.JSONDecodeError:
        endpoint_log.warning("Failed to parse request body as JSON")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body")

    try:
        backend_response = await client.post(target_url, headers=headers, json=request_body)
        return await _handle_backend_response(backend_response, request_id)
    except Exception as exc:
        _handle_httpx_error(exc, target_url, request_id)


@router.get("/query/chats", summary="List chats (Query Service)", description="Forwards GET request to Query Service")
async def query_service_get_chats(
    request: Request,
    user_payload: StrictAuth,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    # Query params se pasan directamente
    limit: int = 50,
    offset: int = 0,
):
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    endpoint_log = log.bind(request_id=request_id)
    endpoint_log.info("Forwarding request to Query Service GET /chats")

    target_url = f"{settings.QUERY_SERVICE_URL}{request.url.path}" # includes /api/v1/query/chats
    headers = _prepare_forwarded_headers(request, user_payload)
    params = request.query_params # Pasa todos los query params originales

    try:
        backend_response = await client.get(target_url, headers=headers, params=params)
        return await _handle_backend_response(backend_response, request_id)
    except Exception as exc:
        _handle_httpx_error(exc, target_url, request_id)


@router.get("/query/chats/{chat_id}/messages", summary="Get chat messages (Query Service)", description="Forwards GET request for messages")
async def query_service_get_chat_messages(
    request: Request,
    user_payload: StrictAuth,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    chat_id: uuid.UUID = Path(...),
    # Query params se pasan directamente
    limit: int = 100,
    offset: int = 0,
):
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    endpoint_log = log.bind(request_id=request_id, chat_id=str(chat_id))
    endpoint_log.info("Forwarding request to Query Service GET /chats/{chat_id}/messages")

    target_url = f"{settings.QUERY_SERVICE_URL}{request.url.path}" # incluye el path completo
    headers = _prepare_forwarded_headers(request, user_payload)
    params = request.query_params # Pasa todos los query params originales (limit, offset)

    try:
        backend_response = await client.get(target_url, headers=headers, params=params)
        return await _handle_backend_response(backend_response, request_id)
    except Exception as exc:
        _handle_httpx_error(exc, target_url, request_id)


@router.delete("/query/chats/{chat_id}", summary="Delete chat (Query Service)", description="Forwards DELETE request")
async def query_service_delete_chat(
    request: Request,
    user_payload: StrictAuth,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    chat_id: uuid.UUID = Path(...),
):
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    endpoint_log = log.bind(request_id=request_id, chat_id=str(chat_id))
    endpoint_log.info("Forwarding request to Query Service DELETE /chats/{chat_id}")

    target_url = f"{settings.QUERY_SERVICE_URL}{request.url.path}"
    headers = _prepare_forwarded_headers(request, user_payload)

    try:
        backend_response = await client.delete(target_url, headers=headers)
        # DELETE a menudo devuelve 204 No Content, manejarlo
        if backend_response.status_code == status.HTTP_204_NO_CONTENT:
            await backend_response.aclose() # Asegurarse de cerrar
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        else:
            # Si devuelve otro código, manejar como respuesta normal
            return await _handle_backend_response(backend_response, request_id)
    except Exception as exc:
        _handle_httpx_error(exc, target_url, request_id)


# --- Endpoints Ingest Service (Llamadas Explícitas) ---

@router.post("/ingest/upload", summary="Upload document (Ingest Service)", description="Forwards file upload using multipart/form-data")
async def ingest_service_upload(
    request: Request,
    user_payload: StrictAuth,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
):
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    endpoint_log = log.bind(request_id=request_id)
    endpoint_log.info("Forwarding request to Ingest Service POST /upload")

    target_url = f"{settings.INGEST_SERVICE_URL}{request.url.path}" # incluye /api/v1/ingest/upload
    headers = _prepare_forwarded_headers(request, user_payload)

    # Reenviar el header Content-Type ORIGINAL, ya que contiene el boundary
    content_type = request.headers.get('content-type')
    if not content_type or 'multipart/form-data' not in content_type:
         endpoint_log.warning("Missing or invalid Content-Type for upload")
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Content-Type header for multipart/form-data is required.")
    headers['Content-Type'] = content_type
    # Eliminar Transfer-Encoding si existe (chunked), ya que leeremos el cuerpo completo
    headers.pop('transfer-encoding', None)

    try:
        # Leer el cuerpo completo tal cual (bytes)
        body_bytes = await request.body()
        # Actualizar Content-Length, aunque httpx puede hacerlo
        headers['Content-Length'] = str(len(body_bytes))
        endpoint_log.debug("Read multipart body for forwarding", body_length=len(body_bytes))
    except Exception as read_err:
        endpoint_log.error("Failed to read request body for upload", error=str(read_err))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Could not read upload body.")

    try:
        backend_response = await client.post(
            target_url,
            headers=headers,
            content=body_bytes # Pasar los bytes directamente
        )
        return await _handle_backend_response(backend_response, request_id)
    except Exception as exc:
        _handle_httpx_error(exc, target_url, request_id)


@router.get("/ingest/status/{document_id}", summary="Get document status (Ingest Service)", description="Forwards GET request for document status")
async def ingest_service_get_status(
    request: Request,
    user_payload: StrictAuth,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    document_id: uuid.UUID = Path(...),
):
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    endpoint_log = log.bind(request_id=request_id, document_id=str(document_id))
    endpoint_log.info("Forwarding request to Ingest Service GET /status/{document_id}")

    target_url = f"{settings.INGEST_SERVICE_URL}{request.url.path}"
    headers = _prepare_forwarded_headers(request, user_payload)

    try:
        backend_response = await client.get(target_url, headers=headers)
        return await _handle_backend_response(backend_response, request_id)
    except Exception as exc:
        _handle_httpx_error(exc, target_url, request_id)


@router.get("/ingest/status", summary="List document statuses (Ingest Service)", description="Forwards GET request to list statuses")
async def ingest_service_list_statuses(
    request: Request,
    user_payload: StrictAuth,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    limit: int = 100,
    offset: int = 0,
):
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    endpoint_log = log.bind(request_id=request_id)
    endpoint_log.info("Forwarding request to Ingest Service GET /status")

    target_url = f"{settings.INGEST_SERVICE_URL}{request.url.path}"
    headers = _prepare_forwarded_headers(request, user_payload)
    params = request.query_params # Pasa limit, offset, etc.

    try:
        backend_response = await client.get(target_url, headers=headers, params=params)
        return await _handle_backend_response(backend_response, request_id)
    except Exception as exc:
        _handle_httpx_error(exc, target_url, request_id)


# --- Rutas de Healthcheck y Raíz (si estaban aquí) ---
# No se suelen proxyar, pero las dejamos aquí si tu router anterior las contenía

# Ejemplo (ya definido en main.py, probablemente eliminar de aquí):
# @router.get("/", include_in_schema=False)
# async def gateway_root():
#     return {"message": "Gateway Router Root"}
```

## File: `app\routers\user_router.py`
```py
# File: app/routers/user_router.py
# api-gateway/app/routers/user_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Request, Body
from typing import Dict, Any, Optional, Annotated # Asegúrate de importar Annotated
import structlog
import uuid

# Importar dependencias de autenticación y DB
from app.auth.auth_middleware import InitialAuth # Para ensure-company
from app.auth.auth_service import authenticate_user, create_access_token
from app.db import postgres_client
from app.core.config import settings

# Importar modelos Pydantic para request/response
from pydantic import BaseModel, EmailStr, Field, validator

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["Users & Authentication"]) # Agrupamos aquí

# --- Modelos Pydantic para la API ---

class LoginRequest(BaseModel):
    """Payload esperado para el login."""
    email: EmailStr
    password: str = Field(..., min_length=6) # Ajusta min_length si es necesario

class LoginResponse(BaseModel):
    """Respuesta devuelta en un login exitoso."""
    access_token: str
    token_type: str = "bearer"
    user_id: str # UUID como string
    email: EmailStr
    full_name: Optional[str] = None
    role: Optional[str] = "user" # Rol por defecto
    company_id: Optional[str] = None # UUID como string, puede ser None

class EnsureCompanyRequest(BaseModel):
    """Payload opcional para forzar una compañía específica en ensure-company."""
    company_id: Optional[str] = None # UUID como string

    @validator('company_id')
    def validate_company_id_format(cls, v):
        if v is not None:
            try:
                uuid.UUID(v)
            except ValueError:
                raise ValueError("Provided company_id is not a valid UUID")
        return v

class EnsureCompanyResponse(BaseModel):
    """Respuesta devuelta al asociar/confirmar compañía."""
    user_id: str # UUID como string
    company_id: str # UUID como string (la que quedó asociada)
    message: str
    # Devolvemos el nuevo token para que el frontend lo use inmediatamente
    new_access_token: str
    token_type: str = "bearer"


# --- Endpoints ---

@router.post("/login", response_model=LoginResponse)
async def login_for_access_token(login_data: LoginRequest):
    """
    Autentica un usuario con email y contraseña.
    Si es exitoso, devuelve un token JWT y datos básicos del usuario.
    """
    log.info("Login attempt initiated", email=login_data.email)
    user = await authenticate_user(login_data.email, login_data.password)

    if not user:
        log.warning("Login failed: Invalid credentials or inactive user", email=login_data.email)
        # Devolver error genérico para no dar pistas sobre si el email existe
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Extraer datos del usuario autenticado para el token y la respuesta
    user_id = user.get("id")
    company_id = user.get("company_id") # Puede ser None
    email = user.get("email")
    full_name = user.get("full_name")
    role = user.get("role", "user") # Default a 'user' si no está en DB

    if not user_id or not email:
         log.error("Critical error: Authenticated user data missing ID or email", user_dict_keys=user.keys())
         raise HTTPException(status_code=500, detail="Internal server error during login")

    # Crear el token JWT
    access_token = create_access_token(
        user_id=user_id,
        email=email,
        company_id=company_id # Pasar company_id (puede ser None)
    )
    log.info("Login successful, token generated", user_id=str(user_id), company_id=str(company_id) if company_id else "None")

    # Devolver la respuesta
    return LoginResponse(
        access_token=access_token,
        user_id=str(user_id),
        email=email,
        full_name=full_name,
        role=role,
        company_id=str(company_id) if company_id else None
    )


@router.post("/me/ensure-company", response_model=EnsureCompanyResponse)
async def ensure_company_association(
    request: Request, # Necesitamos la request para el log
    # *** CORRECCIÓN: Parámetro sin default va primero ***
    # La dependencia se define únicamente a través de la anotación InitialAuth
    user_payload: InitialAuth,
    # Cuerpo opcional para especificar company_id, default a vacío si no se envía
    ensure_request: Optional[EnsureCompanyRequest] = Body(None)
):
    """
    Endpoint para que un usuario autenticado (con token válido)
    se asocie a una compañía si aún no lo está.
    1. Usa la company_id del body si se proporciona.
    2. Si no, usa la company_id por defecto de la configuración.
    3. Si ya tiene una company_id y no se especifica una nueva, no hace nada.
    4. Si se asocia/cambia, actualiza la DB y genera un NUEVO token con la company_id.
    """
    user_id_str = user_payload.get("sub")
    req_id = getattr(request.state, 'request_id', 'N/A') # Obtener request_id para logs
    log_ctx = log.bind(request_id=req_id, user_id=user_id_str)

    log_ctx.info("Ensure company association requested.")

    if not user_id_str:
        log_ctx.error("Ensure company failed: User ID ('sub') missing in token payload.")
        raise HTTPException(status_code=400, detail="User ID not found in token payload.")

    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        log_ctx.error("Ensure company failed: User ID ('sub') in token is not a valid UUID.", sub_value=user_id_str)
        raise HTTPException(status_code=400, detail="Invalid user ID format in token.")

    # Obtener datos actuales del usuario desde la DB (incluye email, full_name para el nuevo token)
    current_user_data = await postgres_client.get_user_by_id(user_id)
    if not current_user_data:
        # Esto no debería pasar si verify_token funciona, pero es una salvaguarda
        log_ctx.error("Ensure company failed: User found in token but not in database.", user_id=user_id_str)
        raise HTTPException(status_code=404, detail="User associated with token not found in database.")

    current_company_id = current_user_data.get("company_id")
    target_company_id_str: Optional[str] = None
    action_taken = "none"

    # Determinar el company_id objetivo
    if ensure_request and ensure_request.company_id:
        target_company_id_str = ensure_request.company_id
        log_ctx.info("Using company_id provided in request body.", target_company=target_company_id_str)
    elif not current_company_id and settings.DEFAULT_COMPANY_ID:
        target_company_id_str = settings.DEFAULT_COMPANY_ID
        log_ctx.info("User has no company_id, using default from settings.", default_company=target_company_id_str)
    elif current_company_id:
        # Ya tiene compañía y no se pidió cambiarla explícitamente
        target_company_id_str = str(current_company_id) # Usar la actual
        log_ctx.info("User already associated with a company, no change requested.", current_company=target_company_id_str)
    else:
        # No tiene compañía, no se proporcionó una, y no hay default configurado
        log_ctx.error("Ensure company failed: No target company_id provided or configured.", user_id=user_id_str)
        raise HTTPException(
            status_code=400,
            detail="Cannot associate company: No company ID provided and no default is configured for the gateway."
        )

    # Validar el formato del target_company_id_str
    try:
        target_company_id = uuid.UUID(target_company_id_str)
    except ValueError:
        log_ctx.error("Ensure company failed: Target company ID is not a valid UUID.", target_value=target_company_id_str)
        raise HTTPException(status_code=400, detail="Invalid target company ID format.")

    # Actualizar la DB solo si el target_company_id es diferente del actual (o si el actual es None)
    if target_company_id != current_company_id:
        log_ctx.info("Attempting to update user's company in database.", new_company_id=str(target_company_id))
        updated = await postgres_client.update_user_company(user_id, target_company_id)
        if not updated:
            # Podría fallar si el usuario fue eliminado entre get y update, o error DB
            log_ctx.error("Failed to update user's company association in database.", user_id=user_id_str)
            raise HTTPException(status_code=500, detail="Failed to update user's company association.")
        action_taken = "updated"
        log_ctx.info("User company association updated successfully in database.", new_company_id=str(target_company_id))
    else:
        action_taken = "confirmed"
        log_ctx.info("User company association already matches target, no database update needed.", company_id=str(target_company_id))

    # Generar un *nuevo* token JWT con la company_id (ya sea la actualizada o la confirmada)
    # Usar los datos recuperados de la DB para otros claims
    user_email = current_user_data.get("email")
    user_full_name = current_user_data.get("full_name")
    if not user_email:
         log_ctx.error("Critical error: User data from DB missing email, cannot generate new token.", user_id=user_id_str)
         raise HTTPException(status_code=500, detail="Internal server error generating updated token.")

    new_access_token = create_access_token(
        user_id=user_id,
        email=user_email,
        company_id=target_company_id # ¡Asegurarse de usar el target_company_id!
        # Podrías añadir full_name, role aquí si los incluyes en create_access_token
    )
    log_ctx.info("New access token generated with company association.", company_id=str(target_company_id))

    # Determinar mensaje de respuesta
    if action_taken == "updated":
        message = f"Company association successfully updated to {target_company_id}."
    elif action_taken == "confirmed":
        message = f"Company association confirmed as {target_company_id}."
    else: # action_taken == "none" (no debería llegar aquí si la lógica es correcta)
        message = f"User already associated with company {target_company_id}."


    return EnsureCompanyResponse(
        user_id=str(user_id),
        company_id=str(target_company_id),
        message=message,
        new_access_token=new_access_token
    )
```

## File: `pyproject.toml`
```toml
# File: pyproject.toml
# api-gateway/pyproject.toml
[tool.poetry]
name = "atenex-api-gateway"
version = "1.0.1"
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

# --- CORRECCIÓN DEFINITIVA: Usar la versión con extras y eliminar la simple ---
# Cliente HTTP asíncrono
# httpx = "^0.27.0" # <-- ELIMINAR ESTA LÍNEA SIMPLE
httpx = {extras = ["http2"], version = "^0.27.0"} # <-- MANTENER/AÑADIR ESTA CON EXTRAS

# Manejo de JWT
python-jose = {extras = ["cryptography"], version = "^3.3.0"}

# Logging estructurado
structlog = "^24.1.0"

# Cliente PostgreSQL Asíncrono
asyncpg = "^0.29.0"

# Hashing de Contraseñas
passlib = {extras = ["bcrypt"], version = "^1.7.4"}

# Dependencia necesaria para httpx[http2]
h2 = "^4.1.0" # <-- MANTENER (o añadir si faltaba)

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
