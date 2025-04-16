# api-gateway/app/routers/gateway_router.py
from fastapi import APIRouter, Request, Response, Depends, HTTPException, status, Path
from fastapi.responses import JSONResponse # Removed StreamingResponse as it wasn't used effectively
from typing import Optional, Annotated, Dict, Any
import httpx
import structlog
import uuid
import json # Ensure json is imported for error handling

from app.core.config import settings
from app.auth.auth_middleware import StrictAuth

log = structlog.get_logger("atenex_api_gateway.router.gateway")

# --- *** CORRECTION: Remove prefix from APIRouter definition *** ---
router = APIRouter(tags=["Gateway (Explicit Calls)"])
# --------------------------------------------------------------------

# --- Helpers (_prepare_forwarded_headers, _handle_backend_response, _handle_httpx_error) ---
# (Keep these helpers exactly as they were in the previous corrected version)
def _prepare_forwarded_headers(request: Request, user_payload: Dict[str, Any]) -> Dict[str, str]:
    headers_to_forward = {}
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    client_host = request.client.host if request.client else "unknown"
    x_forwarded_for = request.headers.get("x-forwarded-for", client_host)
    headers_to_forward["X-Forwarded-For"] = x_forwarded_for
    headers_to_forward["X-Forwarded-Proto"] = request.url.scheme
    if "host" in request.headers: headers_to_forward["X-Forwarded-Host"] = request.headers["host"]
    headers_to_forward["X-Request-ID"] = request_id
    user_id = user_payload.get('sub'); company_id = user_payload.get('company_id'); user_email = user_payload.get('email')
    if not user_id or not company_id: log.critical("GW Error: Payload missing fields post-auth!", keys=list(user_payload.keys())); raise HTTPException(500, "Internal context error")
    headers_to_forward['X-User-ID'] = str(user_id); headers_to_forward['X-Company-ID'] = str(company_id)
    if user_email: headers_to_forward['X-User-Email'] = str(user_email)
    if "accept" in request.headers: headers_to_forward["Accept"] = request.headers["accept"]
    # Content-Type handled per endpoint
    return headers_to_forward

async def _handle_backend_response(backend_response: httpx.Response, request_id: str) -> Response:
    response_log = log.bind(request_id=request_id, backend_status=backend_response.status_code)
    content_type = backend_response.headers.get("content-type", "application/octet-stream")
    response_headers = { n: v for n, v in backend_response.headers.items() if n.lower() not in ["connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "content-encoding"] }
    response_headers["X-Request-ID"] = request_id
    try:
        content_bytes = await backend_response.aread()
        response_headers["Content-Length"] = str(len(content_bytes))
        response_log.debug("Backend response read", len=len(content_bytes), ct=content_type)
    except Exception as read_err:
        response_log.error("Failed reading backend response", error=str(read_err))
        return JSONResponse(content={"detail": "Upstream response read error"}, status_code=502, headers={"X-Request-ID": request_id})
    finally: await backend_response.aclose()
    return Response(content=content_bytes, status_code=backend_response.status_code, headers=response_headers, media_type=content_type)

def _handle_httpx_error(exc: Exception, target_url: str, request_id: str):
    error_log = log.bind(request_id=request_id, target_url=target_url)
    if isinstance(exc, httpx.TimeoutException): error_log.error("GW Timeout"); raise HTTPException(504, "Upstream timeout.")
    elif isinstance(exc, (httpx.ConnectError, httpx.NetworkError)): error_log.error("GW Connection Error", e=str(exc)); raise HTTPException(503, "Upstream connection failed.")
    elif isinstance(exc, httpx.HTTPStatusError):
        error_log.warning("Backend Error Status", status=exc.response.status_code, resp=exc.response.text[:200])
        try: content = exc.response.json()
        except json.JSONDecodeError: content = {"detail": exc.response.text or f"Backend status {exc.response.status_code}"}
        raise HTTPException(status_code=exc.response.status_code, detail=content)
    else: error_log.exception("Unexpected GW Backend Call Error"); raise HTTPException(502, "Unexpected upstream communication error.")


# --- Endpoints Query Service ---
# Paths are now relative to the prefix applied in main.py ("/api/v1")

@router.post("/query/ask", summary="Ask a question (Query Service)")
async def query_service_ask(request: Request, user_payload: StrictAuth):
    client = getattr(request.app.state, 'http_client', None)
    if not client or client.is_closed: raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Gateway dependency unavailable.")
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    # Construct target URL: Base URL from settings + path defined here
    target_url = f"{settings.QUERY_SERVICE_URL}/api/v1/query/ask" # Explicit full path to backend
    headers = _prepare_forwarded_headers(request, user_payload)
    try: request_body = await request.json(); headers["Content-Type"] = "application/json"
    except json.JSONDecodeError: raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON body")
    try:
        backend_response = await client.post(target_url, headers=headers, json=request_body)
        return await _handle_backend_response(backend_response, request_id)
    except Exception as exc: _handle_httpx_error(exc, target_url, request_id)

@router.get("/query/chats", summary="List chats (Query Service)")
async def query_service_get_chats(request: Request, user_payload: StrictAuth):
    client = getattr(request.app.state, 'http_client', None)
    if not client or client.is_closed: raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Gateway dependency unavailable.")
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    target_url = f"{settings.QUERY_SERVICE_URL}/api/v1/query/chats" # Explicit full path
    headers = _prepare_forwarded_headers(request, user_payload)
    params = request.query_params # Pass original query params
    try:
        backend_response = await client.get(target_url, headers=headers, params=params)
        return await _handle_backend_response(backend_response, request_id)
    except Exception as exc: _handle_httpx_error(exc, target_url, request_id)

@router.get("/query/chats/{chat_id}/messages", summary="Get chat messages (Query Service)")
async def query_service_get_chat_messages(request: Request, user_payload: StrictAuth, chat_id: uuid.UUID = Path(...)):
    client = getattr(request.app.state, 'http_client', None)
    if not client or client.is_closed: raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Gateway dependency unavailable.")
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    # Construct backend path carefully
    backend_path = f"/api/v1/query/chats/{chat_id}/messages"
    target_url = f"{settings.QUERY_SERVICE_URL}{backend_path}"
    headers = _prepare_forwarded_headers(request, user_payload)
    params = request.query_params
    try:
        backend_response = await client.get(target_url, headers=headers, params=params)
        return await _handle_backend_response(backend_response, request_id)
    except Exception as exc: _handle_httpx_error(exc, target_url, request_id)

@router.delete("/query/chats/{chat_id}", summary="Delete chat (Query Service)")
async def query_service_delete_chat(request: Request, user_payload: StrictAuth, chat_id: uuid.UUID = Path(...)):
    client = getattr(request.app.state, 'http_client', None)
    if not client or client.is_closed: raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Gateway dependency unavailable.")
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    backend_path = f"/api/v1/query/chats/{chat_id}"
    target_url = f"{settings.QUERY_SERVICE_URL}{backend_path}"
    headers = _prepare_forwarded_headers(request, user_payload)
    try:
        backend_response = await client.delete(target_url, headers=headers)
        if backend_response.status_code == status.HTTP_204_NO_CONTENT:
            await backend_response.aclose(); return Response(status_code=status.HTTP_204_NO_CONTENT)
        else: return await _handle_backend_response(backend_response, request_id)
    except Exception as exc: _handle_httpx_error(exc, target_url, request_id)

# --- Endpoints Ingest Service ---

@router.post("/ingest/upload", summary="Upload document (Ingest Service)")
async def ingest_service_upload(request: Request, user_payload: StrictAuth):
    client = getattr(request.app.state, 'http_client', None)
    if not client or client.is_closed: raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Gateway dependency unavailable.")
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    target_url = f"{settings.INGEST_SERVICE_URL}/api/v1/ingest/upload" # Explicit backend path
    headers = _prepare_forwarded_headers(request, user_payload)
    content_type = request.headers.get('content-type')
    if not content_type or 'multipart/form-data' not in content_type: raise HTTPException(status.HTTP_400_BAD_REQUEST, "Multipart Content-Type required.")
    headers['Content-Type'] = content_type; headers.pop('transfer-encoding', None)
    try: body_bytes = await request.body(); headers['Content-Length'] = str(len(body_bytes))
    except Exception as read_err: raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Could not read upload body: {read_err}")
    try:
        backend_response = await client.post(target_url, headers=headers, content=body_bytes)
        return await _handle_backend_response(backend_response, request_id)
    except Exception as exc: _handle_httpx_error(exc, target_url, request_id)

@router.get("/ingest/status/{document_id}", summary="Get document status (Ingest Service)")
async def ingest_service_get_status(request: Request, user_payload: StrictAuth, document_id: uuid.UUID = Path(...)):
    client = getattr(request.app.state, 'http_client', None)
    if not client or client.is_closed: raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Gateway dependency unavailable.")
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    backend_path = f"/api/v1/ingest/status/{document_id}"
    target_url = f"{settings.INGEST_SERVICE_URL}{backend_path}"
    headers = _prepare_forwarded_headers(request, user_payload)
    try:
        backend_response = await client.get(target_url, headers=headers)
        return await _handle_backend_response(backend_response, request_id)
    except Exception as exc: _handle_httpx_error(exc, target_url, request_id)

@router.get("/ingest/status", summary="List document statuses (Ingest Service)")
async def ingest_service_list_statuses(request: Request, user_payload: StrictAuth):
    client = getattr(request.app.state, 'http_client', None)
    if not client or client.is_closed: raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Gateway dependency unavailable.")
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    target_url = f"{settings.INGEST_SERVICE_URL}/api/v1/ingest/status" # Explicit backend path
    headers = _prepare_forwarded_headers(request, user_payload)
    params = request.query_params # Pass original query params
    try:
        backend_response = await client.get(target_url, headers=headers, params=params)
        return await _handle_backend_response(backend_response, request_id)
    except Exception as exc: _handle_httpx_error(exc, target_url, request_id)