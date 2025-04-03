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
# File: app/core/logging_config.py
# api-gateway/app/core/logging_config.py
import logging
import sys
import structlog
import os
from app.core.config import settings # Importar settings ya parseadas y validadas

def setup_logging():
    """Configura el logging estructurado con structlog para salida JSON."""

    # Procesadores compartidos por structlog y logging stdlib
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars, # Añadir contexto de structlog.contextvars
        structlog.stdlib.add_logger_name, # Añadir nombre del logger
        structlog.stdlib.add_log_level, # Añadir nivel del log (info, warning, etc.)
        structlog.processors.TimeStamper(fmt="iso", utc=True), # Timestamp ISO 8601 en UTC
        structlog.processors.StackInfoRenderer(), # Renderizar info de stack si está presente
        # structlog.processors.format_exc_info, # Formatear excepciones (JSONRenderer lo hace bien)
        # Añadir info de proceso/thread si es útil para depurar concurrencia
        # structlog.processors.ProcessInfoProcessor(),
    ]

    # Añadir información del llamador (fichero, línea) SOLO en modo DEBUG por rendimiento
    if settings.LOG_LEVEL.upper() == "DEBUG":
         # Usar CallsiteParameterAdder para añadir selectivamente
         shared_processors.append(structlog.processors.CallsiteParameterAdder(
             parameters={
                 structlog.processors.CallsiteParameter.FILENAME,
                 structlog.processors.CallsiteParameter.LINENO,
                 # structlog.processors.CallsiteParameter.FUNC_NAME, # Opcional
             }
         ))

    # Configurar structlog para usar el sistema de logging estándar de Python
    structlog.configure(
        processors=shared_processors + [
            # Prepara el diccionario de eventos para el formateador de stdlib logging
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(), # Usar logger factory de stdlib
        wrapper_class=structlog.stdlib.BoundLogger, # Clase wrapper estándar
        cache_logger_on_first_use=True, # Cachear loggers para rendimiento
    )

    # Configurar el formateador que usará el handler de stdlib logging
    # Este formateador tomará el diccionario preparado por structlog y lo renderizará
    formatter = structlog.stdlib.ProcessorFormatter(
        # Procesador final que renderiza el diccionario de eventos
        # Usar JSONRenderer para salida estructurada compatible con agregadores de logs
        processor=structlog.processors.JSONRenderer(),
        # Alternativa para desarrollo local (logs más legibles en consola):
        # processor=structlog.dev.ConsoleRenderer(colors=True), # Requiere 'pip install colorama'

        # Procesadores que se ejecutan ANTES del renderizador final (JSONRenderer/ConsoleRenderer)
        # foreign_pre_chain se aplica a logs de librerías que usan logging directamente
        foreign_pre_chain=shared_processors + [
             structlog.stdlib.ProcessorFormatter.remove_processors_meta, # Limpiar metadatos internos
        ],
    )

    # Configurar el handler raíz de logging (salida a stdout)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter) # Usar el formateador de structlog

    root_logger = logging.getLogger() # Obtener el logger raíz

    # Evitar añadir handlers duplicados si esta función se llama accidentalmente más de una vez
    # Comprobar si ya existe un handler con nuestro formateador específico
    has_structlog_handler = any(
        isinstance(h, logging.StreamHandler) and isinstance(h.formatter, structlog.stdlib.ProcessorFormatter)
        for h in root_logger.handlers
    )

    if not has_structlog_handler:
        # Limpiar handlers existentes (opcional, puede ser destructivo si otros módulos configuran logging)
        # root_logger.handlers.clear()
        root_logger.addHandler(handler)
    else:
        # Si ya existe, al menos asegurar que el formateador esté actualizado (por si acaso)
        for h in root_logger.handlers:
            if isinstance(h, logging.StreamHandler) and isinstance(h.formatter, structlog.stdlib.ProcessorFormatter):
                h.setFormatter(formatter)
                break

    # Establecer el nivel de log en el logger raíz basado en la configuración
    try:
        root_logger.setLevel(settings.LOG_LEVEL.upper())
    except ValueError:
        # Esto no debería ocurrir si el validador en config.py funciona
        root_logger.setLevel(logging.INFO)
        logging.warning(f"Invalid LOG_LEVEL '{settings.LOG_LEVEL}' detected after validation. Defaulting to INFO.")


    # Ajustar niveles de log para librerías de terceros verbosas
    # Poner en WARNING o ERROR para reducir ruido, INFO/DEBUG si necesitas sus logs
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO) # Errores de uvicorn sí son importantes
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING) # Logs de acceso pueden ser muy ruidosos
    logging.getLogger("gunicorn.error").setLevel(logging.INFO)
    logging.getLogger("gunicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING) # Logs de httpx suelen ser verbosos
    logging.getLogger("jose").setLevel(logging.INFO) # JWTs fallidos pueden ser INFO/WARNING
    logging.getLogger("asyncio").setLevel(logging.WARNING) # Evitar logs internos de asyncio
    logging.getLogger("watchfiles").setLevel(logging.WARNING) # Si usas --reload

    # Log inicial para confirmar que la configuración se aplicó
    # Usar un logger específico para la configuración del logging
    log_config_logger = structlog.get_logger("api_gateway.logging_config")
    log_config_logger.info("Structlog logging configured", log_level=settings.LOG_LEVEL.upper(), output_format="JSON") # O Console si se usa ConsoleRenderer
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
        log.info("HTTP Client initialized successfully.", limits=str(limits), timeout=str(timeout)) # Log como string para evitar problemas de serialización
    except Exception as e:
        log.exception("Failed to initialize HTTP client during startup!", error=str(e))
        proxy_http_client = None
        gateway_router.http_client = None

    log.info("Initializing Supabase Admin Client...")
    try:
        supabase_admin_client = get_supabase_admin_client()
        # Añadir una pequeña verificación si es posible
        # try:
        #     # Intenta una operación simple y segura, como listar usuarios con límite 1
        #     # Nota: Esto requiere permisos adecuados para la service key
        #     await supabase_admin_client.auth.admin.list_users(limit=1)
        #     log.info("Supabase Admin Client connection verified.")
        # except Exception as admin_test_e:
        #     log.warning("Supabase Admin Client initialized, but test query failed.", error=str(admin_test_e))
        log.info("Supabase Admin Client initialized successfully.")
    except Exception as e:
        log.exception("Failed to initialize Supabase Admin Client during startup!", error=str(e))
        supabase_admin_client = None

    yield

    log.info("Application shutdown: Closing clients...")
    if proxy_http_client and not proxy_http_client.is_closed:
        try:
            await proxy_http_client.aclose()
            log.info("HTTP Client closed successfully.")
        except Exception as e:
            log.exception("Error closing HTTP client during shutdown.", error=str(e))
    else:
        log.warning("HTTP Client was not available or already closed during shutdown.")
    if supabase_admin_client:
        # No hay método aclose() explícito para el cliente supabase-py estándar
        log.info("Supabase Admin Client shutdown (no explicit close needed).")

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
# Añadir la URL específica de Ngrok vista en los logs
ngrok_url_from_log = "https://5158-2001-1388-53a1-a7c9-fd46-ef87-59cf-a7f7.ngrok-free.app"
log.info(f"Adding specific Ngrok URL from logs to allowed origins: {ngrok_url_from_log}")
allowed_origins.append(ngrok_url_from_log)
# Añadir también desde variable de entorno si existe y es diferente
ngrok_url_env = os.getenv("NGROK_URL")
if ngrok_url_env and ngrok_url_env not in allowed_origins:
    if ngrok_url_env.startswith("https://") or ngrok_url_env.startswith("http://"):
        log.info(f"Adding Ngrok URL from NGROK_URL env var to allowed origins: {ngrok_url_env}")
        allowed_origins.append(ngrok_url_env)
    else:
        log.warning(f"NGROK_URL environment variable has an unexpected format: {ngrok_url_env}")
# Limpiar duplicados y None
allowed_origins = list(set(filter(None, allowed_origins)))
if not allowed_origins:
    log.critical("CRITICAL: No allowed origins configured for CORS.")
else:
    log.info("Final CORS Allowed Origins:", origins=allowed_origins)
# Cabeceras permitidas: Asegurarse que 'Authorization' y 'Content-Type' están presentes
# Añadir 'ngrok-skip-browser-warning' si se usa Ngrok
allowed_headers = ["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With", "ngrok-skip-browser-warning"]
log.info("CORS Allowed Headers:", headers=allowed_headers)
# Métodos permitidos: Asegurarse que 'OPTIONS' y 'POST' están presentes
allowed_methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
log.info("CORS Allowed Methods:", methods=allowed_methods)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins, # Lista de orígenes permitidos
    allow_credentials=True, # Permite cookies/auth headers
    allow_methods=allowed_methods, # Métodos HTTP permitidos
    allow_headers=allowed_headers, # Cabeceras HTTP permitidas
    expose_headers=["X-Request-ID", "X-Process-Time"], # Cabeceras expuestas al frontend
    max_age=600, # Tiempo en segundos que el navegador puede cachear la respuesta preflight
)
# --- Fin CORSMiddleware ---


# --- Otros Middlewares (Request ID, Timing) ---
# Añadido DESPUÉS de CORS
@app.middleware("http")
async def add_process_time_header_and_request_id(request: Request, call_next):
    start_time = time.time()
    # Generar request_id si no viene en la cabecera
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    # Guardar request_id en el estado para posible uso en exception handlers
    request.state.request_id = request_id

    # --- !!! CORRECCIÓN DE INDENTACIÓN AQUÍ !!! ---
    # El 'with' debe envolver la llamada a 'call_next' para que el contexto
    # esté activo durante todo el procesamiento de la solicitud.
    with structlog.contextvars.bind_contextvars(request_id=request_id):
        log.info("Request received", method=request.method, path=request.url.path, client_ip=request.client.host if request.client else "N/A")

        try:
            # Procesar la solicitud
            response = await call_next(request)
            # Calcular tiempo después de obtener la respuesta
            process_time = time.time() - start_time
            # Añadir cabeceras a la respuesta
            response.headers["X-Process-Time"] = str(process_time)
            response.headers["X-Request-ID"] = request_id
            log.info("Request processed successfully", status_code=response.status_code, duration=round(process_time, 4))
        except Exception as e:
            # Loggear excepción no manejada ANTES de relanzarla
            process_time = time.time() - start_time
            # Usar logger ya vinculado con request_id
            log.exception("Unhandled exception during request processing", duration=round(process_time, 4), error=str(e))
            # Relanzar la excepción para que los exception_handlers de FastAPI la capturen
            raise e
        # Devolver la respuesta
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
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Gateway service dependency unavailable (Admin Client).")
     http_client_check: Optional[httpx.AsyncClient] = proxy_http_client
     if not http_client_check or http_client_check.is_closed:
         log.error("Health check failed: HTTP Client not available or closed.")
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Gateway service dependency unavailable (HTTP Client).")
     # Podrías añadir una verificación más profunda del cliente admin si es necesario
     log.debug("Health check passed (basic client availability).")
     return {"status": "healthy", "service": settings.PROJECT_NAME}

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    req_id = getattr(request.state, 'request_id', 'N/A')
    # Usar un logger vinculado si es posible
    bound_log = structlog.get_logger("api_gateway.main").bind(request_id=req_id)
    bound_log.warning("HTTP Exception occurred", status_code=exc.status_code, detail=exc.detail, path=request.url.path)
    # Incluir cabeceras WWW-Authenticate si son relevantes (ej. 401)
    headers = getattr(exc, "headers", None)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=headers # Pasar las cabeceras de la excepción original si existen
    )

# Handler genérico para errores 500 no esperados
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    req_id = getattr(request.state, 'request_id', 'N/A')
    # Usar un logger vinculado
    bound_log = structlog.get_logger("api_gateway.main").bind(request_id=req_id)
    # Loggear con traceback completo
    bound_log.exception("Unhandled internal server error occurred in gateway", path=request.url.path, error_type=type(exc).__name__)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred."}
    )

# Log final antes de iniciar Uvicorn (si se ejecuta directamente)
log.info(f"'{settings.PROJECT_NAME}' application configured and ready to start.",
         allowed_origins=allowed_origins,
         allowed_methods=allowed_methods,
         allowed_headers=allowed_headers)

# --- Punto de entrada para ejecución directa (ej. uvicorn app.main:app) ---
# No se necesita código adicional aquí si se usa Gunicorn/Uvicorn como en los logs
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
# Se asigna en main.py lifespan
http_client: Optional[httpx.AsyncClient] = None

# Cabeceras HTTP que son "hop-by-hop" y no deben ser reenviadas
# directamente entre el cliente, el proxy y el servicio backend.
# Ver RFC 2616, Sección 13.5.1.
# Convertido a minúsculas para comparación insensible a mayúsculas/minúsculas.
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te", # Transfer Encoding Extensions
    "trailers", # Para chunked transfer encoding
    "transfer-encoding",
    "upgrade", # Para WebSockets u otros protocolos
    # También eliminamos Host, ya que httpx lo establecerá al del destino
    "host",
    # Content-Length será recalculado por httpx basado en el stream/body
    "content-length",
    # Content-Encoding (como gzip) a veces causa problemas si el proxy
    # lo decodifica y el backend no lo espera. httpx suele manejarlo.
    # "content-encoding", # Comentar/Descomentar según sea necesario
}

def get_client() -> httpx.AsyncClient:
    """Dependencia FastAPI para obtener el cliente HTTP global inicializado."""
    if http_client is None or http_client.is_closed:
        log.error("Gateway HTTP client is not available or closed. Cannot proxy request.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway service is temporarily unavailable due to internal client error."
        )
    return http_client

async def _proxy_request(
    request: Request,
    target_url_str: str, # URL completa del servicio destino
    client: httpx.AsyncClient,
    # user_payload es el diccionario devuelto por StrictAuth o InitialAuth
    # Puede ser None para rutas que no usan estas dependencias (como el proxy /auth opcional)
    user_payload: Optional[Dict[str, Any]]
):
    """
    Función interna reutilizable para realizar el proxy de la petición HTTP.
    """
    method = request.method
    target_url = httpx.URL(target_url_str) # Convertir a objeto URL de httpx

    # 1. Preparar Headers para reenviar
    headers_to_forward = {}
    # Copiar headers de la petición original, excluyendo hop-by-hop y Host
    for name, value in request.headers.items():
        if name.lower() not in HOP_BY_HOP_HEADERS:
            headers_to_forward[name] = value
        # else:
        #     log.debug(f"Skipping hop-by-hop header: {name}")

    # Añadir/Sobrescribir cabeceras X-Forwarded-* (opcional pero buena práctica detrás de un proxy)
    # Uvicorn/Gunicorn pueden añadir algunos si --forwarded-allow-ips está configurado
    # Aquí podemos asegurarnos de que estén presentes
    client_host = request.client.host if request.client else "unknown"
    headers_to_forward["X-Forwarded-For"] = request.headers.get("x-forwarded-for", client_host)
    headers_to_forward["X-Forwarded-Proto"] = request.headers.get("x-forwarded-proto", request.url.scheme)
    # X-Forwarded-Host contiene el Host original solicitado por el cliente
    headers_to_forward["X-Forwarded-Host"] = request.headers.get("host", "")


    # 2. Inyectar Headers de Autenticación/Contexto (SI HAY PAYLOAD)
    log_context = {} # Para añadir info al log de proxy
    if user_payload:
        user_id = user_payload.get('sub')
        company_id = user_payload.get('company_id') # Añadido por verify_token
        user_email = user_payload.get('email') # Añadido por verify_token

        if user_id:
            headers_to_forward['X-User-ID'] = str(user_id)
            log_context['user_id'] = user_id
        else:
            # Esto sería un error grave si user_payload existe
            log.error("CRITICAL: User payload present but 'sub' (user_id) claim missing!", payload_keys=list(user_payload.keys()))
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal authentication error: User ID missing.")

        # Para rutas protegidas por StrictAuth, company_id DEBE estar aquí
        if company_id:
            headers_to_forward['X-Company-ID'] = str(company_id)
            log_context['company_id'] = company_id
        elif request.scope.get('route') and request.scope['route'].name in ['proxy_ingest_service', 'proxy_query_service']:
             # Si estamos en una ruta que DEBERÍA tener company_id (protegida por StrictAuth)
             # y no lo tenemos, es un error interno grave. verify_token debería haber fallado.
             log.error("CRITICAL: StrictAuth route reached but 'company_id' missing from payload!", user_id=user_id, payload_keys=list(user_payload.keys()))
             raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal authentication error: Company ID missing.")

        if user_email:
             headers_to_forward['X-User-Email'] = str(user_email)
             # No añadir email a log_context por defecto por privacidad, a menos que sea necesario
             # log_context['user_email'] = user_email

    # Vincular contexto al logger para esta petición específica
    bound_log = log.bind(**log_context)

    # 3. Preparar Query Params y Body
    # Query params se pasan directamente a httpx
    query_params = request.query_params
    # Body se pasa como un stream asíncrono
    request_body_stream = request.stream()

    # 4. Realizar la petición downstream usando el cliente HTTPX
    bound_log.info(f"Proxying request", method=method, from_path=request.url.path, to_target=str(target_url))

    # Inicializar rp fuera del try para el finally
    rp: Optional[httpx.Response] = None
    try:
        # Construir la solicitud httpx
        req = client.build_request(
            method=method,
            url=target_url,
            headers=headers_to_forward,
            params=query_params,
            # Pasar el stream directamente para eficiencia (evita cargar todo en memoria)
            content=request_body_stream
        )
        # Enviar la solicitud y obtener la respuesta como stream
        rp = await client.send(req, stream=True)

        # 5. Procesar y devolver la respuesta del servicio backend
        bound_log.info(f"Received response from downstream", status_code=rp.status_code, target=str(target_url))

        # Filtrar headers hop-by-hop de la respuesta antes de devolverla al cliente
        response_headers = {
            k: v for k, v in rp.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS
        }

        # Devolver la respuesta como StreamingResponse para eficiencia
        return StreamingResponse(
            # Pasar el iterador asíncrono del cuerpo de la respuesta
            rp.aiter_raw(),
            status_code=rp.status_code,
            headers=response_headers,
            # Usar el Content-Type de la respuesta original
            media_type=rp.headers.get("content-type"),
            # Pasar background task para asegurar que se cierre la respuesta httpx
            # background=BackgroundTask(rp.aclose) # Alternativa a finally
        )

    except httpx.TimeoutException as exc:
         # Timeout al conectar o esperar respuesta del backend
         bound_log.error(f"Request timed out connecting to or waiting for downstream service", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=f"Upstream service at {target_url.host} timed out.")
    except httpx.ConnectError as exc:
         # Error al establecer conexión con el backend (ej. servicio caído)
         bound_log.error(f"Connection error to downstream service", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Could not connect to upstream service at {target_url.host}.")
    except httpx.RequestError as exc:
         # Otros errores de red/HTTP de httpx
         bound_log.error(f"HTTPX request error during proxy", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Error communicating with upstream service.")
    except Exception as exc:
        # Capturar cualquier otro error inesperado durante el proxy
        bound_log.exception(f"Unexpected error during proxy operation", target=str(target_url))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal gateway error during proxy.")
    finally:
        # Asegurarse de cerrar la respuesta httpx para liberar la conexión
        if rp and hasattr(rp, 'aclose') and callable(rp.aclose):
             try:
                 await rp.aclose()
                 # bound_log.debug("Downstream response closed.", target=str(target_url))
             except Exception as close_exc:
                 bound_log.warning("Error closing downstream response stream", target=str(target_url), error=str(close_exc))


# --- Rutas Proxy Específicas ---

# Ruta para Ingest Service (Protegida)
@router.api_route(
    "/api/v1/ingest/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], # Incluir OPTIONS explícitamente
    # Usar StrictAuth para requerir token válido CON company_id
    dependencies=[Depends(StrictAuth)], # La dependencia se ejecuta pero no necesitamos su valor aquí directamente
    tags=["Proxy - Ingest"],
    summary="Proxy to Ingest Service (Authentication Required)",
    name="proxy_ingest_service", # Nombre para referencia interna
)
async def proxy_ingest_service(
    request: Request, # Petición original
    path: str, # Parte de la ruta capturada
    client: Annotated[httpx.AsyncClient, Depends(get_client)], # Cliente HTTP inyectado
    # Inyectar el payload validado por StrictAuth para pasarlo a _proxy_request
    user_payload: StrictAuth # Ya validado: Dict[str, Any]
):
    """Reenvía peticiones a /api/v1/ingest/* al Ingest Service."""
    base_url = settings.INGEST_SERVICE_URL.rstrip('/')
    # Reconstruir la URL completa del servicio destino
    target_url = f"{base_url}/api/v1/ingest/{path}"
    # Añadir query string si existe
    if request.url.query: target_url += f"?{request.url.query}"

    # Llamar a la función proxy interna pasando el payload
    return await _proxy_request(request, target_url, client, user_payload)

# Ruta para Query Service (Protegida)
@router.api_route(
    "/api/v1/query/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], # Incluir OPTIONS
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
    base_url = settings.QUERY_SERVICE_URL.rstrip('/')
    target_url = f"{base_url}/api/v1/query/{path}"
    if request.url.query: target_url += f"?{request.url.query}"
    return await _proxy_request(request, target_url, client, user_payload)


# --- Proxy Opcional para Auth Service (NO Protegido por el Gateway) ---
if settings.AUTH_SERVICE_URL:
    log.info(f"Auth service proxy enabled for base URL: {settings.AUTH_SERVICE_URL}")
    @router.api_route(
        "/api/v1/auth/{path:path}", # Ruta para proxyficar llamadas de autenticación
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], # Permitir todos los métodos comunes
        tags=["Proxy - Auth"],
        summary="Proxy to Authentication Service (No Gateway Authentication)",
        name="proxy_auth_service",
    )
    async def proxy_auth_service(
        request: Request,
        path: str,
        client: Annotated[httpx.AsyncClient, Depends(get_client)],
        # NO hay dependencia StrictAuth o InitialAuth aquí.
        # El token (si existe) se pasa tal cual al servicio de Auth.
    ):
        """
        Proxy genérico para el servicio de autenticación definido en GATEWAY_AUTH_SERVICE_URL.
        No realiza validación de token en el gateway; reenvía la petición tal cual.
        """
        base_url = settings.AUTH_SERVICE_URL.rstrip('/')
        # Asumir que el servicio de Auth espera la ruta completa incluyendo /api/v1/auth/
        target_url = f"{base_url}/api/v1/auth/{path}"
        if request.url.query: target_url += f"?{request.url.query}"

        # Llamar a _proxy_request sin user_payload (user_payload=None)
        # Los headers de autenticación originales (si los hay) se pasarán
        # dentro de los headers copiados por _proxy_request.
        return await _proxy_request(request, target_url, client, user_payload=None)
else:
     # Si AUTH_SERVICE_URL no está configurado, loguear una advertencia y opcionalmente
     # añadir una ruta que devuelva 501 Not Implemented para evitar 404s confusos.
     log.warning("Auth service proxy is not configured (GATEWAY_AUTH_SERVICE_URL not set). "
                 "Requests to /api/v1/auth/* will result in 501 Not Implemented.")
     @router.api_route(
         "/api/v1/auth/{path:path}",
         methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
         tags=["Proxy - Auth"],
         summary="Auth Proxy (Not Configured)",
         include_in_schema=False # No mostrar en la documentación Swagger/OpenAPI
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
from pydantic import BaseModel, Field # Importado pero no usado, podría quitarse si no hay body

# --- RESTAURAR DEPENDENCIAS ---
from app.auth.auth_middleware import InitialAuth # Usar la dependencia que NO requiere company_id
from app.utils.supabase_admin import get_supabase_admin_client
from supabase import Client as SupabaseClient
from gotrue.errors import AuthApiError # Para capturar errores específicos de Supabase Auth

from app.core.config import settings # Necesitamos settings para el default company id

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["Users"])

@router.post(
    "/me/ensure-company",
    status_code=status.HTTP_200_OK,
    # --- RESTAURAR DESCRIPCIÓN ORIGINAL ---
    summary="Ensure User Company Association",
    description="Checks if the authenticated user (valid JWT required) already has a company ID associated in their app_metadata. If not, associates the default company ID configured in the gateway using admin privileges. Returns success message or indicates if association already existed.",
    responses={
        status.HTTP_200_OK: {"description": "Company association successful or already existed."},
        status.HTTP_400_BAD_REQUEST: {"description": "Default Company ID not configured on server."},
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication token missing or invalid (signature, expiration, audience)."},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Server configuration error or failed to update user metadata."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Supabase Admin client not available."},
    }
)
async def ensure_company_association(
    # --- RESTAURAR DEPENDENCIAS ---
    user_payload: InitialAuth, # Valida token SIN requerir company_id
    supabase_admin: Annotated[Optional[SupabaseClient], Depends(get_supabase_admin_client)], # Hacer opcional y verificar
):
    # --- RESTAURAR LÓGICA ORIGINAL CON MEJORAS ---
    if not supabase_admin:
        log.error("Supabase Admin client dependency failed.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin client is not available."
        )

    user_id = user_payload.get("sub")
    # company_id puede o no estar en el payload de InitialAuth
    current_company_id_from_token = user_payload.get("company_id")

    # Vincular contexto de structlog para esta petición específica
    bound_log = log.bind(user_id=user_id)

    bound_log.info("Ensure company association endpoint called.")

    if current_company_id_from_token:
        bound_log.info("User token already contains company ID.", company_id=current_company_id_from_token)
        # Podríamos verificar si coincide con el default, pero por ahora asumimos que si existe, está bien.
        return {"message": "Company association already exists.", "company_id": current_company_id_from_token}

    # Si el token no lo tiene, verificar directamente en Supabase (más seguro)
    # Esto evita problemas si el token está desactualizado
    try:
        bound_log.debug("Fetching current user data from Supabase Admin to double-check app_metadata...")
        get_user_response = await supabase_admin.auth.admin.get_user_by_id(user_id)
        user_data = get_user_response.user
        existing_app_metadata = user_data.app_metadata if user_data else {}
        current_company_id_from_db = existing_app_metadata.get("company_id") if existing_app_metadata else None

        if current_company_id_from_db:
             bound_log.info("User already has company ID associated in database.", company_id=current_company_id_from_db)
             # Devolver el ID de la DB que es el más actualizado
             return {"message": "Company association already exists.", "company_id": current_company_id_from_db}

    except AuthApiError as e:
        bound_log.error(f"Supabase Admin API error fetching user data: {e}", status_code=e.status)
        # Podría ser un 404 si el user_id es inválido, aunque no debería si el token era válido
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch user data from authentication system: {e.message}"
        )
    except Exception as e:
        bound_log.exception("Unexpected error fetching user data for company check.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while checking user data."
        )

    # Si llegamos aquí, el usuario NO tiene company_id ni en token ni en DB
    bound_log.info("User lacks company ID. Proceeding with association.")

    company_id_to_assign = settings.DEFAULT_COMPANY_ID
    if not company_id_to_assign:
        bound_log.error("Cannot associate company: GATEWAY_DEFAULT_COMPANY_ID is not configured.")
        # Devolver 400 Bad Request porque es un problema de configuración que impide la operación
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server configuration error: Default company ID for association is not set."
        )

    bound_log.info(f"Attempting to associate user with Default Company ID.", default_company_id=company_id_to_assign)

    # Usar los metadatos existentes recuperados antes si es posible
    new_app_metadata = {**(existing_app_metadata or {}), "company_id": company_id_to_assign}

    try:
        bound_log.debug("Updating user with new app_metadata via Supabase Admin", new_metadata=new_app_metadata)
        update_response = await supabase_admin.auth.admin.update_user_by_id(
            user_id,
            attributes={'app_metadata': new_app_metadata}
        )
        # Verificar si la respuesta indica éxito (puede variar según la librería)
        # En supabase-py v2+, la ausencia de error suele indicar éxito.
        bound_log.info("Successfully updated user app_metadata with company ID.", assigned_company_id=company_id_to_assign)
        return {"message": "Company association successful.", "company_id": company_id_to_assign}

    except AuthApiError as e:
        bound_log.error(f"Supabase Admin API error during user update: {e}", status_code=e.status)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update user data in authentication system: {e.message}"
        )
    except Exception as e:
        bound_log.exception("Unexpected error during company association update.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while associating company."
        )
    # --- FIN LÓGICA ORIGINAL RESTAURADA ---
```

## File: `app\utils\supabase_admin.py`
```py
# File: app/utils/supabase_admin.py
# api-gateway/app/utils/supabase_admin.py
from supabase.client import Client, create_client
# from supabase.lib.client_options import ClientOptions # Opciones no usadas actualmente
from functools import lru_cache
import structlog
from typing import Optional # Para el tipo de retorno

from app.core.config import settings # Importar settings validadas

log = structlog.get_logger(__name__)

# Usar lru_cache para crear el cliente una sola vez por proceso/worker
@lru_cache()
def get_supabase_admin_client() -> Optional[Client]:
    """
    Crea y devuelve un cliente Supabase inicializado con la Service Role Key.
    Utiliza caché (lru_cache) para devolver la misma instancia en llamadas subsiguientes
    dentro del mismo proceso. Devuelve None si la configuración es inválida.

    Returns:
        Instancia del cliente Supabase Admin (supabase.Client) o None si falla la inicialización.
    """
    supabase_url = settings.SUPABASE_URL
    service_key = settings.SUPABASE_SERVICE_ROLE_KEY

    # Las validaciones de existencia y no-default ya se hicieron en config.py
    # Si llegamos aquí, SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY son válidos (no None, no default)

    log.info("Attempting to initialize Supabase Admin Client...")
    try:
        # Crear el cliente Supabase usando la URL y la Service Role Key
        # Nota: create_client puede lanzar excepciones si los args son inválidos,
        # aunque pydantic ya debería haberlos validado.
        supabase_admin: Client = create_client(supabase_url, service_key)

        # Validación adicional (opcional pero recomendada):
        # Intentar una operación simple para verificar que la clave funciona.
        # Cuidado: esto puede consumir recursos o fallar si los permisos son mínimos.
        # Ejemplo: Listar tablas del schema 'auth' (requiere permisos de admin)
        # try:
        #     response = await supabase_admin.table('users').select('id', head=True, count='exact').limit(0).execute() # Solo contar, sin traer datos
        #     log.info(f"Supabase Admin Client connection verified. Found {response.count} users.")
        # except Exception as test_e:
        #     log.warning(f"Supabase Admin Client initialized, but test query failed: {test_e}. Check Service Role Key permissions.", exc_info=True)
        #     # Decidir si continuar o devolver None/lanzar error si la verificación es crítica

        log.info("Supabase Admin Client initialized successfully.")
        return supabase_admin

    except Exception as e:
        # Capturar cualquier error durante create_client o la verificación opcional
        log.exception("FATAL: Failed to initialize Supabase Admin Client", error=str(e))
        # Lanzar una excepción explícita para notificar el fallo
        raise ValueError("FATAL: Failed to initialize Supabase Admin Client. Check configuration and permissions.")

# Nota: No se crea una instancia global aquí. La instancia se crea y cachea
# cuando get_supabase_admin_client() es llamada por primera vez (ej. en lifespan o como dependencia).
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
