# File: app/routers/gateway_router.py
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
# Se asigna en main.py lifespan
http_client: Optional[httpx.AsyncClient] = None

# Cabeceras HTTP que son "hop-by-hop" y no deben ser reenviadas
# directamente entre el cliente, el proxy y el servicio backend.
# Ver RFC 2616, Sección 13.5.1.
# Convertido a minúsculas para comparación insensible a mayúsculas/minúsculas.
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te", # Transfer Encoding Extensions
    "trailers", # Para chunked transfer encoding
    "transfer-encoding",
    "upgrade", # Para WebSockets u otros protocolos
    # También eliminamos Host, ya que httpx lo establecerá al del destino
    "host",
    # Content-Length será recalculado por httpx basado en el stream/body
    "content-length",
    # Content-Encoding (como gzip) a veces causa problemas si el proxy
    # lo decodifica y el backend no lo espera. httpx suele manejarlo.
    # "content-encoding", # Comentar/Descomentar según sea necesario
}

def get_client() -> httpx.AsyncClient:
    """Dependencia FastAPI para obtener el cliente HTTP global inicializado."""
    if http_client is None or http_client.is_closed:
        log.error("Gateway HTTP client is not available or closed. Cannot proxy request.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway service is temporarily unavailable due to internal client error."
        )
    return http_client

async def _proxy_request(
    request: Request,
    target_url_str: str, # URL completa del servicio destino
    client: httpx.AsyncClient,
    # user_payload es el diccionario devuelto por StrictAuth o InitialAuth
    # Puede ser None para rutas que no usan estas dependencias (como el proxy /auth opcional)
    user_payload: Optional[Dict[str, Any]]
):
    """
    Función interna reutilizable para realizar el proxy de la petición HTTP.
    """
    method = request.method
    target_url = httpx.URL(target_url_str) # Convertir a objeto URL de httpx

    # 1. Preparar Headers para reenviar
    headers_to_forward = {}
    # Copiar headers de la petición original, excluyendo hop-by-hop y Host
    for name, value in request.headers.items():
        if name.lower() not in HOP_BY_HOP_HEADERS:
            headers_to_forward[name] = value
        # else:
        #     log.debug(f"Skipping hop-by-hop header: {name}")

    # Añadir/Sobrescribir cabeceras X-Forwarded-* (opcional pero buena práctica detrás de un proxy)
    # Uvicorn/Gunicorn pueden añadir algunos si --forwarded-allow-ips está configurado
    # Aquí podemos asegurarnos de que estén presentes
    client_host = request.client.host if request.client else "unknown"
    headers_to_forward["X-Forwarded-For"] = request.headers.get("x-forwarded-for", client_host)
    headers_to_forward["X-Forwarded-Proto"] = request.headers.get("x-forwarded-proto", request.url.scheme)
    # X-Forwarded-Host contiene el Host original solicitado por el cliente
    headers_to_forward["X-Forwarded-Host"] = request.headers.get("host", "")


    # 2. Inyectar Headers de Autenticación/Contexto (SI HAY PAYLOAD)
    log_context = {} # Para añadir info al log de proxy
    if user_payload:
        user_id = user_payload.get('sub')
        company_id = user_payload.get('company_id') # Añadido por verify_token
        user_email = user_payload.get('email') # Añadido por verify_token

        if user_id:
            headers_to_forward['X-User-ID'] = str(user_id)
            log_context['user_id'] = user_id
        else:
            # Esto sería un error grave si user_payload existe
            log.error("CRITICAL: User payload present but 'sub' (user_id) claim missing!", payload_keys=list(user_payload.keys()))
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal authentication error: User ID missing.")

        # Para rutas protegidas por StrictAuth, company_id DEBE estar aquí
        if company_id:
            headers_to_forward['X-Company-ID'] = str(company_id)
            log_context['company_id'] = company_id
        elif request.scope.get('route') and request.scope['route'].name in ['proxy_ingest_service', 'proxy_query_service']:
             # Si estamos en una ruta que DEBERÍA tener company_id (protegida por StrictAuth)
             # y no lo tenemos, es un error interno grave. verify_token debería haber fallado.
             log.error("CRITICAL: StrictAuth route reached but 'company_id' missing from payload!", user_id=user_id, payload_keys=list(user_payload.keys()))
             raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal authentication error: Company ID missing.")

        if user_email:
             headers_to_forward['X-User-Email'] = str(user_email)
             # No añadir email a log_context por defecto por privacidad, a menos que sea necesario
             # log_context['user_email'] = user_email

    # Vincular contexto al logger para esta petición específica
    bound_log = log.bind(**log_context)

    # 3. Preparar Query Params y Body
    # Query params se pasan directamente a httpx
    query_params = request.query_params
    # Body se pasa como un stream asíncrono
    request_body_stream = request.stream()

    # 4. Realizar la petición downstream usando el cliente HTTPX
    bound_log.info(f"Proxying request", method=method, from_path=request.url.path, to_target=str(target_url))

    # Inicializar rp fuera del try para el finally
    rp: Optional[httpx.Response] = None
    try:
        # Construir la solicitud httpx
        req = client.build_request(
            method=method,
            url=target_url,
            headers=headers_to_forward,
            params=query_params,
            # Pasar el stream directamente para eficiencia (evita cargar todo en memoria)
            content=request_body_stream
        )
        # Enviar la solicitud y obtener la respuesta como stream
        rp = await client.send(req, stream=True)

        # 5. Procesar y devolver la respuesta del servicio backend
        bound_log.info(f"Received response from downstream", status_code=rp.status_code, target=str(target_url))

        # Filtrar headers hop-by-hop de la respuesta antes de devolverla al cliente
        response_headers = {
            k: v for k, v in rp.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS
        }

        # Devolver la respuesta como StreamingResponse para eficiencia
        return StreamingResponse(
            # Pasar el iterador asíncrono del cuerpo de la respuesta
            rp.aiter_raw(),
            status_code=rp.status_code,
            headers=response_headers,
            # Usar el Content-Type de la respuesta original
            media_type=rp.headers.get("content-type"),
            # Pasar background task para asegurar que se cierre la respuesta httpx
            # background=BackgroundTask(rp.aclose) # Alternativa a finally
        )

    except httpx.TimeoutException as exc:
         # Timeout al conectar o esperar respuesta del backend
         bound_log.error(f"Request timed out connecting to or waiting for downstream service", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=f"Upstream service at {target_url.host} timed out.")
    except httpx.ConnectError as exc:
         # Error al establecer conexión con el backend (ej. servicio caído)
         bound_log.error(f"Connection error to downstream service", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Could not connect to upstream service at {target_url.host}.")
    except httpx.RequestError as exc:
         # Otros errores de red/HTTP de httpx
         bound_log.error(f"HTTPX request error during proxy", target=str(target_url), error=str(exc))
         raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Error communicating with upstream service.")
    except Exception as exc:
        # Capturar cualquier otro error inesperado durante el proxy
        bound_log.exception(f"Unexpected error during proxy operation", target=str(target_url))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal gateway error during proxy.")
    finally:
        # Asegurarse de cerrar la respuesta httpx para liberar la conexión
        if rp and hasattr(rp, 'aclose') and callable(rp.aclose):
             try:
                 await rp.aclose()
                 # bound_log.debug("Downstream response closed.", target=str(target_url))
             except Exception as close_exc:
                 bound_log.warning("Error closing downstream response stream", target=str(target_url), error=str(close_exc))


# --- Rutas Proxy Específicas ---

# Ruta para Ingest Service (Protegida)
@router.api_route(
    "/api/v1/ingest/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], # Incluir OPTIONS explícitamente
    # Usar StrictAuth para requerir token válido CON company_id
    dependencies=[Depends(StrictAuth)], # La dependencia se ejecuta pero no necesitamos su valor aquí directamente
    tags=["Proxy - Ingest"],
    summary="Proxy to Ingest Service (Authentication Required)",
    name="proxy_ingest_service", # Nombre para referencia interna
)
async def proxy_ingest_service(
    request: Request, # Petición original
    path: str, # Parte de la ruta capturada
    client: Annotated[httpx.AsyncClient, Depends(get_client)], # Cliente HTTP inyectado
    # Inyectar el payload validado por StrictAuth para pasarlo a _proxy_request
    user_payload: StrictAuth # Ya validado: Dict[str, Any]
):
    """Reenvía peticiones a /api/v1/ingest/* al Ingest Service."""
    base_url = settings.INGEST_SERVICE_URL.rstrip('/')
    # Reconstruir la URL completa del servicio destino
    target_url = f"{base_url}/api/v1/ingest/{path}"
    # Añadir query string si existe
    if request.url.query: target_url += f"?{request.url.query}"

    # Llamar a la función proxy interna pasando el payload
    return await _proxy_request(request, target_url, client, user_payload)

# Ruta para Query Service (Protegida)
@router.api_route(
    "/api/v1/query/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], # Incluir OPTIONS
    dependencies=[Depends(StrictAuth)], # Requiere token válido CON company_id
    tags=["Proxy - Query"],
    summary="Proxy to Query Service (Authentication Required)",
    name="proxy_query_service",
)
async def proxy_query_service(
    request: Request,
    path: str,
    client: Annotated[httpx.AsyncClient, Depends(get_client)],
    user_payload: StrictAuth # Payload validado
):
    """Reenvía peticiones a /api/v1/query/* al Query Service."""
    base_url = settings.QUERY_SERVICE_URL.rstrip('/')
    target_url = f"{base_url}/api/v1/query/{path}"
    if request.url.query: target_url += f"?{request.url.query}"
    return await _proxy_request(request, target_url, client, user_payload)


# --- Proxy Opcional para Auth Service (NO Protegido por el Gateway) ---
if settings.AUTH_SERVICE_URL:
    log.info(f"Auth service proxy enabled for base URL: {settings.AUTH_SERVICE_URL}")
    @router.api_route(
        "/api/v1/auth/{path:path}", # Ruta para proxyficar llamadas de autenticación
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], # Permitir todos los métodos comunes
        tags=["Proxy - Auth"],
        summary="Proxy to Authentication Service (No Gateway Authentication)",
        name="proxy_auth_service",
    )
    async def proxy_auth_service(
        request: Request,
        path: str,
        client: Annotated[httpx.AsyncClient, Depends(get_client)],
        # NO hay dependencia StrictAuth o InitialAuth aquí.
        # El token (si existe) se pasa tal cual al servicio de Auth.
    ):
        """
        Proxy genérico para el servicio de autenticación definido en GATEWAY_AUTH_SERVICE_URL.
        No realiza validación de token en el gateway; reenvía la petición tal cual.
        """
        base_url = settings.AUTH_SERVICE_URL.rstrip('/')
        # Asumir que el servicio de Auth espera la ruta completa incluyendo /api/v1/auth/
        target_url = f"{base_url}/api/v1/auth/{path}"
        if request.url.query: target_url += f"?{request.url.query}"

        # Llamar a _proxy_request sin user_payload (user_payload=None)
        # Los headers de autenticación originales (si los hay) se pasarán
        # dentro de los headers copiados por _proxy_request.
        return await _proxy_request(request, target_url, client, user_payload=None)
else:
     # Si AUTH_SERVICE_URL no está configurado, loguear una advertencia y opcionalmente
     # añadir una ruta que devuelva 501 Not Implemented para evitar 404s confusos.
     log.warning("Auth service proxy is not configured (GATEWAY_AUTH_SERVICE_URL not set). "
                 "Requests to /api/v1/auth/* will result in 501 Not Implemented.")
     @router.api_route(
         "/api/v1/auth/{path:path}",
         methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
         tags=["Proxy - Auth"],
         summary="Auth Proxy (Not Configured)",
         include_in_schema=False # No mostrar en la documentación Swagger/OpenAPI
     )
     async def auth_not_configured(request: Request, path: str):
         raise HTTPException(
             status_code=status.HTTP_501_NOT_IMPLEMENTED,
             detail="Authentication endpoint proxy is not configured in this gateway instance."
         )