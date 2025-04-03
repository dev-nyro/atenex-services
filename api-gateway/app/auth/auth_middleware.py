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