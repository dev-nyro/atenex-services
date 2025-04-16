# app/dependencies.py
import httpx
from fastapi import HTTPException, status
import structlog
from typing import Optional

# --- Importar la variable GLOBAL definida en main.py ---
# Esto es seguro porque 'main.py' habrá sido cargado por Gunicorn/Uvicorn
# antes de que esta dependencia sea necesitada por una request.
from app.main import proxy_http_client

log = structlog.get_logger("atenex_api_gateway.dependencies")

def get_client() -> httpx.AsyncClient:
    """
    Dependencia FastAPI para obtener el cliente HTTPX global.
    Verifica que el cliente haya sido inicializado por el lifespan manager.
    """
    if proxy_http_client is None or proxy_http_client.is_closed:
        # Log crítico porque esto indica un problema en el startup
        log.critical("API Gateway HTTP client requested but is not available or closed.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal gateway dependency (HTTP Client) unavailable. Service may not have started correctly."
        )
    return proxy_http_client