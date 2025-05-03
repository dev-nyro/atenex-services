# File: app/auth/auth_service.py
# api-gateway/app/auth/auth_service.py
import uuid
import time
from typing import Dict, Any, Optional, List # Añadido List
from datetime import datetime, timezone, timedelta
from jose import jwt, JWTError, ExpiredSignatureError, JOSEError
import structlog
from passlib.context import CryptContext
from fastapi import HTTPException, status

from app.core.config import settings
from app.db import postgres_client # Importar cliente DB

log = structlog.get_logger(__name__)

# Contexto Passlib para Bcrypt (recomendado)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- Verificación y Hashing de Contraseñas ---

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica una contraseña en texto plano contra un hash almacenado."""
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except Exception as e:
        # Podría haber errores si el hash no es válido o tiene un formato inesperado
        log.error("Password verification failed", error=str(e), hash_used=hashed_password[:10]+"...") # No loguear hash completo
        return False

def get_password_hash(password: str) -> str:
    """Genera un hash bcrypt para una contraseña en texto plano."""
    return pwd_context.hash(password)

# --- Autenticación de Usuario ---

async def authenticate_user(email: str, password: str) -> Optional[Dict[str, Any]]:
    """
    Autentica un usuario por email y contraseña contra la base de datos.
    Retorna el diccionario del usuario si es válido y activo, None en caso contrario.
    Incluye 'roles' si existe.
    """
    log.debug("Attempting to authenticate user", email=email)
    # postgres_client.get_user_by_email ya fue modificado para devolver 'roles'
    user_data = await postgres_client.get_user_by_email(email)

    if not user_data:
        log.info("Authentication failed: User not found", email=email)
        return None

    if not user_data.get('is_active', False): # Verificar si el usuario está activo
        log.warning("Authentication failed: User is inactive", email=email, user_id=str(user_data.get('id')))
        return None

    hashed_password = user_data.get('hashed_password')
    if not hashed_password or not verify_password(password, hashed_password):
        log.warning("Authentication failed: Invalid password", email=email, user_id=str(user_data.get('id')))
        return None

    # Autenticación exitosa
    log.info("User authenticated successfully", email=email, user_id=str(user_data.get('id')), roles=user_data.get('roles'))
    # Eliminar hash de contraseña antes de devolver los datos
    user_data.pop('hashed_password', None)
    return user_data

# --- Creación de Tokens JWT ---

def create_access_token(
    user_id: uuid.UUID,
    email: str,
    company_id: Optional[uuid.UUID] = None,
    roles: Optional[List[str]] = None, # <-- AÑADIDO: Parámetro roles
    # full_name: Optional[str] = None,
    expires_delta: timedelta = timedelta(days=1) # Tiempo de expiración configurable
) -> str:
    """
    Crea un token JWT para el usuario autenticado.
    Incluye 'sub', 'exp', 'iat', 'email', opcionalmente 'company_id' y 'roles'.
    """
    expire = datetime.now(timezone.utc) + expires_delta
    issued_at = datetime.now(timezone.utc)

    to_encode: Dict[str, Any] = {
        "sub": str(user_id),         # Subject (ID de usuario)
        "exp": expire,               # Expiration Time
        "iat": issued_at,            # Issued At
        "email": email,              # Email del usuario
        # "aud": "authenticated",
        # "iss": "AtenexAuth",
    }

    # Añadir company_id si está presente
    if company_id:
        to_encode["company_id"] = str(company_id)

    # --- AÑADIDO: Añadir roles si están presentes ---
    if roles:
        # Asegurarse de que es una lista, aunque la llamada debería pasarla así
        to_encode["roles"] = roles if isinstance(roles, list) else [roles]

    # Codificar el token usando el secreto y algoritmo de la configuración
    try:
        encoded_jwt = jwt.encode(
            to_encode,
            settings.JWT_SECRET.get_secret_value(), # Obtener valor del SecretStr
            algorithm=settings.JWT_ALGORITHM
        )
        log.debug("Access token created", user_id=str(user_id), expires_at=expire.isoformat(), claims_keys=list(to_encode.keys()))
        return encoded_jwt
    except JOSEError as e:
        log.exception("Failed to encode JWT", error=str(e), user_id=str(user_id))
        raise HTTPException(status_code=500, detail="Could not create access token")


# --- Verificación de Tokens JWT ---

async def verify_token(token: str, require_company_id: bool = True) -> Dict[str, Any]:
    """
    Verifica la validez de un token JWT (firma, expiración, claims básicos).
    Opcionalmente requiere la presencia de un 'company_id' válido en el payload.
    Verifica que el usuario ('sub') exista y esté activo en la base de datos.
    Devuelve el payload completo, incluyendo 'roles' si existen en el token.

    Returns:
        El payload decodificado si el token es válido.

    Raises:
        HTTPException(401): Si el token es inválido (firma, exp, formato) o el usuario no existe/inactivo.
        HTTPException(403): Si 'require_company_id' es True y falta un 'company_id' válido.
        HTTPException(500): Error interno.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer error=\"invalid_token\""},
    )
    forbidden_exception = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="User authenticated, but required company association is missing or invalid in token.",
        headers={"WWW-Authenticate": "Bearer error=\"insufficient_scope\""}, # Indica falta de permisos/scope
    )
    user_inactive_exception = HTTPException( # Excepción específica para inactivos
        status_code=status.HTTP_401_UNAUTHORIZED, # Sigue siendo 401
        detail="User associated with token is inactive.",
        headers={"WWW-Authenticate": "Bearer error=\"invalid_token\", error_description=\"User inactive\""},
    )
    internal_error_exception = HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="An internal error occurred during token verification.",
    )

    if not settings.JWT_SECRET or settings.JWT_SECRET.get_secret_value() == "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME_IN_ENV_OR_SECRET":
         log.critical("FATAL: Attempting JWT verification with default or missing GATEWAY_JWT_SECRET!")
         raise internal_error_exception

    payload = {} # Inicializar payload
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True, # Verificar Issued At si está presente
                "verify_nbf": True, # Verificar Not Before si está presente
            }
        )
        log.debug("Token decoded successfully", payload_keys=list(payload.keys()))

        user_id_str = payload.get("sub")
        if not user_id_str:
            log.warning("Token verification failed: 'sub' claim missing.")
            credentials_exception.detail = "Token missing 'sub' (user ID) claim."
            raise credentials_exception

        try:
            user_id = uuid.UUID(user_id_str)
        except ValueError:
            log.warning("Token verification failed: 'sub' claim is not a valid UUID.", sub_value=user_id_str)
            credentials_exception.detail = "Invalid user ID format in token."
            raise credentials_exception

        # Verificar existencia y estado del usuario en DB
        user_db_data = await postgres_client.get_user_by_id(user_id)
        if not user_db_data:
            log.warning("Token verification failed: User specified in 'sub' not found in DB.", user_id=user_id_str)
            credentials_exception.detail = "User associated with token not found."
            raise credentials_exception
        if not user_db_data.get('is_active', False):
            log.warning("Token verification failed: User specified in 'sub' is inactive.", user_id=user_id_str)
            # Usar la excepción específica para inactivos
            raise user_inactive_exception

        # Actualizar payload con roles de la DB (más seguro que confiar sólo en el token)
        # Esto asegura que los roles están actualizados incluso si el token es viejo.
        db_roles = user_db_data.get('roles')
        if db_roles:
             payload['roles'] = db_roles # Sobreescribir/añadir roles desde la DB
             log.debug("Updated payload with roles from database", user_id=user_id_str, db_roles=db_roles)
        elif 'roles' in payload:
             # Si la DB no tiene roles pero el token sí, podría ser un estado inconsistente.
             # Por seguridad, eliminamos los roles del token si no están en DB.
             log.warning("Roles found in token but not in DB, removing from payload for consistency.", user_id=user_id_str, token_roles=payload.get('roles'))
             payload.pop('roles', None)


        # Validar 'company_id' si es requerido
        company_id_str: Optional[str] = payload.get("company_id")
        valid_company_id_present = False
        if company_id_str:
            try:
                uuid.UUID(company_id_str) # Validar formato UUID
                valid_company_id_present = True
            except ValueError:
                log.warning("Token verification: 'company_id' claim present but not a valid UUID.",
                           company_id_value=company_id_str, user_id=user_id_str)
                credentials_exception.detail = "Invalid company ID format in token."
                raise credentials_exception

        if require_company_id and not valid_company_id_present:
            log.info("Token verification failed: Required 'company_id' is missing.", user_id=user_id_str)
            # Pasar el user_id a la excepción para posible logging en la dependencia
            forbidden_exception.user_id = user_id_str # type: ignore
            raise forbidden_exception # Falla porque se requiere y no está

        log.info("Token verified successfully", user_id=user_id_str, company_id=company_id_str or "N/A", roles=payload.get('roles', 'N/A'))
        return payload # Devolver el payload completo y actualizado

    except ExpiredSignatureError:
        log.info("Token verification failed: Token has expired.")
        credentials_exception.detail = "Token has expired."
        credentials_exception.headers["WWW-Authenticate"] = 'Bearer error="invalid_token", error_description="The token has expired"'
        raise credentials_exception
    except JWTError as e:
        log.warning(f"JWT Verification Error: {e}", token_provided=True)
        credentials_exception.detail = f"Token validation failed: {e}"
        raise credentials_exception
    except HTTPException as e:
        # Re-lanzar excepciones HTTP ya manejadas (como 403 por company_id o 401 por inactivo)
        raise e
    except Exception as e:
        # Capturar cualquier otro error inesperado
        user_id_from_payload = payload.get('sub', 'unknown') if payload else 'unknown'
        log.exception(f"Unexpected error during token verification: {e}", user_id=user_id_from_payload)
        raise internal_error_exception