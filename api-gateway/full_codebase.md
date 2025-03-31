# Estructura de la Codebase

```
app/
├── __init__.py
├── auth
│   ├── __init__.py
│   ├── auth_middleware.py
│   └── jwt_handler.py
├── config
│   └── __init__.py
├── config.py
├── core
│   └── logging
│       ├── __init__.py
│       ├── handlers.py
│       ├── logger.py
│       └── middleware.py
├── main.py
├── routers
│   ├── __init__.py
│   ├── auth_router.py
│   └── gateway_router.py
└── utils
    └── __init__.py
```

# Codebase: `app`

## File: `app\__init__.py`
```py
# ...existing code or leave empty...

```

## File: `app\auth\__init__.py`
```py

```

## File: `app\auth\auth_middleware.py`
```py
from fastapi import Request, HTTPException, status, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional, Annotated
from .jwt_handler import verify_token
import logging

logger = logging.getLogger(__name__)

# Define el esquema de seguridad para documentación OpenAPI
bearer_scheme = HTTPBearer(bearerFormat="JWT", auto_error=False) # auto_error=False para manejar nosotros el error

async def get_current_user_payload(
    request: Request,
    authorization: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]
) -> Optional[dict]:
    """
    Dependencia para validar el token JWT y añadir el payload a request.state.
    Permite el acceso si el token es válido, levanta 401/403 si es inválido o falta (para rutas protegidas).
    """
    if authorization is None:
        # Si la ruta requiere autenticación explícita, fallará más adelante.
        # Si es opcional, request.state.user será None.
        logger.debug("No Authorization header found.")
        request.state.user = None
        return None

    token = authorization.credentials
    try:
        payload = verify_token(token)
        request.state.user = payload
        logger.debug(f"Token valid. User payload set: {payload.get('sub')}")
        return payload
    except HTTPException as e:
        # Propaga la excepción HTTP generada por verify_token (401 o 500)
        logger.warning(f"Token verification failed: {e.detail}")
        raise e
    except Exception as e:
        # Captura cualquier otro error inesperado
        logger.error(f"Unexpected error verifying token: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not process authentication token.",
        )

async def require_user(
    user_payload: Annotated[Optional[dict], Depends(get_current_user_payload)]
) -> dict:
    """
    Dependencia que asegura que una ruta requiere un usuario autenticado.
    Reutiliza get_current_user_payload y levanta 401 si no hay usuario.
    """
    if user_payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_payload

# Opcional: Middleware si quieres aplicar JWTBearer a todas las rutas por defecto
# class JWTMiddleware(BaseHTTPMiddleware):
#     async def dispatch(self, request: Request, call_next):
#         # Rutas públicas (ej: /docs, /openapi.json, /health)
#         public_paths = ["/docs", "/openapi.json", "/redoc", "/", "/health"]
#         if request.url.path in public_paths or request.url.path.startswith("/static"):
#              return await call_next(request)

#         try:
#             # Intenta obtener el payload, manejará errores si el token es inválido/falta
#             await get_current_user_payload(request, await bearer_scheme(request))
#             response = await call_next(request)
#             return response
#         except HTTPException as e:
#             # Devuelve la respuesta de error generada por get_current_user_payload
#             return JSONResponse(
#                 status_code=e.status_code,
#                 content={"detail": e.detail},
#                 headers=e.headers
#             )

# auth_middleware = JWTMiddleware() # Si usas el middleware
```

## File: `app\auth\jwt_handler.py`
```py
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from jose import JWTError, jwt
from fastapi import HTTPException, status
from ..config import settings # Asegúrate que importe desde el config.py refactorizado

# JWT Configuration
SECRET_KEY = settings.JWT_SECRET
ALGORITHM = settings.JWT_ALGORITHM
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES

# --- Función create_access_token ---
# Esta función NO debería estar en el Gateway si no genera tokens.
# Si el login/registro se hace en un Auth Service, el Gateway no crea tokens.
# La mantenemos aquí SÓLO si se decide mantener el endpoint /login proxied
# y el Auth Service devuelve los datos para crear el token aquí.
# Es preferible que el Auth Service genere el token.
# Por ahora, la comentamos o eliminamos si no se usa.
"""
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    # ... (Lógica original si es necesaria) ...
    pass
"""

def verify_token(token: str) -> Dict[str, Any]:
    """Verifica el token y devuelve el payload."""
    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM]
        )
        # Validar expiración manualmente por si acaso
        exp = payload.get("exp")
        if exp is None:
            raise JWTError("Token has no expiration")
        if datetime.utcnow() > datetime.utcfromtimestamp(exp):
            raise JWTError("Token has expired")

        # Validar claims mínimos necesarios para el routing/forwarding
        if not all(key in payload for key in ['sub', 'user_id', 'company_id', 'role']):
             raise JWTError("Token missing required claims (sub, user_id, company_id, role)")

        return payload
    except JWTError as e:
        print(f"JWT Verification Error: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Could not validate credentials: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as e:
        print(f"Unexpected error during token verification: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred during token verification",
        )
```

## File: `app\config\__init__.py`
```py
from pydantic import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    # Database settings
    supabase_url: str
    supabase_key: str
    supabase_service_key: str
    
    # JWT settings
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    
    # Milvus settings
    host: str = "localhost"  # Changed from milvus_host
    port: int = 19530       # Changed from milvus_port
    collection_name: str = "document_chunks"
    embedding_model: str = "all-MiniLM-L6-v2"
    vector_dim: int = 384

    class Config:
        env_file = '.env'
        env_file_encoding = 'utf-8'
        case_sensitive = False
        allow_mutation = False
        extra = 'allow'

@lru_cache()
def get_settings() -> Settings:
    return Settings()

settings = get_settings()

__all__ = ['settings']

```

## File: `app\config.py`
```py
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
from typing import Optional

class Settings(BaseSettings):
    # Service URLs
    INGEST_SERVICE_URL: str = "http://ingest-service.nyro-develop.svc.cluster.local:8000" # Ejemplo K8s
    QUERY_SERVICE_URL: str = "http://query-service.nyro-develop.svc.cluster.local:8000" # Ejemplo K8s
    # AUTH_SERVICE_URL: Optional[str] = None # Descomentar si se usa un servicio de auth externo para login/register

    # JWT settings (Necesario si el Gateway valida tokens)
    JWT_SECRET: str = "YOUR_SUPER_SECRET_KEY" # Asegúrate que sea el mismo que usan los otros servicios
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30 # O el valor que corresponda

    # Logging
    LOG_LEVEL: str = "INFO"

    # HTTP Client settings
    HTTP_CLIENT_TIMEOUT: int = 30
    HTTP_CLIENT_MAX_RETRIES: int = 2

    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        case_sensitive=False,
        env_prefix='GATEWAY_' # Prefijo para variables de entorno
    )

@lru_cache()
def get_settings() -> Settings:
    print("Loading Gateway settings...")
    settings = Settings()
    # Ocultar secreto en logs
    print(f"Loaded settings: INGEST_SERVICE_URL={settings.INGEST_SERVICE_URL}, QUERY_SERVICE_URL={settings.QUERY_SERVICE_URL}, JWT_SECRET=***")
    return settings

settings = get_settings()
```

## File: `app\core\logging\__init__.py`
```py

```

## File: `app\core\logging\handlers.py`
```py

```

## File: `app\core\logging\logger.py`
```py
import logging
import json
from datetime import datetime
import sys
from pathlib import Path

# Get absolute path
LOGS_DIR = Path(__file__).parents[3] / "logs"
LOGS_DIR.mkdir(exist_ok=True)

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_object = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "module": record.module,
            "funcName": record.funcName,
            "pathname": record.pathname,
            "lineno": record.lineno
        }
        if hasattr(record, "correlation_id"):
            log_object["correlation_id"] = record.correlation_id
        return json.dumps(log_object)

def setup_logger():
    logger = logging.getLogger("fastapi_app")
    logger.setLevel(logging.INFO)
    
    # Clear any existing handlers
    logger.handlers = []
    
    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(JSONFormatter())
    logger.addHandler(console_handler)
    
    # File Handler with immediate flush
    file_handler = logging.FileHandler(str(LOGS_DIR / "app.log"), mode='a')
    file_handler.setFormatter(JSONFormatter())
    file_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    
    # Error Handler
    error_handler = logging.FileHandler(str(LOGS_DIR / "error.log"), mode='a')
    error_handler.setFormatter(JSONFormatter())
    error_handler.setLevel(logging.ERROR)
    logger.addHandler(error_handler)
    
    # Force flush on every log
    logger.propagate = False
    
    # Test log
    logger.info("Logger initialized successfully")
    
    return logger

logger = setup_logger()

__all__ = ['logger']
```

## File: `app\core\logging\middleware.py`
```py
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
import time
import uuid
from .logger import logger
import traceback

class RequestLoggingMiddleware(BaseHTTPMiddleware):  # Rename to match the import in main.py
    async def dispatch(self, request: Request, call_next):
        correlation_id = str(uuid.uuid4())
        start_time = time.time()
        
        # Log initial request
        logger.info("Request started", extra={
            "correlation_id": correlation_id,
            "method": request.method,
            "path": request.url.path,
            "client_ip": request.client.host,
            "query_params": str(request.query_params),
            "headers": {k: v for k, v in request.headers.items() if k.lower() != "authorization"}
        })
        
        try:
            response = await call_next(request)
            
            process_time = time.time() - start_time
            status_code = response.status_code
            
            # Log errors (4xx, 5xx)
            if status_code >= 400:
                logger.error("Request failed", extra={
                    "correlation_id": correlation_id,
                    "status_code": status_code,
                    "method": request.method,
                    "path": request.url.path,
                    "process_time": f"{process_time:.3f}s",
                    "error_type": "HTTP_ERROR"
                })
            else:
                # Log successful requests
                logger.info("Request completed", extra={
                    "correlation_id": correlation_id,
                    "status_code": status_code,
                    "process_time": f"{process_time:.3f}s"
                })
            
            return response
            
        except Exception as e:
            process_time = time.time() - start_time
            # Log unhandled exceptions
            logger.error("Request failed with exception", extra={
                "correlation_id": correlation_id,
                "error_type": type(e).__name__,
                "error_message": str(e),
                "path": request.url.path,
                "method": request.method,
                "process_time": f"{process_time:.3f}s",
                "traceback": traceback.format_exc()
            })
            raise

__all__ = ['RequestLoggingMiddleware']
```

## File: `app\main.py`
```py
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import httpx
import logging

# Configuración y Settings
from .config import settings

# Routers (Solo el gateway y quizás auth si se proxifica)
from .routers import gateway_router
# from .routers import auth_router # Descomentar si se usa auth_router localmente o proxificado

# Logging (Asegúrate que tu configuración de logging esté correcta)
# from .core.logging.middleware import RequestLoggingMiddleware # Si usas tu middleware de logging
# from .core.logging.logger import setup_logger
# logger = setup_logger() # O usa el logger estándar de FastAPI/Uvicorn

logging.basicConfig(level=settings.LOG_LEVEL.upper())
logger = logging.getLogger("api_gateway")

# --- Ciclo de vida de la aplicación para gestionar el cliente HTTP ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Inicializar cliente HTTP
    logger.info("Starting up API Gateway...")
    limits = httpx.Limits(max_keepalive_connections=100, max_connections=200)
    timeout = httpx.Timeout(settings.HTTP_CLIENT_TIMEOUT)
    # Configurar reintentos si es necesario (requiere tenacity)
    # transport = httpx.AsyncHTTPTransport(retries=settings.HTTP_CLIENT_MAX_RETRIES)
    gateway_router.http_client = httpx.AsyncClient(
        # transport=transport, # Descomentar si se usa retries
        limits=limits,
        timeout=timeout,
        follow_redirects=False # Generalmente un gateway no sigue redirects
    )
    logger.info(f"HTTP Client initialized for Ingest: {settings.INGEST_SERVICE_URL}, Query: {settings.QUERY_SERVICE_URL}")
    yield
    # Shutdown: Cerrar cliente HTTP
    logger.info("Shutting down API Gateway...")
    if gateway_router.http_client:
        await gateway_router.http_client.aclose()
        logger.info("HTTP Client closed.")

# --- Creación de la aplicación FastAPI ---
app = FastAPI(
    title="🚀 Nyro API Gateway",
    description="Punto de entrada único para los microservicios de Nyro.",
    version="1.0.0",
    lifespan=lifespan # Usar el nuevo gestor de ciclo de vida
)

# --- Middlewares ---
# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Cambiar en producción
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Logging Middleware (Opcional, si tienes uno personalizado)
# app.add_middleware(RequestLoggingMiddleware)

# --- Routers ---
# Incluir el router principal del gateway
app.include_router(gateway_router.router)

# Incluir el router de autenticación si se decide mantener/proxificar
# app.include_router(auth_router.router, prefix="/api/auth", tags=["Authentication"])

# --- Endpoints Básicos ---
@app.get("/", tags=["Health Check"])
async def root():
    """Endpoint raíz para verificar que el Gateway está activo."""
    return {"message": "Nyro API Gateway is running!"}

@app.get("/health", tags=["Health Check"])
async def health_check():
    """Endpoint de health check para Kubernetes probes."""
    # Podría expandirse para verificar la conexión con el cliente HTTP
    if gateway_router.http_client is None or gateway_router.http_client.is_closed:
        raise HTTPException(status_code=503, detail="Gateway HTTP client not ready.")
    return {"status": "healthy"}

# --- Manejador de Excepciones Global (Opcional) ---
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception for {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred in the gateway."},
    )

logger.info("API Gateway application configured.")
```

## File: `app\routers\__init__.py`
```py
# This file can be empty, but it should exist to make the directory a proper Python package

```

## File: `app\routers\auth_router.py`
```py
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from datetime import timedelta, datetime
from ..auth.jwt_handler import create_access_token, ACCESS_TOKEN_EXPIRE_MINUTES
from ..models.user_model import User, UserAuth, Token, UserCreate  # Added UserCreate
from ..config.database import get_supabase_client
from ..auth.auth_middleware import auth_middleware
from uuid import UUID
from passlib.context import CryptContext

router = APIRouter(
    prefix="/api/auth",  # Keep this as /api/auth
    tags=["Authentication"],
    redirect_slashes=True  # Add this to handle both with and without trailing slash
)

# Update the token URL to match the full path
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")  # Remove leading slash

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

@router.post("/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    """
    Autenticación de usuario
    
    ### Descripción
    Autentica un usuario y retorna un token JWT.
    
    ### Parámetros
    - **username**: Email del usuario
    - **password**: Contraseña del usuario
    
    ### Retorna
    - **access_token**: Token JWT para autenticación
    - **token_type**: Tipo de token (bearer)
    """
    supabase = get_supabase_client(use_service_role=True)
    
    try:
        print(f"Attempting login for user: {form_data.username}")  # Debug info
        user_query = supabase.table('users').select("*").eq('email', form_data.username).execute()
        
        if not user_query.data:
            print("User not found")  # Debug info
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password",
                headers={"WWW-Authenticate": "Bearer"},
            )
        
        user = user_query.data[0]
        print(f"Found user data: {user}")  # Debug info
        
        # Verify password using pwd_context
        if not pwd_context.verify(form_data.password, user['hashed_password']):
            print("Password verification failed")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password",
                headers={"WWW-Authenticate": "Bearer"},
            )
        
        # Create access token with all necessary data
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        token_data = {
            "sub": user['email'],
            "user_id": str(user['id']),  # Ensure it's a string
            "company_id": str(user['company_id']),  # Ensure it's a string
            "role": user['role']  # Make sure role is included
        }
        print(f"Creating token with data: {token_data}")  # Debug info
        
        access_token = create_access_token(
            data=token_data,
            expires_delta=access_token_expires
        )
        
        # Remove sensitive data
        user_response = dict(user)
        user_response.pop('hashed_password', None)
        
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user": user_response
        }
        
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Login error: {str(e)}")  # For debugging
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed",
            headers={"WWW-Authenticate": "Bearer"},
        )

@router.post("/register", response_model=Token)
async def register(user_data: UserCreate):
    """
    Registro de nuevo usuario
    
    ### Descripción
    Crea un nuevo usuario en el sistema y retorna un token JWT.
    
    ### Parámetros
    - **email**: Email del usuario
    - **password**: Contraseña del usuario
    - **full_name**: Nombre completo
    - **company_id**: ID de la empresa (opcional)
    
    ### Retorna
    - **access_token**: Token JWT para autenticación
    - **token_type**: Tipo de token (bearer)
    """
    supabase = get_supabase_client(use_service_role=True)  # Changed to use service role
    
    try:
        # Check if company exists - use str for UUID comparison
        company = supabase.table('companies').select("*").eq('id', str(user_data.company_id)).execute()
        if not company.data:
            raise HTTPException(
                status_code=400,
                detail="Invalid company_id"
            )
        
        # Check if user exists
        existing = supabase.table('users').select("*").eq('email', user_data.email).execute()
        if existing.data:
            raise HTTPException(
                status_code=400,
                detail="Email already registered"
            )
        
        # Create new user with service role
        new_user = {
            "email": user_data.email,
            "hashed_password": pwd_context.hash(user_data.password),  # Hash the password properly
            "full_name": user_data.full_name,
            "role": user_data.role,
            "company_id": str(user_data.company_id),
            "is_active": True,
            "created_at": datetime.utcnow().isoformat()
        }
        
        print(f"Attempting to insert user: {new_user}")
        response = supabase.table('users').insert(new_user).execute()
        
        if not response.data:
            raise HTTPException(
                status_code=500,
                detail="Failed to create user"
            )
            
        user = response.data[0]
        print(f"User created: {user}")
        
        # Create access token with company_id
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={
                "sub": user['email'],
                "user_id": user['id'],
                "company_id": str(user['company_id']),
                "role": user['role']  # Include role in token
            },
            expires_delta=access_token_expires
        )
        
        # Remove sensitive data
        user.pop('hashed_password', None)
        
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user": user
        }
        
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Register error: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

@router.get("/me", dependencies=[Depends(auth_middleware)])
async def get_current_user(request: Request):
    """
    Test endpoint to verify token and get current user info
    """
    user_payload = request.state.user
    return {
        "message": "Token is valid",
        "user": user_payload
    }
```

## File: `app\routers\gateway_router.py`
```py
from fastapi import APIRouter, Request, Response, Depends, HTTPException, status, Header
from fastapi.responses import StreamingResponse
from typing import Optional, Annotated
import httpx
import logging
from ..config import settings
from ..auth.auth_middleware import require_user # Importar la dependencia de autenticación

logger = logging.getLogger(__name__)
router = APIRouter()

# Cliente HTTP reutilizable (se inicializará en main.py)
http_client: Optional[httpx.AsyncClient] = None

# Headers que no deben pasarse directamente downstream
EXCLUDED_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
}

def get_client() -> httpx.AsyncClient:
    """Dependencia para obtener el cliente HTTP."""
    if http_client is None:
        # Esto no debería ocurrir si el startup event funciona
        logger.error("HTTP Client not initialized.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway service is not ready."
        )
    return http_client

async def _proxy_request(
    request: Request,
    target_url: str,
    client: httpx.AsyncClient,
    user_payload: Optional[dict] = None # Payload del usuario autenticado
):
    """Función interna para realizar el proxy de la petición."""
    method = request.method
    url = httpx.URL(target_url)
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in EXCLUDED_HEADERS
    }
    # Asegurarse de pasar X-Company-ID si existe en el token
    if user_payload and 'company_id' in user_payload:
         # Los servicios esperan X-Company-ID, no company_id
        headers['X-Company-ID'] = str(user_payload['company_id'])
    elif 'x-company-id' in headers:
        # Si ya viene en la request original (ej: desde un test o cliente específico)
        pass # Ya está incluido
    # else:
        # Podrías levantar un error si X-Company-ID es siempre requerido y no está en el token
        # raise HTTPException(status_code=400, detail="Missing Company ID in token or header")

    # Asegurarse de pasar el token de autorización si existe
    if 'authorization' in headers and user_payload:
        # Ya está incluido en los headers copiados
        pass
    elif user_payload:
        # Si la autenticación se hizo pero no había header (poco probable con Depends(require_user))
        # Podríamos necesitar reconstruir el token si el downstream lo necesita tal cual
        logger.warning("User authenticated but no Authorization header found in original request.")

    # Eliminar header 'host' si causa problemas
    headers.pop('host', None)


    query_params = request.query_params
    content = await request.body()

    logger.debug(f"Proxying {method} {request.url.path} -> {url}")
    logger.debug(f"Forwarding Headers: {headers}")
    logger.debug(f"Forwarding Query Params: {query_params}")
    # logger.debug(f"Forwarding Body: {content[:100]}...") # Cuidado con loguear bodies grandes/sensibles

    try:
        rp = await client.request(
            method=method,
            url=url,
            headers=headers,
            params=query_params,
            content=content,
            timeout=settings.HTTP_CLIENT_TIMEOUT,
        )

        # Log de la respuesta del downstream
        logger.debug(f"Downstream Response: Status={rp.status_code}, Headers={rp.headers}")
        # logger.debug(f"Downstream Body: {rp.content[:100]}...")

        # Excluir ciertos headers de la respuesta también
        response_headers = {
            k: v for k, v in rp.headers.items() if k.lower() not in EXCLUDED_HEADERS
        }

        # Devolver como StreamingResponse para manejar diferentes tipos de contenido
        return StreamingResponse(
            rp.aiter_raw(),
            status_code=rp.status_code,
            headers=response_headers,
            media_type=rp.headers.get("content-type"),
        )

    except httpx.TimeoutException:
        logger.error(f"Request to {url} timed out.")
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Upstream service timed out.")
    except httpx.ConnectError:
        logger.error(f"Could not connect to {url}.")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Upstream service unavailable.")
    except Exception as e:
        logger.error(f"Error proxying request to {url}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Gateway error.")


@router.api_route(
    "/api/v1/ingest/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    dependencies=[Depends(require_user)], # Proteger rutas de ingesta
    include_in_schema=False # Ocultar de OpenAPI, ya que son rutas proxy
)
async def proxy_ingest(
    request: Request,
    path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user: Annotated[dict, Depends(require_user)] # Obtener payload del usuario
):
    """Proxy para el Ingest Service."""
    base_url = settings.INGEST_SERVICE_URL.rstrip('/')
    target_url = f"{base_url}/api/v1/ingest/{path}"
    logger.info(f"Routing to Ingest Service: {target_url}")
    return await _proxy_request(request, target_url, client, user)

@router.api_route(
    "/api/v1/query/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    dependencies=[Depends(require_user)], # Proteger rutas de consulta
    include_in_schema=False
)
async def proxy_query(
    request: Request,
    path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user: Annotated[dict, Depends(require_user)]
):
    """Proxy para el Query Service."""
    base_url = settings.QUERY_SERVICE_URL.rstrip('/')
    target_url = f"{base_url}/api/v1/query/{path}"
    logger.info(f"Routing to Query Service: {target_url}")
    return await _proxy_request(request, target_url, client, user)

# --- Rutas de Autenticación (Opcional: si se exponen login/register vía Gateway) ---
# Si Auth es un servicio separado, se haría proxy similar a ingest/query.
# Si se mantiene la lógica original (NO RECOMENDADO):
# from .auth_router import router as auth_router_impl
# router.include_router(auth_router_impl, prefix="/api/auth", tags=["Authentication"])
# Asegurándose que auth_router_impl no interactúe con DB directamente.

# Por ahora, definimos un placeholder si se necesita exponerlas:
@router.api_route(
    "/api/auth/{path:path}",
    methods=["POST"], # Típicamente POST para login/register
    include_in_schema=False # Asumiendo que se documentan por separado o en el Auth Service
)
async def proxy_auth(
    request: Request,
    path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)]
):
    """Proxy para el Auth Service (si existe)."""
    if not settings.AUTH_SERVICE_URL:
         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Auth endpoint not configured")

    base_url = settings.AUTH_SERVICE_URL.rstrip('/')
    target_url = f"{base_url}/api/auth/{path}"
    logger.info(f"Routing to Auth Service: {target_url}")
    # Auth requests no necesariamente tienen un usuario autenticado (ej: login)
    return await _proxy_request(request, target_url, client, user_payload=None)
```

## File: `app\utils\__init__.py`
```py
from .storage import storage, StorageManager
from .document_processor import document_processor
from .subscription_validator import check_document_limits

__all__ = [
    'storage',
    'StorageManager',
    'document_processor',
    'check_document_limits'
]

```
