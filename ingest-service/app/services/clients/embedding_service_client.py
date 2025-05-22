# ingest-service/app/services/clients/embedding_service_client.py
from typing import List, Dict, Any, Tuple
import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log
import logging # Required for before_sleep_log

from app.core.config import settings
from app.services.base_client import BaseServiceClient 

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
        effective_base_url = base_url or str(settings.EMBEDDING_SERVICE_URL).rstrip('/')
        
        parsed_url = httpx.URL(effective_base_url)
        self.service_endpoint_path = parsed_url.path 
        client_base_url = f"{parsed_url.scheme}://{parsed_url.host}:{parsed_url.port}" if parsed_url.port else f"{parsed_url.scheme}://{parsed_url.host}"

        super().__init__(base_url=client_base_url, service_name="EmbeddingService")
        self.log = log.bind(service_client="EmbeddingServiceClient", service_url=effective_base_url)

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
        before_sleep=before_sleep_log(log, logging.WARNING) 
    )
    async def get_embeddings(self, texts: List[str], text_type: str = "passage") -> Tuple[List[List[float]], Dict[str, Any]]:
        """
        Sends a list of texts to the Embedding Service and returns their embeddings.

        Args:
            texts: A list of strings to embed.
            text_type: The type of text, e.g., "passage" or "query". Defaults to "passage".

        Returns:
            A tuple containing:
                - A list of embeddings (list of lists of floats).
                - ModelInfo dictionary.

        Raises:
            EmbeddingServiceClientError: If the request fails or the service returns an error.
        """
        if not texts:
            self.log.warning("get_embeddings called with an empty list of texts.")
            return [], {"model_name": "unknown", "dimension": 0, "provider": "unknown"}

        request_payload = {
            "texts": texts,
            "text_type": text_type 
        }
        self.log.debug(f"Requesting embeddings for {len(texts)} texts from {self.service_endpoint_path}", num_texts=len(texts), text_type=text_type)

        try:
            response = await self._request(
                method="POST",
                endpoint=self.service_endpoint_path, 
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
            self.log.info(f"Successfully retrieved {len(embeddings)} embeddings.", model_name=model_info.get("model_name"), provider=model_info.get("provider"))
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
        health_log = self.log.bind(action="health_check")
        try:
            parsed_service_url = httpx.URL(str(settings.EMBEDDING_SERVICE_URL)) 
            health_endpoint_url_base = f"{parsed_service_url.scheme}://{parsed_service_url.host}"
            if parsed_service_url.port:
                health_endpoint_url_base += f":{parsed_service_url.port}"
            
            async with httpx.AsyncClient(base_url=health_endpoint_url_base, timeout=5) as client:
                response = await client.get("/health")
            response.raise_for_status()
            health_data = response.json()
            
            model_is_ready = health_data.get("model_status") in ["client_ready", "loaded"] 
            if health_data.get("status") == "ok" and model_is_ready:
                health_log.info("Embedding Service is healthy and model is loaded/ready.", health_data=health_data)
                return True
            else:
                health_log.warning("Embedding Service reported unhealthy or model not ready.", health_data=health_data)
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