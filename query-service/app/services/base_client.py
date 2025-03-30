# ./app/services/base_client.py
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import structlog
from typing import Any, Dict, Optional

# Asegurarnos de importar Settings desde la ubicación correcta
from app.core.config import settings

log = structlog.get_logger(__name__)

class BaseServiceClient:
    """Cliente HTTP base asíncrono con reintentos."""

    def __init__(self, base_url: str, service_name: str):
        self.base_url = base_url
        self.service_name = service_name
        # Usar httpx.AsyncClient para operaciones asíncronas
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=settings.HTTP_CLIENT_TIMEOUT
        )
        log.debug(f"{self.service_name} client initialized", base_url=base_url)

    async def close(self):
        """Cierra el cliente HTTP asíncrono."""
        await self.client.aclose()
        log.info(f"{self.service_name} client closed.")

    # Decorador de reintentos usando tenacity
    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES + 1), # +1 porque el primer intento cuenta
        wait=wait_exponential(multiplier=1, min=settings.HTTP_CLIENT_BACKOFF_FACTOR, max=10),
        # Reintentar en errores de red, timeout, y errores 5xx del servidor
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError)) \
              | (lambda e: isinstance(e, httpx.HTTPStatusError) and e.response.status_code >= 500),
        reraise=True, # Vuelve a lanzar la excepción después de los reintentos si todos fallan
        before_sleep=lambda retry_state: log.warning(
            f"Retrying {self.service_name} request",
            method=retry_state.args[1] if len(retry_state.args) > 1 else 'N/A', # Intentar obtener método
            endpoint=retry_state.args[2] if len(retry_state.args) > 2 else 'N/A', # Intentar obtener endpoint
            attempt=retry_state.attempt_number,
            wait_time=f"{retry_state.next_action.sleep:.2f}s",
            error=str(retry_state.outcome.exception()) # Mostrar el error
        )
    )
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None, # Renombrado para claridad
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        """Realiza una petición HTTP asíncrona con reintentos."""
        request_log = log.bind(service=self.service_name, method=method, endpoint=endpoint)
        request_log.debug("Sending request...")
        try:
            response = await self.client.request(
                method,
                endpoint,
                params=params,
                json=json_data, # Usar el parámetro renombrado
                data=data,
                files=files,
                headers=headers
            )
            # Lanzar excepción para códigos de error HTTP (4xx, 5xx)
            response.raise_for_status()
            request_log.debug("Request successful", status_code=response.status_code)
            return response
        except httpx.HTTPStatusError as e:
            # Loguear error pero permitir que Tenacity decida si reintentar (para 5xx) o fallar (para 4xx)
            request_log.error(
                "Request failed with HTTP status code",
                status_code=e.response.status_code,
                response_text=e.response.text[:500], # Limitar longitud del texto de respuesta
                exc_info=False # No es necesario el traceback completo para errores HTTP esperados
            )
            raise # Re-lanzar para que Tenacity la maneje
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            request_log.error(
                "Request failed due to network/timeout issue",
                error_type=type(e).__name__,
                error_details=str(e),
                exc_info=False # Traceback puede ser útil aquí, pero puede ser verboso
            )
            raise # Re-lanzar para que Tenacity la maneje
        except Exception as e:
             # Capturar cualquier otra excepción inesperada
             request_log.exception("An unexpected error occurred during request")
             raise # Re-lanzar la excepción inesperada