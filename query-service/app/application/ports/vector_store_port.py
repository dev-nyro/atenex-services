# query-service/app/application/ports/vector_store_port.py
import abc
from typing import List
# LLM_REFACTOR_STEP_2: Importar el objeto de dominio
from app.domain.models import RetrievedChunk

class VectorStorePort(abc.ABC):
    """Puerto abstracto para interactuar con una base de datos vectorial."""

    @abc.abstractmethod
    async def search(self, embedding: List[float], company_id: str, top_k: int) -> List[RetrievedChunk]:
        """
        Busca chunks relevantes basados en un embedding y filtra por compañía.

        Args:
            embedding: El vector de embedding de la consulta.
            company_id: El ID de la compañía para filtrar los resultados.
            top_k: El número máximo de chunks a devolver.

        Returns:
            Una lista de objetos RetrievedChunk relevantes.

        Raises:
            ConnectionError: Si falla la comunicación con la base de datos vectorial.
            Exception: Para otros errores inesperados.
        """
        raise NotImplementedError