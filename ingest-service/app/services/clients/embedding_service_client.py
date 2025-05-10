# ingest-service/app/services/clients/embedding_service_client.py
from typing import List, Dict, Any, Tuple
import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log
import logging # Required for before_sleep_log

from app.core.config import settings
from app.services.base_client import BaseServiceClient # Reutilizar BaseServiceClient si es adecuado

log = structlog.get_logger(__name__)

class EmbeddingServiceClientError(Exception):
    """Custom exception for Embedding Service client errors."""
    def __init__(self, message: str, status_code: int = None, detail: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail

class EmbeddingServiceClient(BaseServiceClient):
    """
    Client for interacting with the Atenex Embedding Service.
    """
    def __init__(self, base_url: str = None):
        effective_base_url = base_url or str(settings.INGEST_EMBEDDING_SERVICE_URL).rstrip('/')
        # The BaseServiceClient expects the full base URL up to, but not including, the specific endpoint path.
        # So, if INGEST_EMBEDDING_SERVICE_URL is "http://host/api/v1/embed",
        # base_url for BaseServiceClient should be "http://host"
        # and the endpoint path used in _request would be "/api/v1/embed".
        # However, INGEST_EMBEDDING_SERVICE_URL already points to the specific endpoint.
        # For now, let's adapt by making the `endpoint` parameter in `get_embeddings` an empty string.
        # A more robust BaseServiceClient might take the full path in _request.

        # Let's parse the URL to get the base path for BaseServiceClient
        parsed_url = httpx.URL(effective_base_url)
        self.service_endpoint_path = parsed_url.path # e.g., /api/v1/embed
        client_base_url = f"{parsed_url.scheme}://{parsed_url.host}:{parsed_url.port}" if parsed_url.port else f"{parsed_url.scheme}://{parsed_url.host}"

        super().__init__(base_url=client_base_url, service_name="EmbeddingService")
        self.log = log.bind(service_client="EmbeddingServiceClient", service_url=effective_base_url)

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
        before_sleep=before_sleep_log(log, logging.WARNING) # Use log from structlog
    )
    async def get_embeddings(self, texts: List[str]) -> Tuple[List[List[float]], Dict[str, Any]]:
        """
        Sends a list of texts to the Embedding Service and returns their embeddings.

        Args:
            texts: A list of strings to embed.

        Returns:
            A tuple containing:
                - A list of embeddings (list of lists of floats).
                - ModelInfo dictionary.

        Raises:
            EmbeddingServiceClientError: If the request fails or the service returns an error.
        """
        if not texts:
            self.log.warning("get_embeddings called with an empty list of texts.")
            return [], {"model_name": "unknown", "dimension": 0}

        request_payload = {"texts": texts}
        self.log.debug(f"Requesting embeddings for {len(texts)} texts from {self.service_endpoint_path}", num_texts=len(texts))

        try:
            response = await self._request(
                method="POST",
                endpoint=self.service_endpoint_path, # Use the parsed endpoint path
                json=request_payload
            )
            response_data = response.json()

            if "embeddings" not in response_data or "model_info" not in response_data:
                self.log.error("Invalid response format from Embedding Service", response_data=response_data)
                raise EmbeddingServiceClientError(
                    message="Invalid response format from Embedding Service.",
                    status_code=response.status_code,
                    detail=response_data
                )

            embeddings = response_data["embeddings"]
            model_info = response_data["model_info"]
            self.log.info(f"Successfully retrieved {len(embeddings)} embeddings.", model_name=model_info.get("model_name"))
            return embeddings, model_info

        except httpx.HTTPStatusError as e:
            self.log.error(
                "HTTP error from Embedding Service",
                status_code=e.response.status_code,
                response_text=e.response.text
            )
            error_detail = e.response.text
            try: error_detail = e.response.json()
            except: pass
            raise EmbeddingServiceClientError(
                message=f"Embedding Service returned error: {e.response.status_code}",
                status_code=e.response.status_code,
                detail=error_detail
            ) from e
        except httpx.RequestError as e:
            self.log.error("Request error calling Embedding Service", error=str(e))
            raise EmbeddingServiceClientError(
                message=f"Request to Embedding Service failed: {type(e).__name__}",
                detail=str(e)
            ) from e
        except Exception as e:
            self.log.exception("Unexpected error in EmbeddingServiceClient")
            raise EmbeddingServiceClientError(message=f"Unexpected error: {e}") from e

    async def health_check(self) -> bool:
        """
        Checks the health of the Embedding Service.
        Assumes the embedding service has a /health endpoint at its root.
        """
        health_log = self.log.bind(action="health_check")
        try:
            # BaseServiceClient's _request uses the base_url.
            # The health endpoint is typically at the root of the service, not under /api/v1/embed
            # We need to construct the health URL carefully.
            # If embedding_service_url is "http://host:port/api/v1/embed", health is "http://host:port/health"
            
            parsed_service_url = httpx.URL(str(settings.INGEST_EMBEDDING_SERVICE_URL)) # Full URL to embed endpoint
            health_endpoint_url_base = f"{parsed_service_url.scheme}://{parsed_service_url.host}"
            if parsed_service_url.port:
                health_endpoint_url_base += f":{parsed_service_url.port}"
            
            async with httpx.AsyncClient(base_url=health_endpoint_url_base, timeout=5) as client:
                response = await client.get("/health")
            response.raise_for_status()
            health_data = response.json()
            if health_data.get("status") == "ok" and health_data.get("model_status") == "loaded":
                health_log.info("Embedding Service is healthy and model is loaded.")
                return True
            else:
                health_log.warning("Embedding Service reported unhealthy or model not loaded.", health_data=health_data)
                return False
        except httpx.HTTPStatusError as e:
            health_log.error("Embedding Service health check failed (HTTP error)", status_code=e.response.status_code, response_text=e.response.text)
            return False
        except httpx.RequestError as e:
            health_log.error("Embedding Service health check failed (Request error)", error=str(e))
            return False
        except Exception as e:
            health_log.exception("Unexpected error during Embedding Service health check")
            return False