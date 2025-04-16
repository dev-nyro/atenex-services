# app/dependencies.py
import httpx
from fastapi import HTTPException, status
import structlog
from typing import Optional

# Importar el cliente global desde main
from app.main import proxy_http_client

log = structlog.get_logger("atenex_api_gateway.dependencies")

def get_client() -> httpx.AsyncClient:
    """Devuelve el cliente HTTPX global inicializado en lifespan."""
    if proxy_http_client is None or proxy_http_client.is_closed:
        log.error("API Gateway HTTP client is not available or closed.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal gateway dependency (HTTP Client) unavailable."
        )
    return proxy_http_client
