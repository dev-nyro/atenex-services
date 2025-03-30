import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import structlog
from typing import Any, Dict, Optional

from app.core.config import settings

log = structlog.get_logger(__name__)

class BaseServiceClient:
    """Cliente HTTP base asíncrono con reintentos."""

    def __init__(self, base_url: str, service_name: str):
        self.base_url = base_url
        self.service_name = service_name
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=settings.HTTP_CLIENT_TIMEOUT
        )

    async def close(self):
        """Cierra el cliente HTTP."""
        await self.client.aclose()
        log.info(f"{self.service_name} client closed.")

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES),
        wait=wait_exponential(multiplier=1, min=settings.HTTP_CLIENT_BACKOFF_FACTOR, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError)),
        reraise=True, # Vuelve a lanzar la excepción después de los reintentos
        before_sleep=lambda retry_state: log.warning(
            f"Retrying {retry_state.fn.__name__} for {self.service_name}",
            attempt=retry_state.attempt_number,
            wait_time=retry_state.next_action.sleep,
            error=retry_state.outcome.exception()
        )
    )
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        """Realiza una petición HTTP con reintentos."""
        log.debug(f"Requesting {self.service_name}", method=method, endpoint=endpoint, params=params, json_keys=list(json.keys()) if json else None)
        try:
            response = await self.client.request(
                method,
                endpoint,
                params=params,
                json=json,
                data=data,
                files=files,
                headers=headers
            )
            response.raise_for_status() # Lanza HTTPStatusError para 4xx/5xx
            log.debug(f"{self.service_name} request successful", method=method, endpoint=endpoint, status_code=response.status_code)
            return response
        except httpx.HTTPStatusError as e:
            log.error(
                f"{self.service_name} request failed with status code",
                method=method, endpoint=endpoint, status_code=e.response.status_code, response_text=e.response.text,
                exc_info=False # No mostrar traceback completo para errores HTTP esperados
            )
            raise # Re-lanzar para que tenacity lo capture si es necesario
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            log.error(
                f"{self.service_name} request failed due to network/timeout issue",
                method=method, endpoint=endpoint, error=type(e).__name__,
                exc_info=True
            )
            raise # Re-lanzar para que tenacity lo capture
        except Exception as e:
             log.error(
                f"An unexpected error occurred during {self.service_name} request",
                method=method, endpoint=endpoint, error=e,
                exc_info=True
            )
             raise