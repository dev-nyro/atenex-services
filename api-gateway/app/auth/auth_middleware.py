# File: app/auth/auth_middleware.py
# api-gateway/app/auth/auth_middleware.py
from fastapi import Request, HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional, Annotated, Dict, Any, List # Añadido List
import structlog
import httpx

from app.auth.auth_service import verify_token

log = structlog.get_logger(__name__)

bearer_scheme = HTTPBearer(bearerFormat="JWT", auto_error=False)

async def _get_user_payload_internal(
    request: Request,
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)],
    require_company_id: bool
) -> Optional[Dict[str, Any]]:
    if hasattr(request.state, 'user'):
        del request.state.user
    if authorization is None:
        log.debug("No Authorization Bearer header found.")
        return None
    token = authorization.credentials
    try:
        # verify_token ahora puede incluir 'roles' actualizados desde la DB
        payload = await verify_token(token, require_company_id=require_company_id)
        request.state.user = payload
        log_msg = "Token verified successfully via internal getter"
        if require_company_id:
            log_msg += " (company_id required and present)"
        else:
            log_msg += " (company_id check passed or not required)"
        log.debug(log_msg, subject=payload.get('sub'), company_id=payload.get('company_id'), roles=payload.get('roles'))
        return payload
    except HTTPException as e:
        log_detail = getattr(e, 'detail', 'No detail provided')
        # Extraer user_id del payload si está disponible en la excepción
        user_id_from_exception = getattr(e, 'user_id', None)
        log.info(f"Token verification failed in dependency: {log_detail}",
                 status_code=e.status_code,
                 user_id=user_id_from_exception)
        raise e
    except Exception as e:
        log.exception("Unexpected error during internal payload retrieval", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal error during authentication check."
        )

async def get_current_user_payload(
    request: Request,
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]
) -> Optional[Dict[str, Any]]:
    try:
        return await _get_user_payload_internal(request, authorization, require_company_id=True)
    except HTTPException as e:
        raise e
    except Exception as e:
         log.exception("Unexpected error in get_current_user_payload wrapper", error=str(e))
         raise HTTPException(status_code=500, detail="Internal Server Error in auth wrapper")

async def get_initial_user_payload(
    request: Request,
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]
) -> Optional[Dict[str, Any]]:
    try:
        # No requiere company_id, pero sí valida firma, exp, usuario existe y activo
        return await _get_user_payload_internal(request, authorization, require_company_id=False)
    except HTTPException as e:
        raise e
    except Exception as e:
         log.exception("Unexpected error in get_initial_user_payload wrapper", error=str(e))
         raise HTTPException(status_code=500, detail="Internal Server Error in auth wrapper")

async def require_user(
    user_payload: Annotated[Optional[Dict[str, Any]], Depends(get_current_user_payload)]
) -> Dict[str, Any]:
    if user_payload is None:
        log.info("Access denied by require_user: Authentication required but no token was provided.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Las excepciones 401 (inválido) o 403 (sin company_id) ya fueron lanzadas por get_current_user_payload
    return user_payload

async def require_initial_user(
    user_payload: Annotated[Optional[Dict[str, Any]], Depends(get_initial_user_payload)]
) -> Dict[str, Any]:
    if user_payload is None:
        log.info("Access denied by require_initial_user: Authentication required but no token was provided.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # La excepción 401 (inválido, no existe, inactivo) ya fue lanzada por get_initial_user_payload
    return user_payload

# --- NUEVA DEPENDENCIA: Require Admin User ---
async def require_admin_user(
    # Depende de require_initial_user para la validación base del token
    # (firma, exp, usuario existe y activo). No requiere company_id en este punto.
    user_payload: Annotated[Dict[str, Any], Depends(require_initial_user)]
) -> Dict[str, Any]:
    """
    Dependencia FastAPI que asegura que el usuario autenticado tiene el rol 'admin'.
    Relanza 401 si el token base es inválido (gestionado por require_initial_user).
    Lanza 403 si el token es válido pero el usuario no tiene el rol 'admin'.
    """
    # user_payload ya está validado como perteneciente a un usuario existente y activo.
    # Ahora verificamos el rol específico de admin.
    # Usamos los roles del payload, que verify_token actualizó desde la DB.
    roles: Optional[List[str]] = user_payload.get("roles")

    if not roles or "admin" not in roles:
        log.warning("Admin access denied: User does not have 'admin' role.",
                   user_id=user_payload.get("sub"),
                   roles=roles)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions. Administrator role required.",
        )

    # Si llegamos aquí, el usuario es admin
    log.info("Admin access granted.", user_id=user_payload.get("sub"), roles=roles)
    return user_payload # Devuelve el payload con toda la info del admin


# --- Tipos anotados para usar en las rutas ---
StrictAuth = Annotated[Dict[str, Any], Depends(require_user)]
InitialAuth = Annotated[Dict[str, Any], Depends(require_initial_user)]
AdminAuth = Annotated[Dict[str, Any], Depends(require_admin_user)] # <-- NUEVO: Tipo para Admin