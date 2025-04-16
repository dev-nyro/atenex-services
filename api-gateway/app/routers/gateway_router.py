# File: app/routers/gateway_router.py
# api-gateway/app/routers/gateway_router.py
from fastapi import APIRouter, Request, Response, Depends, HTTPException, status, Path
from fastapi.responses import StreamingResponse
from typing import Optional, Annotated, Dict, Any
import httpx
import structlog
import asyncio
import uuid
import re

from app.core.config import settings
from app.auth.auth_middleware import StrictAuth, InitialAuth

# --- Loggers y Router (sin cambios) ---
log = structlog.get_logger("atenex_api_gateway.router.gateway")
dep_log = structlog.get_logger("atenex_api_gateway.dependency.client")
auth_dep_log = structlog.get_logger("atenex_api_gateway.dependency.auth")
router = APIRouter()
http_client: Optional[httpx.AsyncClient] = None

# --- Constantes y Dependencias (sin cambios) ---
HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "content-encoding", "content-length"
}

def get_client() -> httpx.AsyncClient:
    if http_client is None or http_client.is_closed:
        dep_log.error("Gateway HTTP client dependency check failed: Client not available or closed.")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Gateway service dependency unavailable (HTTP Client).")
    return http_client

async def logged_strict_auth(user_payload: StrictAuth) -> Dict[str, Any]:
    return user_payload

LoggedStrictAuth = Annotated[Dict[str, Any], Depends(logged_strict_auth)]

# --- Función Principal de Proxy ---
async def _proxy_request(
    request: Request,
    target_service_base_url_str: str,
    client: httpx.AsyncClient,
    user_payload: Optional[Dict[str, Any]],
    backend_service_path: str,
):
    method = request.method
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    proxy_log = log.bind(
        request_id=request_id, method=method, original_path=request.url.path,
        target_service=target_service_base_url_str, target_path=backend_service_path
    )
    proxy_log.info("Initiating proxy request")
    try:
        target_base_url = httpx.URL(target_service_base_url_str)
        # Asegura que backend_service_path es absoluto
        if not backend_service_path.startswith("/"):
            backend_service_path = "/" + backend_service_path
        # Construir URL: base + path + query original
        target_url = target_base_url.copy_with(path=backend_service_path, query=request.url.query.encode("utf-8"))
        proxy_log.debug("Target URL constructed", url=str(target_url))
    except Exception as e:
        proxy_log.error("Failed to construct target URL", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal gateway configuration error.")

    headers_to_forward = {}
    client_host = request.client.host if request.client else "unknown"
    x_forwarded_for = request.headers.get("x-forwarded-for", client_host)
    headers_to_forward["X-Forwarded-For"] = x_forwarded_for
    headers_to_forward["X-Forwarded-Proto"] = request.url.scheme
    if "host" in request.headers: headers_to_forward["X-Forwarded-Host"] = request.headers["host"]
    headers_to_forward["X-Request-ID"] = request_id

    # Reenviar todos los headers excepto hop-by-hop, pero NUNCA eliminar Content-Type ni boundaries
    for name, value in request.headers.items():
        lower_name = name.lower()
        if lower_name not in HOP_BY_HOP_HEADERS and lower_name != "host":
            headers_to_forward[name] = value
        elif lower_name == "content-type":
            headers_to_forward[name] = value

    log_context_headers = {}
    if user_payload:
        user_id = user_payload.get('sub')
        company_id = user_payload.get('company_id')
        user_email = user_payload.get('email')
        if not user_id or not company_id:
             proxy_log.critical("Payload missing required fields post-auth!", payload_keys=list(user_payload.keys()))
             raise HTTPException(status_code=500, detail="Internal authentication context error.")
        headers_to_forward['X-User-ID'] = str(user_id)
        headers_to_forward['X-Company-ID'] = str(company_id)
        headers_to_forward['x-user-id'] = str(user_id)
        headers_to_forward['x-company-id'] = str(company_id)
        log_context_headers['user_id'] = str(user_id)
        log_context_headers['company_id'] = str(company_id)
        if user_email:
            headers_to_forward['X-User-Email'] = str(user_email)
            log_context_headers['user_email'] = str(user_email)
        proxy_log = proxy_log.bind(**log_context_headers)
        proxy_log.debug("Context headers prepared", headers_added=list(log_context_headers.keys()))

    # Detectar si el método permite body (POST, PUT, PATCH, DELETE)
    method_allows_body = method.upper() in ["POST", "PUT", "PATCH", "DELETE"]
    request_body_bytes: Optional[bytes] = None
    if method_allows_body:
        try:
            request_body_bytes = await request.body()
            proxy_log.debug(f"Read request body ({len(request_body_bytes)} bytes)")
            if request_body_bytes:
                headers_to_forward['content-length'] = str(len(request_body_bytes))
            else:
                headers_to_forward.pop('content-length', None)
            headers_to_forward.pop('transfer-encoding', None)
        except Exception as e:
            proxy_log.error("Failed to read request body", error=str(e), exc_info=True)
            raise HTTPException(status_code=400, detail="Could not read request body.")
    else:
        headers_to_forward.pop('content-length', None)
        proxy_log.debug("No request body expected or present.")

    backend_response: Optional[httpx.Response] = None
    try:
        proxy_log.debug(f"Sending {method} request to {target_url}", headers=list(headers_to_forward.keys()))
        req = client.build_request(method=method, url=target_url, headers=headers_to_forward, content=request_body_bytes)
        backend_response = await client.send(req, stream=True)
        proxy_log.info(f"Received response from backend", status_code=backend_response.status_code, content_type=backend_response.headers.get("content-type"))
        response_headers = {}
        for name, value in backend_response.headers.items():
            if name.lower() not in HOP_BY_HOP_HEADERS:
                 response_headers[name] = value
        response_headers["X-Request-ID"] = request_id

        return StreamingResponse(
            backend_response.aiter_raw(),
            status_code=backend_response.status_code,
            headers=response_headers,
            media_type=backend_response.headers.get("content-type"),
            background=backend_response.aclose
        )
    except httpx.TimeoutException as exc:
        proxy_log.error(f"Proxy request timed out waiting for backend", target=str(target_url), error=str(exc), exc_info=False)
        if backend_response: await backend_response.aclose()
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Upstream service timed out.")
    except httpx.ConnectError as exc:
        proxy_log.error(f"Proxy connection error: Could not connect to backend", target=str(target_url), error=str(exc), exc_info=False)
        if backend_response: await backend_response.aclose()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not connect to upstream service.")
    except httpx.RequestError as exc:
        proxy_log.error(f"Proxy request error communicating with backend", target=str(target_url), error=str(exc), exc_info=True)
        if backend_response: await backend_response.aclose()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Error communicating with upstream service: {exc}")
    except Exception as exc:
        proxy_log.exception(f"Unexpected error occurred during proxy request", target=str(target_url))
        if backend_response: await backend_response.aclose()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred in the gateway.")

# --- CORS OPTIONS HANDLERS (sin cambios) ---
@router.options("/api/v1/ingest/{endpoint_path:path}", tags=["CORS", "Proxy - Ingest Service"], include_in_schema=False)
async def options_proxy_ingest_service_generic(endpoint_path: str = Path(...)): return Response(status_code=200)
@router.options("/api/v1/query/ask", tags=["CORS", "Proxy - Query Service"], include_in_schema=False)
async def options_query_ask(): return Response(status_code=200)
@router.options("/api/v1/query/chats", tags=["CORS", "Proxy - Query Service"], include_in_schema=False)
async def options_query_chats(): return Response(status_code=200)
@router.options("/api/v1/query/chats/{chat_id}/messages", tags=["CORS", "Proxy - Query Service"], include_in_schema=False)
async def options_chat_messages(chat_id: uuid.UUID = Path(...)): return Response(status_code=200)
@router.options("/api/v1/query/chats/{chat_id}", tags=["CORS", "Proxy - Query Service"], include_in_schema=False)
async def options_delete_chat(chat_id: uuid.UUID = Path(...)): return Response(status_code=200)
if settings.AUTH_SERVICE_URL:
    @router.options("/api/v1/auth/{endpoint_path:path}", tags=["CORS", "Proxy - Auth Service (Optional)"], include_in_schema=False)
    async def options_proxy_auth_service_generic(endpoint_path: str = Path(...)): return Response(status_code=200)

# --- Rutas Proxy Específicas para Query Service ---

# GET /chats
@router.get(
    "/api/v1/query/chats",
    dependencies=[Depends(LoggedStrictAuth)],
    tags=["Proxy - Query Service"],
    summary="List user's chats (Proxied)"
)
async def proxy_get_chats(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: LoggedStrictAuth,
):
    """Reenvía GET /api/v1/query/chats al Query Service."""
    backend_path = "/api/v1/query/chats"
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

# POST /ask
@router.post(
    "/api/v1/query/ask",
    dependencies=[Depends(LoggedStrictAuth)],
    tags=["Proxy - Query Service"],
    summary="Submit a query or message to a chat (Proxied)"
)
async def proxy_post_query(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: LoggedStrictAuth,
):
    """Reenvía POST /api/v1/query/ask al endpoint /api/v1/query/ask del Query Service."""
    backend_path = "/api/v1/query/ask"
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

# GET /chats/{chat_id}/messages
@router.get(
    "/api/v1/query/chats/{chat_id}/messages",
    dependencies=[Depends(LoggedStrictAuth)],
    tags=["Proxy - Query Service"],
    summary="Get messages for a specific chat (Proxied)"
)
async def proxy_get_chat_messages(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: LoggedStrictAuth,
    chat_id: uuid.UUID = Path(...),
):
    """Reenvía GET /api/v1/query/chats/{chat_id}/messages al Query Service."""
    backend_path = f"/api/v1/query/chats/{chat_id}/messages"
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

# DELETE /chats/{chat_id}
@router.delete(
    "/api/v1/query/chats/{chat_id}",
    dependencies=[Depends(LoggedStrictAuth)],
    tags=["Proxy - Query Service"],
    summary="Delete a specific chat (Proxied)"
)
async def proxy_delete_chat(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: LoggedStrictAuth,
    chat_id: uuid.UUID = Path(...),
):
    """Reenvía DELETE /api/v1/query/chats/{chat_id} al Query Service."""
    backend_path = f"/api/v1/query/chats/{chat_id}"
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

# --- Rutas Proxy Genéricas para Ingest Service ---

@router.api_route(
    "/api/v1/ingest/{endpoint_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    dependencies=[Depends(LoggedStrictAuth)],
    tags=["Proxy - Ingest Service"],
    summary="Generic proxy for Ingest Service endpoints (Authenticated)"
)
async def proxy_ingest_service_generic(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: LoggedStrictAuth,
    endpoint_path: str = Path(...),
):
    """Reenvía solicitudes autenticadas a `/api/v1/ingest/*` al Ingest Service."""
    backend_path = f"/api/v1/ingest/{endpoint_path}"
    return await _proxy_request(
        request=request,
        target_service_base_url_str=str(settings.INGEST_SERVICE_URL),
        client=client,
        user_payload=user_payload,
        backend_service_path=backend_path
    )

# --- Proxy Opcional para Auth Service (Sin cambios aquí) ---
if settings.AUTH_SERVICE_URL:
    pass
else:
    log.info("Auth service proxy is disabled (GATEWAY_AUTH_SERVICE_URL not set).")