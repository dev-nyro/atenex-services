# api-gateway/app/auth/auth_middleware.py
from fastapi import Request, HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional, Annotated, Dict, Any
import structlog

# --- MODIFICACIÓN: Importar verify_token CON el nuevo parámetro ---
from .jwt_handler import verify_token

log = structlog.get_logger(__name__)
bearer_scheme = HTTPBearer(bearerFormat="JWT", auto_error=False)

async def _get_user_payload_internal(
    request: Request,
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)],
    require_company_id: bool # Parámetro interno para controlar la verificación
) -> Optional[Dict[str, Any]]:
    """
    Función interna para obtener y validar el payload, controlando si se requiere company_id.
    """
    request.state.user = None # Resetear estado por defecto

    if authorization is None:
        log.debug("No Authorization Bearer header found.")
        return None

    token = authorization.credentials
    try:
        # --- MODIFICACIÓN: Pasar require_company_id a verify_token ---
        payload = verify_token(token, require_company_id=require_company_id)
        request.state.user = payload
        log_msg = "Token verified" + (" (company_id required)" if require_company_id else " (company_id not required)")
        log.debug(log_msg, subject=payload.get('sub'), company_id=payload.get('company_id'))
        return payload
    except HTTPException as e:
        log.info(f"Token verification failed in dependency: {e.detail}", status_code=e.status_code)
        # Re-lanzar la excepción (401 o 403) para que FastAPI la maneje
        raise e
    except Exception as e:
        # Capturar errores inesperados de verify_token (aunque no debería ocurrir)
        log.exception("Unexpected error during internal payload retrieval", error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal error during authentication check."
        )

# --- Dependencia Estándar (sin cambios en su definición externa) ---
# Intenta obtener el payload, requiere company_id por defecto.
# Devuelve None si no hay token, lanza 401/403 si el token es inválido o falta company_id.
async def get_current_user_payload(
    request: Request,
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]
) -> Optional[Dict[str, Any]]:
    """
    Dependencia FastAPI para intentar validar el token JWT (requiriendo company_id).
    """
    # Llama a la función interna requiriendo company_id
    try:
        return await _get_user_payload_internal(request, authorization, require_company_id=True)
    except HTTPException as e:
        # Si _get_user_payload_internal lanza 401/403, lo relanzamos
        raise e
    except Exception as e:
         # Manejar cualquier otro error inesperado aquí también
         log.exception("Unexpected error in get_current_user_payload wrapper", error=e)
         raise HTTPException(status_code=500, detail="Internal Server Error")


# --- Dependencia que Requiere Usuario (sin cambios) ---
# Falla si get_current_user_payload devuelve None o lanza excepción.
async def require_user(
    user_payload: Annotated[Optional[Dict[str, Any]], Depends(get_current_user_payload)]
) -> Dict[str, Any]:
    """
    Dependencia FastAPI que *asegura* que una ruta requiere un usuario autenticado
    con un token válido Y con company_id asociado.
    """
    if user_payload is None:
        # Esto ocurre si get_current_user_payload devolvió None (sin token)
        log.info("Access denied: Authentication required but no valid token was found or provided.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Si get_current_user_payload lanzó 401/403, la ejecución ya se detuvo.
    log.debug("User requirement met (with company_id check).", subject=user_payload.get('sub'))
    return user_payload

# --- NUEVA Dependencia: Requiere Autenticación pero NO company_id ---
async def require_authenticated_user_no_company_check(
    request: Request,
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]
) -> Dict[str, Any]:
    """
    Dependencia FastAPI que asegura que el usuario está autenticado (token válido)
    pero NO requiere que el company_id esté presente en el token todavía.
    Útil para endpoints como el de asociación de compañía.
    """
    try:
        # Llama a la función interna SIN requerir company_id
        payload = await _get_user_payload_internal(request, authorization, require_company_id=False)

        if payload is None:
            # Si no hay token, lanzar 401
            log.info("Access denied: Authentication required for initial setup but no token provided.")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        # Si había token pero era inválido (firma, exp, aud), _get_user_payload_internal ya lanzó 401.
        log.debug("User requirement met (without company_id check).", subject=payload.get('sub'))
        return payload
    except HTTPException as e:
        # Re-lanzar 401 si el token era inválido
        raise e
    except Exception as e:
        log.exception("Unexpected error in require_authenticated_user_no_company_check", error=e)
        raise HTTPException(status_code=500, detail="Internal Server Error")

# Alias para claridad
InitialAuth = Annotated[Dict[str, Any], Depends(require_authenticated_user_no_company_check)]
StrictAuth = Annotated[Dict[str, Any], Depends(require_user)]