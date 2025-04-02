# api-gateway/app/routers/gateway_router.py
from fastapi import APIRouter, Request, Response, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from typing import Optional, Annotated, Dict, Any
import httpx
import structlog
import asyncio # Para timeouts específicos

from app.core.config import settings
from app.auth.auth_middleware import require_user # Dependencia para proteger rutas

log = structlog.get_logger(__name__)
router = APIRouter()

# Cliente HTTP global reutilizable (se inicializará/cerrará en main.py lifespan)
http_client: Optional[httpx.AsyncClient] = None

# Headers que no deben pasarse directamente downstream ni upstream
# Añadir otros si es necesario (e.g., server, x-powered-by)
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host", # Host siempre debe ser el del servicio destino
    "content-length", # Será recalculado por httpx o el servidor destino
}

def get_client() -> httpx.AsyncClient:
    """Dependencia para obtener el cliente HTTP inicializado."""
    if http_client is None or http_client.is_closed:
        log.error("Gateway HTTP client is not available or closed.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway service is temporarily unavailable (client error)."
        )
    return http_client

# api-gateway/app/routers/gateway_router.py
# ... (importaciones y código existente) ...

async def _proxy_request(
    request: Request,
    target_url: str,
    client: httpx.AsyncClient,
    # user_payload es el diccionario devuelto por verify_token (via require_user)
    user_payload: Optional[Dict[str, Any]] # Puede ser None para rutas no protegidas
):
    """Función interna para realizar el proxy de la petición."""
    method = request.method
    downstream_url = httpx.URL(target_url)
    log_context = {} # Para añadir al log

    # 1. Preparar Headers
    headers_to_forward = {}
    # ... (código para copiar headers y quitar hop-by-hop) ...

    # 2. Inyectar Headers basados en el Payload del Token (SI EXISTE)
    if user_payload:
        # Extraer user_id (del claim 'sub')
        user_id = user_payload.get('sub')
        if user_id:
            headers_to_forward['X-User-ID'] = str(user_id)
            log_context['user_id'] = user_id
        else:
            log.warning("User payload present but 'sub' (user_id) claim missing!", payload_keys=list(user_payload.keys()))
            # Decide si esto es un error fatal (403) o si puedes continuar

        # Extraer company_id (que añadimos en verify_token)
        company_id = user_payload.get('company_id')
        if company_id:
            headers_to_forward['X-Company-ID'] = str(company_id) # Asegurar string
            log_context['company_id'] = company_id
        else:
            # Esto no debería ocurrir si verify_token lo requiere y lo añade,
            # pero es una verificación de seguridad adicional.
            log.error("CRITICAL: Valid user payload is missing 'company_id'!", payload_info=user_payload)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal configuration error: Company ID missing after auth.")

        # Podrías extraer y añadir otros headers como X-User-Email, X-User-Roles etc.
        user_email = user_payload.get('email')
        if user_email:
             headers_to_forward['X-User-Email'] = str(user_email)
             log_context['user_email'] = user_email

    # Vincular contexto al logger para esta petición
    log_with_context = log.bind(**log_context)

    # ... (código para preparar query params y body) ...
    query_params = request.query_params
    request_body_bytes = request.stream()

    # 3. Realizar la petición downstream
    log_with_context.info(f"Proxying request", method=method, path=request.url.path, target=str(downstream_url))

    try:
        req = client.build_request(
            method=method,
            url=downstream_url,
            headers=headers_to_forward,
            params=query_params,
            content=request_body_bytes
        )
        rp = await client.send(req, stream=True)

        # ... (código para procesar y devolver la respuesta) ...
        log_with_context.info(f"Received response from downstream", status_code=rp.status_code, target=str(downstream_url))
        # ... (filtrar headers de respuesta) ...
        response_headers = {k: v for k, v in rp.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}

        return StreamingResponse(
            rp.aiter_raw(),
            status_code=rp.status_code,
            headers=response_headers,
            media_type=rp.headers.get("content-type"),
        )

    # ... (manejo de excepciones httpx) ...
    except httpx.TimeoutException as exc:
         log_with_context.error(f"Request timed out", target=str(downstream_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=f"Upstream service timeout.")
    except httpx.ConnectError as exc:
         log_with_context.error(f"Connection error", target=str(downstream_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Upstream service unavailable.")
    except Exception as exc:
        log_with_context.exception(f"Unexpected error during proxy", target=str(downstream_url))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal gateway error.")
    finally:
        # Cerrar respuesta si es necesario
        if 'rp' in locals() and rp and hasattr(rp, 'aclose') and callable(rp.aclose):
             await rp.aclose()


# --- Rutas Proxy ---
# Asegúrate que las rutas que requieren autenticación tengan `Depends(require_user)`

@router.api_route(
    "/api/v1/ingest/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    dependencies=[Depends(require_user)], # <-- REQUIERE AUTH
    tags=["Proxy - Ingest"],
    summary="Proxy to Ingest Service (Auth Required)",
)
async def proxy_ingest_service(
    request: Request,
    path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    # Inyecta el payload validado por require_user
    user_payload: Annotated[Dict[str, Any], Depends(require_user)]
):
    base_url = settings.INGEST_SERVICE_URL.rstrip('/')
    target_url = f"{base_url}/api/v1/ingest/{path}" # Reconstruir URL destino
    if request.url.query: target_url += f"?{request.url.query}"
    return await _proxy_request(request, target_url, client, user_payload)

@router.api_route(
    "/api/v1/query/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    dependencies=[Depends(require_user)], # <-- REQUIERE AUTH
    tags=["Proxy - Query"],
    summary="Proxy to Query Service (Auth Required)",
)
async def proxy_query_service(
    request: Request,
    path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: Annotated[Dict[str, Any], Depends(require_user)]
):
    base_url = settings.QUERY_SERVICE_URL.rstrip('/')
    target_url = f"{base_url}/api/v1/query/{path}" # Reconstruir URL destino
    if request.url.query: target_url += f"?{request.url.query}"
    return await _proxy_request(request, target_url, client, user_payload)


# --- Proxy para Auth Service (OPCIONAL - SIN require_user) ---
# Si tienes un microservicio de Auth o quieres proxyficar llamadas a Supabase Auth
if settings.AUTH_SERVICE_URL:
    log.info(f"Auth service proxy enabled for: {settings.AUTH_SERVICE_URL}")
    @router.api_route(
        "/api/v1/auth/{path:path}", # Usar prefijo /api/v1 consistentemente?
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        tags=["Proxy - Auth"],
        summary="Proxy to Authentication Service (No Gateway Auth)",
    )
    async def proxy_auth_service(
        request: Request,
        path: str,
        client: Annotated[httpx.AsyncClient, Depends(get_client)],
        # NO hay dependencia require_user aquí
    ):
        """Proxy genérico para el servicio de autenticación."""
        base_url = settings.AUTH_SERVICE_URL.rstrip('/')
        # Construir URL destino, asumiendo que el Auth service espera la ruta completa
        # Ejemplo: /api/v1/auth/login -> http://auth-service/api/v1/auth/login
        target_url = f"{base_url}/api/v1/auth/{path}"
        if request.url.query: target_url += f"?{request.url.query}"

        # Pasar user_payload=None ya que estas rutas no requieren token *validado por el gateway*
        # (el token podría pasarse para operaciones como 'refresh' o 'get user info')
        return await _proxy_request(request, target_url, client, user_payload=None)
else:
     log.warning("Auth service proxy is not configured (GATEWAY_AUTH_SERVICE_URL not set).")
     # Podrías añadir una ruta aquí para devolver un 501 Not Implemented si se llama a /api/v1/auth/*
     @router.api_route("/api/v1/auth/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], include_in_schema=False)
     async def auth_not_configured(path: str):
         raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Authentication endpoint proxy not configured in the gateway.")