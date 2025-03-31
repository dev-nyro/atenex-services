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

async def _proxy_request(
    request: Request,
    target_url: str,
    client: httpx.AsyncClient,
    user_payload: Optional[dict] # Payload del usuario autenticado (viene de require_user)
):
    """Función interna para realizar el proxy de la petición de forma segura."""
    method = request.method
    downstream_url = httpx.URL(target_url)

    # 1. Preparar Headers para downstream
    headers_to_forward = {}
    # Añadir X-Request-ID o similar para tracing si no existe
    # request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    # headers_to_forward["x-request-id"] = request_id
    # log = log.bind(request_id=request_id) # Vincular ID a logs

    for name, value in request.headers.items():
        if name.lower() not in HOP_BY_HOP_HEADERS:
            headers_to_forward[name] = value

    # Añadir/Sobrescribir X-Company-ID basado en el token verificado
    if user_payload and 'company_id' in user_payload:
        company_id = str(user_payload['company_id'])
        headers_to_forward['X-Company-ID'] = company_id
        log = log.bind(company_id=company_id) # Vincular company_id a logs
    else:
        # Si una ruta protegida llegó aquí sin company_id en el token, es un error de configuración/token
        log.error("Protected route reached without company_id in user payload!", user_payload=user_payload)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing company identifier.")

    # Pasar información del usuario si es necesario (con prefijo para evitar colisiones)
    # if user_payload and 'user_id' in user_payload:
    #     headers_to_forward['X-User-ID'] = str(user_payload['user_id'])
    # if user_payload and 'role' in user_payload:
    #     headers_to_forward['X-User-Role'] = str(user_payload['role'])

    # 2. Preparar Query Params y Body
    query_params = request.query_params
    # Leer el body como stream para evitar cargarlo en memoria si es grande
    request_body_bytes = request.stream()

    # 3. Realizar la petición downstream
    log.info(f"Proxying request", method=method, path=request.url.path, target=str(downstream_url))
    # log.debug(f"Forwarding Headers", headers=headers_to_forward) # Puede ser verboso

    try:
        # Construir el request para httpx
        req = client.build_request(
            method=method,
            url=downstream_url,
            headers=headers_to_forward,
            params=query_params,
            content=request_body_bytes # Pasar el stream directamente
        )
        # Enviar el request y obtener la respuesta como stream
        rp = await client.send(req, stream=True) # stream=True es clave

        # 4. Preparar y devolver la respuesta al cliente original
        # Loguear la respuesta del downstream
        log.info(f"Received response from downstream", status_code=rp.status_code, target=str(downstream_url))

        # Filtrar hop-by-hop headers de la respuesta
        response_headers = {}
        for name, value in rp.headers.items():
            if name.lower() not in HOP_BY_HOP_HEADERS:
                response_headers[name] = value

        # Devolver como StreamingResponse para eficiencia
        return StreamingResponse(
            rp.aiter_raw(), # Stream de bytes de la respuesta
            status_code=rp.status_code,
            headers=response_headers,
            media_type=rp.headers.get("content-type"),
        )

    except httpx.TimeoutException as exc:
        log.error(f"Request to downstream service timed out", target=str(downstream_url), timeout=settings.HTTP_CLIENT_TIMEOUT, error=str(exc))
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=f"Upstream service at {downstream_url.host} timed out.")
    except httpx.ConnectError as exc:
        log.error(f"Could not connect to downstream service", target=str(downstream_url), error=str(exc))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Upstream service at {downstream_url.host} is unavailable.")
    except httpx.RequestError as exc: # Otros errores de request (SSL, etc.)
        log.error(f"Error during request to downstream service", target=str(downstream_url), error=str(exc), exc_info=True)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Error communicating with upstream service: {type(exc).__name__}")
    except Exception as exc:
        log.exception(f"Unexpected error during proxy request", target=str(downstream_url))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An internal gateway error occurred.")
    finally:
        # Asegurarse de cerrar la respuesta si no se usa StreamingResponse o si hay error antes
        if 'rp' in locals() and rp and not rp.is_closed:
             await rp.aclose()


# --- Rutas Proxy Específicas ---

# Usamos un path parameter genérico '{path:path}' para capturar todo después del prefijo
# Aplicamos la dependencia 'require_user' a todas las rutas proxificadas
@router.api_route(
    "/api/v1/ingest/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"], # Excluir OPTIONS/HEAD si no se manejan explícitamente
    dependencies=[Depends(require_user)], # Proteger rutas de ingesta
    tags=["Proxy"],
    summary="Proxy to Ingest Service",
    # include_in_schema=False # Ocultar de OpenAPI para no duplicar? Depende de la estrategia
)
async def proxy_ingest_service(
    request: Request,
    path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user: Annotated[dict, Depends(require_user)] # Inyectar payload validado
):
    """Proxy genérico para todas las rutas bajo /api/v1/ingest/"""
    base_url = settings.INGEST_SERVICE_URL.rstrip('/')
    # Construir la URL completa del servicio downstream
    target_url = f"{base_url}{request.url.path}" # Usar path completo original
    if request.url.query:
        target_url += f"?{request.url.query}"

    return await _proxy_request(request, target_url, client, user)

@router.api_route(
    "/api/v1/query/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    dependencies=[Depends(require_user)], # Proteger rutas de consulta
    tags=["Proxy"],
    summary="Proxy to Query Service",
    # include_in_schema=False
)
async def proxy_query_service(
    request: Request,
    path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user: Annotated[dict, Depends(require_user)]
):
    """Proxy genérico para todas las rutas bajo /api/v1/query/"""
    base_url = settings.QUERY_SERVICE_URL.rstrip('/')
    target_url = f"{base_url}{request.url.path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    return await _proxy_request(request, target_url, client, user)


# --- Proxy para Auth Service (Opcional) ---
# Si tienes un servicio de Auth separado para /login, /register, etc.
# Estas rutas NO estarían protegidas por 'require_user'
# if settings.AUTH_SERVICE_URL:
#     @router.api_route(
#         "/api/auth/{path:path}",
#         methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
#         tags=["Proxy"],
#         summary="Proxy to Authentication Service",
#         # include_in_schema=False
#     )
#     async def proxy_auth_service(
#         request: Request,
#         path: str,
#         client: Annotated[httpx.AsyncClient, Depends(get_client)],
#         # NO hay dependencia 'require_user' aquí
#     ):
#         """Proxy genérico para el servicio de autenticación."""
#         base_url = settings.AUTH_SERVICE_URL.rstrip('/')
#         target_url = f"{base_url}{request.url.path}"
#         if request.url.query:
#             target_url += f"?{request.url.query}"

#         # Pasar user_payload=None ya que estas rutas no requieren autenticación previa
#         return await _proxy_request(request, target_url, client, user_payload=None)