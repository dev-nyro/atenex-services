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
# Importar dependencias de autenticación y tipo anotado
from app.auth.auth_middleware import StrictAuth, InitialAuth # Se necesitan ambos aquí si alguna ruta usa InitialAuth

# --- Definición del Router y Loggers PRIMERO ---
log = structlog.get_logger("atenex_api_gateway.router.gateway")
dep_log = structlog.get_logger("atenex_api_gateway.dependency.client")
auth_dep_log = structlog.get_logger("atenex_api_gateway.dependency.auth")

router = APIRouter() # Router principal para las rutas proxy
# ------------------------------------------------

# Cliente HTTP global (inyectado desde lifespan en main.py)
http_client: Optional[httpx.AsyncClient] = None

# Cabeceras Hop-by-Hop a excluir al hacer proxy
HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    # 'host' se maneja por separado
    # 'content-length' se recalcula o elimina para streaming/chunked
    "content-encoding", # El backend debe manejar la descompresión si acepta gzip/deflate
}

# --- Dependencias con Logging ---

# Dependencia para obtener el cliente HTTP inyectado
def get_client() -> httpx.AsyncClient:
    # Loguear intento de obtención del cliente
    dep_log.debug("Attempting to resolve HTTP client dependency...")
    if http_client is None or http_client.is_closed:
        dep_log.error("Gateway HTTP client dependency check failed: Client not available or closed.")
        # Fallar rápido si el cliente no está listo (problema de inicialización)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway service dependency unavailable (HTTP Client)."
        )
    dep_log.info("HTTP client dependency resolved successfully.")
    return http_client

# Wrapper para loguear el éxito de la dependencia StrictAuth
async def logged_strict_auth(
    # Dependemos de la dependencia original 'require_user' que está detrás de StrictAuth
    user_payload: StrictAuth # StrictAuth ya es Annotated[Dict[str, Any], Depends(require_user)]
) -> Dict[str, Any]:
     # Si llega aquí, require_user tuvo éxito (token válido con company_id)
     auth_dep_log.info("StrictAuth dependency successfully resolved in wrapper.",
                     user_id=user_payload.get('sub'),
                     company_id=user_payload.get('company_id'))
     # Devolvemos el payload para que la ruta lo pueda usar
     return user_payload

# Alias para usar en las rutas proxy protegidas
LoggedStrictAuth = Annotated[Dict[str, Any], Depends(logged_strict_auth)]

# --- Función Principal de Proxy ---

async def _proxy_request(
    request: Request,
    target_service_base_url_str: str,
    client: httpx.AsyncClient,
    # El payload del usuario validado (viene de LoggedStrictAuth o es None para rutas públicas)
    user_payload: Optional[Dict[str, Any]],
    # El path específico dentro del servicio backend (ej: "/api/v1/chats")
    backend_service_path: str
):
    method = request.method
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4())) # Asegurar un request_id
    # Crear un logger contextual para esta solicitud proxy
    proxy_log = log.bind(
        request_id=request_id,
        method=method,
        original_path=request.url.path,
        target_service=target_service_base_url_str,
        target_path=backend_service_path
    )

    proxy_log.info("Initiating proxy request")

    # Construir la URL completa de destino
    try:
        target_base_url = httpx.URL(target_service_base_url_str)
        # Usar copy_with para combinar base, path y query params originales
        target_url = target_base_url.copy_with(
            path=backend_service_path,
            query=request.url.query.encode("utf-8") # Pasar query params tal cual
        )
    except Exception as e:
        proxy_log.error("Failed to construct target URL", error=str(e))
        raise HTTPException(status_code=500, detail="Internal gateway configuration error.")

    # 1. Preparar Headers a reenviar
    headers_to_forward = {}
    client_host = request.client.host if request.client else "unknown"
    # Mantener o añadir X-Forwarded-For
    x_forwarded_for = request.headers.get("x-forwarded-for", client_host)
    headers_to_forward["X-Forwarded-For"] = x_forwarded_for
    headers_to_forward["X-Forwarded-Proto"] = request.url.scheme
    # Añadir X-Forwarded-Host si no está presente (algunos frameworks lo usan)
    if "host" in request.headers:
         headers_to_forward["X-Forwarded-Host"] = request.headers["host"]
    # Añadir X-Request-ID para tracing
    headers_to_forward["X-Request-ID"] = request_id

    # Copiar headers del cliente, excluyendo hop-by-hop y Host
    for name, value in request.headers.items():
        lower_name = name.lower()
        if lower_name not in HOP_BY_HOP_HEADERS and lower_name != "host":
            headers_to_forward[name] = value
        elif lower_name == "content-type": # Asegurar Content-Type siempre se pasa
             headers_to_forward[name] = value

    # 2. Inyectar Headers de Contexto (si hay payload)
    log_context_headers = {}
    if user_payload: # Solo si la ruta está protegida y el token es válido
        user_id = user_payload.get('sub')
        company_id = user_payload.get('company_id')
        user_email = user_payload.get('email')

        # Validar que los claims esperados están presentes (StrictAuth ya debería haberlo hecho)
        if not user_id:
             proxy_log.critical("Payload from StrictAuth missing 'sub'!", payload_keys=list(user_payload.keys()))
             raise HTTPException(status_code=500, detail="Internal authentication context error (missing user ID).")
        if not company_id: # StrictAuth requiere company_id, así que esto no debería ocurrir
             proxy_log.critical("Payload from StrictAuth missing 'company_id'!", payload_keys=list(user_payload.keys()))
             raise HTTPException(status_code=500, detail="Internal authentication context error (missing company ID).")

        # Añadir los headers para el servicio backend
        headers_to_forward['X-User-ID'] = str(user_id)
        headers_to_forward['X-Company-ID'] = str(company_id)
        log_context_headers['user_id'] = str(user_id)
        log_context_headers['company_id'] = str(company_id)
        if user_email:
            headers_to_forward['X-User-Email'] = str(user_email)
            log_context_headers['user_email'] = str(user_email)

        # Actualizar logger contextual
        proxy_log = proxy_log.bind(**log_context_headers)
        proxy_log.debug("Added context headers based on user payload.")

    # 3. Preparar Body (si existe)
    request_body_bytes: Optional[bytes] = None
    # Métodos que pueden tener cuerpo
    if method.upper() in ["POST", "PUT", "PATCH"]:
        # Leer el cuerpo de la request original
        # Nota: request.stream() podría ser más eficiente para cuerpos grandes,
        # pero request.body() es más simple si los cuerpos no son enormes.
        try:
            request_body_bytes = await request.body()
            if request_body_bytes:
                proxy_log.debug(f"Read request body for {method}", body_length=len(request_body_bytes))
                # httpx manejará Content-Length basado en el body proporcionado
            else:
                 proxy_log.debug(f"No request body found for {method}.")
        except Exception as e:
            proxy_log.error("Failed to read request body", error=str(e))
            raise HTTPException(status_code=400, detail="Could not read request body.")

    # Log antes de la llamada al backend
    proxy_log.debug("Prepared request for backend",
                    backend_method=method,
                    backend_url=str(target_url),
                    backend_headers=list(headers_to_forward.keys()), # No loguear valores de headers
                    has_body=(request_body_bytes is not None and len(request_body_bytes) > 0))

    # 4. Realizar la Petición al Servicio Backend usando stream=True
    backend_response: Optional[httpx.Response] = None
    try:
        proxy_log.info(f"Sending request to backend target: {method} {target_url.path}")
        # Construir la request de httpx explícitamente
        req = client.build_request(
            method=method,
            url=target_url,
            headers=headers_to_forward,
            content=request_body_bytes # Pasar el cuerpo leído
            # timeout=settings.HTTP_CLIENT_TIMEOUT # Timeout ya está en el cliente
        )
        # Enviar la request y obtener la respuesta como stream
        backend_response = await client.send(req, stream=True)

        # 5. Procesar y Devolver la Respuesta del Backend (Streaming)
        proxy_log.info(f"Received response from backend", status_code=backend_response.status_code)

        # Preparar headers de la respuesta, excluyendo hop-by-hop
        response_headers = {}
        for name, value in backend_response.headers.items():
            if name.lower() not in HOP_BY_HOP_HEADERS:
                response_headers[name] = value
        # Añadir X-Request-ID a la respuesta también
        response_headers["X-Request-ID"] = request_id

        # Crear una StreamingResponse para enviar los datos al cliente original
        # a medida que llegan del backend.
        # background=backend_response.aclose asegura que la conexión se cierre al terminar.
        return StreamingResponse(
            backend_response.aiter_raw(), # Iterador asíncrono del cuerpo raw
            status_code=backend_response.status_code,
            headers=response_headers,
            media_type=backend_response.headers.get("content-type"),
            background=backend_response.aclose # Tarea para cerrar la conexión backend
        )

    # --- Manejo de Errores Específicos de HTTPX ---
    except httpx.TimeoutException as exc:
        proxy_log.error(f"Proxy request timed out waiting for backend", target=str(target_url), error=str(exc))
        # Cerrar la conexión backend si se abrió antes del timeout
        if backend_response: await backend_response.aclose()
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Upstream service timed out.")
    except httpx.ConnectError as exc:
        proxy_log.error(f"Proxy connection error: Could not connect to backend", target=str(target_url), error=str(exc))
        if backend_response: await backend_response.aclose()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not connect to upstream service.")
    except httpx.RequestError as exc:
        # Otros errores de request (ej. SSL error, error de protocolo)
        proxy_log.error(f"Proxy request error communicating with backend", target=str(target_url), error=str(exc))
        if backend_response: await backend_response.aclose()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Error communicating with upstream service: {exc}")
    except Exception as exc:
        # Capturar cualquier otro error inesperado durante el proxy
        proxy_log.exception(f"Unexpected error occurred during proxy request", target=str(target_url))
        # Asegurarse de cerrar la conexión si existe
        if backend_response: await backend_response.aclose()
        # Relanzar como 500 internal server error
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred in the gateway.")


# --- Rutas Proxy Específicas para Query Service ---

# Los handlers de ruta ahora llaman a _proxy_request con los parámetros correctos.
# Usamos LoggedStrictAuth para rutas protegidas.

@router.get(
    "/api/v1/query/chats",
    dependencies=[Depends(LoggedStrictAuth)], # Asegura autenticación estricta
    tags=["Proxy - Query Service"],
    summary="List user's chats (Proxied)"
)
async def proxy_get_chats(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: LoggedStrictAuth # El payload validado está disponible aquí
):
    proxy_log = log.bind(request_id=getattr(request.state, 'request_id', 'N/A'))
    proxy_log.info("Entering proxy handler for GET /api/v1/query/chats")
    backend_path = "/api/v1/chats" # Path en el Query Service
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

@router.post(
    "/api/v1/query",
    dependencies=[Depends(LoggedStrictAuth)],
    tags=["Proxy - Query Service"],
    summary="Submit a query or message (Proxied)"
)
async def proxy_post_query(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: LoggedStrictAuth
):
    proxy_log = log.bind(request_id=getattr(request.state, 'request_id', 'N/A'))
    proxy_log.info("Entering proxy handler for POST /api/v1/query")
    backend_path = "/api/v1/query" # Path en el Query Service
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
    chat_id: uuid.UUID = Path(..., description="The UUID of the chat") # Validar UUID en path
):
    proxy_log = log.bind(request_id=getattr(request.state, 'request_id', 'N/A'), chat_id=str(chat_id))
    proxy_log.info("Entering proxy handler for GET /api/v1/query/chats/{chat_id}/messages")
    backend_path = f"/api/v1/chats/{chat_id}/messages" # Path dinámico en Query Service
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
    chat_id: uuid.UUID = Path(..., description="The UUID of the chat to delete")
):
    proxy_log = log.bind(request_id=getattr(request.state, 'request_id', 'N/A'), chat_id=str(chat_id))
    proxy_log.info("Entering proxy handler for DELETE /api/v1/query/chats/{chat_id}")
    backend_path = f"/api/v1/chats/{chat_id}" # Path dinámico en Query Service
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload, backend_path)

# --- Rutas OPTIONS para CORS Preflight ---
# Necesarias si el frontend hace peticiones con headers custom (Authorization) o métodos no simples (PUT/DELETE)

@router.options("/api/v1/query/chats", tags=["CORS"], include_in_schema=False)
async def options_query_chats(): return Response(status_code=200)

@router.options("/api/v1/query", tags=["CORS"], include_in_schema=False)
async def options_query(): return Response(status_code=200)

@router.options("/api/v1/query/chats/{chat_id}/messages", tags=["CORS"], include_in_schema=False)
async def options_chat_messages(chat_id: uuid.UUID = Path(...)): return Response(status_code=200)

@router.options("/api/v1/query/chats/{chat_id}", tags=["CORS"], include_in_schema=False)
async def options_delete_chat(chat_id: uuid.UUID = Path(...)): return Response(status_code=200)

# --- Proxy Genérico para Ingest Service ---
# Reenvía cualquier path bajo /api/v1/ingest/* al Ingest Service
@router.api_route(
    "/api/v1/ingest/{endpoint_path:path}", # Captura todo el path restante
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    dependencies=[Depends(LoggedStrictAuth)], # Protegido por defecto
    tags=["Proxy - Ingest Service"],
    summary="Generic proxy for Ingest Service endpoints"
)
async def proxy_ingest_service_generic(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: LoggedStrictAuth,
    endpoint_path: str = Path(..., description="Path within the Ingest service")
):
    proxy_log = log.bind(request_id=getattr(request.state, 'request_id', 'N/A'), endpoint_path=endpoint_path)
    proxy_log.info(f"Entering generic proxy handler for Ingest service")
    # Asumimos que Ingest Service espera las rutas sin el prefijo /api/v1/ingest
    # Si Ingest espera el prefijo completo, ajusta backend_path
    backend_path = f"/{endpoint_path}" # Path relativo dentro del servicio Ingest
    return await _proxy_request(
        request=request,
        target_service_base_url_str=str(settings.INGEST_SERVICE_URL),
        client=client,
        user_payload=user_payload,
        backend_service_path=backend_path
    )

# --- Proxy Opcional para Auth Service (Si se configura) ---
# Reenvía cualquier path bajo /api/v1/auth/* al Auth Service, SIN autenticación en el Gateway
if settings.AUTH_SERVICE_URL:
    log.info(f"Auth service proxy enabled, forwarding to {settings.AUTH_SERVICE_URL}")
    @router.api_route(
        "/api/v1/auth/{endpoint_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        # SIN Dependencia de autenticación aquí
        tags=["Proxy - Auth Service (Optional)"],
        summary="Generic proxy for Auth Service endpoints (No Gateway Auth)"
    )
    async def proxy_auth_service_generic(
        request: Request,
        client: Annotated[httpx.AsyncClient, Depends(get_client)],
        endpoint_path: str = Path(..., description="Path within the Auth service")
        # Sin user_payload aquí
    ):
        proxy_log = log.bind(request_id=getattr(request.state, 'request_id', 'N/A'), endpoint_path=endpoint_path)
        proxy_log.info(f"Entering generic proxy handler for Auth service (unauthenticated at gateway)")
        # Asumimos que Auth Service espera las rutas sin el prefijo /api/v1/auth
        backend_path = f"/{endpoint_path}"
        return await _proxy_request(
            request=request,
            target_service_base_url_str=str(settings.AUTH_SERVICE_URL),
            client=client,
            user_payload=None, # Pasar None explícitamente
            backend_service_path=backend_path
        )
else:
     log.info("Auth service proxy is disabled (GATEWAY_AUTH_SERVICE_URL not set).")