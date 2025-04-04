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

# Configurar logging ANTES de importar otros módulos que puedan loguear
from app.core.logging_config import setup_logging
setup_logging()
# Ahora importar el resto
from app.core.config import settings
from app.utils.supabase_admin import get_supabase_admin_client
from app.routers import gateway_router, user_router
# *** CORRECCIÓN: Comentar o eliminar importación de auth_router si ya no se usa ***
# from app.routers import auth_router
# Importar dependencias de autenticación para verificar su carga
from app.auth.auth_middleware import StrictAuth, InitialAuth

log = structlog.get_logger("api_gateway.main") # Logger para el módulo main

# Clientes globales que se inicializarán en el lifespan
proxy_http_client: Optional[httpx.AsyncClient] = None
supabase_admin_client: Optional[SupabaseClient] = None

# --- Lifespan para inicializar/cerrar clientes (Sin cambios) ---
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
        proxy_http_client = httpx.AsyncClient(limits=limits, timeout=timeout, follow_redirects=False, http2=True)
        gateway_router.http_client = proxy_http_client
        log.info("HTTP Client initialized successfully.", limits=str(limits), timeout=str(timeout))
    except Exception as e:
        log.exception("CRITICAL: Failed to initialize HTTP client during startup!", error=str(e))
        proxy_http_client = None; gateway_router.http_client = None
    log.info("Initializing Supabase Admin Client...")
    try:
        supabase_admin_client = get_supabase_admin_client()
        user_router.supabase_admin = supabase_admin_client # Inyección simple, mejor Depends
        log.info("Supabase Admin Client reference obtained (initialized via get_supabase_admin_client).")
    except Exception as e:
        log.exception("CRITICAL: Failed to get Supabase Admin Client during startup!", error=str(e))
        supabase_admin_client = None; user_router.supabase_admin = None
    yield
    log.info("Application shutdown: Closing clients...")
    if proxy_http_client and not proxy_http_client.is_closed:
        try: await proxy_http_client.aclose(); log.info("HTTP Client closed successfully.")
        except Exception as e: log.exception("Error closing HTTP client during shutdown.", error=str(e))
    log.info("Supabase Admin Client shutdown check complete (no explicit close needed).")


# --- Creación de la App FastAPI ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Punto de entrada único y seguro para los microservicios de Nyro. Valida JWTs de Supabase, gestiona asociación inicial de compañía y reenvía tráfico a servicios backend.",
    version="1.0.0",
    lifespan=lifespan,
)

# --- Middlewares ---

# 1. CORS Middleware (Configuración robusta - Sin cambios respecto a tu código)
allowed_origins = []
vercel_url = os.getenv("VERCEL_FRONTEND_URL", "https://atenex-frontend.vercel.app")
allowed_origins.append(vercel_url)
localhost_url = "http://localhost:3000"
allowed_origins.append(localhost_url)
ngrok_url_env = os.getenv("NGROK_URL")
if ngrok_url_env:
    if ngrok_url_env.startswith("https://") and ".ngrok" in ngrok_url_env:
        allowed_origins.append(ngrok_url_env)
ngrok_url_from_logs = "https://1942-2001-1388-53a1-a7c9-241c-4a44-2b12-938f.ngrok-free.app"
if ngrok_url_from_logs not in allowed_origins:
    allowed_origins.append(ngrok_url_from_logs)
allowed_origins = list(set(filter(None, allowed_origins)))
allowed_headers = ["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With", "ngrok-skip-browser-warning", "X-Request-ID"]
allowed_methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
log.info("Final CORS Allowed Origins:", origins=allowed_origins)
log.info("CORS Allowed Headers:", headers=allowed_headers)
log.info("CORS Allowed Methods:", methods=allowed_methods)
app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_credentials=True, allow_methods=allowed_methods, allow_headers=allowed_headers, expose_headers=["X-Request-ID", "X-Process-Time"], max_age=600)

# 2. Middleware Request ID, Timing, Logging (Sin cambios respecto a tu código)
@app.middleware("http")
async def add_request_id_timing_logging(request: Request, call_next):
    start_time = time.time(); request_id = request.headers.get("x-request-id", str(uuid.uuid4())); request.state.request_id = request_id
    with structlog.contextvars.bound_contextvars(request_id=request_id):
        bound_log = structlog.get_logger("api_gateway.requests")
        bound_log.info("Request received", method=request.method, path=request.url.path, client_ip=request.client.host if request.client else "N/A", user_agent=request.headers.get("user-agent", "N/A")[:100])
        response = None
        try:
            response = await call_next(request)
            process_time = time.time() - start_time
            response.headers["X-Process-Time"] = f"{process_time:.4f}"; response.headers["X-Request-ID"] = request_id
            bound_log.info("Request processed successfully", status_code=response.status_code, duration=round(process_time, 4))
        except Exception as e:
            process_time = time.time() - start_time
            bound_log.exception("Unhandled exception during request processing", duration=round(process_time, 4), error_type=type(e).__name__, error=str(e))
            raise e
        finally: pass
        return response

# --- Routers ---
# *** CORRECCIÓN: Comentar o eliminar inclusión de auth_router si ya no se usa o está vacío ***
# app.include_router(auth_router.router) # Ya no necesario si no hay rutas auth en gateway
app.include_router(gateway_router.router) # Proxy principal
app.include_router(user_router.router) # Rutas relacionadas con usuario (si las hay)

# --- Endpoints Básicos (Sin cambios) ---
@app.get("/", tags=["Gateway Status"], summary="Root endpoint", include_in_schema=False)
async def root(): return {"message": f"{settings.PROJECT_NAME} is running!"}
@app.get("/health", tags=["Gateway Status"], summary="Kubernetes Health Check", status_code=status.HTTP_200_OK, response_description="Indicates if the gateway and its core dependencies are healthy.")
async def health_check():
    admin_client_status = "available" if supabase_admin_client else "unavailable"
    http_client_status = "available" if proxy_http_client and not proxy_http_client.is_closed else "unavailable"
    is_healthy = admin_client_status == "available" and http_client_status == "available"
    health_details = {"status": "healthy" if is_healthy else "unhealthy", "service": settings.PROJECT_NAME, "dependencies": {"supabase_admin_client": admin_client_status, "proxy_http_client": http_client_status}}
    if not is_healthy: log.error("Health check failed", details=health_details); raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=health_details)
    log.debug("Health check passed.", details=health_details); return health_details

# --- Manejadores de Excepciones Globales (Sin cambios) ---
@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    req_id = getattr(request.state, 'request_id', 'N/A'); bound_log = log.bind(request_id=req_id)
    log_level_name = "warning" if 400 <= exc.status_code < 500 else "error"; log_method = getattr(bound_log, log_level_name, bound_log.info)
    log_method("HTTP Exception occurred", status_code=exc.status_code, detail=exc.detail, path=request.url.path, exception_headers=exc.headers)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    req_id = getattr(request.state, 'request_id', 'N/A'); bound_log = log.bind(request_id=req_id)
    bound_log.exception("Unhandled internal server error occurred in gateway", path=request.url.path, error_type=type(exc).__name__)
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "An unexpected internal server error occurred."})

# --- Log final de configuración (Sin cambios) ---
log.info(f"'{settings.PROJECT_NAME}' application configured and ready to start.", log_level=settings.LOG_LEVEL, ingest_service=str(settings.INGEST_SERVICE_URL), query_service=str(settings.QUERY_SERVICE_URL), auth_proxy_enabled=bool(settings.AUTH_SERVICE_URL), supabase_url=str(settings.SUPABASE_URL), default_company_id_set=bool(settings.DEFAULT_COMPANY_ID))
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

# Dependencias (Aunque no se usen ahora, las dejamos por si se añaden otras rutas)
from app.utils.supabase_admin import get_supabase_admin_client
from supabase import Client as SupabaseClient
from gotrue.errors import AuthApiError
from postgrest.exceptions import APIError as PostgrestError

from app.core.config import settings

log = structlog.get_logger(__name__)
# Cambiado el prefijo o eliminarlo si ya no hay rutas de auth manejadas aquí
# router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])
router = APIRouter() # Sin prefijo, o comenta la inclusión en main.py

# --- Pydantic Models (Ya no se usan para registro) ---
# class RegisterPayload(BaseModel):
#    email: EmailStr
#    password: str = Field(..., min_length=8)
#    name: Optional[str] = Field(None, min_length=2)
#
# class RegisterResponse(BaseModel):
#    message: str
#    user_id: Optional[uuid.UUID] = None


# --- Endpoint de Registro ELIMINADO ---
# La ruta POST /api/v1/auth/register ya no existirá o devolverá 404/501
# si el router está montado pero la ruta no está definida,
# o si el Auth Service Proxy (si estaba habilitado) ya no se usa.

# Puedes añadir aquí otros endpoints de auth si los gestiona el gateway
# (ej: refrescar token, obtener perfil propio), pero si no, este
# archivo puede quedar vacío o eliminarse junto con su inclusión en main.py.

log.info("Auth router loaded (registration endpoint removed/disabled).")
```

## File: `app\routers\gateway_router.py`
```py
# api-gateway/app/routers/gateway_router.py
from fastapi import APIRouter, Request, Response, Depends, HTTPException, status, Path
from fastapi.responses import StreamingResponse
from typing import Optional, Annotated, Dict, Any
import httpx
import structlog
import asyncio
import uuid

from app.core.config import settings
from app.auth.auth_middleware import StrictAuth # Dependencia estándar

# Logs más específicos para este router
log = structlog.get_logger("api_gateway.router.gateway")
# Log específico para la dependencia (para ver si se resuelve)
dep_log = structlog.get_logger("api_gateway.dependency.client")
auth_dep_log = structlog.get_logger("api_gateway.dependency.auth")


# Cliente HTTP global (inyectado desde lifespan)
http_client: Optional[httpx.AsyncClient] = None

# Cabeceras Hop-by-Hop
HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length",
}

# Dependencia para obtener el cliente CON LOGGING
def get_client() -> httpx.AsyncClient:
    dep_log.debug("Attempting to get HTTP client via dependency...")
    if http_client is None or http_client.is_closed:
        dep_log.error("Gateway HTTP client dependency check failed: Client not available or closed.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway service dependency unavailable (HTTP Client)."
        )
    dep_log.info("HTTP client dependency resolved successfully.")
    return http_client

# LOGGING PARA DEPENDENCIA StrictAuth (Envolvemos la original para loggear)
async def logged_strict_auth(
    user_payload: StrictAuth # Dependemos de la dependencia original
) -> Dict[str, Any]:
     auth_dep_log.info("StrictAuth dependency successfully resolved in wrapper.",
                     user_id=user_payload.get('sub'),
                     company_id=user_payload.get('company_id'))
     return user_payload

# Alias para usar en las rutas
LoggedStrictAuth = Annotated[Dict[str, Any], Depends(logged_strict_auth)]

# --- Función interna _proxy_request ---
# Añadimos logs justo al inicio y antes de enviar la petición
async def _proxy_request(
    request: Request,
    target_url_str: str,
    client: httpx.AsyncClient,
    user_payload: Optional[Dict[str, Any]],
    backend_service_path: str
):
    method = request.method
    # Bind request_id al log aquí también para contexto consistente
    request_id = getattr(request.state, 'request_id', None)
    proxy_log = log.bind(request_id=request_id)

    proxy_log.info(f"Entered _proxy_request for '{method} {request.url.path}'") # Log de entrada

    target_service_base_url = httpx.URL(target_url_str)
    target_url = target_service_base_url.copy_with(path=backend_service_path, query=request.url.query.encode("utf-8"))

    # 1. Preparar Headers
    headers_to_forward = {}
    client_host = request.client.host if request.client else "unknown"
    x_forwarded_for = request.headers.get("x-forwarded-for", client_host)
    # Loggear headers entrantes (con precaución en producción por datos sensibles)
    # proxy_log.debug("Incoming headers", headers=dict(request.headers))
    for name, value in request.headers.items():
        lower_name = name.lower()
        if lower_name not in HOP_BY_HOP_HEADERS and lower_name != "host":
            headers_to_forward[name] = value
    headers_to_forward["X-Forwarded-For"] = x_forwarded_for
    headers_to_forward["X-Forwarded-Proto"] = request.url.scheme
    headers_to_forward["X-Forwarded-Host"] = request.headers.get("host", "")
    if request_id: headers_to_forward["X-Request-ID"] = request_id

    # 2. Inyectar Headers de Contexto
    log_context = {} # Empezar log context vacío
    if user_payload and method.upper() != 'OPTIONS':
        user_id = user_payload.get('sub')
        company_id = user_payload.get('company_id')
        user_email = user_payload.get('email')
        if not user_id or not company_id:
             proxy_log.critical("Payload missing required fields!", payload_keys=list(user_payload.keys()))
             raise HTTPException(status_code=500, detail="Internal authentication context error.")
        headers_to_forward['X-User-ID'] = str(user_id)
        headers_to_forward['X-Company-ID'] = str(company_id)
        log_context['user_id'] = str(user_id); log_context['company_id'] = str(company_id)
        if user_email: headers_to_forward['X-User-Email'] = str(user_email)
        proxy_log.debug("Added context headers", headers_added=list(log_context.keys()))

    # Rebind con el contexto completo
    proxy_log = proxy_log.bind(**log_context)

    # 3. Preparar Body
    request_body_bytes: Optional[bytes] = None
    if method.upper() in ["POST", "PUT", "PATCH"]:
        proxy_log.debug("Attempting to read request body...")
        request_body_bytes = await request.body()
        proxy_log.info(f"Read request body for {method}", body_length=len(request_body_bytes) if request_body_bytes else 0)

    # Log detallado justo antes de la llamada a httpx
    proxy_log.debug("Preparing to send request to backend",
                   backend_method=method,
                   backend_url=str(target_url),
                   backend_headers=headers_to_forward,
                   has_body=request_body_bytes is not None)

    # 4. Realizar petición downstream
    rp: Optional[httpx.Response] = None
    try:
        proxy_log.info(f"Sending request '{method}' to backend target", backend_target=str(target_url))
        req = client.build_request(method=method, url=target_url, headers=headers_to_forward, content=request_body_bytes)
        rp = await client.send(req, stream=True)

        # 5. Procesar respuesta
        proxy_log.info(f"Received response from backend", status_code=rp.status_code, backend_target=str(target_url))
        response_headers = {k: v for k, v in rp.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}
        proxy_log.debug("Returning StreamingResponse to client", headers=response_headers, media_type=rp.headers.get("content-type"))
        return StreamingResponse(rp.aiter_raw(), status_code=rp.status_code, headers=response_headers, media_type=rp.headers.get("content-type"), background=rp.aclose)

    # Manejo de errores
    except httpx.TimeoutException as exc:
         proxy_log.error(f"Proxy timeout", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Upstream service timed out.")
    except httpx.ConnectError as exc:
         proxy_log.error(f"Proxy connection error", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not connect to upstream service.")
    except httpx.RequestError as exc:
         proxy_log.error(f"Proxy request error", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Error communicating with upstream service.")
    except Exception as exc:
        # Captura la excepción original aquí
        proxy_log.exception(f"Proxy unexpected error occurred in _proxy_request", target=str(target_url))
        if rp:
            try:
                await rp.aclose()
            except Exception as close_exc:
                proxy_log.error("Error closing downstream response during exception handling", close_error=close_exc)
        # Re-lanza la excepción original para que el manejador de FastAPI la capture
        raise exc


# --- Rutas Proxy Específicas ---

# 1. Proxy para Query Service
@router.get( "/api/v1/query/chats", dependencies=[Depends(LoggedStrictAuth)], tags=["Proxy - Query"])
async def proxy_get_chats(request: Request, client: Annotated[httpx.AsyncClient, Depends(get_client)], user_payload: LoggedStrictAuth):
    # LOG INICIO HANDLER
    log.info(f"Entering GET /api/v1/query/chats handler (before proxy call)", request_id=getattr(request.state, 'request_id', None))
    backend_path = "/api/v1/chats"
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

@router.post( "/api/v1/query/ask", dependencies=[Depends(LoggedStrictAuth)], tags=["Proxy - Query"])
async def proxy_post_ask(request: Request, client: Annotated[httpx.AsyncClient, Depends(get_client)], user_payload: LoggedStrictAuth):
    # LOG INICIO HANDLER
    log.info(f"Entering POST /api/v1/query/ask handler (before proxy call)", request_id=getattr(request.state, 'request_id', None))
    backend_path = "/api/v1/ask" # Verifica este path contra query-service -> query.py
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

@router.get( "/api/v1/query/chats/{chat_id}/messages", dependencies=[Depends(LoggedStrictAuth)], tags=["Proxy - Query"])
async def proxy_get_chat_messages(request: Request, client: Annotated[httpx.AsyncClient, Depends(get_client)], user_payload: LoggedStrictAuth, chat_id: uuid.UUID = Path(...)):
    # LOG INICIO HANDLER
    log.info(f"Entering GET /api/v1/query/chats/.../messages handler (before proxy call)", chat_id=str(chat_id), request_id=getattr(request.state, 'request_id', None))
    backend_path = f"/api/v1/chats/{chat_id}/messages" # Verifica este path contra query-service -> chat.py
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

@router.delete( "/api/v1/query/chats/{chat_id}", dependencies=[Depends(LoggedStrictAuth)], tags=["Proxy - Query"])
async def proxy_delete_chat(request: Request, client: Annotated[httpx.AsyncClient, Depends(get_client)], user_payload: LoggedStrictAuth, chat_id: uuid.UUID = Path(...)):
    # LOG INICIO HANDLER
    log.info(f"Entering DELETE /api/v1/query/chats/... handler (before proxy call)", chat_id=str(chat_id), request_id=getattr(request.state, 'request_id', None))
    backend_path = f"/api/v1/chats/{chat_id}" # Verifica este path contra query-service -> chat.py
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

# Rutas OPTIONS (Sin cambios)
@router.options( "/api/v1/query/chats", tags=["Proxy - Query"], include_in_schema=False)
async def options_get_chats(): log.debug("Handling OPTIONS /api/v1/query/chats"); return Response(status_code=200)
@router.options( "/api/v1/query/ask", tags=["Proxy - Query"], include_in_schema=False)
async def options_post_ask(): log.debug("Handling OPTIONS /api/v1/query/ask"); return Response(status_code=200)
@router.options( "/api/v1/query/chats/{chat_id}/messages", tags=["Proxy - Query"], include_in_schema=False)
async def options_get_chat_messages(chat_id: uuid.UUID = Path(...)): log.debug("Handling OPTIONS /api/v1/query/chats/.../messages"); return Response(status_code=200)
@router.options( "/api/v1/query/chats/{chat_id}", tags=["Proxy - Query"], include_in_schema=False)
async def options_delete_chat(chat_id: uuid.UUID = Path(...)): log.debug("Handling OPTIONS /api/v1/query/chats/..."); return Response(status_code=200)


# 2. Proxy para Ingest Service (genérico)
@router.api_route( "/api/v1/ingest/{endpoint_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], dependencies=[Depends(LoggedStrictAuth)], tags=["Proxy - Ingest"], name="proxy_ingest_service")
async def proxy_ingest_service_generic(request: Request, endpoint_path: str, client: Annotated[httpx.AsyncClient, Depends(get_client)], user_payload: LoggedStrictAuth):
    # LOG INICIO HANDLER
    log.info(f"Entering /{endpoint_path} handler for Ingest service (before proxy call)", endpoint=endpoint_path, request_id=getattr(request.state, 'request_id', None))
    backend_path = f"/{endpoint_path}"
    # IMPORTANTE: Verificar que las rutas en ingest.py NO incluyen /api/v1/ingest
    # Ejemplo: En ingest.py debe ser @router.get("/status"), no @router.get("/api/v1/ingest/status")
    return await _proxy_request(request, str(settings.INGEST_SERVICE_URL), client, user_payload, backend_service_path=backend_path)


# 3. Proxy para Auth Service (Opcional y genérico, si está habilitado)
if settings.AUTH_SERVICE_URL:
    log.info(f"Auth service proxy enabled for base URL: {settings.AUTH_SERVICE_URL}")
    @router.api_route( "/api/v1/auth/{endpoint_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], tags=["Proxy - Auth"], name="proxy_auth_service")
    async def proxy_auth_service_generic(request: Request, endpoint_path: str, client: Annotated[httpx.AsyncClient, Depends(get_client)]):
        log.info(f"Entering /{endpoint_path} handler for Auth service (before proxy call)", endpoint=endpoint_path, request_id=getattr(request.state, 'request_id', None))
        backend_path = f"/{endpoint_path}"
        return await _proxy_request(request, str(settings.AUTH_SERVICE_URL), client, user_payload=None, backend_service_path=backend_path)
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
