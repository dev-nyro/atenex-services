# api-gateway/app/routers/gateway_router.py
from fastapi import APIRouter, Request, Response, Depends, HTTPException, status, Path
from fastapi.responses import StreamingResponse
from typing import Optional, Annotated, Dict, Any
import httpx
import structlog
import asyncio
import uuid # Importar uuid

from app.core.config import settings
from app.auth.auth_middleware import StrictAuth # Dependencia estándar

log = structlog.get_logger(__name__)
router = APIRouter()

# Cliente HTTP global (inyectado desde lifespan)
http_client: Optional[httpx.AsyncClient] = None

# Cabeceras Hop-by-Hop (sin cambios)
HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length",
}

# Dependencia para obtener el cliente (sin cambios)
def get_client() -> httpx.AsyncClient:
    if http_client is None or http_client.is_closed:
        log.error("Gateway HTTP client is not available or closed.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway service dependency unavailable (HTTP Client)."
        )
    return client

# Función interna _proxy_request (sin cambios funcionales mayores, sólo añadimos log detallado)
async def _proxy_request(
    request: Request,
    target_url_str: str,
    client: httpx.AsyncClient,
    user_payload: Optional[Dict[str, Any]],
    # Aceptamos el path específico del backend para construir la URL
    backend_service_path: str
):
    method = request.method
    target_service_base_url = httpx.URL(target_url_str)

    # Construir la URL final para el backend usando el path específico
    target_url = target_service_base_url.copy_with(path=backend_service_path, query=request.url.query.encode("utf-8"))

    # 1. Preparar Headers (sin cambios)
    headers_to_forward = {}
    client_host = request.client.host if request.client else "unknown"
    x_forwarded_for = request.headers.get("x-forwarded-for", client_host)
    for name, value in request.headers.items():
        lower_name = name.lower()
        if lower_name not in HOP_BY_HOP_HEADERS and lower_name != "host":
            headers_to_forward[name] = value
    headers_to_forward["X-Forwarded-For"] = x_forwarded_for
    headers_to_forward["X-Forwarded-Proto"] = request.url.scheme
    headers_to_forward["X-Forwarded-Host"] = request.headers.get("host", "")
    request_id = getattr(request.state, 'request_id', None)
    if request_id: headers_to_forward["X-Request-ID"] = request_id

    # 2. Inyectar Headers de Contexto (sin cambios)
    log_context = {'request_id': request_id} if request_id else {}
    if user_payload and method.upper() != 'OPTIONS':
        user_id = user_payload.get('sub')
        company_id = user_payload.get('company_id')
        user_email = user_payload.get('email')
        if not user_id or not company_id:
             log.critical("Payload missing required fields after StrictAuth!", payload_keys=list(user_payload.keys()))
             raise HTTPException(status_code=500, detail="Internal authentication context error.")
        headers_to_forward['X-User-ID'] = str(user_id)
        headers_to_forward['X-Company-ID'] = str(company_id)
        log_context['user_id'] = str(user_id); log_context['company_id'] = str(company_id)
        if user_email: headers_to_forward['X-User-Email'] = str(user_email)
    bound_log = log.bind(**log_context)

    # 3. Preparar Body (sin cambios)
    request_body_bytes: Optional[bytes] = None
    if method.upper() in ["POST", "PUT", "PATCH"]:
        request_body_bytes = await request.body()
        bound_log.debug(f"Read request body for {method}", body_length=len(request_body_bytes) if request_body_bytes else 0)

    # 4. Realizar petición downstream (log mejorado)
    bound_log.info(f"Proxying request '{method} {request.url.path}' to backend", backend_target=str(target_url))

    rp: Optional[httpx.Response] = None
    try:
        req = client.build_request(method=method, url=target_url, headers=headers_to_forward, content=request_body_bytes)
        rp = await client.send(req, stream=True)

        # 5. Procesar respuesta (log mejorado)
        bound_log.info(f"Received response from backend for '{method} {request.url.path}'", status_code=rp.status_code, backend_target=str(target_url))
        response_headers = {k: v for k, v in rp.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}
        return StreamingResponse(rp.aiter_raw(), status_code=rp.status_code, headers=response_headers, media_type=rp.headers.get("content-type"), background=rp.aclose)

    # Manejo de errores (sin cambios)
    except httpx.TimeoutException as exc:
         bound_log.error(f"Proxy timeout", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Upstream service timed out.")
    except httpx.ConnectError as exc:
         bound_log.error(f"Proxy connection error", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not connect to upstream service.")
    except httpx.RequestError as exc:
         bound_log.error(f"Proxy request error", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Error communicating with upstream service.")
    except Exception as exc:
        bound_log.exception(f"Proxy unexpected error", target=str(target_url))
        if rp: await rp.aclose()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal gateway error during proxy.")


# --- Rutas Proxy Específicas ---

# 1. Proxy para Query Service

# GET /api/v1/query/chats
@router.get(
    "/api/v1/query/chats",
    dependencies=[Depends(StrictAuth)],
    tags=["Proxy - Query"],
    summary="List user chats via Query Service",
    response_description="Proxied response from Query Service.",
)
async def proxy_get_chats(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: StrictAuth # Inyecta el payload validado
):
    """Proxies GET /chats request to the Query Service."""
    # El path en el backend es simplemente "/api/v1/chats" o similar, según el Query Service
    # El README del Query Service indica que sus endpoints empiezan con /api/v1
    backend_path = "/api/v1/chats"
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

# POST /api/v1/query/ask
@router.post(
    "/api/v1/query/ask",
    dependencies=[Depends(StrictAuth)],
    tags=["Proxy - Query"],
    summary="Send a query via Query Service",
    response_description="Proxied response from Query Service.",
)
async def proxy_post_ask(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: StrictAuth
):
    """Proxies POST /ask request to the Query Service."""
    # Path en el Query Service
    backend_path = "/api/v1/ask" # Ajustar si el endpoint es diferente en Query Service
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

# GET /api/v1/query/chats/{chat_id}/messages
@router.get(
    "/api/v1/query/chats/{chat_id}/messages",
    dependencies=[Depends(StrictAuth)],
    tags=["Proxy - Query"],
    summary="Get chat messages via Query Service",
    response_description="Proxied response from Query Service.",
)
async def proxy_get_chat_messages(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: StrictAuth,
    chat_id: uuid.UUID = Path(..., title="The ID of the chat to fetch messages for")
):
    """Proxies GET /chats/{chat_id}/messages request to the Query Service."""
    backend_path = f"/api/v1/chats/{chat_id}/messages" # Path en el Query Service
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

# DELETE /api/v1/query/chats/{chat_id}
@router.delete(
    "/api/v1/query/chats/{chat_id}",
    dependencies=[Depends(StrictAuth)],
    tags=["Proxy - Query"],
    summary="Delete a chat via Query Service",
    response_description="Proxied response from Query Service (likely 204 No Content).",
)
async def proxy_delete_chat(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: StrictAuth,
    chat_id: uuid.UUID = Path(..., title="The ID of the chat to delete")
):
    """Proxies DELETE /chats/{chat_id} request to the Query Service."""
    backend_path = f"/api/v1/chats/{chat_id}" # Path en el Query Service
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

# Añadir ruta OPTIONS explícita para las rutas con path parameters si CORS lo requiere
# FastAPI suele manejarlas, pero por si acaso:
@router.options(
    "/api/v1/query/chats/{chat_id}/messages",
    tags=["Proxy - Query"], include_in_schema=False
)
async def options_get_chat_messages(): return Response(status_code=200)
@router.options(
    "/api/v1/query/chats/{chat_id}",
    tags=["Proxy - Query"], include_in_schema=False
)
async def options_delete_chat(): return Response(status_code=200)


# 2. Proxy para Ingest Service (requiere ruta genérica por flexibilidad)

# *** Mantenemos la ruta genérica para ingest ya que sus endpoints pueden variar más ***
# Podrías hacerla específica si solo usas /upload, etc.
@router.api_route(
    "/api/v1/ingest/{endpoint_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], # OPTIONS aquí también
    dependencies=[Depends(StrictAuth)],
    tags=["Proxy - Ingest"],
    summary="Proxy to Ingest Service (Authentication Required)",
    response_description="Response proxied directly from the Ingest Service.",
    name="proxy_ingest_service",
)
async def proxy_ingest_service_generic(
    request: Request,
    endpoint_path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: StrictAuth
):
    """Reenvía peticiones a /api/v1/ingest/* al Ingest Service."""
    # El backend_path es lo que viene después de /api/v1/ingest/
    backend_path = f"/{endpoint_path}"
    return await _proxy_request(
        request,
        str(settings.INGEST_SERVICE_URL),
        client,
        user_payload,
        backend_service_path=backend_path # Pasar el path específico
    )

# 3. Proxy para Auth Service (Opcional y genérico)
if settings.AUTH_SERVICE_URL:
    log.info(f"Auth service proxy enabled for base URL: {settings.AUTH_SERVICE_URL}")
    @router.api_route(
        "/api/v1/auth/{endpoint_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], # OPTIONS
        tags=["Proxy - Auth"],
        summary="Proxy to Authentication Service (No Gateway Authentication)",
        response_description="Response proxied directly from the Auth Service.",
        name="proxy_auth_service",
    )
    async def proxy_auth_service_generic(
        request: Request,
        endpoint_path: str,
        client: Annotated[httpx.AsyncClient, Depends(get_client)],
    ):
        """Proxy genérico para el servicio de autenticación."""
        backend_path = f"/{endpoint_path}"
        return await _proxy_request(
            request,
            str(settings.AUTH_SERVICE_URL),
            client,
            user_payload=None, # Sin autenticación aquí
            backend_service_path=backend_path
        )
# else: ya no es necesario el log aquí
