# query-service/app/infrastructure/retrievers/remote_sparse_retriever_adapter.py
import structlog
import uuid
from typing import List, Tuple

from app.application.ports.retrieval_ports import SparseRetrieverPort
from app.infrastructure.clients.sparse_search_service_client import SparseSearchServiceClient
from app.domain.models import SparseSearchResultItem # Para el tipo de resultado del cliente

log = structlog.get_logger(__name__)

class RemoteSparseRetrieverAdapter(SparseRetrieverPort):
    """
    Adaptador que utiliza SparseSearchServiceClient para realizar búsquedas dispersas
    llamando al servicio externo sparse-search-service.
    """
    def __init__(self, client: SparseSearchServiceClient):
        self.client = client
        log.info("RemoteSparseRetrieverAdapter initialized")

    async def search(self, query: str, company_id: uuid.UUID, top_k: int) -> List[Tuple[str, float]]:
        """
        Realiza una búsqueda dispersa llamando al servicio remoto.
        Devuelve una lista de tuplas (chunk_id, score).
        """
        adapter_log = log.bind(adapter="RemoteSparseRetrieverAdapter", action="search",
                               company_id=str(company_id), top_k=top_k)
        try:
            # El cliente devuelve una lista de SparseSearchResultItem
            search_results_domain: List[SparseSearchResultItem] = await self.client.search(
                query_text=query,
                company_id=company_id,
                top_k=top_k
            )

            # Mapear los resultados del dominio a List[Tuple[str, float]]
            # SparseSearchResultItem tiene 'chunk_id' y 'score'
            mapped_results: List[Tuple[str, float]] = [
                (item.chunk_id, item.score) for item in search_results_domain
            ]

            adapter_log.info(f"Sparse search successful via remote service. Returned {len(mapped_results)} results.")
            return mapped_results
        except ConnectionError as e:
            adapter_log.error("Connection error during remote sparse search.", error=str(e), exc_info=False)
            # Devolver una lista vacía para que el pipeline RAG pueda continuar
            # si la búsqueda dispersa no es estrictamente crítica.
            return []
        except ValueError as e: # Por ej. si la respuesta del servicio es inválida
            adapter_log.error("Value error during remote sparse search (invalid response from service?).", error=str(e), exc_info=True)
            return []
        except Exception as e:
            adapter_log.exception("Unexpected error during remote sparse search.")
            return [] # Devolver vacío en caso de error inesperado.

    async def health_check(self) -> bool:
        """
        Delega la verificación de salud al cliente del servicio de búsqueda dispersa.
        """
        return await self.client.check_health()