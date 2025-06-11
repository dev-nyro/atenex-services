# ingest-service/app/services/clients/embedding_service_client.py
from typing import List, Dict, Any, Tuple, Optional
import httpx
import structlog

from app.core.config import settings
from app.services.base_client import BaseServiceClient
from app.domain.exceptions import ServiceDependencyException

log = structlog.get_logger(__name__)


class EmbeddingServiceClient(BaseServiceClient):
    def __init__(self, base_url: Optional[str] = None):
        effective_base_url = base_url or str(settings.EMBEDDING_SERVICE_URL).rstrip('/')
        parsed_url = httpx.URL(effective_base_url)
        self.service_endpoint_path = parsed_url.path or "/"
        client_base_url = f"{parsed_url.scheme}://{parsed_url.host}"
        if parsed_url.port:
            client_base_url += f":{parsed_url.port}"
            
        super().__init__(base_url=client_base_url, service_name="EmbeddingService")
        self.log = log.bind(service_client="EmbeddingServiceClient", base_url=self.base_url, endpoint_path=self.service_endpoint_path)

    async def get_embeddings_async(self, texts: List[str], text_type: str = "passage") -> Tuple[List[List[float]], Dict[str, Any]]:
        if not texts:
            self.log.warning("get_embeddings_async called with an empty list of texts.")
            return [], {"model_name": "unknown", "dimension": 0, "provider": "unknown"}

        request_payload = {"texts": texts, "text_type": text_type}
        self.log.debug(f"Requesting embeddings (async) for {len(texts)} texts", num_texts=len(texts), text_type=text_type)

        try:
            response = await self._request_async(method="POST", endpoint=self.service_endpoint_path, json_payload=request_payload)
            response_data = response.json()
            if "embeddings" not in response_data or "model_info" not in response_data:
                self.log.error("Invalid response format from Embedding Service (async)", response_data_preview=str(response_data)[:200])
                raise ServiceDependencyException(service_name=self.service_name, message="Invalid response format from Embedding Service.", original_error=str(response_data)[:200])
            
            embeddings = response_data["embeddings"]
            model_info = response_data["model_info"]
            self.log.info(f"Successfully retrieved {len(embeddings)} embeddings (async).", model_name=model_info.get("model_name"))
            return embeddings, model_info
        except httpx.HTTPStatusError as e:
            err_msg = f"Embedding Service HTTP error: {e.response.status_code}"
            self.log.error(err_msg, response_text=e.response.text)
            raise ServiceDependencyException(service_name=self.service_name, message=err_msg, original_error=e.response.text) from e
        except Exception as e:
            self.log.exception("Unexpected error in EmbeddingServiceClient (async)")
            raise ServiceDependencyException(service_name=self.service_name, message=f"Unexpected error: {type(e).__name__}", original_error=str(e)) from e

    def get_embeddings_sync(self, texts: List[str], text_type: str = "passage") -> Tuple[List[List[float]], Dict[str, Any]]:
        if not texts:
            self.log.warning("get_embeddings_sync called with an empty list of texts.")
            return [], {"model_name": "unknown", "dimension": 0, "provider": "unknown"}

        request_payload = {"texts": texts, "text_type": text_type}
        self.log.debug(f"Requesting embeddings (sync) for {len(texts)} texts", num_texts=len(texts), text_type=text_type)

        try:
            response = self._request_sync(method="POST", endpoint=self.service_endpoint_path, json_payload=request_payload)
            response_data = response.json()
            if "embeddings" not in response_data or "model_info" not in response_data:
                self.log.error("Invalid response format from Embedding Service (sync)", response_data_preview=str(response_data)[:200])
                raise ServiceDependencyException(service_name=self.service_name, message="Invalid response format from Embedding Service.", original_error=str(response_data)[:200])

            embeddings = response_data["embeddings"]
            model_info = response_data["model_info"]
            self.log.info(f"Successfully retrieved {len(embeddings)} embeddings (sync).", model_name=model_info.get("model_name"))
            return embeddings, model_info
        except httpx.HTTPStatusError as e:
            err_msg = f"Embedding Service HTTP error: {e.response.status_code}"
            self.log.error(err_msg, response_text=e.response.text)
            raise ServiceDependencyException(service_name=self.service_name, message=err_msg, original_error=e.response.text) from e
        except Exception as e:
            self.log.exception("Unexpected error in EmbeddingServiceClient (sync)")
            raise ServiceDependencyException(service_name=self.service_name, message=f"Unexpected error: {type(e).__name__}", original_error=str(e)) from e