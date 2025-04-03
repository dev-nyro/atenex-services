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
│   ├── auth_router.py
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
# File: app/auth/auth_middleware.py
# api-gateway/app/auth/auth_middleware.py
from fastapi import Request, HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional, Annotated, Dict, Any
import structlog

# Importar verify_token CON el parámetro require_company_id
from .jwt_handler import verify_token

log = structlog.get_logger(__name__)
# Crear instancia del scheme. auto_error=False para manejar manualmente la ausencia de token
bearer_scheme = HTTPBearer(bearerFormat="JWT", auto_error=False)

async def _get_user_payload_internal(
    request: Request,
    # Usar Annotated para combinar Depends y el tipo
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)],
    require_company_id: bool # Parámetro interno para controlar la verificación
) -> Optional[Dict[str, Any]]:
    """
    Función interna para obtener y validar el payload, controlando si se requiere company_id.
    Devuelve el payload si es válido según los criterios, None si no hay token,
    y lanza HTTPException si el token existe pero es inválido.
    """
    # Limpiar estado previo si existiera
    request.state.user = None

    if authorization is None:
        # No hay cabecera Authorization: Bearer
        log.debug("No Authorization Bearer header found.")
        # No es un error aún, la ruta decidirá si lo requiere
        return None

    token = authorization.credentials
    try:
        # Pasar require_company_id a verify_token
        payload = verify_token(token, require_company_id=require_company_id)
        # Guardar payload en el estado de la request para posible uso posterior
        request.state.user = payload
        log_msg = "Token verified" + (" (company_id required and present)" if require_company_id and 'company_id' in payload else " (company_id check passed/not required)")
        log.debug(log_msg, subject=payload.get('sub'), company_id=payload.get('company_id'))
        return payload
    except HTTPException as e:
        # verify_token lanza 401 (inválido) o 403 (falta company_id requerido)
        log.info(f"Token verification failed in dependency: {e.detail}", status_code=e.status_code, user_id=getattr(e, 'user_id', None)) # Loggear detalle
        # Re-lanzar la excepción para que FastAPI la maneje
        raise e
    except Exception as e:
        # Capturar errores inesperados de verify_token
        log.exception("Unexpected error during internal payload retrieval", error=str(e))
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
    Dependencia FastAPI para intentar validar el token JWT (requiriendo company_id).
    Devuelve el payload si es válido Y tiene company_id.
    Devuelve None si no hay token.
    Lanza 401/403 si el token es inválido o falta company_id.
    """
    try:
        # Llama a la función interna requiriendo company_id
        return await _get_user_payload_internal(request, authorization, require_company_id=True)
    except HTTPException as e:
        # Si _get_user_payload_internal lanza 401/403, lo relanzamos
        raise e
    except Exception as e:
         # Manejar cualquier otro error inesperado aquí también
         log.exception("Unexpected error in get_current_user_payload wrapper", error=str(e))
         raise HTTPException(status_code=500, detail="Internal Server Error in auth wrapper")


# --- Dependencia que Requiere Usuario Estricto (Falla si no hay token válido CON company_id) ---
async def require_user(
    # Depende de la función anterior. Si esa función lanza excepción, esta no se ejecuta.
    # Si devuelve None (sin token), esta dependencia lanzará 401.
    user_payload: Annotated[Optional[Dict[str, Any]], Depends(get_current_user_payload)]
) -> Dict[str, Any]:
    """
    Dependencia FastAPI que *asegura* que una ruta requiere un usuario autenticado
    con un token válido Y con company_id asociado.
    Lanza 401 si no hay token o es inválido.
    Lanza 403 si el token es válido pero falta company_id.
    """
    if user_payload is None:
        # Esto ocurre si get_current_user_payload devolvió None (sin token)
        # o si _get_user_payload_internal devolvió None (sin token)
        log.info("Access denied by require_user: Authentication required but no valid token was found or provided.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"}, # Indica al cliente cómo autenticarse
        )
    # Si get_current_user_payload lanzó 403 (falta company_id), la ejecución ya se detuvo allí.
    # Si llegamos aquí, user_payload es un diccionario válido con company_id.
    log.debug("User requirement met (StrictAuth: token valid with company_id).", subject=user_payload.get('sub'), company_id=user_payload.get('company_id'))
    return user_payload # Devolver el payload para uso en la ruta

# --- Dependencia que Requiere Autenticación pero NO company_id ---
async def require_authenticated_user_no_company_check(
    request: Request,
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]
) -> Dict[str, Any]:
    """
    Dependencia FastAPI que asegura que el usuario está autenticado (token válido:
    firma, exp, aud) pero NO requiere que el company_id esté presente en el token.
    Útil para endpoints como el de asociación inicial de compañía.
    Lanza 401 si no hay token o es inválido.
    """
    try:
        # Llama a la función interna SIN requerir company_id
        payload = await _get_user_payload_internal(request, authorization, require_company_id=False)

        if payload is None:
            # Si no hay token, lanzar 401
            log.info("Access denied by require_authenticated_user_no_company_check: Authentication required but no token provided.")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        # Si había token pero era inválido (firma, exp, aud), _get_user_payload_internal ya lanzó 401.
        # Si llegamos aquí, el token es válido según los criterios básicos.
        log.debug("User requirement met (InitialAuth: token valid, company_id not checked).", subject=payload.get('sub'), company_id=payload.get('company_id', 'N/A'))
        return payload
    except HTTPException as e:
        # Re-lanzar 401 si el token era inválido
        raise e
    except Exception as e:
        log.exception("Unexpected error in require_authenticated_user_no_company_check", error=str(e))
        raise HTTPException(status_code=500, detail="Internal Server Error during initial auth check")

# Alias para claridad en las dependencias de las rutas
InitialAuth = Annotated[Dict[str, Any], Depends(require_authenticated_user_no_company_check)]
StrictAuth = Annotated[Dict[str, Any], Depends(require_user)]
```

## File: `app\auth\jwt_handler.py`
```py
# File: app/auth/jwt_handler.py
# api-gateway/app/auth/jwt_handler.py
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from jose import JWTError, jwt
from fastapi import HTTPException, status
import structlog
import uuid # Para validar UUID

from app.core.config import settings

log = structlog.get_logger(__name__)

SECRET_KEY = settings.JWT_SECRET
ALGORITHM = settings.JWT_ALGORITHM
REQUIRED_CLAIMS = ['sub', 'aud', 'exp'] # Claims esenciales según Supabase/JWT estándar
EXPECTED_AUDIENCE = 'authenticated' # Audiencia esperada para usuarios logueados en Supabase

def _validate_uuid(uuid_string: Optional[str]) -> Optional[str]:
    """Intenta validar si un string es un UUID válido. Devuelve el string o None."""
    if not uuid_string:
        return None
    try:
        uuid.UUID(uuid_string)
        return uuid_string
    except ValueError:
        log.warning("Invalid UUID format found in token metadata.", provided_uuid=uuid_string)
        return None

# Añadir parámetro require_company_id
def verify_token(token: str, require_company_id: bool = True) -> Dict[str, Any]:
    """
    Verifica el token JWT usando el secreto y algoritmo de Supabase.
    Valida la firma, expiración, audiencia ('authenticated') y claims requeridos ('sub', 'aud', 'exp').
    Extrae 'email' y 'company_id' de 'app_metadata' si existen.
    Opcionalmente requiere la presencia de un 'company_id' válido en app_metadata.

    Args:
        token: El string del token JWT.
        require_company_id: Si es True (default), falla con 403 si 'company_id'
                            no se encuentra o no es un UUID válido en app_metadata.
                            Si es False, permite tokens válidos sin company_id.

    Returns:
        El payload decodificado (diccionario) si el token es válido según los criterios.
        El payload devuelto incluirá 'company_id' y 'email' en el nivel superior si se encontraron.

    Raises:
        HTTPException(401): Si el token es inválido (firma, exp, aud, formato, claims faltantes) o no se proporciona.
        HTTPException(403): Si require_company_id es True y falta un company_id válido.
        HTTPException(500): Si ocurre un error inesperado o de configuración (ej. JWT_SECRET por defecto).
    """
    # Excepción base para errores 401
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer error=\"invalid_token\""}, # Header estándar para 401
    )
    # Excepción para error 403 (prohibido por falta de company_id)
    forbidden_exception = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="User authenticated, but company association is missing or invalid in token.",
    )
    # Excepción para errores internos del servidor
    internal_error_exception = HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="An internal error occurred during token verification.",
    )

    if not token:
        log.warning("Attempted verification with empty token string.")
        credentials_exception.detail = "Authentication token was not provided."
        raise credentials_exception

    # Verificación crítica de seguridad: NUNCA usar el secreto por defecto
    if SECRET_KEY == "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME_IN_ENV_OR_SECRET" or not SECRET_KEY:
         log.critical("FATAL: Attempting JWT verification with default or missing GATEWAY_JWT_SECRET!")
         # No dar detalles al cliente, es un error interno grave
         raise internal_error_exception

    try:
        # Decodificar y validar firma, expiración y audiencia todo en uno
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            audience=EXPECTED_AUDIENCE, # Validar que el token es para 'authenticated'
            options={
                "verify_signature": True,
                "verify_aud": True,
                "verify_exp": True,
                "verify_iat": True, # Verificar 'issued at' si está presente
                "verify_nbf": True, # Verificar 'not before' si está presente
            }
        )

        # Verificar presencia de claims requeridos
        missing_claims = [claim for claim in REQUIRED_CLAIMS if claim not in payload]
        if missing_claims:
            log.warning("Token verification failed: Missing required claims.",
                        missing_claims=missing_claims,
                        token_subject=payload.get('sub')) # Loguear sub si existe
            credentials_exception.detail = f"Token missing required claims: {', '.join(missing_claims)}"
            raise credentials_exception

        # Extraer información relevante y añadirla al nivel superior del payload para conveniencia
        user_id = payload.get('sub') # User ID de Supabase
        email = payload.get('email') # Email del usuario
        app_metadata = payload.get('app_metadata')
        company_id: Optional[str] = None

        if isinstance(app_metadata, dict):
            company_id_raw = app_metadata.get('company_id')
            if company_id_raw is not None:
                 # Validar que el company_id parece un UUID antes de usarlo
                 company_id = _validate_uuid(str(company_id_raw))
                 if company_id:
                     payload['company_id'] = company_id # Añadir al payload principal
                 else:
                     # Si require_company_id es True, fallaremos más abajo.
                     # Si es False, simplemente no estará disponible.
                     log.warning("Found company_id in app_metadata, but it's not a valid UUID.",
                                 provided_value=company_id_raw, subject=user_id)

            # Podríamos extraer otros datos de app_metadata si fuera necesario
            # roles = app_metadata.get('roles')
            # if roles: payload['roles'] = roles

        # Añadir email al payload principal si existe
        if email:
            payload['email'] = email

        # Aplicar requerimiento de company_id (si aplica)
        if require_company_id and company_id is None:
             # Si se requiere company_id pero no se encontró uno válido
             log.error("Token lacks required and valid 'company_id' in app_metadata.",
                       token_subject=user_id,
                       app_metadata_keys=list(app_metadata.keys()) if isinstance(app_metadata, dict) else None)
             raise forbidden_exception # Lanzar 403 Forbidden

        # Log final de éxito
        log_status = "verified" + (", company_id present" if company_id else ", company_id absent/not required")
        log.debug(f"Token {log_status}", subject=user_id, company_id=company_id, email=email)

        # Asegurarnos de que 'sub' existe antes de devolver (aunque ya lo comprobamos)
        if 'sub' not in payload:
             log.error("Critical consistency error: 'sub' claim missing after validation.", payload_keys=list(payload.keys()))
             raise credentials_exception

        return payload # Devolver el payload enriquecido

    except JWTError as e:
        # Capturar errores específicos de la librería jose
        log.warning(f"JWT Verification Error: {e}", token_provided=True, algorithm=ALGORITHM, audience=EXPECTED_AUDIENCE)
        error_desc = str(e).lower()
        if "signature verification failed" in error_desc:
            credentials_exception.detail = "Invalid token signature."
            credentials_exception.headers["WWW-Authenticate"] = 'Bearer error="invalid_token", error_description="Invalid signature"'
        elif "token is expired" in error_desc:
            credentials_exception.detail = "Token has expired."
            credentials_exception.headers["WWW-Authenticate"] = 'Bearer error="invalid_token", error_description="The token has expired"'
        elif "audience verification failed" in error_desc:
             credentials_exception.detail = f"Invalid token audience. Expected '{EXPECTED_AUDIENCE}'."
             credentials_exception.headers["WWW-Authenticate"] = f'Bearer error="invalid_token", error_description="Invalid audience"'
        elif "invalid header string" in error_desc or "invalid crypto padding" in error_desc or "malformed" in error_desc:
             credentials_exception.detail = "Invalid token format or structure."
             credentials_exception.headers["WWW-Authenticate"] = 'Bearer error="invalid_token", error_description="Malformed token"'
        else:
            # Error genérico de JWT
            credentials_exception.detail = f"Token validation failed: {e}"
            credentials_exception.headers["WWW-Authenticate"] = 'Bearer error="invalid_token"'
        raise credentials_exception from e # Relanzar como 401

    except HTTPException as e:
        # Re-lanzar 403 (forbidden_exception) u otras HTTPExceptions que puedan surgir
        raise e
    except Exception as e:
        # Capturar cualquier otro error inesperado
        log.exception(f"Unexpected error during token verification: {e}")
        raise internal_error_exception from e # Relanzar como 500
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
from pydantic import Field, validator, ValidationError, HttpUrl # Importar validator, ValidationError, HttpUrl
from functools import lru_cache
import sys
import logging
from typing import Optional, List # Añadir List
import uuid # Para validar UUID

# URLs por defecto si no se especifican en el entorno (típico para K8s)
K8S_INGEST_SVC_URL_DEFAULT = "http://ingest-api-service.nyro-develop.svc.cluster.local:80"
K8S_QUERY_SVC_URL_DEFAULT = "http://query-service.nyro-develop.svc.cluster.local:80"

class Settings(BaseSettings):
    # Configuración de Pydantic-Settings
    model_config = SettingsConfigDict(
        env_file='.env', # Buscar archivo .env
        env_prefix='GATEWAY_', # Buscar variables de entorno con prefijo GATEWAY_
        case_sensitive=False, # Insensible a mayúsculas/minúsculas
        env_file_encoding='utf-8',
        extra='ignore' # Ignorar variables extra en el entorno/archivo .env
    )

    # Información del Proyecto
    PROJECT_NAME: str = "Nyro API Gateway"
    API_V1_STR: str = "/api/v1"

    # URLs de Servicios Backend (Obligatorias)
    INGEST_SERVICE_URL: HttpUrl = K8S_INGEST_SVC_URL_DEFAULT # Usar HttpUrl para validación básica
    QUERY_SERVICE_URL: HttpUrl = K8S_QUERY_SVC_URL_DEFAULT  # Usar HttpUrl
    AUTH_SERVICE_URL: Optional[HttpUrl] = None # Opcional (GATEWAY_AUTH_SERVICE_URL)

    # Configuración JWT (Obligatoria)
    JWT_SECRET: str # Obligatorio, sin valor por defecto inseguro
    JWT_ALGORITHM: str = "HS256" # Valor por defecto común para Supabase

    # Configuración Supabase Admin (Obligatoria)
    SUPABASE_URL: HttpUrl # Obligatorio, usar HttpUrl
    SUPABASE_SERVICE_ROLE_KEY: str # Obligatorio, sin valor por defecto inseguro

    # Configuración de Asociación de Compañía
    DEFAULT_COMPANY_ID: Optional[str] = None # Opcional, pero necesario para la asociación

    # Configuración General
    LOG_LEVEL: str = "INFO"
    HTTP_CLIENT_TIMEOUT: int = 60 # Timeout en segundos para llamadas downstream
    # Nombre de Pydantic (KEEPALIAS) vs httpx (keepalive) - Mantener el de Pydantic si la variable de entorno se llama así.
    HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS: int = 100 # Máximo conexiones keep-alive
    HTTP_CLIENT_MAX_CONNECTIONS: int = 200 # Máximo conexiones totales

    # Validadores Pydantic
    @validator('JWT_SECRET')
    def check_jwt_secret(cls, v):
        if not v or v == "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME_IN_ENV_OR_SECRET":
            raise ValueError("GATEWAY_JWT_SECRET is not set or uses the insecure default value.")
        # Podría añadirse una validación de longitud mínima si se desea
        return v

    @validator('SUPABASE_SERVICE_ROLE_KEY')
    def check_supabase_service_key(cls, v):
        if not v or v == "YOUR_SUPABASE_SERVICE_ROLE_KEY_HERE":
            raise ValueError("GATEWAY_SUPABASE_SERVICE_ROLE_KEY is not set or uses the insecure default value.")
        # Podría añadirse validación de formato si Supabase tiene uno específico (ej. longitud)
        return v

    # La validación de URL ahora la hace Pydantic con HttpUrl, no se necesita check_supabase_url

    @validator('DEFAULT_COMPANY_ID', always=True) # always=True para que se ejecute incluso si es None
    def check_default_company_id_format(cls, v): # Renombrado para claridad
        if v is not None: # Solo validar si se proporciona un valor
            try:
                uuid.UUID(str(v)) # Convertir a string por si acaso
            except ValueError:
                raise ValueError(f"GATEWAY_DEFAULT_COMPANY_ID ('{v}') is not a valid UUID.")
        # Si es None, es válido (aunque la lógica de asociación fallará si se necesita)
        return v

    @validator('LOG_LEVEL')
    def check_log_level(cls, v):
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        normalized_v = v.upper()
        if normalized_v not in valid_levels:
            raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return normalized_v # Normalizar a mayúsculas

# Usar lru_cache para asegurar que las settings se cargan una sola vez
@lru_cache()
def get_settings() -> Settings:
    # Configurar un logger temporal BÁSICO para la carga de settings
    # Esto evita problemas si el logging completo aún no está configurado
    temp_log = logging.getLogger("api_gateway.config.loader") # Nombre más específico
    if not temp_log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        # Formato simple para logs de carga
        formatter = logging.Formatter('%(levelname)s: %(message)s')
        handler.setFormatter(formatter)
        temp_log.addHandler(handler)
        temp_log.setLevel(logging.INFO) # Usar INFO para ver mensajes de carga

    temp_log.info("Loading Gateway settings...")
    try:
        # Pydantic-settings leerá las variables de entorno/archivo .env
        # y ejecutará los validadores
        settings_instance = Settings()

        # Loguear valores cargados (excepto secretos) - Convertir HttpUrl a string para loggear
        temp_log.info("Gateway Settings Loaded Successfully:")
        temp_log.info(f"  PROJECT_NAME: {settings_instance.PROJECT_NAME}")
        temp_log.info(f"  INGEST_SERVICE_URL: {str(settings_instance.INGEST_SERVICE_URL)}")
        temp_log.info(f"  QUERY_SERVICE_URL: {str(settings_instance.QUERY_SERVICE_URL)}")
        temp_log.info(f"  AUTH_SERVICE_URL: {str(settings_instance.AUTH_SERVICE_URL) if settings_instance.AUTH_SERVICE_URL else 'Not Set'}")
        temp_log.info(f"  JWT_SECRET: *** SET (Validated) ***")
        temp_log.info(f"  JWT_ALGORITHM: {settings_instance.JWT_ALGORITHM}")
        temp_log.info(f"  SUPABASE_URL: {str(settings_instance.SUPABASE_URL)}")
        temp_log.info(f"  SUPABASE_SERVICE_ROLE_KEY: *** SET (Validated) ***")
        if settings_instance.DEFAULT_COMPANY_ID:
            temp_log.info(f"  DEFAULT_COMPANY_ID: {settings_instance.DEFAULT_COMPANY_ID} (Validated as UUID if set)")
        else:
            # Es una advertencia porque el endpoint de asociación fallará si se llama
            temp_log.warning("  DEFAULT_COMPANY_ID: Not Set (Ensure-company endpoint requires this)")
        temp_log.info(f"  LOG_LEVEL: {settings_instance.LOG_LEVEL}")
        temp_log.info(f"  HTTP_CLIENT_TIMEOUT: {settings_instance.HTTP_CLIENT_TIMEOUT}")
        temp_log.info(f"  HTTP_CLIENT_MAX_CONNECTIONS: {settings_instance.HTTP_CLIENT_MAX_CONNECTIONS}")
        temp_log.info(f"  HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS: {settings_instance.HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS}")

        return settings_instance

    except ValidationError as e:
        # Captura errores de validación de Pydantic (incluyendo los validadores personalizados)
        temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        temp_log.critical("! FATAL: Error validating Gateway settings:")
        for error in e.errors():
            loc = " -> ".join(map(str, error['loc'])) if error.get('loc') else 'N/A'
            temp_log.critical(f"!  - {loc}: {error['msg']}")
        temp_log.critical("! Check your .env file or environment variables.")
        temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        sys.exit("FATAL: Invalid Gateway configuration. Check logs.")
    except Exception as e:
        # Captura cualquier otro error durante la carga
        temp_log.exception(f"FATAL: Unexpected error loading Gateway settings: {e}")
        sys.exit(f"FATAL: Unexpected error loading Gateway settings: {e}")

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
import logging # <-- Importar logging estándar
from supabase.client import Client as SupabaseClient
from app.routers import gateway_router, user_router, auth_router

from app.core.config import settings
from app.core.logging_config import setup_logging
# Configurar logging ANTES de importar otros módulos que puedan loguear
setup_logging()
# Ahora importar el resto
from app.utils.supabase_admin import get_supabase_admin_client
from app.routers import gateway_router, user_router
# Importar dependencias de autenticación para verificar su carga
from app.auth.auth_middleware import StrictAuth, InitialAuth

log = structlog.get_logger("api_gateway.main") # Logger para el módulo main

# Clientes globales que se inicializarán en el lifespan
proxy_http_client: Optional[httpx.AsyncClient] = None
supabase_admin_client: Optional[SupabaseClient] = None

# --- Lifespan para inicializar/cerrar clientes ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global proxy_http_client, supabase_admin_client
    log.info("Application startup: Initializing global clients...")

    # Inicializar cliente HTTPX para proxy
    try:
        # Configurar límites y timeouts desde settings
        limits = httpx.Limits(
            max_keepalive_connections=settings.HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS,
            max_connections=settings.HTTP_CLIENT_MAX_CONNECTIONS
        )
        # Usar timeout general, se puede sobreescribir por request si es necesario
        # Añadir connect timeout más corto
        timeout = httpx.Timeout(
            settings.HTTP_CLIENT_TIMEOUT, connect=10.0, # Timeout de conexión más corto
            # read, write, pool usan el general
        )
        proxy_http_client = httpx.AsyncClient(
             limits=limits,
             timeout=timeout,
             follow_redirects=False, # No seguir redirects automáticamente en el proxy
             http2=True # Habilitar HTTP/2 si los servicios backend lo soportan
        )
        # Inyectar el cliente en el módulo del router (alternativa a pasarlo en cada request)
        gateway_router.http_client = proxy_http_client
        log.info("HTTP Client initialized successfully.", limits=str(limits), timeout=str(timeout))
    except Exception as e:
        log.exception("CRITICAL: Failed to initialize HTTP client during startup!", error=str(e))
        # Podríamos decidir si la app puede arrancar sin cliente HTTP
        # sys.exit("Failed to initialize HTTP client.") # Opcional: Salir si es crítico
        proxy_http_client = None
        gateway_router.http_client = None # Asegurar que el router lo vea como None

    # Inicializar cliente Supabase Admin
    log.info("Initializing Supabase Admin Client...")
    try:
        # get_supabase_admin_client usa lru_cache y maneja errores internos
        supabase_admin_client = get_supabase_admin_client()
        # Inyectar en el router de usuario (menos ideal, mejor usar Depends en la ruta)
        user_router.supabase_admin = supabase_admin_client
        log.info("Supabase Admin Client reference obtained (initialized via get_supabase_admin_client).")
        # Podríamos hacer un test rápido aquí si fuera necesario, pero get_client lo hace al primer uso
    except Exception as e:
        # get_supabase_admin_client ya loguea el error crítico
        log.exception("CRITICAL: Failed to get Supabase Admin Client during startup!", error=str(e))
        # Podríamos decidir si salir o continuar sin cliente admin
        # sys.exit("Failed to initialize Supabase Admin client.") # Opcional
        supabase_admin_client = None
        user_router.supabase_admin = None # Asegurar que el router lo vea como None

    # Punto donde la aplicación está lista para recibir requests
    yield
    # --- Shutdown ---
    log.info("Application shutdown: Closing clients...")

    # Cerrar cliente HTTPX
    if proxy_http_client and not proxy_http_client.is_closed:
        try:
            await proxy_http_client.aclose()
            log.info("HTTP Client closed successfully.")
        except Exception as e:
            log.exception("Error closing HTTP client during shutdown.", error=str(e))
    elif proxy_http_client is None:
        log.warning("HTTP Client was not initialized during startup.")
    else: # Estaba inicializado pero ya cerrado
        log.info("HTTP Client was already closed.")

    # Cliente Supabase (supabase-py) no requiere cierre explícito عادةً
    log.info("Supabase Admin Client shutdown check complete (no explicit close needed).")


# --- Creación de la App FastAPI ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Punto de entrada único y seguro para los microservicios de Nyro. Valida JWTs de Supabase, gestiona asociación inicial de compañía y reenvía tráfico a servicios backend.",
    version="1.0.0",
    lifespan=lifespan, # Usar el lifespan definido arriba
    # Se pueden añadir otros parámetros como openapi_url, docs_url, redoc_url
)

# --- Middlewares ---

# 1. CORS Middleware (Configuración más robusta)
allowed_origins = []
# Orígenes desde variables de entorno o configuración
vercel_url = os.getenv("VERCEL_FRONTEND_URL", "https://atenex-frontend.vercel.app") # Usar fallback
log.info(f"Adding Vercel frontend URL to allowed origins: {vercel_url}")
allowed_origins.append(vercel_url)

localhost_url = "http://localhost:3000" # Frontend local estándar
log.info(f"Adding localhost frontend URL to allowed origins: {localhost_url}")
allowed_origins.append(localhost_url)

# Añadir Ngrok URL de forma dinámica o desde env var
ngrok_url_env = os.getenv("NGROK_URL")
if ngrok_url_env:
    if ngrok_url_env.startswith("https://") and ".ngrok" in ngrok_url_env:
        log.info(f"Adding Ngrok URL from NGROK_URL env var: {ngrok_url_env}")
        allowed_origins.append(ngrok_url_env)
    else:
        log.warning(f"NGROK_URL environment variable ('{ngrok_url_env}') doesn't look like a valid https Ngrok URL. Ignoring.")
# También añadir la URL específica observada en logs si es diferente y no estaba en env
ngrok_url_from_logs = "https://1942-2001-1388-53a1-a7c9-241c-4a44-2b12-938f.ngrok-free.app"
if ngrok_url_from_logs not in allowed_origins:
    log.info(f"Adding specific Ngrok URL observed in logs: {ngrok_url_from_logs}")
    allowed_origins.append(ngrok_url_from_logs)

# Eliminar duplicados y None
allowed_origins = list(set(filter(None, allowed_origins)))
if not allowed_origins:
    log.critical("CRITICAL: No allowed origins configured for CORS. Frontend requests will likely fail.")
else:
    log.info("Final CORS Allowed Origins:", origins=allowed_origins)

# Headers permitidos (incluir estándar y específicos como ngrok)
allowed_headers = [
    "Authorization", "Content-Type", "Accept", "Origin",
    "X-Requested-With", "ngrok-skip-browser-warning", "X-Request-ID" # Permitir pasar X-Request-ID
]
log.info("CORS Allowed Headers:", headers=allowed_headers)

# Métodos permitidos
allowed_methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
log.info("CORS Allowed Methods:", methods=allowed_methods)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True, # Importante para pasar cookies/auth headers
    allow_methods=allowed_methods,
    allow_headers=allowed_headers,
    expose_headers=["X-Request-ID", "X-Process-Time"], # Exponer headers custom
    max_age=600, # Cachear respuesta preflight OPTIONS por 10 mins
)

# 2. Middleware para Request ID, Timing y Logging Estructurado
@app.middleware("http")
async def add_request_id_timing_logging(request: Request, call_next):
    start_time = time.time()
    # Usar X-Request-ID del header si existe, sino generar uno nuevo
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    # Guardar en el estado para acceso posterior
    request.state.request_id = request_id

    # Vincular request_id al contexto de structlog para todos los logs de esta request
    with structlog.contextvars.bound_contextvars(request_id=request_id):
        # Logger específico para requests entrantes/salientes
        bound_log = structlog.get_logger("api_gateway.requests")
        # Loggear inicio de request
        bound_log.info("Request received",
                       method=request.method,
                       path=request.url.path,
                       client_ip=request.client.host if request.client else "N/A",
                       user_agent=request.headers.get("user-agent", "N/A")[:100]) # Limitar longitud UA

        response = None
        try:
            response = await call_next(request)
            # Calcular duración después de obtener la respuesta
            process_time = time.time() - start_time
            # Añadir headers custom a la respuesta
            response.headers["X-Process-Time"] = f"{process_time:.4f}"
            response.headers["X-Request-ID"] = request_id
            # Loggear fin de request exitosa
            bound_log.info("Request processed successfully",
                           status_code=response.status_code,
                           duration=round(process_time, 4))
        except Exception as e:
            # Loggear excepción no manejada ANTES de re-lanzarla
            process_time = time.time() - start_time
            # Usar logger del middleware para excepciones no capturadas por handlers específicos
            bound_log.exception("Unhandled exception during request processing",
                                duration=round(process_time, 4),
                                error_type=type(e).__name__,
                                error=str(e))
            # Re-lanzar para que los exception_handlers de FastAPI la capturen
            raise e
        finally:
            # Este bloque se ejecuta siempre, incluso si hay return o raise
            # Asegurar que el contexto de structlog se limpie (aunque contextvars debería hacerlo)
            pass

        return response

# --- Routers ---
# Incluir los routers definidos en otros módulos
app.include_router(auth_router.router)
app.include_router(gateway_router.router)
app.include_router(user_router.router)

# --- Endpoints Básicos ---
@app.get("/", tags=["Gateway Status"], summary="Root endpoint", include_in_schema=False)
async def root():
    """Endpoint raíz simple para verificar que el gateway está corriendo."""
    return {"message": f"{settings.PROJECT_NAME} is running!"}

@app.get("/health",
         tags=["Gateway Status"],
         summary="Kubernetes Health Check",
         status_code=status.HTTP_200_OK,
         response_description="Indicates if the gateway and its core dependencies are healthy.",
         responses={
             status.HTTP_200_OK: {"description": "Gateway is healthy."},
             status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Gateway is unhealthy due to dependency issues."}
         })
async def health_check():
     """
     Verifica el estado del gateway y sus dependencias críticas (Cliente HTTP, Cliente Supabase Admin).
     Usado por Kubernetes Liveness/Readiness Probes.
     """
     admin_client_status = "available" if supabase_admin_client else "unavailable"
     http_client_status = "available" if proxy_http_client and not proxy_http_client.is_closed else "unavailable"

     # Considerar la app saludable sólo si AMBOS clientes están listos
     is_healthy = admin_client_status == "available" and http_client_status == "available"

     health_details = {
         "status": "healthy" if is_healthy else "unhealthy",
         "service": settings.PROJECT_NAME,
         "dependencies": {
             "supabase_admin_client": admin_client_status,
             "proxy_http_client": http_client_status
         }
     }

     if not is_healthy:
         log.error("Health check failed", details=health_details)
         # Levantar 503 si no está saludable
         raise HTTPException(
             status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
             detail=health_details
         )

     log.debug("Health check passed.", details=health_details)
     return health_details

# --- Manejadores de Excepciones Globales ---
# Captura excepciones HTTP que ocurren en las rutas o dependencias
@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    req_id = getattr(request.state, 'request_id', 'N/A')
    # Usar el logger principal o uno específico para excepciones
    bound_log = log.bind(request_id=req_id)

    # Determinar nivel de log basado en status code
    # Errores de cliente (4xx) son WARNING, errores de servidor (5xx) son ERROR
    log_level_name = "warning" if 400 <= exc.status_code < 500 else "error"
    # *** CORRECCIÓN: Usar getattr para llamar al método de log correcto ***
    log_method = getattr(bound_log, log_level_name, bound_log.info) # Fallback a info si es un nivel desconocido

    log_method("HTTP Exception occurred",
               status_code=exc.status_code,
               detail=exc.detail,
               path=request.url.path,
               # Incluir headers de la excepción si existen (ej. WWW-Authenticate)
               exception_headers=exc.headers)

    # Devolver respuesta JSON estándar para HTTPExceptions
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers # Pasar headers de la excepción a la respuesta
    )

# Captura cualquier otra excepción no manejada (errores 500 inesperados)
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    req_id = getattr(request.state, 'request_id', 'N/A')
    bound_log = log.bind(request_id=req_id)

    # Loguear la excepción completa para diagnóstico
    bound_log.exception("Unhandled internal server error occurred in gateway",
                        path=request.url.path,
                        error_type=type(exc).__name__) # Incluir tipo de error

    # Devolver respuesta 500 genérica al cliente
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected internal server error occurred."}
    )

# --- Log final de configuración (opcional) ---
# Este log se ejecutará sólo una vez cuando el módulo se cargue
log.info(f"'{settings.PROJECT_NAME}' application configured and ready to start.",
         log_level=settings.LOG_LEVEL,
         ingest_service=str(settings.INGEST_SERVICE_URL),
         query_service=str(settings.QUERY_SERVICE_URL),
         auth_proxy_enabled=bool(settings.AUTH_SERVICE_URL),
         supabase_url=str(settings.SUPABASE_URL),
         default_company_id_set=bool(settings.DEFAULT_COMPANY_ID)
         )

# --- Ejecución con Uvicorn (para desarrollo local) ---
# Esto normalmente no se incluye si usas Gunicorn en producción
# if __name__ == "__main__":
#     uvicorn.run(
#         "app.main:app",
#         host="0.0.0.0",
#         port=8080, # Puerto estándar interno
#         log_level=settings.LOG_LEVEL.lower(), # Pasar nivel de log a uvicorn
#         reload=True # Habilitar reload para desarrollo
#         # Añadir --log-config app/logging.yaml si usas config de logging externa
#     )
```

## File: `app\routers\__init__.py`
```py

```

## File: `app\routers\auth_router.py`
```py
# api-gateway/app/routers/auth_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Request, Body
from pydantic import BaseModel, EmailStr, Field
from typing import Annotated, Dict, Any, Optional
import structlog
import uuid

# Dependencias
from app.utils.supabase_admin import get_supabase_admin_client
from supabase import Client as SupabaseClient
from gotrue.errors import AuthApiError
# (+) Importar PostgrestError para manejo más específico
from postgrest.exceptions import APIError as PostgrestError


from app.core.config import settings

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])

# --- Pydantic Models (sin cambios) ---
class RegisterPayload(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)
    name: Optional[str] = Field(None, min_length=2)

class RegisterResponse(BaseModel):
    message: str
    user_id: Optional[uuid.UUID] = None

# --- Helpers (Solo búsqueda de compañía) ---

def _get_company_id_by_name(admin_client: SupabaseClient, company_name: str) -> Optional[uuid.UUID]:
    """Busca el UUID de una compañía por su nombre en public.companies."""
    bound_log = log.bind(lookup_company_name=company_name)
    try:
        bound_log.debug("Looking up company ID by name...")
        response = admin_client.table("companies").select("id").eq("name", company_name).limit(1).maybe_single().execute()

        # Revisar errores PostgREST primero
        postgrest_error = getattr(response, 'error', None)
        if postgrest_error:
             bound_log.error("PostgREST error during company lookup.", response_error=postgrest_error)
             return None # Falló la búsqueda

        if response and response.data:
            company_id = response.data.get("id")
            if company_id:
                 bound_log.info("Company found.", company_id=company_id)
                 return uuid.UUID(company_id)
            else:
                 bound_log.warning("Company query returned data but no ID found.")
                 return None
        elif response and not response.data:
             bound_log.warning("Company not found by name.")
             return None
        else:
            # Caso inesperado si no hay error ni datos
            bound_log.error("Unexpected response (None or no data/error) during company lookup.", response_details=response)
            return None

    except Exception as e:
        bound_log.exception("Error looking up company ID.")
        return None

# --- Endpoint de Registro (Modificado para llamar a RPC y mejorar logging) ---
@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=RegisterResponse,
    summary="Register a new user via Backend (RPC Sync)",
    description="Creates user in Supabase Auth with metadata, then calls SQL function to sync public profile.",
    # ... (responses sin cambios) ...
    responses={
        status.HTTP_201_CREATED: {"description": "User registered successfully, confirmation email sent."},
        status.HTTP_400_BAD_REQUEST: {"description": "Invalid input data or default company 'nyrouwu' not found."},
        status.HTTP_409_CONFLICT: {"description": "A user with this email already exists."},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Failed to create user or profile due to a server error."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Supabase Admin client not available."},
    }
)
async def register_user_endpoint(
    payload: RegisterPayload,
    admin_client: Annotated[SupabaseClient, Depends(get_supabase_admin_client)],
):
    bound_log = log.bind(user_email=payload.email)
    bound_log.info("Backend registration endpoint called (RPC Flow).")

    # 1. Obtener el ID de la compañía por defecto "nyrouwu"
    default_company_name = "nyrouwu"
    company_id = _get_company_id_by_name(admin_client, default_company_name)

    if not company_id:
        bound_log.error(f"Default company '{default_company_name}' not found in database.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Required default company configuration ('{default_company_name}') not found."
        )

    # 2. Preparar metadatos
    user_metadata = {"name": payload.name} if payload.name else {}
    app_metadata = {
        "company_id": str(company_id),
        "roles": ["user"]
    }

    # 3. Crear usuario en Supabase Auth (llamada SÍNCRONA)
    new_user_id: Optional[uuid.UUID] = None
    try:
        bound_log.debug("Attempting to create user in Supabase Auth (sync call)...")
        create_user_response = admin_client.auth.admin.create_user({
            "email": payload.email,
            "password": payload.password,
            "email_confirm": True,
            "user_metadata": user_metadata,
            "app_metadata": app_metadata
        })
        bound_log.debug("Supabase Auth create_user call completed.")

        new_user = create_user_response.user
        if not new_user or not new_user.id:
            bound_log.error("Supabase Auth create_user succeeded but returned no user or ID.", response=create_user_response)
            raise HTTPException(status_code=500, detail="Failed to retrieve user details after creation.")

        new_user_id = new_user.id
        bound_log.info("User successfully created in Supabase Auth.", user_id=new_user_id)

    except AuthApiError as e:
        bound_log.error("AuthApiError creating Supabase Auth user.", status_code=e.status, error_message=e.message)
        if "user already exists" in str(e.message).lower() or e.status == 422:
             raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A user with this email already exists.")
        else:
             status_code = e.status if 400 <= e.status < 600 else 500
             raise HTTPException(status_code=status_code, detail=f"Error creating user in auth system: {e.message}")
    except Exception as e:
        bound_log.exception("Unexpected error creating Supabase Auth user.")
        raise HTTPException(status_code=500, detail=f"Internal server error during user creation: {e}")

    # 4. Llamar a la función SQL para crear/actualizar el perfil público (llamada SÍNCRONA)
    if new_user_id:
        rpc_success = False
        rpc_error_details = None
        try:
            bound_log.debug("Calling RPC public.create_public_profile_for_user...", user_id=new_user_id)
            rpc_response = admin_client.rpc(
                "create_public_profile_for_user",
                {"user_id": str(new_user_id)}
            ).execute()

            rpc_error = getattr(rpc_response, 'error', None)
            if rpc_error:
                 # Captura errores estructurados de PostgREST devueltos por RPC
                 rpc_error_details = str(rpc_error)
                 bound_log.error("PostgREST error calling create_public_profile_for_user RPC.",
                                 status_code=getattr(rpc_response,'status_code','N/A'),
                                 error_details=rpc_error_details, user_id=new_user_id)
            # (+) Verificar si la respuesta en sí indica un problema (aunque no tenga .error)
            elif rpc_response is None or getattr(rpc_response, 'data', 'NOT_PRESENT') == 'NOT_PRESENT':
                 # Si la respuesta es None o no tiene el atributo 'data' (inesperado para .execute())
                 rpc_error_details = f"RPC call returned unexpected response object: {type(rpc_response)}"
                 bound_log.error(rpc_error_details, user_id=new_user_id, rpc_response_obj=rpc_response)
            else:
                 # Asumimos éxito si no hubo error explícito y la respuesta parece válida
                 rpc_success = True
                 bound_log.info("Successfully called RPC to sync public profile.", user_id=new_user_id)

        except PostgrestError as pg_error:
             # Capturar excepciones levantadas por la librería postgrest-py
             rpc_error_details = f"PostgrestError Exception: {pg_error}"
             bound_log.exception("PostgrestError exception calling RPC.", user_id=new_user_id, error=pg_error)
        except Exception as e:
             # Capturar cualquier otro error durante la llamada RPC
             rpc_error_details = f"Unexpected Exception: {e}"
             bound_log.exception("Unexpected error calling create_public_profile_for_user RPC.", user_id=new_user_id)

        # Loguear críticamente si la RPC no fue exitosa
        if not rpc_success:
            log.error("CRITICAL: Failed to sync public profile via RPC after auth user creation.",
                      user_id=new_user_id,
                      rpc_error_reason=rpc_error_details or "Unknown RPC failure reason")
            # NOTA: Decidimos NO lanzar HTTPException aquí para permitir que el registro en Auth se complete.
            # El usuario existe pero su perfil público podría estar incompleto.

    # 5. Devolver éxito (incluso si la RPC falló, el usuario auth existe)
    return RegisterResponse(
        message="Registration successful. Please check your email for confirmation.",
        user_id=new_user_id
    )
```

## File: `app\routers\gateway_router.py`
```py
# File: app/routers/gateway_router.py
# api-gateway/app/routers/gateway_router.py
from fastapi import APIRouter, Request, Response, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from typing import Optional, Annotated, Dict, Any
import httpx
import structlog
import asyncio # Para timeouts específicos si fueran necesarios

from app.core.config import settings
# Importar la dependencia correcta para rutas protegidas
from app.auth.auth_middleware import StrictAuth # Alias para require_user

log = structlog.get_logger(__name__)
router = APIRouter()

# Cliente HTTP global reutilizable (se inyecta/cierra en main.py lifespan)
http_client: Optional[httpx.AsyncClient] = None

# Cabeceras HTTP que son "hop-by-hop" y no deben ser reenviadas
HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    # 'content-encoding' a veces se quita, pero puede ser necesario si el backend responde comprimido y el cliente espera eso.
    # Lo dejaremos pasar por defecto a menos que cause problemas.
    # "content-encoding",
}

def get_client() -> httpx.AsyncClient:
    """Dependencia FastAPI para obtener el cliente HTTP global inicializado."""
    if http_client is None or http_client.is_closed:
        log.error("Gateway HTTP client is not available or closed. Cannot proxy request.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway service dependency unavailable (HTTP Client)."
        )
    return http_client

async def _proxy_request(
    request: Request,
    target_url_str: str, # URL completa del servicio destino
    client: httpx.AsyncClient,
    user_payload: Optional[Dict[str, Any]] # Payload del token validado (si aplica)
):
    """Función interna reutilizable para realizar el proxy de la petición HTTP."""
    method = request.method
    target_url = httpx.URL(target_url_str)

    # 1. Preparar Headers para reenviar
    headers_to_forward = {}
    client_host = request.client.host if request.client else "unknown"
    # Usar X-Forwarded-For existente o el IP del cliente directo
    x_forwarded_for = request.headers.get("x-forwarded-for", client_host)

    for name, value in request.headers.items():
        if name.lower() not in HOP_BY_HOP_HEADERS:
            headers_to_forward[name] = value

    # Añadir/Actualizar cabeceras X-Forwarded-*
    headers_to_forward["X-Forwarded-For"] = x_forwarded_for
    headers_to_forward["X-Forwarded-Proto"] = request.url.scheme
    # X-Forwarded-Host debe ser el host original solicitado al gateway
    headers_to_forward["X-Forwarded-Host"] = request.headers.get("host", "")

    # Añadir X-Request-ID para tracing downstream si está disponible en el estado
    request_id = getattr(request.state, 'request_id', None)
    if request_id:
        headers_to_forward["X-Request-ID"] = request_id

    # 2. Inyectar Headers de Autenticación/Contexto (SI HAY PAYLOAD y NO es OPTIONS)
    # No inyectar en OPTIONS ya que no llevan contexto de usuario usualmente
    log_context = {'request_id': request_id} if request_id else {}
    if user_payload and method.upper() != 'OPTIONS':
        user_id = user_payload.get('sub')
        company_id = user_payload.get('company_id') # Asegurado por StrictAuth que existe
        user_email = user_payload.get('email')

        # Doble chequeo por si acaso, aunque StrictAuth debería garantizarlo
        if not user_id or not company_id:
             log.critical("CRITICAL: Payload missing required fields (sub/company_id) after StrictAuth!",
                          payload_keys=list(user_payload.keys()), user_id=user_id, company_id=company_id)
             raise HTTPException(status_code=500, detail="Internal authentication context error.")

        headers_to_forward['X-User-ID'] = str(user_id)
        headers_to_forward['X-Company-ID'] = str(company_id)
        log_context['user_id'] = str(user_id)
        log_context['company_id'] = str(company_id)
        if user_email:
             headers_to_forward['X-User-Email'] = str(user_email)

    # Vincular contexto al logger para esta operación de proxy
    bound_log = log.bind(**log_context)

    # 3. Preparar Query Params y Body
    query_params = request.query_params
    # Usar request.stream() para eficiencia, evita cargar todo el body en memoria
    request_body_stream = request.stream()

    # 4. Realizar la petición downstream
    bound_log.info(f"Proxying request", method=method, from_path=request.url.path, to_target=str(target_url))

    rp: Optional[httpx.Response] = None
    try:
        # Construir la solicitud httpx
        req = client.build_request(
            method=method,
            url=target_url,
            headers=headers_to_forward,
            params=query_params,
            # Pasar el stream directamente como contenido
            content=request_body_stream
        )
        # Enviar la solicitud y obtener la respuesta como stream
        # Aumentar timeout aquí si es necesario para rutas específicas, ej. con timeout=httpx.Timeout(120.0)
        rp = await client.send(req, stream=True)

        # 5. Procesar y devolver la respuesta del servicio backend
        bound_log.info(f"Received response from downstream", status_code=rp.status_code, target=str(target_url))

        # Filtrar cabeceras hop-by-hop de la respuesta del backend
        response_headers = {
            k: v for k, v in rp.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS
        }

        # Preservar Content-Length si el backend lo envió y no usa chunked encoding
        # StreamingResponse maneja esto bien, pero es bueno ser explícito si es necesario
        # if 'content-length' in rp.headers and rp.headers.get('transfer-encoding') != 'chunked':
        #     response_headers['content-length'] = rp.headers['content-length']

        # Devolver StreamingResponse para eficiencia de memoria
        return StreamingResponse(
            rp.aiter_raw(), # Iterador asíncrono del cuerpo de la respuesta
            status_code=rp.status_code,
            headers=response_headers,
            media_type=rp.headers.get("content-type"), # Preservar content-type original
            background=rp.aclose # Tarea para cerrar la respuesta httpx cuando FastAPI termine
        )

    except httpx.TimeoutException as exc:
         bound_log.error(f"Request timed out to downstream service", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=f"Upstream service timed out.")
    except httpx.ConnectError as exc:
         bound_log.error(f"Connection error to downstream service", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Could not connect to upstream service.")
    except httpx.RequestError as exc:
         # Otros errores de httpx (ej. SSL, problemas de proxy interno, etc.)
         bound_log.error(f"HTTPX request error during proxy", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Error communicating with upstream service.")
    except Exception as exc:
        # Capturar cualquier otro error inesperado
        bound_log.exception(f"Unexpected error during proxy operation", target=str(target_url))
        # Intentar cerrar la respuesta httpx si se abrió antes de la excepción
        if rp and hasattr(rp, 'aclose') and callable(rp.aclose):
            try: await rp.aclose()
            except Exception: pass # Ignorar errores al cerrar en medio de otra excepción
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal gateway error during proxy.")

# --- Rutas Proxy Específicas ---

@router.api_route(
    "/api/v1/ingest/{path:path}",
    # *** CORRECCIÓN: Añadido OPTIONS ***
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    dependencies=[Depends(StrictAuth)], # Requiere token válido CON company_id
    tags=["Proxy - Ingest"],
    summary="Proxy to Ingest Service (Authentication Required)",
    response_description="Response proxied directly from the Ingest Service.",
    name="proxy_ingest_service",
)
async def proxy_ingest_service(
    request: Request,
    path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    # La dependencia StrictAuth ya validó y puso el payload en request.state.user
    # Aquí la recibimos para pasarla a _proxy_request
    user_payload: StrictAuth
):
    """Reenvía peticiones a /api/v1/ingest/* al Ingest Service configurado.
       Requiere autenticación JWT válida con `company_id`. Inyecta headers
       `X-User-ID`, `X-Company-ID`, `X-User-Email`.
    """
    base_url = str(settings.INGEST_SERVICE_URL).rstrip('/') # Convertir HttpUrl a string y quitar / final
    # Construir URL completa, preservando path y query params
    target_url = f"{base_url}/api/v1/ingest/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"
    return await _proxy_request(request, target_url, client, user_payload)

@router.api_route(
    "/api/v1/query/{path:path}",
    # *** CORRECCIÓN: Añadido OPTIONS ***
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    dependencies=[Depends(StrictAuth)], # Requiere token válido CON company_id
    tags=["Proxy - Query"],
    summary="Proxy to Query Service (Authentication Required)",
    response_description="Response proxied directly from the Query Service.",
    name="proxy_query_service",
)
async def proxy_query_service(
    request: Request,
    path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: StrictAuth # Payload validado por la dependencia
):
    """Reenvía peticiones a /api/v1/query/* al Query Service configurado.
       Requiere autenticación JWT válida con `company_id`. Inyecta headers
       `X-User-ID`, `X-Company-ID`, `X-User-Email`.
    """
    base_url = str(settings.QUERY_SERVICE_URL).rstrip('/') # Convertir HttpUrl a string y quitar / final
    target_url = f"{base_url}/api/v1/query/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"
    return await _proxy_request(request, target_url, client, user_payload)


# --- Proxy Opcional para Auth Service ---
if settings.AUTH_SERVICE_URL:
    log.info(f"Auth service proxy enabled for base URL: {settings.AUTH_SERVICE_URL}")
    @router.api_route(
        "/api/v1/auth/{path:path}",
        # *** CORRECCIÓN: Añadido OPTIONS ***
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        tags=["Proxy - Auth"],
        summary="Proxy to Authentication Service (No Gateway Authentication)",
        response_description="Response proxied directly from the Auth Service.",
        name="proxy_auth_service",
    )
    async def proxy_auth_service(
        request: Request,
        path: str,
        client: Annotated[httpx.AsyncClient, Depends(get_client)],
        # NO hay dependencia StrictAuth o InitialAuth aquí.
        # El token (si existe) pasará tal cual al servicio de auth.
    ):
        """
        Proxy genérico para el servicio de autenticación (si está configurado).
        No realiza validación de token en el gateway. Reenvía la solicitud
        tal cual al servicio de autenticación backend.
        """
        base_url = str(settings.AUTH_SERVICE_URL).rstrip('/') # Convertir HttpUrl a string
        target_url = f"{base_url}/api/v1/auth/{path}"
        if request.url.query:
            target_url += f"?{request.url.query}"
        # Llamar a _proxy_request sin user_payload (None)
        return await _proxy_request(request, target_url, client, user_payload=None)
else:
     # Loguear sólo una vez al inicio si no está configurado
     log.warning("Auth service proxy is not configured (GATEWAY_AUTH_SERVICE_URL not set). "
                 "Requests to /api/v1/auth/* will result in 501 Not Implemented.")
     @router.api_route(
         "/api/v1/auth/{path:path}",
         # *** CORRECCIÓN: Añadido OPTIONS ***
         methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
         tags=["Proxy - Auth"],
         summary="Auth Proxy (Not Configured)",
         response_description="Error indicating the auth proxy is not configured.",
         include_in_schema=False # Ocultar de la documentación si no está activo
     )
     async def auth_not_configured(request: Request, path: str):
         # Devuelve 501 si se intenta acceder a la ruta no configurada
         raise HTTPException(
             status_code=status.HTTP_501_NOT_IMPLEMENTED,
             detail="Authentication endpoint proxy is not configured in this gateway instance."
         )
```

## File: `app\routers\user_router.py`
```py
# api-gateway/app/routers/user_router.py
from fastapi import APIRouter, Depends, HTTPException, status, Request
from typing import Annotated, Dict, Any, Optional
import structlog
import traceback

# Dependencias
from app.auth.auth_middleware import InitialAuth
from app.utils.supabase_admin import get_supabase_admin_client
from supabase import Client as SupabaseClient
from gotrue.errors import AuthApiError
from gotrue.types import UserResponse, User
from postgrest import APIResponse as PostgrestAPIResponse
# *** CORRECCIÓN: Eliminada la importación problemática ***
# from postgrest.utils import SyncMaybeSingleResponse

from app.core.config import settings

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["Users"])

# --- (Código eliminado relacionado a ensure_company_association) ---

```

## File: `app\utils\supabase_admin.py`
```py
# File: app/utils/supabase_admin.py
# api-gateway/app/utils/supabase_admin.py
from supabase.client import Client, create_client
from functools import lru_cache
import structlog
from typing import Optional # Para el tipo de retorno

from app.core.config import settings # Importar settings validadas

log = structlog.get_logger(__name__)

# Usar lru_cache para crear el cliente una sola vez por proceso/worker
# maxsize=None significa caché ilimitada (o 1 si solo hay un worker)
@lru_cache(maxsize=None)
def get_supabase_admin_client() -> Client: # Cambiado Optional[Client] a Client, lanzará error si falla
    """
    Crea y devuelve un cliente Supabase inicializado con la Service Role Key.
    Utiliza caché (lru_cache) para devolver la misma instancia.
    Lanza una excepción si la configuración es inválida o falla la inicialización.

    Returns:
        Instancia del cliente Supabase Admin (supabase.Client).

    Raises:
        ValueError: Si la configuración es inválida o la creación del cliente falla.
    """
    supabase_url = str(settings.SUPABASE_URL) # Convertir HttpUrl a string
    service_key = settings.SUPABASE_SERVICE_ROLE_KEY

    # Las validaciones de existencia y no-default ya se hicieron en config.py

    log.info("Attempting to initialize Supabase Admin Client...")
    try:
        # Crear el cliente Supabase
        supabase_admin: Client = create_client(supabase_url, service_key)

        # Optional: Intenta una operación simple para verificar que la clave funciona.
        # Esto añade una pequeña latencia al inicio pero aumenta la confianza.
        # Ejemplo: Listar usuarios con límite 0 (solo verifica la conexión/permiso)
        try:
            # Nota: Esta llamada es síncrona en supabase-py v1, necesita ser async en v2
            # Asumiendo v2+ y que estamos en un contexto async para la verificación inicial
            # Esto realmente no puede hacerse aquí fácilmente en una función síncrona cacheada.
            # La verificación real ocurrirá en el primer uso en una ruta async.
            # response = await supabase_admin.auth.admin.list_users(limit=0) # Necesitaría ser async
            # log.info(f"Supabase Admin Client connection appears valid (basic check).")
            pass # Saltamos la verificación activa aquí
        except Exception as test_e:
             # Esto probablemente no se ejecute aquí. El error ocurrirá en el primer uso.
             log.warning(f"Supabase Admin Client test query failed (will likely fail on first use): {test_e}", exc_info=False)
             # Podrías lanzar el error aquí si quieres que falle al inicio
             # raise ValueError(f"Supabase Admin Client test query failed: {test_e}") from test_e

        log.info("Supabase Admin Client initialized successfully (pending first use validation).")
        return supabase_admin

    except Exception as e:
        # Capturar cualquier error durante create_client
        log.exception("FATAL: Failed to initialize Supabase Admin Client", error=str(e))
        # Lanzar una excepción explícita para notificar el fallo claramente
        raise ValueError(f"FATAL: Failed to initialize Supabase Admin Client: {e}") from e

# Nota: No se crea una instancia global aquí. La instancia se crea y cachea
# cuando get_supabase_admin_client() es llamada por primera vez (ej. como dependencia).
# Si la función falla, la excepción se propagará a través de Depends().
```

## File: `pyproject.toml`
```toml
# File: pyproject.toml
# api-gateway/pyproject.toml
[tool.poetry]
name = "api-gateway"
version = "1.0.0"
description = "API Gateway for Nyro Microservices"
authors = ["Nyro <dev@nyro.com>"]
readme = "README.md" # Aunque no lo modifiquemos, lo referenciamos

[tool.poetry.dependencies]
python = "^3.10"

# Core FastAPI y servidor ASGI
fastapi = "^0.110.0" # O la versión que estés usando
uvicorn = {extras = ["standard"], version = "^0.28.0"} # Servidor ASGI con dependencias estándar (watchfiles, etc.)
gunicorn = "^21.2.0" # Servidor WSGI/Process Manager (usado en los logs)

# Configuración y validación
pydantic = {extras = ["email"], version = "^2.6.4"} # Para validación de datos y settings
pydantic-settings = "^2.2.1" # Para cargar settings desde .env/entorno

# Cliente HTTP asíncrono
httpx = "^0.27.0" # Para hacer las llamadas proxy

# Manejo de JWT
python-jose = {extras = ["cryptography"], version = "^3.3.0"} # Para decodificar y validar JWTs

# Logging estructurado
structlog = "^24.1.0" # Para logging JSON estructurado

# Cliente Supabase
supabase = "^2.5.0" # Cliente oficial Python para Supabase (incluye gotrue-py para Auth)

# Utilidades (opcional, pero útil)
tenacity = "^8.2.3" # Para reintentos (podría usarse en llamadas a Supabase o backends)
# email-validator = "^2.1.1" # Si necesitas validación de email más estricta que la de Pydantic

[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4" # Framework de testing
pytest-asyncio = "^0.21.1" # Para testear código async con pytest
pytest-httpx = "^0.29.0" # Para mockear respuestas HTTPX en tests
# black = "^24.3.0" # Formateador de código (opcional)
# ruff = "^0.3.4" # Linter rápido (opcional)
# mypy = "^1.9.0" # Type checker (opcional)

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
