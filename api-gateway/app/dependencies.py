# app/dependencies.py
import httpx
from fastapi import HTTPException, status, Request # <-- Import Request
import structlog
from typing import Optional

# --- REMOVE Import from app.main ---
# from app.main import proxy_http_client # REMOVED

log = structlog.get_logger("atenex_api_gateway.dependencies")

# --- Modify get_client to use request.app.state ---
def get_client(request: Request) -> httpx.AsyncClient:
    """
    Dependencia FastAPI para obtener el cliente HTTPX global desde app.state.
    Verifica que el cliente haya sido inicializado por el lifespan manager.
    """
    http_client = getattr(request.app.state, 'http_client', None)

    if http_client is None:
        # Log crítico porque esto indica un problema en el startup
        log.critical("HTTP client requested but not found in app.state.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal gateway dependency (HTTP Client State) unavailable. Service may not have initialized correctly."
        )

    if http_client.is_closed:
         log.error("HTTP client requested but found closed in app.state.")
         raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal gateway dependency (HTTP Client) is closed."
         )

    return http_client