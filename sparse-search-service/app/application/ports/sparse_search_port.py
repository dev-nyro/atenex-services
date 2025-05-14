# sparse-search-service/app/application/ports/sparse_search_port.py
import abc
import uuid
from typing import List, Tuple, Dict, Any

from app.domain.models import SparseSearchResultItem # Reutilizar el modelo de dominio

class SparseSearchPort(abc.ABC):
    """
    Puerto abstracto para realizar búsquedas dispersas (como BM25).
    """

    @abc.abstractmethod
    async def search(
        self,
        query: str,
        company_id: uuid.UUID,
        corpus_chunks: List[Dict[str, Any]], # Lista de chunks [{'id': str, 'content': str}, ...]
        top_k: int
    ) -> List[SparseSearchResultItem]:
        """
        Realiza una búsqueda dispersa en el corpus de chunks proporcionado.

        Args:
            query: La consulta del usuario.
            company_id: El ID de la compañía (para logging o contexto, aunque el corpus ya está filtrado).
            corpus_chunks: Una lista de diccionarios, donde cada diccionario representa
                           un chunk y debe contener al menos las claves 'id' (str, único)
                           y 'content' (str).
            top_k: El número máximo de resultados a devolver.

        Returns:
            Una lista de objetos SparseSearchResultItem, ordenados por relevancia descendente.

        Raises:
            ValueError: Si los datos de entrada son inválidos.
            Exception: Para errores inesperados durante la búsqueda.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def initialize_engine(self) -> None:
        """
        Método para inicializar cualquier componente pesado del motor de búsqueda,
        como cargar modelos o verificar dependencias. Se llama durante el startup.
        """
        raise NotImplementedError