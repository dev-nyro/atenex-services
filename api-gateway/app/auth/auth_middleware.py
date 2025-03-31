# api-gateway/app/auth/auth_middleware.py
from fastapi import Request, HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional, Annotated, Dict, Any
import structlog

from .jwt_handler import verify_token

log = structlog.get_logger(__name__)

# Define el esquema de seguridad para documentación OpenAPI y extracción de token.
# auto_error=False significa que manejaremos el error si falta el token nosotros mismos.
bearer_scheme = HTTPBearer(bearerFormat="JWT", auto_error=False)

async def get_current_user_payload(
    request: Request,
    # Usa Annotated y Depends para inyectar el resultado de bearer_scheme
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]
) -> Optional[Dict[str, Any]]:
    """
    Dependencia FastAPI para intentar validar el token JWT si está presente.

    - Si el token es válido, devuelve el payload decodificado.
    - Si el token es inválido o expirado, lanza la HTTPException de verify_token (401).
    - Si no hay token (authorization is None o no es Bearer), devuelve None.

    Almacena el payload (o None) en request.state.user para posible uso posterior.
    Esta dependencia NO fuerza la autenticación, solo la intenta si hay token.
    """
    if authorization is None:
        # No hay header Authorization o no es Bearer.
        log.debug("No Authorization Bearer header found. Proceeding as anonymous.")
        request.state.user = None # Marcar que no hay usuario autenticado
        return None

    token = authorization.credentials
    try:
        payload = verify_token(token) # Lanza HTTPException(401) o (500) si es inválido
        request.state.user = payload # Almacenar payload para otros middlewares/endpoints
        log.debug("Token verified in dependency. User payload set in request.state",
                  subject=payload.get('sub'),
                  company_id=payload.get('company_id'))
        return payload
    except HTTPException as e:
        # Propaga la excepción HTTP generada por verify_token (normalmente 401)
        log.info(f"Token verification failed in dependency: {e.detail}", status_code=e.status_code)
        request.state.user = None # Asegurar que no hay payload en estado
        # IMPORTANTE: Re-lanzamos la excepción para que FastAPI la maneje
        # y la ruta que depende de 'require_user' no se ejecute.
        raise e
    # No es necesario capturar Exception genérica aquí, verify_token ya lo hace

async def require_user(
    # Usar Annotated y Depends para obtener el resultado de get_current_user_payload
    # FastAPI primero ejecutará get_current_user_payload. Si esa dependencia
    # lanza una excepción (ej: 401 por token inválido), la ejecución se detiene
    # y esta dependencia 'require_user' ni siquiera se completa.
    user_payload: Annotated[Optional[Dict[str, Any]], Depends(get_current_user_payload)]
) -> Dict[str, Any]:
    """
    Dependencia FastAPI que *asegura* que una ruta requiere un usuario autenticado
    y con un token válido.

    Reutiliza get_current_user_payload. Si get_current_user_payload:
    - Devuelve un payload: Esta dependencia devuelve ese payload.
    - Devuelve None (sin token): Esta dependencia lanza un 401 explícito.
    - Lanza una excepción (token inválido): FastAPI ya habrá detenido la ejecución.

    Returns:
        El payload del usuario si la autenticación fue exitosa.

    Raises:
        HTTPException(401): Si no se proporcionó un token válido.
    """
    if user_payload is None:
        # Esto ocurre si get_current_user_payload devolvió None (porque no había token).
        # Si había un token pero era inválido, get_current_user_payload ya lanzó 401.
        log.info("Access denied: Authentication required but no valid token was found or provided.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"}, # Indica que se requiere Bearer token
        )
    # Si llegamos aquí, user_payload es un diccionario válido
    log.debug("User requirement met.", subject=user_payload.get('sub'))
    return user_payload

# No usaremos un Middleware global para JWT, aplicaremos 'require_user' por ruta.