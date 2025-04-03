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
import logging # <-- Importar logging
from supabase.client import Client as SupabaseClient

# ... (resto de imports y código inicial sin cambios) ...
from app.core.config import settings
from app.core.logging_config import setup_logging
setup_logging()
from app.utils.supabase_admin import get_supabase_admin_client
from app.routers import gateway_router, user_router

log = structlog.get_logger("api_gateway.main")
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
        timeout = httpx.Timeout(
            settings.HTTP_CLIENT_TIMEOUT, connect=10.0,
            write=settings.HTTP_CLIENT_TIMEOUT, pool=settings.HTTP_CLIENT_TIMEOUT
        )
        proxy_http_client = httpx.AsyncClient(
             limits=limits, timeout=timeout, follow_redirects=False, http2=True
        )
        gateway_router.http_client = proxy_http_client
        log.info("HTTP Client initialized successfully.", limits=str(limits), timeout=str(timeout))
    except Exception as e:
        log.exception("Failed to initialize HTTP client during startup!", error=str(e))
        proxy_http_client = None
        gateway_router.http_client = None

    log.info("Initializing Supabase Admin Client...")
    try:
        supabase_admin_client = get_supabase_admin_client()
        user_router.supabase_admin = supabase_admin_client # Inyección menos ideal
        log.info("Supabase Admin Client reference obtained (initialized via get_supabase_admin_client).")
    except Exception as e:
        log.exception("Failed to get Supabase Admin Client during startup!", error=str(e))
        supabase_admin_client = None

    yield # Application runs here

    log.info("Application shutdown: Closing clients...")
    if proxy_http_client and not proxy_http_client.is_closed:
        try:
            await proxy_http_client.aclose()
            log.info("HTTP Client closed successfully.")
        except Exception as e:
            log.exception("Error closing HTTP client during shutdown.", error=str(e))
    else:
        log.warning("HTTP Client was not available or already closed during shutdown.")
    log.info("Supabase Admin Client shutdown check complete (no explicit close needed).")


# --- FastAPI App Creation (Sin cambios) ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Punto de entrada único y seguro para los microservicios de Nyro.",
    version="1.0.0",
    lifespan=lifespan,
)

# --- Middlewares (CORS y Request ID/Timing - Sin cambios respecto a la versión anterior) ---
# ... (Código de configuración CORS y middleware add_request_id_timing_logging sin cambios) ...
allowed_origins = []
# ... (lógica para añadir Vercel, localhost, Ngrok a allowed_origins) ...
vercel_url = os.getenv("VERCEL_FRONTEND_URL")
if vercel_url:
    log.info(f"Adding Vercel frontend URL from env var to allowed origins: {vercel_url}")
    allowed_origins.append(vercel_url)
else:
    vercel_fallback_url = "https://atenex-frontend.vercel.app"
    log.warning(f"VERCEL_FRONTEND_URL env var not set. Using fallback: {vercel_fallback_url}")
    allowed_origins.append(vercel_fallback_url)
localhost_url = "http://localhost:3000"
log.info(f"Adding localhost frontend URL to allowed origins: {localhost_url}")
allowed_origins.append(localhost_url)
ngrok_url_from_frontend_logs = "https://1942-2001-1388-53a1-a7c9-241c-4a44-2b12-938f.ngrok-free.app"
log.info(f"Adding specific Ngrok URL observed in frontend logs: {ngrok_url_from_frontend_logs}")
allowed_origins.append(ngrok_url_from_frontend_logs)
ngrok_url_env = os.getenv("NGROK_URL")
if ngrok_url_env and ngrok_url_env not in allowed_origins:
    if ngrok_url_env.startswith("https://") or ngrok_url_env.startswith("http://"):
        log.info(f"Adding Ngrok URL from NGROK_URL env var to allowed origins: {ngrok_url_env}")
        allowed_origins.append(ngrok_url_env)
    else:
        log.warning(f"NGROK_URL environment variable has an unexpected format: {ngrok_url_env}. Ignoring.")
allowed_origins = list(set(filter(None, allowed_origins)))
if not allowed_origins: log.critical("CRITICAL: No allowed origins configured for CORS.")
else: log.info("Final CORS Allowed Origins:", origins=allowed_origins)
allowed_headers = ["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With", "ngrok-skip-browser-warning"]
log.info("CORS Allowed Headers:", headers=allowed_headers)
allowed_methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
log.info("CORS Allowed Methods:", methods=allowed_methods)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins, allow_credentials=True, allow_methods=allowed_methods,
    allow_headers=allowed_headers, expose_headers=["X-Request-ID", "X-Process-Time"], max_age=600,
)
@app.middleware("http")
async def add_request_id_timing_logging(request: Request, call_next):
    start_time = time.time()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = request_id
    with structlog.contextvars.bound_contextvars(request_id=request_id):
        bound_log = structlog.get_logger("api_gateway.requests")
        bound_log.info("Request received", method=request.method, path=request.url.path, client_ip=request.client.host if request.client else "N/A", user_agent=request.headers.get("user-agent", "N/A")[:100])
        try:
            response = await call_next(request)
            process_time = time.time() - start_time
            response.headers["X-Process-Time"] = f"{process_time:.4f}"
            response.headers["X-Request-ID"] = request_id
            bound_log.info("Request processed successfully", status_code=response.status_code, duration=round(process_time, 4))
        except Exception as e:
            process_time = time.time() - start_time
            bound_log.exception("Unhandled exception during request processing", duration=round(process_time, 4), error_type=type(e).__name__, error=str(e))
            raise e
        return response

# --- Routers (Sin cambios) ---
app.include_router(gateway_router.router)
app.include_router(user_router.router)

# --- Endpoints Básicos (Sin cambios) ---
@app.get("/", tags=["Gateway Status"], summary="Root endpoint", include_in_schema=False)
async def root():
    return {"message": f"{settings.PROJECT_NAME} is running!"}

@app.get("/health", tags=["Gateway Status"], summary="Kubernetes Health Check", status_code=status.HTTP_200_OK)
async def health_check():
     admin_client_status = "available" if supabase_admin_client else "unavailable"
     http_client_status = "available" if proxy_http_client and not proxy_http_client.is_closed else "unavailable"
     is_healthy = admin_client_status == "available" and http_client_status == "available"
     health_details = {"status": "healthy" if is_healthy else "unhealthy", "service": settings.PROJECT_NAME,"dependencies": {"supabase_admin_client": admin_client_status,"proxy_http_client": http_client_status}}
     if not is_healthy:
         log.error("Health check failed", details=health_details)
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=health_details)
     log.debug("Health check passed.", details=health_details)
     return health_details

# --- Manejadores de Excepciones (CORREGIDO) ---
@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    req_id = getattr(request.state, 'request_id', 'N/A')
    bound_log = log.bind(request_id=req_id)

    # *** CORRECCIÓN: Usar niveles numéricos de logging ***
    log_level_int = logging.WARNING if 400 <= exc.status_code < 500 else logging.ERROR
    log_level_name = logging.getLevelName(log_level_int).lower() # Obtener nombre ('warning', 'error')

    # Usar el método específico (warning, error) en lugar de .log() para evitar KeyError
    log_method = getattr(bound_log, log_level_name, bound_log.info) # Fallback a info si el nivel no existe
    log_method("HTTP Exception occurred",
               status_code=exc.status_code,
               detail=exc.detail,
               path=request.url.path)

    headers = getattr(exc, "headers", None)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=headers
    )

# Handler genérico para errores 500 no esperados (Sin cambios, ya usaba exception)
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    req_id = getattr(request.state, 'request_id', 'N/A')
    bound_log = log.bind(request_id=req_id)
    bound_log.exception("Unhandled internal server error occurred in gateway",
                        path=request.url.path,
                        error_type=type(exc).__name__)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected internal server error occurred."}
    )

# Log final (Sin cambios)
log.info(f"'{settings.PROJECT_NAME}' application configured and ready to start.",
         allowed_origins=allowed_origins,
         allowed_methods=allowed_methods,
         allowed_headers=allowed_headers)
```

## File: `app\routers\__init__.py`
```py

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
    # Considerar si quitar 'content-encoding' es necesario en tu caso
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
    user_payload: Optional[Dict[str, Any]]
):
    """Función interna reutilizable para realizar el proxy de la petición HTTP."""
    method = request.method
    target_url = httpx.URL(target_url_str)

    # 1. Preparar Headers para reenviar
    headers_to_forward = {}
    client_host = request.client.host if request.client else "unknown"
    x_forwarded_for = request.headers.get("x-forwarded-for", client_host)

    for name, value in request.headers.items():
        if name.lower() not in HOP_BY_HOP_HEADERS:
            headers_to_forward[name] = value

    headers_to_forward["X-Forwarded-For"] = x_forwarded_for
    headers_to_forward["X-Forwarded-Proto"] = request.url.scheme
    headers_to_forward["X-Forwarded-Host"] = request.headers.get("host", "")
    # Añadir X-Request-ID para tracing downstream si está disponible
    request_id = getattr(request.state, 'request_id', None)
    if request_id:
        headers_to_forward["X-Request-ID"] = request_id


    # 2. Inyectar Headers de Autenticación/Contexto (SI HAY PAYLOAD)
    log_context = {'request_id': request_id} if request_id else {}
    if user_payload:
        user_id = user_payload.get('sub')
        company_id = user_payload.get('company_id') # Asegurado por StrictAuth
        user_email = user_payload.get('email')

        if not user_id or not company_id: # Doble chequeo por seguridad
             log.critical("CRITICAL: Payload missing required fields (sub/company_id) after StrictAuth!",
                          payload_keys=list(user_payload.keys()), user_id=user_id, company_id=company_id)
             raise HTTPException(status_code=500, detail="Internal authentication context error.")

        headers_to_forward['X-User-ID'] = str(user_id)
        headers_to_forward['X-Company-ID'] = str(company_id)
        log_context['user_id'] = str(user_id)
        log_context['company_id'] = str(company_id)
        if user_email:
             headers_to_forward['X-User-Email'] = str(user_email)

    # Vincular contexto al logger
    bound_log = log.bind(**log_context)

    # 3. Preparar Query Params y Body
    query_params = request.query_params
    request_body_stream = request.stream()
    # Detectar si el body ya fue consumido (puede pasar con algunos middlewares/frameworks)
    # body_bytes = await request.body() # Alternativa si stream() da problemas, menos eficiente

    # 4. Realizar la petición downstream
    bound_log.info(f"Proxying request", method=method, from_path=request.url.path, to_target=str(target_url))

    rp: Optional[httpx.Response] = None
    try:
        req = client.build_request(
            method=method,
            url=target_url,
            headers=headers_to_forward,
            params=query_params,
            content=request_body_stream # Stream body
            # content=body_bytes # Usar si se leyó con request.body()
        )
        # Aumentar timeout si es necesario para rutas específicas
        rp = await client.send(req, stream=True) # Stream response

        # 5. Procesar y devolver la respuesta
        bound_log.info(f"Received response from downstream", status_code=rp.status_code, target=str(target_url))

        response_headers = {
            k: v for k, v in rp.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS
        }
        # Mantener Content-Length si el backend lo envía y no usamos chunked encoding
        if 'content-length' in rp.headers and rp.headers.get('transfer-encoding') != 'chunked':
            response_headers['content-length'] = rp.headers['content-length']


        # Devolver StreamingResponse
        return StreamingResponse(
            rp.aiter_raw(), # Iterador asíncrono del cuerpo de la respuesta
            status_code=rp.status_code,
            headers=response_headers,
            media_type=rp.headers.get("content-type"),
            background=rp.aclose # Asegurar que se cierra la respuesta httpx
        )

    except httpx.TimeoutException as exc:
         bound_log.error(f"Request timed out to downstream service", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=f"Upstream service timed out.")
    except httpx.ConnectError as exc:
         bound_log.error(f"Connection error to downstream service", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Could not connect to upstream service.")
    except httpx.RequestError as exc:
         bound_log.error(f"HTTPX request error during proxy", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Error communicating with upstream service.")
    except Exception as exc:
        bound_log.exception(f"Unexpected error during proxy operation", target=str(target_url))
        # Cerrar respuesta si ya se había abierto antes de la excepción
        if rp and hasattr(rp, 'aclose') and callable(rp.aclose):
            try: await rp.aclose()
            except Exception: pass # Ignorar errores al cerrar en medio de otra excepción
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal gateway error during proxy.")
    # No necesitamos finally si usamos background task en StreamingResponse o context manager

# --- Rutas Proxy Específicas (AÑADIR 'OPTIONS') ---

@router.api_route(
    "/api/v1/ingest/{path:path}",
    # *** AÑADIDO OPTIONS ***
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    dependencies=[Depends(StrictAuth)], # Requiere token válido CON company_id
    tags=["Proxy - Ingest"],
    summary="Proxy to Ingest Service (Authentication Required)",
    name="proxy_ingest_service",
)
async def proxy_ingest_service(
    request: Request,
    path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: StrictAuth # Payload validado
):
    """Reenvía peticiones a /api/v1/ingest/* al Ingest Service."""
    base_url = str(settings.INGEST_SERVICE_URL).rstrip('/') # Convertir HttpUrl a string
    target_url = f"{base_url}/api/v1/ingest/{path}"
    if request.url.query: target_url += f"?{request.url.query}"
    return await _proxy_request(request, target_url, client, user_payload)

@router.api_route(
    "/api/v1/query/{path:path}",
    # *** AÑADIDO OPTIONS ***
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    dependencies=[Depends(StrictAuth)], # Requiere token válido CON company_id
    tags=["Proxy - Query"],
    summary="Proxy to Query Service (Authentication Required)",
    name="proxy_query_service",
)
async def proxy_query_service(
    request: Request,
    path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: StrictAuth # Payload validado
):
    """Reenvía peticiones a /api/v1/query/* al Query Service."""
    base_url = str(settings.QUERY_SERVICE_URL).rstrip('/') # Convertir HttpUrl a string
    target_url = f"{base_url}/api/v1/query/{path}"
    if request.url.query: target_url += f"?{request.url.query}"
    return await _proxy_request(request, target_url, client, user_payload)


# --- Proxy Opcional para Auth Service (AÑADIR 'OPTIONS') ---
if settings.AUTH_SERVICE_URL:
    log.info(f"Auth service proxy enabled for base URL: {settings.AUTH_SERVICE_URL}")
    @router.api_route(
        "/api/v1/auth/{path:path}",
        # *** AÑADIDO OPTIONS ***
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        tags=["Proxy - Auth"],
        summary="Proxy to Authentication Service (No Gateway Authentication)",
        name="proxy_auth_service",
    )
    async def proxy_auth_service(
        request: Request,
        path: str,
        client: Annotated[httpx.AsyncClient, Depends(get_client)],
        # NO hay dependencia StrictAuth o InitialAuth aquí.
    ):
        """
        Proxy genérico para el servicio de autenticación. No valida token en gateway.
        """
        base_url = str(settings.AUTH_SERVICE_URL).rstrip('/') # Convertir HttpUrl a string
        target_url = f"{base_url}/api/v1/auth/{path}"
        if request.url.query: target_url += f"?{request.url.query}"
        # Llamar a _proxy_request sin user_payload
        return await _proxy_request(request, target_url, client, user_payload=None)
else:
     log.warning("Auth service proxy is not configured (GATEWAY_AUTH_SERVICE_URL not set). "
                 "Requests to /api/v1/auth/* will result in 501 Not Implemented.")
     @router.api_route(
         "/api/v1/auth/{path:path}",
         # *** AÑADIDO OPTIONS ***
         methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
         tags=["Proxy - Auth"],
         summary="Auth Proxy (Not Configured)",
         include_in_schema=False
     )
     async def auth_not_configured(request: Request, path: str):
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
# from pydantic import BaseModel # No usado

# Dependencias
from app.auth.auth_middleware import InitialAuth
from app.utils.supabase_admin import get_supabase_admin_client
from supabase import Client as SupabaseClient
from gotrue.errors import AuthApiError
from gotrue.types import UserResponse, User

from app.core.config import settings

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["Users"])

# Inyección global (menos ideal)
supabase_admin: Optional[SupabaseClient] = None

@router.post(
    "/me/ensure-company",
    # ... (metadata de la ruta sin cambios) ...
    status_code=status.HTTP_200_OK,
    summary="Ensure User Company Association",
    description="Checks if the authenticated user (valid JWT required) already has a company ID associated. If not, associates the default company ID using admin privileges. Requires GATEWAY_DEFAULT_COMPANY_ID to be configured.",
    responses={
        status.HTTP_200_OK: {"description": "Company association successful or already existed."},
        status.HTTP_400_BAD_REQUEST: {"description": "Default Company ID not configured on server."},
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication token missing or invalid."},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Failed to check or update user metadata."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Supabase Admin client not available."},
    }
)
async def ensure_company_association(
    user_payload: InitialAuth,
    admin_client: Annotated[SupabaseClient, Depends(get_supabase_admin_client)],
):
    user_id = user_payload.get("sub")
    current_company_id_from_token = user_payload.get("company_id")
    bound_log = log.bind(user_id=user_id)
    bound_log.info("Ensure company association endpoint called.")

    if current_company_id_from_token:
        bound_log.info("User token already contains company ID.", company_id=current_company_id_from_token)
        return {"message": "Company association already exists (found in token).", "company_id": current_company_id_from_token}

    # Definir variable fuera del try para usarla en el bloque de asociación
    existing_app_metadata: Optional[Dict] = None
    current_company_id_from_db: Optional[str] = None

    try:
        bound_log.debug("Fetching current user data from Supabase Admin...")
        get_user_response: UserResponse = await admin_client.auth.admin.get_user_by_id(user_id)
        user_data: Optional[User] = get_user_response.user if get_user_response else None

        if user_data:
            # --- Bloque de acceso a metadatos MÁS DEFENSIVO ---
            bound_log.debug("User data received from Supabase.", user_keys=list(user_data.model_dump().keys()) if user_data else []) # Log keys available

            temp_app_metadata = getattr(user_data, 'app_metadata', None) # Usar getattr con default None

            if isinstance(temp_app_metadata, dict):
                existing_app_metadata = temp_app_metadata # Asignar solo si es un dict
                company_id_raw = existing_app_metadata.get("company_id")
                current_company_id_from_db = str(company_id_raw) if company_id_raw else None
                bound_log.debug("Successfully processed app_metadata.", metadata=existing_app_metadata, found_company_id=current_company_id_from_db)
            else:
                # Si app_metadata no existe o no es un diccionario
                existing_app_metadata = {} # Inicializar como dict vacío
                current_company_id_from_db = None
                bound_log.warning("app_metadata not found or not a dictionary in user data.", app_metadata_type=type(temp_app_metadata).__name__)
            # --- Fin bloque defensivo ---

            if current_company_id_from_db:
                bound_log.info("User already has company ID associated in database.", company_id=current_company_id_from_db)
                return {"message": "Company association already exists (found in database).", "company_id": current_company_id_from_db}
            else:
                 bound_log.info("User confirmed in DB, lacks company ID in app_metadata. Proceeding to association.")
                 # Continuar al bloque de asociación

        else:
             bound_log.error("User not found in Supabase database despite valid token.", user_id=user_id)
             raise HTTPException(
                 status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                 detail="User inconsistency: User authenticated but not found in database."
             )

    except AuthApiError as e:
        bound_log.error("Supabase Admin API error fetching user data", status_code=e.status, error_message=e.message)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify user data: {e.message}"
        )
    except Exception as e:
        # Este es el bloque donde caía el error anterior
        bound_log.exception("Unexpected error during user data retrieval or processing.") # Mensaje más genérico
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while checking user data."
        ) from e

    # --- Bloque de Asociación (Se llega aquí si no tenía ID) ---
    bound_log.info("User lacks company ID. Attempting association.")
    company_id_to_assign = settings.DEFAULT_COMPANY_ID
    if not company_id_to_assign:
        bound_log.critical("CONFIGURATION ERROR: GATEWAY_DEFAULT_COMPANY_ID is not set.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server configuration error: Default company ID for association is not set."
        )

    # Crear los nuevos metadatos asegurándose que existing_app_metadata es un diccionario
    # Si existing_app_metadata es None (nunca debería serlo por la lógica anterior, pero por si acaso), usar {}
    new_app_metadata = {**(existing_app_metadata if existing_app_metadata is not None else {}), "company_id": company_id_to_assign}
    bound_log.debug("Constructed new app_metadata for update.", metadata_to_send=new_app_metadata) # Log extra

    try:
        bound_log.info("Updating user with new app_metadata via Supabase Admin")
        update_response: UserResponse = await admin_client.auth.admin.update_user_by_id(
            user_id,
            attributes={'app_metadata': new_app_metadata}
        )

        # Verificación post-actualización (opcional pero recomendada)
        updated_user_data = update_response.user if update_response else None
        if not (updated_user_data and hasattr(updated_user_data, 'app_metadata') and isinstance(updated_user_data.app_metadata, dict) and updated_user_data.app_metadata.get("company_id") == company_id_to_assign):
             bound_log.error("Failed to confirm company ID update in Supabase response.", update_response_user=updated_user_data.model_dump_json(indent=2) if updated_user_data else "None")
             # Podríamos intentar leer de nuevo, pero por ahora lanzamos error
             raise HTTPException(status_code=500, detail="Failed to confirm company association update after API call.")

        bound_log.info("Successfully updated user app_metadata with company ID.", assigned_company_id=company_id_to_assign)
        return {"message": "Company association successful.", "company_id": company_id_to_assign}

    except AuthApiError as e:
        bound_log.error("Supabase Admin API error during user update", status_code=e.status, error_message=e.message)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to associate company: {e.message}"
        )
    except HTTPException as e: # Re-lanzar HTTPException de la verificación post-update
         raise e
    except Exception as e:
        bound_log.exception("Unexpected error during company association update.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while associating company."
        ) from e
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
