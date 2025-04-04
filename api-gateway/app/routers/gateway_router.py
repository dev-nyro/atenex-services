# api-gateway/app/routers/gateway_router.py
from fastapi import APIRouter, Request, Response, Depends, HTTPException, status, Path
from fastapi.responses import StreamingResponse
from typing import Optional, Annotated, Dict, Any
import httpx
import structlog
import asyncio
import uuid

from app.core.config import settings
from app.auth.auth_middleware import StrictAuth # Dependencia estándar

# Logs más específicos para este router
log = structlog.get_logger("api_gateway.router.gateway")
# Log específico para la dependencia (para ver si se resuelve)
dep_log = structlog.get_logger("api_gateway.dependency.client")
auth_dep_log = structlog.get_logger("api_gateway.dependency.auth")


# Cliente HTTP global (inyectado desde lifespan)
http_client: Optional[httpx.AsyncClient] = None

# Cabeceras Hop-by-Hop
HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length",
}

# Dependencia para obtener el cliente CON LOGGING
def get_client() -> httpx.AsyncClient:
    dep_log.debug("Attempting to get HTTP client via dependency...")
    if http_client is None or http_client.is_closed:
        dep_log.error("Gateway HTTP client dependency check failed: Client not available or closed.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway service dependency unavailable (HTTP Client)."
        )
    dep_log.info("HTTP client dependency resolved successfully.")
    return http_client

# LOGGING PARA DEPENDENCIA StrictAuth (Envolvemos la original para loggear)
async def logged_strict_auth(
    user_payload: StrictAuth # Dependemos de la dependencia original
) -> Dict[str, Any]:
     auth_dep_log.info("StrictAuth dependency successfully resolved in wrapper.",
                     user_id=user_payload.get('sub'),
                     company_id=user_payload.get('company_id'))
     return user_payload

# Alias para usar en las rutas
LoggedStrictAuth = Annotated[Dict[str, Any], Depends(logged_strict_auth)]

# --- Función interna _proxy_request ---
# Añadimos logs justo al inicio y antes de enviar la petición
async def _proxy_request(
    request: Request,
    target_url_str: str,
    client: httpx.AsyncClient,
    user_payload: Optional[Dict[str, Any]],
    backend_service_path: str
):
    method = request.method
    # Bind request_id al log aquí también para contexto consistente
    request_id = getattr(request.state, 'request_id', None)
    proxy_log = log.bind(request_id=request_id)

    proxy_log.info(f"Entered _proxy_request for '{method} {request.url.path}'") # Log de entrada

    target_service_base_url = httpx.URL(target_url_str)
    target_url = target_service_base_url.copy_with(path=backend_service_path, query=request.url.query.encode("utf-8"))

    # 1. Preparar Headers
    headers_to_forward = {}
    client_host = request.client.host if request.client else "unknown"
    x_forwarded_for = request.headers.get("x-forwarded-for", client_host)
    # Loggear headers entrantes (con precaución en producción por datos sensibles)
    # proxy_log.debug("Incoming headers", headers=dict(request.headers))
    for name, value in request.headers.items():
        lower_name = name.lower()
        if lower_name not in HOP_BY_HOP_HEADERS and lower_name != "host":
            headers_to_forward[name] = value
    headers_to_forward["X-Forwarded-For"] = x_forwarded_for
    headers_to_forward["X-Forwarded-Proto"] = request.url.scheme
    headers_to_forward["X-Forwarded-Host"] = request.headers.get("host", "")
    if request_id: headers_to_forward["X-Request-ID"] = request_id

    # 2. Inyectar Headers de Contexto
    log_context = {} # Empezar log context vacío
    if user_payload and method.upper() != 'OPTIONS':
        user_id = user_payload.get('sub')
        company_id = user_payload.get('company_id')
        user_email = user_payload.get('email')
        if not user_id or not company_id:
             proxy_log.critical("Payload missing required fields!", payload_keys=list(user_payload.keys()))
             raise HTTPException(status_code=500, detail="Internal authentication context error.")
        headers_to_forward['X-User-ID'] = str(user_id)
        headers_to_forward['X-Company-ID'] = str(company_id)
        log_context['user_id'] = str(user_id); log_context['company_id'] = str(company_id)
        if user_email: headers_to_forward['X-User-Email'] = str(user_email)
        proxy_log.debug("Added context headers", headers_added=list(log_context.keys()))

    # Rebind con el contexto completo
    proxy_log = proxy_log.bind(**log_context)

    # 3. Preparar Body
    request_body_bytes: Optional[bytes] = None
    if method.upper() in ["POST", "PUT", "PATCH"]:
        proxy_log.debug("Attempting to read request body...")
        request_body_bytes = await request.body()
        proxy_log.info(f"Read request body for {method}", body_length=len(request_body_bytes) if request_body_bytes else 0)

    # Log detallado justo antes de la llamada a httpx
    proxy_log.debug("Preparing to send request to backend",
                   backend_method=method,
                   backend_url=str(target_url),
                   backend_headers=headers_to_forward,
                   has_body=request_body_bytes is not None)

    # 4. Realizar petición downstream
    rp: Optional[httpx.Response] = None
    try:
        proxy_log.info(f"Sending request '{method}' to backend target", backend_target=str(target_url))
        req = client.build_request(method=method, url=target_url, headers=headers_to_forward, content=request_body_bytes)
        rp = await client.send(req, stream=True)

        # 5. Procesar respuesta
        proxy_log.info(f"Received response from backend", status_code=rp.status_code, backend_target=str(target_url))
        response_headers = {k: v for k, v in rp.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}
        proxy_log.debug("Returning StreamingResponse to client", headers=response_headers, media_type=rp.headers.get("content-type"))
        return StreamingResponse(rp.aiter_raw(), status_code=rp.status_code, headers=response_headers, media_type=rp.headers.get("content-type"), background=rp.aclose)

    # Manejo de errores
    except httpx.TimeoutException as exc:
         proxy_log.error(f"Proxy timeout", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Upstream service timed out.")
    except httpx.ConnectError as exc:
         proxy_log.error(f"Proxy connection error", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not connect to upstream service.")
    except httpx.RequestError as exc:
         proxy_log.error(f"Proxy request error", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Error communicating with upstream service.")
    except Exception as exc:
        # Captura la excepción original aquí
        proxy_log.exception(f"Proxy unexpected error occurred in _proxy_request", target=str(target_url))
        if rp:
            try:
                await rp.aclose()
            except Exception as close_exc:
                proxy_log.error("Error closing downstream response during exception handling", close_error=close_exc)
        # Re-lanza la excepción original para que el manejador de FastAPI la capture
        raise exc


# --- Rutas Proxy Específicas ---

# 1. Proxy para Query Service
@router.get( "/api/v1/query/chats", dependencies=[Depends(LoggedStrictAuth)], tags=["Proxy - Query"])
async def proxy_get_chats(request: Request, client: Annotated[httpx.AsyncClient, Depends(get_client)], user_payload: LoggedStrictAuth):
    # LOG INICIO HANDLER
    log.info(f"Entering GET /api/v1/query/chats handler (before proxy call)", request_id=getattr(request.state, 'request_id', None))
    backend_path = "/api/v1/chats"
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

@router.post( "/api/v1/query/ask", dependencies=[Depends(LoggedStrictAuth)], tags=["Proxy - Query"])
async def proxy_post_ask(request: Request, client: Annotated[httpx.AsyncClient, Depends(get_client)], user_payload: LoggedStrictAuth):
    # LOG INICIO HANDLER
    log.info(f"Entering POST /api/v1/query/ask handler (before proxy call)", request_id=getattr(request.state, 'request_id', None))
    backend_path = "/api/v1/ask" # Verifica este path contra query-service -> query.py
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

@router.get( "/api/v1/query/chats/{chat_id}/messages", dependencies=[Depends(LoggedStrictAuth)], tags=["Proxy - Query"])
async def proxy_get_chat_messages(request: Request, client: Annotated[httpx.AsyncClient, Depends(get_client)], user_payload: LoggedStrictAuth, chat_id: uuid.UUID = Path(...)):
    # LOG INICIO HANDLER
    log.info(f"Entering GET /api/v1/query/chats/.../messages handler (before proxy call)", chat_id=str(chat_id), request_id=getattr(request.state, 'request_id', None))
    backend_path = f"/api/v1/chats/{chat_id}/messages" # Verifica este path contra query-service -> chat.py
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

@router.delete( "/api/v1/query/chats/{chat_id}", dependencies=[Depends(LoggedStrictAuth)], tags=["Proxy - Query"])
async def proxy_delete_chat(request: Request, client: Annotated[httpx.AsyncClient, Depends(get_client)], user_payload: LoggedStrictAuth, chat_id: uuid.UUID = Path(...)):
    # LOG INICIO HANDLER
    log.info(f"Entering DELETE /api/v1/query/chats/... handler (before proxy call)", chat_id=str(chat_id), request_id=getattr(request.state, 'request_id', None))
    backend_path = f"/api/v1/chats/{chat_id}" # Verifica este path contra query-service -> chat.py
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

# Rutas OPTIONS (Sin cambios)
@router.options( "/api/v1/query/chats", tags=["Proxy - Query"], include_in_schema=False)
async def options_get_chats(): log.debug("Handling OPTIONS /api/v1/query/chats"); return Response(status_code=200)
@router.options( "/api/v1/query/ask", tags=["Proxy - Query"], include_in_schema=False)
async def options_post_ask(): log.debug("Handling OPTIONS /api/v1/query/ask"); return Response(status_code=200)
@router.options( "/api/v1/query/chats/{chat_id}/messages", tags=["Proxy - Query"], include_in_schema=False)
async def options_get_chat_messages(chat_id: uuid.UUID = Path(...)): log.debug("Handling OPTIONS /api/v1/query/chats/.../messages"); return Response(status_code=200)
@router.options( "/api/v1/query/chats/{chat_id}", tags=["Proxy - Query"], include_in_schema=False)
async def options_delete_chat(chat_id: uuid.UUID = Path(...)): log.debug("Handling OPTIONS /api/v1/query/chats/..."); return Response(status_code=200)


# 2. Proxy para Ingest Service (genérico)
@router.api_route( "/api/v1/ingest/{endpoint_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], dependencies=[Depends(LoggedStrictAuth)], tags=["Proxy - Ingest"], name="proxy_ingest_service")
async def proxy_ingest_service_generic(request: Request, endpoint_path: str, client: Annotated[httpx.AsyncClient, Depends(get_client)], user_payload: LoggedStrictAuth):
    # LOG INICIO HANDLER
    log.info(f"Entering /{endpoint_path} handler for Ingest service (before proxy call)", endpoint=endpoint_path, request_id=getattr(request.state, 'request_id', None))
    backend_path = f"/{endpoint_path}"
    # IMPORTANTE: Verificar que las rutas en ingest.py NO incluyen /api/v1/ingest
    # Ejemplo: En ingest.py debe ser @router.get("/status"), no @router.get("/api/v1/ingest/status")
    return await _proxy_request(request, str(settings.INGEST_SERVICE_URL), client, user_payload, backend_service_path=backend_path)


# 3. Proxy para Auth Service (Opcional y genérico, si está habilitado)
if settings.AUTH_SERVICE_URL:
    log.info(f"Auth service proxy enabled for base URL: {settings.AUTH_SERVICE_URL}")
    @router.api_route( "/api/v1/auth/{endpoint_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], tags=["Proxy - Auth"], name="proxy_auth_service")
    async def proxy_auth_service_generic(request: Request, endpoint_path: str, client: Annotated[httpx.AsyncClient, Depends(get_client)]):
        log.info(f"Entering /{endpoint_path} handler for Auth service (before proxy call)", endpoint=endpoint_path, request_id=getattr(request.state, 'request_id', None))
        backend_path = f"/{endpoint_path}"
        return await _proxy_request(request, str(settings.AUTH_SERVICE_URL), client, user_payload=None, backend_service_path=backend_path)