# api-gateway/app/auth/jwt_handler.py
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from jose import JWTError, jwt
from fastapi import HTTPException, status
import structlog

# Importar settings refactorizadas
from app.core.config import settings

log = structlog.get_logger(__name__)

SECRET_KEY = settings.JWT_SECRET
ALGORITHM = settings.JWT_ALGORITHM
# Define los claims que esperas encontrar en un token válido
# Basado en los READMEs, 'company_id' es crucial
REQUIRED_CLAIMS = ['sub', 'company_id', 'exp'] # 'sub' (subject) y 'exp' (expiration) son estándar

def verify_token(token: str) -> Dict[str, Any]:
    """
    Verifica el token JWT proporcionado.

    Args:
        token: El string del token JWT.

    Returns:
        El payload decodificado si el token es válido.

    Raises:
        HTTPException(401): Si el token es inválido, expirado, malformado,
                           o le faltan claims requeridos.
        HTTPException(500): Si ocurre un error inesperado durante la verificación.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer error=\"invalid_token\""}, # Añadir info según RFC 6750
    )
    internal_error_exception = HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="An error occurred during token verification",
    )

    if not token:
        log.warning("Attempted verification with empty token string.")
        credentials_exception.detail = "Authentication token was not provided."
        raise credentials_exception

    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            options={
                "verify_signature": True,
                "verify_aud": False, # Ajustar si usas 'aud' (audiencia)
                "verify_exp": True, # jose-jwt verifica la expiración por defecto
                "require": REQUIRED_CLAIMS # Exigir claims requeridos
            }
        )

        # Verificación adicional (aunque 'require' ya lo hace, por si acaso)
        missing_claims = [claim for claim in REQUIRED_CLAIMS if claim not in payload]
        if missing_claims:
            log.warning("Token verification failed: Missing required claims.",
                        missing_claims=missing_claims,
                        token_subject=payload.get('sub'))
            credentials_exception.detail = f"Token missing required claims: {', '.join(missing_claims)}"
            raise credentials_exception

        # Validar que 'exp' no está en el pasado (jose-jwt ya lo hace, pero doble check)
        exp_timestamp = payload.get("exp")
        if datetime.now(timezone.utc) >= datetime.fromtimestamp(exp_timestamp, timezone.utc):
             # Esto no debería ocurrir si jose-jwt funciona, pero por si acaso
             log.warning("Token verification failed: Token has expired (redundant check).",
                         token_exp=exp_timestamp,
                         token_subject=payload.get('sub'))
             credentials_exception.detail = "Token has expired"
             raise credentials_exception

        # Log de éxito (opcional, puede ser verboso)
        log.debug("Token verified successfully",
                  subject=payload.get('sub'),
                  company_id=payload.get('company_id'))

        return payload

    except JWTError as e:
        # Errores específicos de JWT (firma inválida, malformado, expirado, claim faltante)
        log.warning(f"JWT Verification Error: {e}", token_provided=True)
        credentials_exception.detail = f"Could not validate credentials: {e}"
        # Podrías dar detalles más específicos basados en el tipo de JWTError
        if "expired" in str(e).lower():
             credentials_exception.headers["WWW-Authenticate"] = "Bearer error=\"invalid_token\", error_description=\"The token has expired\""
        raise credentials_exception from e
    except Exception as e:
        # Errores inesperados durante la decodificación/validación
        log.exception(f"Unexpected error during token verification: {e}")
        raise internal_error_exception from e

# --- create_access_token function removed ---
# El Gateway solo VERIFICA tokens, no los crea.