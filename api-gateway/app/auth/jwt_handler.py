# api-gateway/app/auth/jwt_handler.py
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from jose import JWTError, jwt
from fastapi import HTTPException, status
import structlog

from app.core.config import settings

log = structlog.get_logger(__name__)

SECRET_KEY = settings.JWT_SECRET
ALGORITHM = settings.JWT_ALGORITHM
REQUIRED_CLAIMS = ['sub', 'aud', 'exp']
EXPECTED_AUDIENCE = 'authenticated'

# --- MODIFICACIÓN: Añadir parámetro require_company_id ---
def verify_token(token: str, require_company_id: bool = True) -> Dict[str, Any]:
    """
    Verifica el token JWT usando el secreto y algoritmo de Supabase.
    Valida la firma, expiración, audiencia y claims requeridos.
    Opcionalmente requiere la presencia de 'company_id' en app_metadata.

    Args:
        token: El string del token JWT.
        require_company_id: Si es True (default), falla si 'company_id'
                            no se encuentra en app_metadata. Si es False,
                            permite tokens válidos sin company_id.

    Returns:
        El payload decodificado si el token es válido.

    Raises:
        HTTPException(401): Si el token es inválido, expirado, malformado,
                           le faltan claims, o la audiencia no es correcta.
        HTTPException(403): Si require_company_id es True y falta company_id.
        HTTPException(500): Si ocurre un error inesperado.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer error=\"invalid_token\""},
    )
    forbidden_exception = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="User authenticated, but company association is missing in token.",
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
         raise internal_error_exception

    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            audience=EXPECTED_AUDIENCE,
            options={
                "verify_signature": True,
                "verify_aud": True,
                "verify_exp": True,
            }
        )

        missing_claims = [claim for claim in REQUIRED_CLAIMS if claim not in payload]
        if missing_claims:
            log.warning("Token verification failed: Missing required claims.",
                        missing_claims=missing_claims,
                        token_subject=payload.get('sub'))
            credentials_exception.detail = f"Token missing required claims: {', '.join(missing_claims)}"
            raise credentials_exception

        company_id: Optional[str] = None
        app_metadata = payload.get('app_metadata')
        if isinstance(app_metadata, dict):
            company_id_raw = app_metadata.get('company_id')
            if company_id_raw is not None:
                 company_id = str(company_id_raw) # Asegurar string
                 payload['company_id'] = company_id # Añadir al payload para conveniencia

        # --- MODIFICACIÓN: Aplicar requerimiento de company_id ---
        if require_company_id and company_id is None:
             log.error("Token lacks required 'company_id' in app_metadata.",
                       token_subject=payload.get('sub'),
                       payload_keys=list(payload.keys()))
             raise forbidden_exception # Lanzar 403 si se requiere y falta
        elif company_id:
             log.debug("Token verified, company_id present.",
                       subject=payload.get('sub'), company_id=company_id)
        else: # company_id is None but require_company_id is False
             log.debug("Token verified, company_id not required or not present.",
                       subject=payload.get('sub'))
        # --- FIN MODIFICACIÓN ---

        if 'sub' not in payload:
             log.error("Critical: 'sub' claim missing after initial check.", payload_keys=list(payload.keys()))
             raise credentials_exception

        return payload

    except JWTError as e:
        log.warning(f"JWT Verification Error: {e}", token_provided=True, algorithm=ALGORITHM, audience=EXPECTED_AUDIENCE)
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
        else:
            credentials_exception.detail = f"Token validation failed: {e}"
        raise credentials_exception from e
    except HTTPException as e:
        raise e # Re-lanzar 403 o cualquier otra HTTPException
    except Exception as e:
        log.exception(f"Unexpected error during token verification: {e}")
        raise internal_error_exception from e