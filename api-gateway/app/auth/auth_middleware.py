# api-gateway/app/auth/auth_middleware.py
from fastapi import Request, HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional, Annotated
import structlog

from .jwt_handler import verify_token

log = structlog.get_logger(__name__)

# Define el esquema de seguridad para documentación OpenAPI y extracción de token
# auto_error=False -> Nosotros manejamos explícitamente el error si falta el token en require_user
bearer_scheme = HTTPBearer(bearerFormat="JWT", auto_error=False)

async def get_current_user_payload(
    request: Request,
    # Usar Annotated y Depends para inyectar el resultado de bearer_scheme
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]
) -> Optional[dict]:
    """
    Dependencia FastAPI para validar el token JWT si está presente.
    - Si el token es válido, devuelve el payload decodificado.
    - Si el token es inválido, lanza la HTTPException de verify_token (401 o 500).
    - Si no hay token (authorization is None), devuelve None.
    Almacena el payload (o None) en request.state.user para uso posterior.
    """
    if authorization is None:
        # No hay header Authorization o no es Bearer.
        log.debug("No valid Authorization Bearer header found.")
        request.state.user = None # Marcar que no hay usuario autenticado
        return None

    token = authorization.credentials
    try:
        payload = verify_token(token) # Lanza excepción si es inválido
        request.state.user = payload # Almacenar payload para otros middlewares/endpoints
        log.debug("Token verified. User payload set in request.state", subject=payload.get('sub'))
        return payload
    except HTTPException as e:
        # Propaga la excepción HTTP generada por verify_token (401 o 500)
        log.warning(f"Token verification failed in dependency: {e.detail}")
        request.state.user = None # Asegurar que no hay payload en estado
        raise e # FastAPI manejará esta excepción
    # No es necesario capturar Exception genérica aquí, verify_token ya lo hace

async def require_user(
    # Usar Annotated y Depends para obtener el resultado de get_current_user_payload
    user_payload: Annotated[Optional[dict], Depends(get_current_user_payload)]
) -> dict:
    """
    Dependencia FastAPI que *asegura* que una ruta requiere un usuario autenticado.
    Reutiliza get_current_user_payload y levanta 401 si no se pudo obtener un payload válido.
    """
    if user_payload is None:
        # Esto ocurre si get_current_user_payload devolvió None (sin token)
        # o si lanzó una excepción (que ya fue manejada por FastAPI antes de llegar aquí,
        # pero por seguridad, lo comprobamos).
        log.info("Access denied: Authentication required but no valid token found.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Si llegamos aquí, user_payload es un diccionario válido
    return user_payload

# No usaremos el Middleware global por ahora, aplicaremos 'require_user' por ruta.
# class JWTMiddleware(BaseHTTPMiddleware): ... (Código eliminado)