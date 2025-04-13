# query-service/app/services/base_client.py
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, retry_if_exception
import structlog
from typing import Any, Dict, Optional

from app.core.config import settings

log = structlog.get_logger(__name__)

# Define qué errores HTTP son recuperables (Server errors)
RETRYABLE_HTTP_STATUS = (500, 502, 503, 504)

class BaseServiceClient:
    """Cliente HTTP base asíncrono con reintentos configurables."""

    def __init__(self, base_url: str, service_name: str):
        self.base_url = base_url
        self.service_name = service_name
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=settings.HTTP_CLIENT_TIMEOUT)
        log.debug(f"{self.service_name} client initialized", base_url=base_url)

    async def close(self):
        await self.client.aclose()
        log.info(f"{self.service_name} client closed.")

    # Decorador de reintentos Tenacity
    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES + 1), # Total attempts = initial + retries
        wait=wait_exponential(multiplier=1, min=settings.HTTP_CLIENT_BACKOFF_FACTOR, max=10),
        # *** CORREGIDO: Lógica de reintento explícita ***
        retry=(
            # Reintentar en errores de red o timeout
            retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout)) |
            # Reintentar si es un error HTTP y el status code está en la lista de recuperables
            retry_if_exception(lambda e: isinstance(e, httpx.HTTPStatusError) and e.response.status_code in RETRYABLE_HTTP_STATUS)
        ),
        reraise=True, # Relanzar la última excepción si todos los reintentos fallan
        before_sleep=lambda retry_state: log.warning(
            f"Retrying {self.service_name} request",
            # Intenta obtener detalles del intento fallido
            method=getattr(retry_state.args[0], 'method', 'N/A') if retry_state.args and isinstance(retry_state.args[0], httpx.Request) else 'N/A',
            url=str(getattr(retry_state.args[0], 'url', 'N/A')) if retry_state.args and isinstance(retry_state.args[0], httpx.Request) else 'N/A',
            attempt=retry_state.attempt_number,
            wait_time=f"{retry_state.next_action.sleep:.2f}s",
            error_type=type(retry_state.outcome.exception()).__name__,
            error_details=str(retry_state.outcome.exception())
        )
    )
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None, # Renombrado
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        """Realiza una petición HTTP asíncrona con reintentos."""
        request_log = log.bind(service=self.service_name, method=method, endpoint=endpoint)
        request_log.debug("Sending request...")
        request_obj = self.client.build_request( # Construir objeto request para logging
             method, endpoint, params=params, json=json_data, data=data, files=files, headers=headers
        )
        try:
            response = await self.client.send(request_obj) # Usar send con el objeto request
            response.raise_for_status() # Lanza excepción para 4xx/5xx
            request_log.debug("Request successful", status_code=response.status_code)
            return response
        except httpx.HTTPStatusError as e:
            log_level = log.warning if e.response.status_code < 500 else log.error
            log_level(
                "Request failed with HTTP status code",
                status_code=e.response.status_code,
                response_preview=e.response.text[:200], # Preview de la respuesta
                exc_info=False
            )
            raise # Re-lanzar para que Tenacity maneje reintentos (para 5xx) o falle (para 4xx)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as net_err:
            request_log.error("Request failed due to network/timeout issue", error_type=type(net_err).__name__, error_details=str(net_err))
            raise # Re-lanzar para Tenacity
        except Exception as e:
             request_log.exception("An unexpected error occurred during request")
             raise # Re-lanzar excepción inesperada