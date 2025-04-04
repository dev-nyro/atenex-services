# api-gateway/app/routers/gateway_router.py
from fastapi import APIRouter, Request, Response, Depends, HTTPException, status, Path # APIRouter debe estar importado
from fastapi.responses import StreamingResponse
from typing import Optional, Annotated, Dict, Any
import httpx
import structlog
import asyncio
import uuid

from app.core.config import settings
from app.auth.auth_middleware import StrictAuth # Dependencia estándar

# --- Definición del Router y Loggers PRIMERO ---
log = structlog.get_logger("api_gateway.router.gateway")
dep_log = structlog.get_logger("api_gateway.dependency.client")
auth_dep_log = structlog.get_logger("api_gateway.dependency.auth")

router = APIRouter() # *** ASEGURAR QUE ESTÁ AQUÍ, DESPUÉS DE IMPORTS Y LOGGERS ***
# ------------------------------------------------

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
async def _proxy_request(
    request: Request,
    target_url_str: str,
    client: httpx.AsyncClient,
    user_payload: Optional[Dict[str, Any]],
    backend_service_path: str
):
    method = request.method
    request_id = getattr(request.state, 'request_id', None)
    proxy_log = log.bind(request_id=request_id) # Bind request_id al inicio

    proxy_log.info(f"Entered _proxy_request for '{method} {request.url.path}'")

    target_service_base_url = httpx.URL(target_url_str)
    target_url = target_service_base_url.copy_with(path=backend_service_path, query=request.url.query.encode("utf-8"))

    # 1. Preparar Headers
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
    if request_id: headers_to_forward["X-Request-ID"] = request_id

    # 2. Inyectar Headers de Contexto
    log_context = {}
    if user_payload and method.upper() != 'OPTIONS':
        user_id = user_payload.get('sub'); company_id = user_payload.get('company_id'); user_email = user_payload.get('email')
        if not user_id or not company_id:
             proxy_log.critical("Payload missing required fields!", payload_keys=list(user_payload.keys())); raise HTTPException(status_code=500, detail="Internal authentication context error.")
        headers_to_forward['X-User-ID'] = str(user_id); headers_to_forward['X-Company-ID'] = str(company_id)
        log_context['user_id'] = str(user_id); log_context['company_id'] = str(company_id)
        if user_email: headers_to_forward['X-User-Email'] = str(user_email)
        proxy_log.debug("Added context headers", headers_added=list(log_context.keys()))

    # Rebind con contexto ANTES de preparar body
    proxy_log = proxy_log.bind(**log_context)

    # 3. Preparar Body
    request_body_bytes: Optional[bytes] = None
    if method.upper() in ["POST", "PUT", "PATCH"]:
        proxy_log.debug("Attempting to read request body...")
        request_body_bytes = await request.body()
        proxy_log.info(f"Read request body for {method}", body_length=len(request_body_bytes) if request_body_bytes else 0)

    # Log antes de la llamada
    proxy_log.debug("Preparing to send request to backend", backend_method=method, backend_url=str(target_url), backend_headers=headers_to_forward, has_body=request_body_bytes is not None)

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
    except httpx.TimeoutException as exc: proxy_log.error(f"Proxy timeout", target=str(target_url), error=str(exc)); raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Upstream service timed out.")
    except httpx.ConnectError as exc: proxy_log.error(f"Proxy connection error", target=str(target_url), error=str(exc)); raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not connect to upstream service.")
    except httpx.RequestError as exc: proxy_log.error(f"Proxy request error", target=str(target_url), error=str(exc)); raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Error communicating with upstream service.")
    except Exception as exc: proxy_log.exception(f"Proxy unexpected error occurred in _proxy_request", target=str(target_url)); await rp.aclose() if rp else None; raise # Re-lanza la original


# --- Rutas Proxy Específicas ---
# Los handlers de ruta ahora llaman a _proxy_request

@router.get( "/api/v1/query/chats", dependencies=[Depends(LoggedStrictAuth)], tags=["Proxy - Query"])
async def proxy_get_chats(request: Request, client: Annotated[httpx.AsyncClient, Depends(get_client)], user_payload: LoggedStrictAuth):
    log.info(f"Entering GET /api/v1/query/chats handler", request_id=getattr(request.state, 'request_id', None)) # Log de Entrada al Handler
    backend_path = "/api/v1/chats" # Path relativo en Query Service
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

@router.post( "/api/v1/query/ask", dependencies=[Depends(LoggedStrictAuth)], tags=["Proxy - Query"])
async def proxy_post_ask(request: Request, client: Annotated[httpx.AsyncClient, Depends(get_client)], user_payload: LoggedStrictAuth):
    log.info(f"Entering POST /api/v1/query/ask handler", request_id=getattr(request.state, 'request_id', None)) # Log de Entrada al Handler
    backend_path = "/api/v1/ask" # Verifica este path contra query-service/endpoints/query.py
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

@router.get( "/api/v1/query/chats/{chat_id}/messages", dependencies=[Depends(LoggedStrictAuth)], tags=["Proxy - Query"])
async def proxy_get_chat_messages(request: Request, client: Annotated[httpx.AsyncClient, Depends(get_client)], user_payload: LoggedStrictAuth, chat_id: uuid.UUID = Path(...)):
    log.info(f"Entering GET /api/v1/query/chats/.../messages handler", chat_id=str(chat_id), request_id=getattr(request.state, 'request_id', None)) # Log de Entrada
    backend_path = f"/api/v1/chats/{chat_id}/messages" # Verifica contra query-service/endpoints/chat.py
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

@router.delete( "/api/v1/query/chats/{chat_id}", dependencies=[Depends(LoggedStrictAuth)], tags=["Proxy - Query"])
async def proxy_delete_chat(request: Request, client: Annotated[httpx.AsyncClient, Depends(get_client)], user_payload: LoggedStrictAuth, chat_id: uuid.UUID = Path(...)):
    log.info(f"Entering DELETE /api/v1/query/chats/... handler", chat_id=str(chat_id), request_id=getattr(request.state, 'request_id', None)) # Log de Entrada
    backend_path = f"/api/v1/chats/{chat_id}" # Verifica contra query-service/endpoints/chat.py
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

# Rutas OPTIONS
@router.options( "/api/v1/query/chats", tags=["Proxy - Query"], include_in_schema=False)
async def options_get_chats(): log.debug("Handling OPTIONS /api/v1/query/chats"); return Response(status_code=200)
@router.options( "/api/v1/query/ask", tags=["Proxy - Query"], include_in_schema=False)
async def options_post_ask(): log.debug("Handling OPTIONS /api/v1/query/ask"); return Response(status_code=200)
@router.options( "/api/v1/query/chats/{chat_id}/messages", tags=["Proxy - Query"], include_in_schema=False)
async def options_get_chat_messages(chat_id: uuid.UUID = Path(...)): log.debug("Handling OPTIONS /api/v1/query/chats/.../messages"); return Response(status_code=200)
@router.options( "/api/v1/query/chats/{chat_id}", tags=["Proxy - Query"], include_in_schema=False)
async def options_delete_chat(chat_id: uuid.UUID = Path(...)): log.debug("Handling OPTIONS /api/v1/query/chats/..."); return Response(status_code=200)

# Proxy para Ingest Service (genérico)
@router.api_route( "/api/v1/ingest/{endpoint_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], dependencies=[Depends(LoggedStrictAuth)], tags=["Proxy - Ingest"], name="proxy_ingest_service")
async def proxy_ingest_service_generic(request: Request, endpoint_path: str, client: Annotated[httpx.AsyncClient, Depends(get_client)], user_payload: LoggedStrictAuth):
    log.info(f"Entering /{endpoint_path} handler for Ingest service", endpoint=endpoint_path, request_id=getattr(request.state, 'request_id', None)) # Log de Entrada
    backend_path = f"/{endpoint_path}"
    # IMPORTANTE: Asegúrate que en ingest-service las rutas son relativas, ej @router.get("/status")
    return await _proxy_request(request, str(settings.INGEST_SERVICE_URL), client, user_payload, backend_service_path=backend_path)

# Proxy para Auth Service (opcional, genérico)
if settings.AUTH_SERVICE_URL:
    log.info(f"Auth service proxy enabled", base_url=settings.AUTH_SERVICE_URL)
    @router.api_route( "/api/v1/auth/{endpoint_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], tags=["Proxy - Auth"], name="proxy_auth_service")
    async def proxy_auth_service_generic(request: Request, endpoint_path: str, client: Annotated[httpx.AsyncClient, Depends(get_client)]):
        log.info(f"Entering /{endpoint_path} handler for Auth service", endpoint=endpoint_path, request_id=getattr(request.state, 'request_id', None)) # Log de Entrada
        backend_path = f"/{endpoint_path}"
        return await _proxy_request(request, str(settings.AUTH_SERVICE_URL), client, user_payload=None, backend_service_path=backend_path)