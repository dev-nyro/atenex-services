# api-gateway/app/routers/gateway_router.py
from fastapi import APIRouter, Request, Response, Depends, HTTPException, status, Path
from fastapi.responses import JSONResponse, StreamingResponse
from typing import Optional, Annotated, Dict, Any, List, Union
import httpx
import structlog
import asyncio
import uuid
import json

from app.core.config import settings
# Usar las dependencias de autenticación directamente donde se necesiten
from app.auth.auth_middleware import StrictAuth

log = structlog.get_logger("atenex_api_gateway.router.gateway")

# Instancia del Router
router = APIRouter(prefix="/api/v1", tags=["Gateway Proxy (Explicit Calls)"]) # Cambiar tags

# --- Helper para construir headers comunes ---
def _prepare_forwarded_headers(request: Request, user_payload: Dict[str, Any]) -> Dict[str, str]:
    """Prepara los headers para reenviar, incluyendo X-User-ID y X-Company-ID."""
    headers_to_forward = {}
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))

    # Headers de contexto de proxy estándar
    client_host = request.client.host if request.client else "unknown"
    x_forwarded_for = request.headers.get("x-forwarded-for", client_host)
    headers_to_forward["X-Forwarded-For"] = x_forwarded_for
    headers_to_forward["X-Forwarded-Proto"] = request.url.scheme
    if "host" in request.headers:
        headers_to_forward["X-Forwarded-Host"] = request.headers["host"]
    headers_to_forward["X-Request-ID"] = request_id

    # Añadir Headers de Autenticación/Contexto
    user_id = user_payload.get('sub')
    company_id = user_payload.get('company_id')
    user_email = user_payload.get('email')

    if not user_id or not company_id:
        log.critical("Gateway Internal Error: User payload missing sub or company_id after auth!", payload_keys=list(user_payload.keys()))
        raise HTTPException(status_code=500, detail="Internal context error")

    headers_to_forward['X-User-ID'] = str(user_id)
    headers_to_forward['X-Company-ID'] = str(company_id)
    if user_email:
        headers_to_forward['X-User-Email'] = str(user_email)

    # Añadir Accept header si existe
    if "accept" in request.headers:
        headers_to_forward["Accept"] = request.headers["accept"]

    # **NO** añadir Content-Type aquí por defecto, se maneja por endpoint
    return headers_to_forward

# --- Helper para manejar la respuesta del backend ---
async def _handle_backend_response(backend_response: httpx.Response, request_id: str) -> Response:
    """Procesa la respuesta del backend y la convierte en una respuesta FastAPI."""
    response_log = log.bind(request_id=request_id, backend_status=backend_response.status_code)

    content_type = backend_response.headers.get("content-type", "application/octet-stream")
    response_log.debug("Handling backend response", content_type=content_type)

    # Preparar headers de respuesta
    response_headers = {
        name: value for name, value in backend_response.headers.items()
        # Excluir hop-by-hop headers de la respuesta del backend
        if name.lower() not in [
            "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
            "te", "trailers", "transfer-encoding", "upgrade", "content-encoding" # Excluir content-length aquí también
        ]
    }
    response_headers["X-Request-ID"] = request_id # Propagar Request ID

    # Leer contenido - NO USAR STREAMINGRESPONSE a menos que sea necesario y se maneje
    try:
        # Leer el cuerpo completo. Ideal para JSON/texto/binarios pequeños.
        content_bytes = await backend_response.aread()
        # Actualizar content-length basado en lo leído
        response_headers["Content-Length"] = str(len(content_bytes))
        response_log.debug("Backend response body read", body_length=len(content_bytes))

    except Exception as read_err:
        response_log.error("Failed to read backend response body", error=str(read_err))
        # Devolver error genérico si no podemos leer la respuesta
        return JSONResponse(
             content={"detail": "Error reading response from upstream service"},
             status_code=status.HTTP_502_BAD_GATEWAY,
             headers={"X-Request-ID": request_id}
        )
    finally:
        # Siempre cerrar la respuesta del backend
        await backend_response.aclose()


    return Response(
        content=content_bytes,
        status_code=backend_response.status_code,
        headers=response_headers,
        media_type=content_type
    )

# --- Helper para manejar errores HTTPX ---
def _handle_httpx_error(exc: Exception, target_url: str, request_id: str):
    error_log = log.bind(request_id=request_id, target_url=target_url)
    if isinstance(exc, httpx.TimeoutException):
        error_log.error("Gateway timeout calling backend service", exc_info=False)
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Upstream service timed out.")
    elif isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        error_log.error("Gateway connection error calling backend service", error=str(exc), exc_info=False)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not connect to upstream service.")
    elif isinstance(exc, httpx.HTTPStatusError): # Errores 4xx/5xx devueltos por el backend
        error_log.warning("Backend service returned error status", status_code=exc.response.status_code, response_preview=exc.response.text[:200])
        # Reintentar devolver la respuesta original si es posible
        try:
            content = exc.response.json()
        except json.JSONDecodeError:
            content = {"detail": exc.response.text or f"Backend returned status {exc.response.status_code}"}
        raise HTTPException(status_code=exc.response.status_code, detail=content)
    else: # Otros errores httpx o inesperados
        error_log.exception("Unexpected error during backend call")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="An unexpected error occurred communicating with upstream service.")

# --- Endpoints Query Service (Llamadas Explícitas) ---

@router.post("/query/ask", summary="Ask a question (Query Service)")
async def query_service_ask(
    request: Request, # Inject request
    user_payload: StrictAuth,
    # client: HttpClientDep, # REMOVE Dependency injection here
):
    # --- Get client from app state ---
    client = getattr(request.app.state, 'http_client', None)
    if not client or client.is_closed:
         log.error("HTTP client unavailable in query_service_ask endpoint.")
         raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Gateway dependency unavailable.")
    # --------------------------------

    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    endpoint_log = log.bind(request_id=request_id)
    endpoint_log.info("Forwarding request to Query Service POST /ask")

    target_url = f"{settings.QUERY_SERVICE_URL}{request.url.path}"
    headers = _prepare_forwarded_headers(request, user_payload)

    try:
        request_body = await request.json()
        headers["Content-Type"] = "application/json"
    except json.JSONDecodeError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON body")

    try:
        backend_response = await client.post(target_url, headers=headers, json=request_body) # Use the retrieved client
        return await _handle_backend_response(backend_response, request_id)
    except Exception as exc:
        _handle_httpx_error(exc, target_url, request_id)

@router.get("/query/chats", summary="List chats (Query Service)")
async def query_service_get_chats(
    request: Request, # Inject request
    user_payload: StrictAuth,
    # client: HttpClientDep, # REMOVE Dependency injection here
):
    # --- Get client from app state ---
    client = getattr(request.app.state, 'http_client', None)
    if not client or client.is_closed: raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Gateway dependency unavailable.")
    # --------------------------------

    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    target_url = f"{settings.QUERY_SERVICE_URL}{request.url.path}"
    headers = _prepare_forwarded_headers(request, user_payload)
    params = request.query_params
    try:
        backend_response = await client.get(target_url, headers=headers, params=params) # Use the retrieved client
        return await _handle_backend_response(backend_response, request_id)
    except Exception as exc:
        _handle_httpx_error(exc, target_url, request_id)

@router.get("/query/chats/{chat_id}/messages", summary="Get chat messages (Query Service)")
async def query_service_get_chat_messages(
    request: Request, # Inject request
    user_payload: StrictAuth,
    # client: HttpClientDep, # REMOVE Dependency injection here
    chat_id: uuid.UUID = Path(...),
):
    # --- Get client from app state ---
    client = getattr(request.app.state, 'http_client', None)
    if not client or client.is_closed: raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Gateway dependency unavailable.")
    # --------------------------------

    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    target_url = f"{settings.QUERY_SERVICE_URL}{request.url.path}"
    headers = _prepare_forwarded_headers(request, user_payload)
    params = request.query_params
    try:
        backend_response = await client.get(target_url, headers=headers, params=params) # Use the retrieved client
        return await _handle_backend_response(backend_response, request_id)
    except Exception as exc:
        _handle_httpx_error(exc, target_url, request_id)


@router.delete("/query/chats/{chat_id}", summary="Delete chat (Query Service)")
async def query_service_delete_chat(
    request: Request, # Inject request
    user_payload: StrictAuth,
    # client: HttpClientDep, # REMOVE Dependency injection here
    chat_id: uuid.UUID = Path(...),
):
    # --- Get client from app state ---
    client = getattr(request.app.state, 'http_client', None)
    if not client or client.is_closed: raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Gateway dependency unavailable.")
    # --------------------------------

    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    target_url = f"{settings.QUERY_SERVICE_URL}{request.url.path}"
    headers = _prepare_forwarded_headers(request, user_payload)
    try:
        backend_response = await client.delete(target_url, headers=headers) # Use the retrieved client
        if backend_response.status_code == status.HTTP_204_NO_CONTENT:
            await backend_response.aclose()
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        else: return await _handle_backend_response(backend_response, request_id)
    except Exception as exc:
        _handle_httpx_error(exc, target_url, request_id)


# --- Endpoints Ingest Service (Get client from request.app.state) ---

@router.post("/ingest/upload", summary="Upload document (Ingest Service)")
async def ingest_service_upload(
    request: Request, # Inject request
    user_payload: StrictAuth,
    # client: HttpClientDep, # REMOVE Dependency injection here
):
    # --- Get client from app state ---
    client = getattr(request.app.state, 'http_client', None)
    if not client or client.is_closed: raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Gateway dependency unavailable.")
    # --------------------------------

    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    target_url = f"{settings.INGEST_SERVICE_URL}{request.url.path}"
    headers = _prepare_forwarded_headers(request, user_payload)
    content_type = request.headers.get('content-type')
    if not content_type or 'multipart/form-data' not in content_type:
         raise HTTPException(status.HTTP_400_BAD_REQUEST, "Content-Type multipart/form-data required.")
    headers['Content-Type'] = content_type
    headers.pop('transfer-encoding', None)
    try: body_bytes = await request.body(); headers['Content-Length'] = str(len(body_bytes))
    except Exception as read_err: raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Could not read upload body: {read_err}")
    try:
        backend_response = await client.post(target_url, headers=headers, content=body_bytes) # Use the retrieved client
        return await _handle_backend_response(backend_response, request_id)
    except Exception as exc:
        _handle_httpx_error(exc, target_url, request_id)


@router.get("/ingest/status/{document_id}", summary="Get document status (Ingest Service)")
async def ingest_service_get_status(
    request: Request, # Inject request
    user_payload: StrictAuth,
    # client: HttpClientDep, # REMOVE Dependency injection here
    document_id: uuid.UUID = Path(...),
):
    # --- Get client from app state ---
    client = getattr(request.app.state, 'http_client', None)
    if not client or client.is_closed: raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Gateway dependency unavailable.")
    # --------------------------------

    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    target_url = f"{settings.INGEST_SERVICE_URL}{request.url.path}"
    headers = _prepare_forwarded_headers(request, user_payload)
    try:
        backend_response = await client.get(target_url, headers=headers) # Use the retrieved client
        return await _handle_backend_response(backend_response, request_id)
    except Exception as exc:
        _handle_httpx_error(exc, target_url, request_id)


@router.get("/ingest/status", summary="List document statuses (Ingest Service)")
async def ingest_service_list_statuses(
    request: Request, # Inject request
    user_payload: StrictAuth,
    # client: HttpClientDep, # REMOVE Dependency injection here
):
    # --- Get client from app state ---
    client = getattr(request.app.state, 'http_client', None)
    if not client or client.is_closed: raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Gateway dependency unavailable.")
    # --------------------------------

    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    target_url = f"{settings.INGEST_SERVICE_URL}{request.url.path}"
    headers = _prepare_forwarded_headers(request, user_payload)
    params = request.query_params
    try:
        backend_response = await client.get(target_url, headers=headers, params=params) # Use the retrieved client
        return await _handle_backend_response(backend_response, request_id)
    except Exception as exc:
        _handle_httpx_error(exc, target_url, request_id)