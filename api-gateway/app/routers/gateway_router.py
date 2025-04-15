# File: app/routers/gateway_router.py
# api-gateway/app/routers/gateway_router.py
from fastapi import APIRouter, Request, Response, Depends, HTTPException, status, Path
from fastapi.responses import StreamingResponse
from typing import Optional, Annotated, Dict, Any
import httpx
import structlog
import asyncio
import uuid

from app.core.config import settings
from app.auth.auth_middleware import StrictAuth, InitialAuth # Mantener ambos si son necesarios

# --- Loggers y Router ---
log = structlog.get_logger("atenex_api_gateway.router.gateway")
dep_log = structlog.get_logger("atenex_api_gateway.dependency.client")
auth_dep_log = structlog.get_logger("atenex_api_gateway.dependency.auth")
router = APIRouter()
http_client: Optional[httpx.AsyncClient] = None # Inyectado desde main

# --- Constantes y Dependencias ---
HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    # Añadimos estos también, suelen ser problemáticos
    "content-encoding", "content-length"
}

def get_client() -> httpx.AsyncClient:
    """Dependencia para obtener el cliente HTTP global."""
    if http_client is None or http_client.is_closed:
        dep_log.error("Gateway HTTP client dependency check failed: Client not available or closed.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway service dependency unavailable (HTTP Client)."
        )
    # dep_log.info("HTTP client dependency resolved successfully.") # Muy verboso
    return http_client

async def logged_strict_auth(user_payload: StrictAuth) -> Dict[str, Any]:
    """Wrapper para StrictAuth que loguea éxito (útil para debug)."""
    # auth_dep_log.info("StrictAuth dependency successfully resolved in wrapper.", # Muy verboso
    #                  user_id=user_payload.get('sub'),
    #                  company_id=user_payload.get('company_id'))
    return user_payload

LoggedStrictAuth = Annotated[Dict[str, Any], Depends(logged_strict_auth)]

# --- Función Principal de Proxy ---
async def _proxy_request(
    request: Request,
    target_service_base_url_str: str,
    client: httpx.AsyncClient,
    user_payload: Optional[Dict[str, Any]], # Payload JWT decodificado (si la ruta está protegida)
    backend_service_path: str # Path al que llamar en el servicio backend
):
    """Función interna para realizar la solicitud proxy al servicio backend."""
    method = request.method
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))

    # Crear un logger con contexto para esta solicitud proxy específica
    proxy_log = log.bind(
        request_id=request_id, method=method, original_path=request.url.path,
        target_service=target_service_base_url_str, target_path=backend_service_path
    )
    proxy_log.info("Initiating proxy request") # Log inicio proxy

    # Construir URL de destino
    try:
        target_base_url = httpx.URL(target_service_base_url_str)
        # Usar copy_with para combinar base, path y query params originales
        target_url = target_base_url.copy_with(path=backend_service_path, query=request.url.query.encode("utf-8"))
    except Exception as e:
        proxy_log.error("Failed to construct target URL", error=str(e))
        raise HTTPException(status_code=500, detail="Internal gateway configuration error.")

    # Preparar cabeceras para reenviar
    headers_to_forward = {}

    # Añadir cabeceras X-Forwarded-*
    client_host = request.client.host if request.client else "unknown"
    x_forwarded_for = request.headers.get("x-forwarded-for", client_host)
    headers_to_forward["X-Forwarded-For"] = x_forwarded_for
    headers_to_forward["X-Forwarded-Proto"] = request.url.scheme
    if "host" in request.headers: headers_to_forward["X-Forwarded-Host"] = request.headers["host"]
    headers_to_forward["X-Request-ID"] = request_id # Propagar request ID

    # Copiar cabeceras originales, excluyendo hop-by-hop y host
    for name, value in request.headers.items():
        lower_name = name.lower()
        # Excluimos los hop-by-hop definidos arriba y el 'host' original
        if lower_name not in HOP_BY_HOP_HEADERS and lower_name != "host":
            headers_to_forward[name] = value
        # Conservamos Content-Type explícitamente si está presente
        elif lower_name == "content-type":
             headers_to_forward[name] = value

    # Añadir cabeceras de contexto si hay payload de usuario (ruta autenticada)
    log_context_headers = {}
    if user_payload:
        user_id = user_payload.get('sub')
        company_id = user_payload.get('company_id')
        user_email = user_payload.get('email')

        # StrictAuth ya valida esto, pero una doble verificación no hace daño
        if not user_id or not company_id:
             proxy_log.critical("Payload from auth dependency missing required fields!", payload_keys=list(user_payload.keys()))
             raise HTTPException(status_code=500, detail="Internal authentication context error after auth check.")

        # Inyectar las cabeceras X-*
        headers_to_forward['X-User-ID'] = str(user_id)
        headers_to_forward['X-Company-ID'] = str(company_id)
        log_context_headers['user_id'] = str(user_id)
        log_context_headers['company_id'] = str(company_id)
        if user_email:
            headers_to_forward['X-User-Email'] = str(user_email)
            log_context_headers['user_email'] = str(user_email)

        # Añadir contexto al logger para trazabilidad
        proxy_log = proxy_log.bind(**log_context_headers)
        # proxy_log.debug("Added context headers based on user payload.") # Podría ser verboso

    # Leer cuerpo de la solicitud si es necesario (POST, PUT, PATCH)
    request_body_bytes: Optional[bytes] = None
    # Evitar leer body para GET, DELETE, etc.
    if method.upper() in ["POST", "PUT", "PATCH"]:
        try:
            request_body_bytes = await request.body()
            # proxy_log.debug(f"Read request body ({len(request_body_bytes)} bytes)") # Verboso
        except Exception as e:
            proxy_log.error("Failed to read request body", error=str(e))
            raise HTTPException(status_code=400, detail="Could not read request body.")

    # Preparar la solicitud al backend usando el cliente httpx
    backend_response: Optional[httpx.Response] = None # Inicializar por si falla antes del send
    try:
        proxy_log.info(f"Sending request to backend target: {method} {target_url.path}")
        # Construir la request con los datos preparados
        req = client.build_request(
            method=method,
            url=target_url,
            headers=headers_to_forward,
            content=request_body_bytes
        )
        # Enviar la solicitud (stream=True para manejar respuestas grandes/streaming)
        backend_response = await client.send(req, stream=True)

        # Loguear respuesta recibida del backend
        proxy_log.info(f"Received response from backend", status_code=backend_response.status_code,
                       content_type=backend_response.headers.get("content-type"))

        # Preparar cabeceras de respuesta para enviar al cliente original
        response_headers = {}
        for name, value in backend_response.headers.items():
            # Filtrar cabeceras hop-by-hop de la respuesta del backend
            if name.lower() not in HOP_BY_HOP_HEADERS:
                response_headers[name] = value

        # Añadir X-Request-ID a la respuesta final
        response_headers["X-Request-ID"] = request_id

        # Devolver una StreamingResponse para reenviar el cuerpo eficientemente
        return StreamingResponse(
            backend_response.aiter_raw(), # Iterador asíncrono del cuerpo
            status_code=backend_response.status_code,
            headers=response_headers,
            media_type=backend_response.headers.get("content-type"), # Preservar media type
            background=backend_response.aclose # Asegurar que la conexión al backend se cierre
        )

    # Manejo de errores específicos de HTTPX
    except httpx.TimeoutException as exc:
        proxy_log.error(f"Proxy request timed out waiting for backend", target=str(target_url), error=str(exc), exc_info=False) # No log stack trace for timeouts
        if backend_response: await backend_response.aclose()
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Upstream service timed out.")
    except httpx.ConnectError as exc:
        proxy_log.error(f"Proxy connection error: Could not connect to backend", target=str(target_url), error=str(exc), exc_info=False) # No log stack trace
        if backend_response: await backend_response.aclose()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not connect to upstream service.")
    except httpx.RequestError as exc: # Otros errores de httpx (ej. SSL)
        proxy_log.error(f"Proxy request error communicating with backend", target=str(target_url), error=str(exc), exc_info=True)
        if backend_response: await backend_response.aclose()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Error communicating with upstream service: {exc}")
    except Exception as exc: # Errores inesperados
        proxy_log.exception(f"Unexpected error occurred during proxy request", target=str(target_url))
        if backend_response: await backend_response.aclose() # Intentar cerrar si existe
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred in the gateway.")


# --- *** INICIO SECCIÓN CORS OPTIONS HANDLERS *** ---
# (Sin cambios aquí, ya están correctos)
@router.options(
    "/api/v1/ingest/{endpoint_path:path}",
    tags=["CORS", "Proxy - Ingest Service"],
    summary="CORS Preflight handler for Ingest Service proxy",
    include_in_schema=False
)
async def options_proxy_ingest_service_generic(endpoint_path: str = Path(...)):
    return Response(status_code=200)

@router.options(
    "/api/v1/query/ask",
    tags=["CORS", "Proxy - Query Service"],
    include_in_schema=False)
async def options_query_ask():
    return Response(status_code=200)

@router.options(
    "/api/v1/query/chats",
    tags=["CORS", "Proxy - Query Service"],
    include_in_schema=False)
async def options_query_chats():
    return Response(status_code=200)

@router.options(
    "/api/v1/query/chats/{chat_id}/messages",
    tags=["CORS", "Proxy - Query Service"],
    include_in_schema=False)
async def options_chat_messages(chat_id: uuid.UUID = Path(...)):
    return Response(status_code=200)

@router.options(
    "/api/v1/query/chats/{chat_id}",
    tags=["CORS", "Proxy - Query Service"],
    include_in_schema=False)
async def options_delete_chat(chat_id: uuid.UUID = Path(...)):
    return Response(status_code=200)

if settings.AUTH_SERVICE_URL:
    @router.options(
        "/api/v1/auth/{endpoint_path:path}",
        tags=["CORS", "Proxy - Auth Service (Optional)"],
        include_in_schema=False
    )
    async def options_proxy_auth_service_generic(endpoint_path: str = Path(...)):
        return Response(status_code=200)
# --- *** FIN SECCIÓN CORS OPTIONS HANDLERS *** ---


# --- Rutas Proxy Específicas para Query Service ---

@router.get(
    "/api/v1/query/chats",
    dependencies=[Depends(LoggedStrictAuth)],
    tags=["Proxy - Query Service"],
    summary="List user's chats (Proxied)"
)
async def proxy_get_chats(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: LoggedStrictAuth
):
    """Reenvía GET /api/v1/query/chats al Query Service."""
    # La ruta interna en query-service coincide con la externa expuesta aquí
    backend_path = "/api/v1/query/chats"
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

@router.post(
    "/api/v1/query/ask", # Ruta externa/frontend
    dependencies=[Depends(LoggedStrictAuth)],
    tags=["Proxy - Query Service"],
    summary="Submit a query or message to a chat (Proxied)"
)
async def proxy_post_query(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: LoggedStrictAuth
):
    """Reenvía POST /api/v1/query/ask al endpoint /api/v1/query/ask del Query Service."""
    # *** CORRECCIÓN RUTA DESTINO ***
    # Ahora la ruta interna en query-service también es /ask
    backend_path = "/api/v1/query/ask"
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

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
    chat_id: uuid.UUID = Path(...)
):
    """Reenvía GET /api/v1/query/chats/{chat_id}/messages al Query Service."""
    backend_path = f"/api/v1/query/chats/{chat_id}/messages"
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

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
    chat_id: uuid.UUID = Path(...)
):
    """Reenvía DELETE /api/v1/query/chats/{chat_id} al Query Service."""
    backend_path = f"/api/v1/query/chats/{chat_id}"
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)


# --- Rutas Proxy Genéricas para Ingest Service ---
# (Sin cambios, ya estaba bien mapeado)
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
    endpoint_path: str = Path(...)
):
    """
    Reenvía solicitudes autenticadas a `/api/v1/ingest/*` al Ingest Service.
    El path enviado al backend es `/api/v1/ingest/{endpoint_path}`.
    """
    backend_path = f"/api/v1/ingest/{endpoint_path}"
    return await _proxy_request(
        request=request,
        target_service_base_url_str=str(settings.INGEST_SERVICE_URL),
        client=client,
        user_payload=user_payload,
        backend_service_path=backend_path
    )


# --- Proxy Opcional para Auth Service (Excluir OPTIONS) ---
# (Sin cambios, ya estaba bien mapeado si está activo)
if settings.AUTH_SERVICE_URL:
    log.info(f"Auth service proxy enabled, forwarding to {settings.AUTH_SERVICE_URL}")
    @router.api_route(
        "/api/v1/auth/{endpoint_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        tags=["Proxy - Auth Service (Optional)"],
        summary="Generic proxy for Auth Service endpoints (No Gateway Auth)"
    )
    async def proxy_auth_service_generic(
        request: Request,
        client: Annotated[httpx.AsyncClient, Depends(get_client)],
        endpoint_path: str = Path(...)
    ):
        """Reenvía solicitudes a `/api/v1/auth/*` al Auth Service externo (si está configurado)."""
        backend_path = f"/api/v1/auth/{endpoint_path}"
        return await _proxy_request(
            request=request,
            target_service_base_url_str=str(settings.AUTH_SERVICE_URL),
            client=client,
            user_payload=None,
            backend_service_path=backend_path
        )
else:
     log.info("Auth service proxy is disabled (GATEWAY_AUTH_SERVICE_URL not set).")