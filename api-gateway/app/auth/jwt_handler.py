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