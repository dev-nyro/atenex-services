# File: app/auth/auth_middleware.py
# api-gateway/app/auth/auth_middleware.py
from fastapi import Request, HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional, Annotated, Dict, Any
import structlog

# Importar verify_token del nuevo servicio de autenticación
from app.auth.auth_service import verify_token # <-- Asegúrate que la importación es correcta

log = structlog.get_logger(__name__)

# Instancia del scheme HTTPBearer. auto_error=False para manejar manualmente la ausencia de token.
bearer_scheme = HTTPBearer(bearerFormat="JWT", auto_error=False)

async def _get_user_payload_internal(
    request: Request,
    # Usar Annotated para combinar Depends y el tipo
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)],
    require_company_id: bool # Parámetro interno para controlar la verificación
) -> Optional[Dict[str, Any]]:
    """
    Función interna para obtener y validar el payload JWT.
    Controla si se requiere company_id según el parámetro.
    Devuelve el payload si el token es válido y cumple el requisito de company_id.
    Devuelve None si no se proporciona token (cabecera Authorization ausente).
    Lanza HTTPException si el token existe pero es inválido (firma, exp, usuario no existe)
    o si falta company_id cuando es requerido (403).
    """
    # Limpiar estado previo si existiera para evitar contaminación entre requests
    if hasattr(request.state, 'user'):
        del request.state.user

    if authorization is None:
        # No hay cabecera Authorization: Bearer
        log.debug("No Authorization Bearer header found.")
        # No es un error aún, la dependencia que lo use decidirá si lo requiere
        return None

    token = authorization.credentials
    try:
        # Llamar a la función de verificación centralizada
        # Esta función ya maneja las excepciones 401 y 403 internamente
        payload = await verify_token(token, require_company_id=require_company_id)

        # Guardar payload en el estado de la request para posible uso posterior (ej. logging middleware)
        request.state.user = payload

        # Log de éxito más descriptivo
        log_msg = "Token verified successfully via internal getter"
        if require_company_id:
            log_msg += " (company_id required and present)"
        else:
            log_msg += " (company_id check passed or not required)"
        log.debug(log_msg, subject=payload.get('sub'), company_id=payload.get('company_id'))

        return payload
    except HTTPException as e:
        # verify_token lanza 401 (inválido) o 403 (falta company_id requerido)
        # Loguear el error específico antes de relanzar
        log_detail = getattr(e, 'detail', 'No detail provided')
        log.info(f"Token verification failed in dependency: {log_detail}",
                 status_code=e.status_code,
                 user_id=getattr(e, 'user_id', None)) # Loguear user_id si está disponible
        # Re-lanzar la excepción para que FastAPI la maneje y devuelva la respuesta HTTP correcta
        raise e
    except Exception as e:
        # Capturar errores inesperados *dentro* de verify_token que no sean HTTPException
        log.exception("Unexpected error during internal payload retrieval", error=str(e))
        # Devolver un error 500 genérico para no exponer detalles internos
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
    Dependencia FastAPI para intentar validar el token JWT requiriendo company_id.
    - Devuelve el payload si el token es válido Y tiene company_id.
    - Devuelve None si no se proporciona token.
    - Lanza 401 si el token es inválido (firma, exp, usuario no existe).
    - Lanza 403 si el token es válido pero falta company_id.
    """
    try:
        # Llama a la función interna requiriendo company_id
        return await _get_user_payload_internal(request, authorization, require_company_id=True)
    except HTTPException as e:
        # Si _get_user_payload_internal lanza 401 o 403, simplemente relanzamos
        raise e
    except Exception as e:
         # Manejar cualquier otro error inesperado aquí también
         log.exception("Unexpected error in get_current_user_payload wrapper", error=str(e))
         raise HTTPException(status_code=500, detail="Internal Server Error in auth wrapper")

# --- Dependencia para la Asociación Inicial (NO requiere company_id) ---
async def get_initial_user_payload(
    request: Request,
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]
) -> Optional[Dict[str, Any]]:
    """
    Dependencia FastAPI para validar el token JWT sin requerir company_id.
    Útil para la ruta de asociación inicial de company_id.
    - Devuelve el payload si el token es válido (firma, exp, usuario existe), incluso sin company_id.
    - Devuelve None si no se proporciona token.
    - Lanza 401 si el token es inválido (firma, exp, usuario no existe).
    """
    try:
        # Llama a la función interna sin requerir company_id
        return await _get_user_payload_internal(request, authorization, require_company_id=False)
    except HTTPException as e:
        # Si _get_user_payload_internal lanza 401 (o 403, aunque no debería aquí), lo relanzamos
        raise e
    except Exception as e:
         # Manejar cualquier otro error inesperado aquí también
         log.exception("Unexpected error in get_initial_user_payload wrapper", error=str(e))
         raise HTTPException(status_code=500, detail="Internal Server Error in auth wrapper")

# --- Dependencia que Requiere Usuario Estricto (Falla si no hay token válido CON company_id) ---
async def require_user(
    # Depende de la función anterior (get_current_user_payload).
    # Si esa función devuelve None (sin token) o lanza una excepción (401/403),
    # esta dependencia manejará el caso o no se ejecutará.
    user_payload: Annotated[Optional[Dict[str, Any]], Depends(get_current_user_payload)]
) -> Dict[str, Any]:
    """
    Dependencia FastAPI que *asegura* que una ruta requiere un usuario autenticado
    con un token válido Y con company_id asociado.
    - Lanza 401 si no hay token o es inválido (lo hace la dependencia `get_current_user_payload`).
    - Lanza 403 si el token es válido pero falta company_id (lo hace la dependencia `get_current_user_payload`).
    - Si `get_current_user_payload` devuelve `None` (porque no se envió token), esta función lanzará 401.
    """
    if user_payload is None:
        # Esto ocurre si get_current_user_payload devolvió None (sin cabecera Authorization)
        log.info("Access denied by require_user: Authentication required but no token was provided.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"}, # Indica al cliente cómo autenticarse
        )
    # Si user_payload no es None, significa que get_current_user_payload tuvo éxito
    # (el token era válido y tenía company_id), así que simplemente lo devolvemos.
    # Las excepciones 401/403 ya habrían sido lanzadas por la dependencia anterior.
    return user_payload

# --- Dependencia que Requiere Usuario Inicial (Falla si no hay token válido, pero NO requiere company_id) ---
async def require_initial_user(
    # Depende de get_initial_user_payload.
    # Si esa función devuelve None (sin token) o lanza una excepción (401),
    # esta dependencia manejará el caso o no se ejecutará.
    user_payload: Annotated[Optional[Dict[str, Any]], Depends(get_initial_user_payload)]
) -> Dict[str, Any]:
    """
    Dependencia FastAPI que *asegura* que una ruta requiere un usuario autenticado
    con un token válido (firma, exp, usuario existe), pero NO requiere company_id.
    Útil para la ruta de asociación inicial de company_id.
    - Lanza 401 si no hay token o es inválido (lo hace la dependencia `get_initial_user_payload`).
    - Si `get_initial_user_payload` devuelve `None` (sin token), esta función lanzará 401.
    """
    if user_payload is None:
        # Esto ocurre si get_initial_user_payload devolvió None (sin cabecera Authorization)
        log.info("Access denied by require_initial_user: Authentication required but no token was provided.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Si user_payload no es None, significa que get_initial_user_payload tuvo éxito
    # (el token era válido), así que simplemente lo devolvemos.
    return user_payload

# --- Tipos anotados para usar en las rutas y mejorar la legibilidad ---
# StrictAuth: Requiere token válido con company_id
StrictAuth = Annotated[Dict[str, Any], Depends(require_user)]

# InitialAuth: Requiere token válido, pero company_id no es obligatorio
InitialAuth = Annotated[Dict[str, Any], Depends(require_initial_user)]