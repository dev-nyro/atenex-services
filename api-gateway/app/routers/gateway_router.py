# api-gateway/app/routers/gateway_router.py
from fastapi import APIRouter, Request, Response, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from typing import Optional, Annotated, Dict, Any
import httpx
import structlog
import asyncio # Para timeouts específicos si fueran necesarios

from app.core.config import settings
# Importar la dependencia correcta para rutas protegidas
from app.auth.auth_middleware import StrictAuth # Alias para require_user

log = structlog.get_logger(__name__)
router = APIRouter()

# Cliente HTTP global reutilizable (se inyecta/cierra en main.py lifespan)
http_client: Optional[httpx.AsyncClient] = None

# Cabeceras HTTP que son "hop-by-hop" y no deben ser reenviadas
HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    # "content-encoding", # Considerar quitar si causa problemas con compresión
    "content-length", # Content-Length debe ser recalculado por httpx
}

def get_client() -> httpx.AsyncClient:
    """Dependencia FastAPI para obtener el cliente HTTP global inicializado."""
    if http_client is None or http_client.is_closed:
        log.error("Gateway HTTP client is not available or closed. Cannot proxy request.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway service dependency unavailable (HTTP Client)."
        )
    return http_client

async def _proxy_request(
    request: Request,
    target_url_str: str,
    client: httpx.AsyncClient,
    user_payload: Optional[Dict[str, Any]],
    # Añadido endpoint_path capturado para loguear mejor
    gateway_endpoint_path: str
):
    method = request.method
    # Construcción de URL: Determinar el path real para el backend
    base_url_obj = httpx.URL(str(request.url).split(request.url.path)[0]) # URL base del gateway
    target_service_base_url = httpx.URL(target_url_str) # URL base del servicio destino

    # El path relativo para el servicio destino es el que capturamos en el gateway
    # después del prefijo de servicio (ej. '/upload', '/chats', '/ask')
    # gateway_endpoint_path ya contendrá esto (ej. 'upload', 'chats', 'ask')
    # Añadir el '/' inicial si no está
    service_path = gateway_endpoint_path if gateway_endpoint_path.startswith('/') else f"/{gateway_endpoint_path}"

    # Construir la URL final para el backend
    target_url = target_service_base_url.copy_with(path=service_path, query=request.url.query.encode("utf-8"))


    # 1. Preparar Headers para reenviar
    headers_to_forward = {}
    client_host = request.client.host if request.client else "unknown"
    x_forwarded_for = request.headers.get("x-forwarded-for", client_host)

    for name, value in request.headers.items():
        # Excluir hop-by-hop y host explícitamente
        lower_name = name.lower()
        if lower_name not in HOP_BY_HOP_HEADERS and lower_name != "host":
            headers_to_forward[name] = value

    # Añadir/Actualizar cabeceras X-Forwarded-*
    headers_to_forward["X-Forwarded-For"] = x_forwarded_for
    headers_to_forward["X-Forwarded-Proto"] = request.url.scheme
    headers_to_forward["X-Forwarded-Host"] = request.headers.get("host", "") # Host original pedido al gateway

    # Añadir X-Request-ID para tracing downstream si está disponible
    request_id = getattr(request.state, 'request_id', None)
    if request_id:
        headers_to_forward["X-Request-ID"] = request_id

    # 2. Inyectar Headers de Autenticación/Contexto (SI HAY PAYLOAD y NO es OPTIONS)
    log_context = {'request_id': request_id} if request_id else {}
    if user_payload and method.upper() != 'OPTIONS':
        user_id = user_payload.get('sub')
        company_id = user_payload.get('company_id') # Asegurado por StrictAuth
        user_email = user_payload.get('email')

        if not user_id or not company_id:
             log.critical("CRITICAL: Payload missing required fields (sub/company_id) after StrictAuth!",
                          payload_keys=list(user_payload.keys()), user_id=user_id, company_id=company_id)
             raise HTTPException(status_code=500, detail="Internal authentication context error.")

        headers_to_forward['X-User-ID'] = str(user_id)
        headers_to_forward['X-Company-ID'] = str(company_id)
        log_context['user_id'] = str(user_id)
        log_context['company_id'] = str(company_id)
        if user_email:
             headers_to_forward['X-User-Email'] = str(user_email)

    bound_log = log.bind(**log_context)

    # 3. Preparar Body (stream body si es posible/necesario)
    # body_iterator = request.stream() # Alternativa para streaming
    # content=body_iterator -> para streaming puro
    # Mejor leer el cuerpo completo para métodos comunes, ya que httpx puede manejarlo.
    # Esto asegura que Content-Length se envíe correctamente al backend si es necesario.
    request_body_bytes: Optional[bytes] = None
    if method.upper() in ["POST", "PUT", "PATCH"]:
        request_body_bytes = await request.body()
        bound_log.debug(f"Read request body for {method}", body_length=len(request_body_bytes) if request_body_bytes else 0)
    # Para otros métodos (GET, DELETE, OPTIONS), body es None por defecto

    # 4. Realizar la petición downstream
    bound_log.info(f"Proxying request", method=method, from_path=request.url.path, to_target=str(target_url))

    rp: Optional[httpx.Response] = None
    try:
        # Construir la solicitud httpx
        req = client.build_request(
            method=method,
            url=target_url,
            headers=headers_to_forward,
            content=request_body_bytes # Pasar el cuerpo leído (o None)
            # cookies=request.cookies, # Pasar cookies si es necesario
        )
        # Enviar la solicitud y obtener la respuesta como stream
        rp = await client.send(req, stream=True)

        # 5. Procesar y devolver la respuesta del servicio backend
        bound_log.info(f"Received response from downstream", status_code=rp.status_code, target=str(target_url))

        # Filtrar cabeceras hop-by-hop de la respuesta del backend
        response_headers = {
            k: v for k, v in rp.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS
        }

        # Devolver StreamingResponse
        return StreamingResponse(
            rp.aiter_raw(), # Iterador asíncrono del cuerpo de la respuesta
            status_code=rp.status_code,
            headers=response_headers,
            media_type=rp.headers.get("content-type"), # Preservar content-type
            background=rp.aclose # Cerrar la respuesta httpx al terminar
        )

    except httpx.TimeoutException as exc:
         bound_log.error(f"Request timed out to downstream service", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=f"Upstream service timed out.")
    except httpx.ConnectError as exc:
         bound_log.error(f"Connection error to downstream service", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Could not connect to upstream service.")
    except httpx.RequestError as exc:
         bound_log.error(f"HTTPX request error during proxy", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Error communicating with upstream service.")
    except Exception as exc:
        bound_log.exception(f"Unexpected error during proxy operation", target=str(target_url))
        if rp and hasattr(rp, 'aclose') and callable(rp.aclose):
            try: await rp.aclose()
            except Exception: pass
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal gateway error during proxy.")

# --- Rutas Proxy Específicas (MODIFICADAS) ---

@router.api_route(
    # ----- CAMBIO AQUÍ: Renombrar parámetro de 'path' a 'endpoint_path' -----
    "/api/v1/ingest/{endpoint_path:path}",
    # Asegurar que OPTIONS está incluido
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    dependencies=[Depends(StrictAuth)],
    tags=["Proxy - Ingest"],
    summary="Proxy to Ingest Service (Authentication Required)",
    response_description="Response proxied directly from the Ingest Service.",
    name="proxy_ingest_service",
)
async def proxy_ingest_service(
    request: Request,
    # ----- CAMBIO AQUÍ: Usar el nuevo nombre del parámetro -----
    endpoint_path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: StrictAuth
):
    """Reenvía peticiones a /api/v1/ingest/* al Ingest Service."""
    return await _proxy_request(
        request,
        str(settings.INGEST_SERVICE_URL),
        client,
        user_payload,
        gateway_endpoint_path=endpoint_path # Pasar el path capturado
    )

@router.api_route(
    # ----- CAMBIO AQUÍ: Renombrar parámetro de 'path' a 'endpoint_path' -----
    "/api/v1/query/{endpoint_path:path}",
    # Asegurar que OPTIONS está incluido
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    dependencies=[Depends(StrictAuth)],
    tags=["Proxy - Query"],
    summary="Proxy to Query Service (Authentication Required)",
    response_description="Response proxied directly from the Query Service.",
    name="proxy_query_service",
)
async def proxy_query_service(
    request: Request,
    # ----- CAMBIO AQUÍ: Usar el nuevo nombre del parámetro -----
    endpoint_path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: StrictAuth
):
    """Reenvía peticiones a /api/v1/query/* al Query Service."""
    return await _proxy_request(
        request,
        str(settings.QUERY_SERVICE_URL),
        client,
        user_payload,
        gateway_endpoint_path=endpoint_path # Pasar el path capturado
    )


# --- Proxy Opcional para Auth Service ---
if settings.AUTH_SERVICE_URL:
    log.info(f"Auth service proxy enabled for base URL: {settings.AUTH_SERVICE_URL}")
    @router.api_route(
        # ----- CAMBIO AQUÍ: Renombrar parámetro de 'path' a 'endpoint_path' -----
        "/api/v1/auth/{endpoint_path:path}",
        # Asegurar que OPTIONS está incluido
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        tags=["Proxy - Auth"],
        summary="Proxy to Authentication Service (No Gateway Authentication)",
        response_description="Response proxied directly from the Auth Service.",
        name="proxy_auth_service",
    )
    async def proxy_auth_service(
        request: Request,
        # ----- CAMBIO AQUÍ: Usar el nuevo nombre del parámetro -----
        endpoint_path: str,
        client: Annotated[httpx.AsyncClient, Depends(get_client)],
    ):
        """Proxy genérico para el servicio de autenticación (si está configurado)."""
        return await _proxy_request(
            request,
            str(settings.AUTH_SERVICE_URL),
            client,
            user_payload=None, # Sin payload porque no hay autenticación aquí
            gateway_endpoint_path=endpoint_path # Pasar el path capturado
        )
else:
     # Solo se loguea una vez al inicio si no está configurado
     pass # El log ya se hace en main.py al cargar