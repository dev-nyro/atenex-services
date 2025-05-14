# query-service/app/infrastructure/clients/sparse_search_service_client.py
import httpx
import structlog
import uuid
from typing import List, Dict, Any, Tuple, Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.core.config import settings # Para timeouts y URL base
from app.domain.models import SparseSearchResultItem # Para el tipo de retorno del servicio

log = structlog.get_logger(__name__)

class SparseSearchServiceClient:
    """
    Cliente HTTP para interactuar con el Atenex Sparse Search Service.
    """
    def __init__(self, base_url: str, timeout: int = settings.HTTP_CLIENT_TIMEOUT):
        # Asegurar que la URL base no tenga /api/v1 al final si el endpoint ya lo incluye
        self.base_url = base_url.rstrip('/')
        # El endpoint del sparse-search-service es /api/v1/search
        self.search_endpoint = f"{self.base_url}/api/v1/search"
        self.health_endpoint = f"{self.base_url}/health"

        # Validar que la URL base no incluya /api/v1 si el endpoint ya lo hace.
        # Ejemplo: si base_url es http://service/api/v1, endpoint es http://service/api/v1/search
        # Ejemplo: si base_url es http://service, endpoint es http://service/api/v1/search
        if self.base_url.endswith("/api/v1"):
            self.search_endpoint = f"{self.base_url.rsplit('/api/v1', 1)[0]}/api/v1/search"
        elif self.base_url.endswith("/api"):
             self.search_endpoint = f"{self.base_url.rsplit('/api', 1)[0]}/api/v1/search"

        self._client = httpx.AsyncClient(timeout=timeout)
        log.info("SparseSearchServiceClient initialized",
                 base_url=self.base_url,
                 search_endpoint=self.search_endpoint,
                 health_endpoint=self.health_endpoint)

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES + 1),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError)),
        reraise=True
    )
    async def search(self, query_text: str, company_id: uuid.UUID, top_k: int) -> List[SparseSearchResultItem]:
        """
        Solicita una búsqueda dispersa al sparse-search-service.
        Devuelve una lista de SparseSearchResultItem del dominio.
        """
        client_log = log.bind(action="sparse_search_remote",
                              company_id=str(company_id),
                              query_preview=query_text[:50]+"...",
                              top_k=top_k,
                              target_service="sparse-search-service")
        if not query_text:
            client_log.warning("No query text provided for sparse search.")
            return []

        payload = {
            "query": query_text,
            "company_id": str(company_id), # El servicio espera un UUID string en JSON
            "top_k": top_k
        }
        try:
            client_log.debug("Sending request to sparse search service")
            response = await self._client.post(self.search_endpoint, json=payload)
            response.raise_for_status()

            data = response.json()
            
            if "results" not in data or not isinstance(data["results"], list):
                client_log.error("Invalid response format from sparse search service: 'results' field missing or not a list.", response_data=data)
                raise ValueError("Invalid response format from sparse search service: 'results' field.")

            # Mapear a SparseSearchResultItem del dominio
            domain_results = []
            for item_data in data["results"]:
                # El servicio sparse-search ya devuelve items que coinciden con SparseSearchResultItem
                # así que podemos instanciarlos directamente si el schema coincide.
                # Asumimos que 'chunk_id' y 'score' están presentes.
                domain_results.append(SparseSearchResultItem(**item_data))
            
            client_log.info("Sparse search results received successfully from service", num_results=len(domain_results))
            return domain_results

        except httpx.HTTPStatusError as e:
            client_log.error("HTTP error from sparse search service", status_code=e.response.status_code, response_body=e.response.text)
            raise ConnectionError(f"Sparse search service returned error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            client_log.error("Request error while contacting sparse search service", error=str(e))
            raise ConnectionError(f"Could not connect to sparse search service: {e}") from e
        except (ValueError, TypeError, AttributeError) as e: # Errores de parsing JSON o validación de Pydantic
            client_log.error("Error processing response from sparse search service", error=str(e))
            raise ValueError(f"Invalid response or data from sparse search service: {e}") from e

    async def check_health(self) -> bool:
        client_log = log.bind(action="check_health_sparse_search", target_service="sparse-search-service")
        try:
            response = await self._client.get(self.health_endpoint, timeout=5) # Shorter timeout for health
            if response.status_code == 200:
                data = response.json()
                # El health check del sparse-search-service devuelve un JSON con `status` y `ready`
                if data.get("status") == "ok" and data.get("ready") is True:
                    client_log.info("Sparse search service health check successful.", health_data=data)
                    return True
                else:
                    client_log.warning("Sparse search service health check returned ok status but service/dependencies not ready.", health_data=data)
                    return False
            else:
                client_log.warning("Sparse search service health check failed.", status_code=response.status_code, response_text=response.text)
                return False
        except httpx.RequestError as e:
            client_log.error("Error connecting to sparse search service for health check.", error=str(e))
            return False
        except Exception as e:
            client_log.error("Unexpected error during sparse search service health check.", error=str(e))
            return False

    async def close(self):
        await self._client.aclose()
        log.info("SparseSearchServiceClient closed.")