# query-service/app/infrastructure/clients/embedding_service_client.py
import httpx
import structlog
from typing import List, Dict, Any, Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import json # FLAG_CORRECTION: Import json

from app.core.config import settings

log = structlog.get_logger(__name__)

class EmbeddingServiceClient:
    """
    Cliente HTTP para interactuar con el Atenex Embedding Service.
    """
    def __init__(self, base_url: str, timeout: int = settings.HTTP_CLIENT_TIMEOUT):
        self.base_url = base_url.rstrip('/')
        self.embed_endpoint = f"{self.base_url}/api/v1/embed" 
        self.health_endpoint = f"{self.base_url}/health"

        if "/api/v1" in self.base_url.split('/')[-2:]:
             self.embed_endpoint = f"{self.base_url}/embed"
        else: 
             self.embed_endpoint = f"{self.base_url}/api/v1/embed"


        self._client = httpx.AsyncClient(timeout=timeout)
        log.info("EmbeddingServiceClient initialized", base_url=self.base_url, embed_endpoint=self.embed_endpoint)

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES + 1),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError)),
        reraise=True 
    )
    async def generate_embeddings(self, texts: List[str]) -> List[List[float]]:
        """
        Solicita embeddings para una lista de textos al servicio de embedding.
        """
        client_log = log.bind(action="generate_embeddings", num_texts=len(texts), target_service="embedding-service")
        if not texts:
            client_log.warning("No texts provided to generate_embeddings.")
            return []

        payload = {"texts": texts}
        try:
            client_log.debug("Sending request to embedding service")
            response = await self._client.post(self.embed_endpoint, json=payload)
            response.raise_for_status() 

            data = response.json()
            if "embeddings" not in data or not isinstance(data["embeddings"], list):
                client_log.error("Invalid response format from embedding service: 'embeddings' field missing or not a list.", response_data=data)
                raise ValueError("Invalid response format from embedding service: 'embeddings' field.")

            client_log.info("Embeddings received successfully from service", num_embeddings=len(data["embeddings"]))
            return data["embeddings"]

        except httpx.HTTPStatusError as e:
            client_log.error("HTTP error from embedding service", status_code=e.response.status_code, response_body=e.response.text)
            raise ConnectionError(f"Embedding service returned error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            client_log.error("Request error while contacting embedding service", error=str(e))
            raise ConnectionError(f"Could not connect to embedding service: {e}") from e
        except json.JSONDecodeError as e_json: # FLAG_CORRECTION: Catch json.JSONDecodeError
            client_log.error("Error parsing JSON response from embedding service", error=str(e_json), raw_response=response.text if 'response' in locals() else "N/A")
            raise ValueError(f"Invalid JSON response from embedding service: {e_json}") from e_json
        except (ValueError, TypeError) as e: 
            client_log.error("Error processing response from embedding service (ValueError/TypeError)", error=str(e))
            raise ValueError(f"Invalid response data from embedding service: {e}") from e


    async def get_model_info(self) -> Optional[Dict[str, Any]]:
        client_log = log.bind(action="get_model_info_via_embed", target_service="embedding-service")
        try:
            response = await self._client.post(self.embed_endpoint, json={"texts": ["test"]}) 
            response.raise_for_status()
            data = response.json()
            if "model_info" in data and isinstance(data["model_info"], dict):
                client_log.info("Model info retrieved from embedding service", model_info=data["model_info"])
                return data["model_info"]
            client_log.warning("Model info not found in embedding service response.", response_data=data)
            return None
        except json.JSONDecodeError as e_json: # FLAG_CORRECTION: Catch json.JSONDecodeError
            client_log.error("Failed to parse JSON for get_model_info from embedding service", error=str(e_json), raw_response=response.text if 'response' in locals() else "N/A")
            return None
        except Exception as e:
            client_log.error("Failed to get model_info from embedding service via /embed", error=str(e))
            return None

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES + 1),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=5), 
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError, ConnectionError)),
        reraise=True,
        before_sleep=lambda retry_state: log.warning(
            "Retrying EmbeddingServiceClient.check_health",
            attempt=retry_state.attempt_number,
            wait_time=f"{retry_state.next_action.sleep:.2f}s", # type: ignore
            error_type=type(retry_state.outcome.exception()).__name__ if retry_state.outcome else "N/A", # type: ignore
            error_message=str(retry_state.outcome.exception()) if retry_state.outcome else "N/A" # type: ignore
        )
    )
    async def check_health(self) -> bool:
        client_log = log.bind(action="check_health_with_retry", target_service="embedding-service")
        try:
            client_log.debug("Attempting health check...")
            response = await self._client.get(self.health_endpoint)
            response.raise_for_status() 

            data = response.json()
            # FLAG_CORRECTION: Accept 'client_ready' or 'loaded' as valid model_status
            model_is_ready = data.get("model_status") in ["loaded", "client_ready"]
            if data.get("status") == "ok" and model_is_ready:
                client_log.info("Embedding service health check successful.", health_data=data)
                return True
            else:
                client_log.warning("Embedding service health check returned ok status but model not fully ready or unexpected payload.", health_data=data)
                raise ConnectionError(f"Embedding service not fully ready: status={data.get('status')}, model_status={data.get('model_status')}")
        except httpx.HTTPStatusError as e:
            client_log.warning("HTTP error during embedding service health check (will be retried or reraised).", status_code=e.response.status_code, response_text=e.response.text)
            raise ConnectionError(f"HTTP error from embedding service: {e.response.status_code}") from e 
        except httpx.RequestError as e:
            client_log.error("Request error during embedding service health check (will be retried or reraised).", error=str(e))
            raise ConnectionError(f"Request error connecting to embedding service: {e}") from e
        except json.JSONDecodeError as e_json: # FLAG_CORRECTION: Catch json.JSONDecodeError
            client_log.error("Failed to parse JSON response from embedding service health check.", error=str(e_json), raw_response=response.text if 'response' in locals() else "N/A")
            raise ConnectionError(f"Invalid JSON response from embedding service health: {e_json}") from e_json
        except Exception as e: 
            client_log.error("Unexpected error during embedding service health check (will be retried or reraised).", error=str(e))
            raise ConnectionError(f"Unexpected error during health check: {e}") from e

    async def close(self):
        await self._client.aclose()
        log.info("EmbeddingServiceClient closed.")