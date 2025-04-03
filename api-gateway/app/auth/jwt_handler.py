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