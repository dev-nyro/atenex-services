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