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
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    # 'content-encoding' a veces se quita, pero puede ser necesario si el backend responde comprimido y el cliente espera eso.
    # Lo dejaremos pasar por defecto a menos que cause problemas.
    # "content-encoding",
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
    target_url_str: str, # URL completa del servicio destino
    client: httpx.AsyncClient,
    user_payload: Optional[Dict[str, Any]] # Payload del token validado (si aplica)
):
    """Función interna reutilizable para realizar el proxy de la petición HTTP."""
    method = request.method
    # --- CORRECCIÓN: Asegurar que target_url_str NO tenga doble '//' ---
    # Esto puede pasar si base_url termina en '/' y path empieza con '/'
    # Construir URL cuidadosamente
    base_url_obj = httpx.URL(str(request.url).split(request.url.path)[0]) # Obtener base URL de la request original
    target_url_obj = httpx.URL(target_url_str) # Target base
    # Reconstruir path y query
    full_target_path = target_url_obj.path + (request.url.path.split(settings.API_V1_STR, 1)[1] if settings.API_V1_STR in request.url.path else request.url.path)
    target_url = target_url_obj.copy_with(path=full_target_path, query=request.url.query.encode("utf-8"))


    # 1. Preparar Headers para reenviar
    headers_to_forward = {}
    client_host = request.client.host if request.client else "unknown"
    # Usar X-Forwarded-For existente o el IP del cliente directo
    x_forwarded_for = request.headers.get("x-forwarded-for", client_host)

    for name, value in request.headers.items():
        if name.lower() not in HOP_BY_HOP_HEADERS:
            headers_to_forward[name] = value

    # Añadir/Actualizar cabeceras X-Forwarded-*
    headers_to_forward["X-Forwarded-For"] = x_forwarded_for
    headers_to_forward["X-Forwarded-Proto"] = request.url.scheme
    # X-Forwarded-Host debe ser el host original solicitado al gateway
    headers_to_forward["X-Forwarded-Host"] = request.headers.get("host", "")

    # Añadir X-Request-ID para tracing downstream si está disponible en el estado
    request_id = getattr(request.state, 'request_id', None)
    if request_id:
        headers_to_forward["X-Request-ID"] = request_id

    # 2. Inyectar Headers de Autenticación/Contexto (SI HAY PAYLOAD y NO es OPTIONS)
    log_context = {'request_id': request_id} if request_id else {}
    if user_payload and method.upper() != 'OPTIONS':
        user_id = user_payload.get('sub')
        company_id = user_payload.get('company_id') # Asegurado por StrictAuth que existe
        user_email = user_payload.get('email')

        # Doble chequeo por si acaso, aunque StrictAuth debería garantizarlo
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

    # Vincular contexto al logger para esta operación de proxy
    bound_log = log.bind(**log_context)

    # 3. Preparar Query Params (ya están en la target_url) y Body
    # Usar request.stream() para eficiencia, evita cargar todo el body en memoria
    request_body_stream = request.stream()

    # 4. Realizar la petición downstream
    bound_log.info(f"Proxying request", method=method, from_path=request.url.path, to_target=str(target_url))

    rp: Optional[httpx.Response] = None
    try:
        # Construir la solicitud httpx
        req = client.build_request(
            method=method,
            url=target_url, # Usar la URL reconstruida
            headers=headers_to_forward,
            # params=query_params, # Query params ya están en la URL
            # Pasar el stream directamente como contenido
            content=request_body_stream
        )
        # Enviar la solicitud y obtener la respuesta como stream
        rp = await client.send(req, stream=True)

        # 5. Procesar y devolver la respuesta del servicio backend
        bound_log.info(f"Received response from downstream", status_code=rp.status_code, target=str(target_url))

        # Filtrar cabeceras hop-by-hop de la respuesta del backend
        response_headers = {
            k: v for k, v in rp.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS
        }

        # Devolver StreamingResponse para eficiencia de memoria
        return StreamingResponse(
            rp.aiter_raw(), # Iterador asíncrono del cuerpo de la respuesta
            status_code=rp.status_code,
            headers=response_headers,
            media_type=rp.headers.get("content-type"), # Preservar content-type original
            background=rp.aclose # Tarea para cerrar la respuesta httpx cuando FastAPI termine
        )

    except httpx.TimeoutException as exc:
         bound_log.error(f"Request timed out to downstream service", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=f"Upstream service timed out.")
    except httpx.ConnectError as exc:
         bound_log.error(f"Connection error to downstream service", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Could not connect to upstream service.")
    except httpx.RequestError as exc:
         # Otros errores de httpx (ej. SSL, problemas de proxy interno, etc.)
         bound_log.error(f"HTTPX request error during proxy", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Error communicating with upstream service.")
    except Exception as exc:
        # Capturar cualquier otro error inesperado
        bound_log.exception(f"Unexpected error during proxy operation", target=str(target_url))
        # Intentar cerrar la respuesta httpx si se abrió antes de la excepción
        if rp and hasattr(rp, 'aclose') and callable(rp.aclose):
            try: await rp.aclose()
            except Exception: pass # Ignorar errores al cerrar en medio de otra excepción
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal gateway error during proxy.")

# --- Rutas Proxy Específicas ---

@router.api_route(
    "/api/v1/ingest/{path:path}",
    # *** CORRECCIÓN: Añadido OPTIONS ***
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    dependencies=[Depends(StrictAuth)], # Requiere token válido CON company_id
    tags=["Proxy - Ingest"],
    summary="Proxy to Ingest Service (Authentication Required)",
    response_description="Response proxied directly from the Ingest Service.",
    name="proxy_ingest_service",
)
async def proxy_ingest_service(
    request: Request,
    path: str, # El path capturado por {path:path}
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: StrictAuth # La dependencia StrictAuth ya validó
):
    """Reenvía peticiones a /api/v1/ingest/* al Ingest Service configurado."""
    # No es necesario construir la URL aquí, _proxy_request lo hará
    return await _proxy_request(request, str(settings.INGEST_SERVICE_URL), client, user_payload)

@router.api_route(
    "/api/v1/query/{path:path}",
    # *** CORRECCIÓN: Añadido OPTIONS ***
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    dependencies=[Depends(StrictAuth)], # Requiere token válido CON company_id
    tags=["Proxy - Query"],
    summary="Proxy to Query Service (Authentication Required)",
    response_description="Response proxied directly from the Query Service.",
    name="proxy_query_service",
)
async def proxy_query_service(
    request: Request,
    path: str, # El path capturado por {path:path}
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: StrictAuth # Payload validado por la dependencia
):
    """Reenvía peticiones a /api/v1/query/* al Query Service configurado."""
    # No es necesario construir la URL aquí, _proxy_request lo hará
    return await _proxy_request(request, str(settings.QUERY_SERVICE_URL), client, user_payload)


# --- Proxy Opcional para Auth Service (Mantenido pero ahora sin rutas activas definidas) ---
if settings.AUTH_SERVICE_URL:
    log.info(f"Auth service proxy enabled for base URL: {settings.AUTH_SERVICE_URL}")
    @router.api_route(
        "/api/v1/auth/{path:path}",
        # *** CORRECCIÓN: Añadido OPTIONS ***
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        tags=["Proxy - Auth"],
        summary="Proxy to Authentication Service (No Gateway Authentication)",
        response_description="Response proxied directly from the Auth Service.",
        name="proxy_auth_service",
    )
    async def proxy_auth_service(
        request: Request,
        path: str,
        client: Annotated[httpx.AsyncClient, Depends(get_client)],
        # NO hay dependencia StrictAuth o InitialAuth aquí.
    ):
        """Proxy genérico para el servicio de autenticación (si está configurado)."""
        # No es necesario construir la URL aquí, _proxy_request lo hará
        return await _proxy_request(request, str(settings.AUTH_SERVICE_URL), client, user_payload=None)
else:
     # Loguear sólo una vez al inicio si no está configurado
     log.warning("Auth service proxy is not configured (GATEWAY_AUTH_SERVICE_URL not set). "
                 "Requests to /api/v1/auth/* will result in 501 Not Implemented (unless handled by other routers).")
     # No definimos la ruta aquí si está deshabilitado para evitar conflictos
     # con el auth_router.py local si se monta (aunque ahora esté vacío).