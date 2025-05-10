# query-service/app/infrastructure/clients/embedding_service_client.py
import httpx
import structlog
from typing import List, Dict, Any, Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.core.config import settings

log = structlog.get_logger(__name__)

class EmbeddingServiceClient:
    """
    Cliente HTTP para interactuar con el Atenex Embedding Service.
    """
    def __init__(self, base_url: str, timeout: int = settings.HTTP_CLIENT_TIMEOUT):
        self.base_url = base_url.rstrip('/')
        self.embed_endpoint = f"{self.base_url}/api/v1/embed" # Asumiendo que la URL base ya tiene /api/v1
        self.health_endpoint = f"{self.base_url}/health"

        # Ajuste para no incluir /api/v1 dos veces si ya está en base_url
        if "/api/v1" in self.base_url.split('/')[-2:]: # si base_url es .../api/v1
             self.embed_endpoint = f"{self.base_url}/embed"
        else: # si base_url es ...embedding-service:8003
             self.embed_endpoint = f"{self.base_url}/api/v1/embed"


        self._client = httpx.AsyncClient(timeout=timeout)
        log.info("EmbeddingServiceClient initialized", base_url=self.base_url, embed_endpoint=self.embed_endpoint)

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES + 1),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError)),
        reraise=True # Re-raise la excepción original después de los reintentos
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
            response.raise_for_status() # Lanza HTTPStatusError para 4xx/5xx

            data = response.json()
            if "embeddings" not in data or not isinstance(data["embeddings"], list):
                client_log.error("Invalid response format from embedding service: 'embeddings' field missing or not a list.", response_data=data)
                raise ValueError("Invalid response format from embedding service: 'embeddings' field.")

            # Opcional: Validar que cada embedding sea una lista de floats y tenga la dimensión esperada
            # Esto se podría hacer en el adaptador.
            client_log.info("Embeddings received successfully from service", num_embeddings=len(data["embeddings"]))
            return data["embeddings"]

        except httpx.HTTPStatusError as e:
            client_log.error("HTTP error from embedding service", status_code=e.response.status_code, response_body=e.response.text)
            raise ConnectionError(f"Embedding service returned error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            client_log.error("Request error while contacting embedding service", error=str(e))
            raise ConnectionError(f"Could not connect to embedding service: {e}") from e
        except (ValueError, TypeError) as e: # Errores de parsing JSON o validación
            client_log.error("Error processing response from embedding service", error=str(e))
            raise ValueError(f"Invalid response from embedding service: {e}") from e


    async def get_model_info(self) -> Optional[Dict[str, Any]]:
        """
        Intenta obtener información del modelo desde la respuesta del endpoint /embed.
        Nota: El embedding-service actual no tiene un endpoint /info,
        pero el /embed response incluye model_info. Esta función es una forma
        de inferirlo si se necesita, aunque es mejor tener un health check más completo.
        """
        client_log = log.bind(action="get_model_info_via_embed", target_service="embedding-service")
        try:
            # Enviamos un texto de prueba para obtener la respuesta que incluye model_info
            response = await self._client.post(self.embed_endpoint, json={"texts": ["test"]})
            response.raise_for_status()
            data = response.json()
            if "model_info" in data and isinstance(data["model_info"], dict):
                client_log.info("Model info retrieved from embedding service", model_info=data["model_info"])
                return data["model_info"]
            client_log.warning("Model info not found in embedding service response.", response_data=data)
            return None
        except Exception as e:
            client_log.error("Failed to get model_info from embedding service via /embed", error=str(e))
            return None

    async def check_health(self) -> bool:
        """
        Verifica la salud del embedding service llamando a su endpoint /health.
        """
        client_log = log.bind(action="check_health", target_service="embedding-service")
        try:
            response = await self._client.get(self.health_endpoint)
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "ok" and data.get("model_status") == "loaded":
                    client_log.info("Embedding service health check successful.", health_data=data)
                    return True
                else:
                    client_log.warning("Embedding service health check returned ok status but model not ready.", health_data=data)
                    return False
            else:
                client_log.warning("Embedding service health check failed.", status_code=response.status_code, response_text=response.text)
                return False
        except httpx.RequestError as e:
            client_log.error("Error connecting to embedding service for health check.", error=str(e))
            return False
        except Exception as e: # Para errores de JSON parsing u otros
            client_log.error("Unexpected error during embedding service health check.", error=str(e))
            return False

    async def close(self):
        """Cierra el cliente HTTP."""
        await self._client.aclose()
        log.info("EmbeddingServiceClient closed.")