# sparse-search-service/app/application/ports/repository_ports.py
import abc
import uuid
from typing import Dict, List, Optional, Any # LLM: CORRECTION - Añadir Any

class ChunkContentRepositoryPort(abc.ABC):
    """
    Puerto abstracto para obtener contenido textual de chunks desde la persistencia.
    Este servicio necesita esto para construir los índices BM25.
    """

    @abc.abstractmethod
    async def get_chunk_contents_by_company(self, company_id: uuid.UUID) -> Dict[str, str]:
        """
        Obtiene un diccionario de {chunk_id: content} para una compañía específica.
        El `chunk_id` aquí se espera que sea el `embedding_id` o `pk_id` que se utiliza
        como identificador único del chunk en el sistema de búsqueda vectorial y logging.

        Args:
            company_id: El UUID de la compañía.

        Returns:
            Un diccionario donde las claves son los IDs de los chunks (str) y los valores
            son el contenido textual de dichos chunks (str).

        Raises:
            ConnectionError: Si hay problemas de comunicación con la base de datos.
            Exception: Para otros errores inesperados.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def get_chunks_with_metadata_by_company(
        self, company_id: uuid.UUID
    ) -> List[Dict[str, Any]]: # El uso de Any aquí es correcto ahora
        """
        Obtiene una lista de chunks para una compañía, cada uno como un diccionario
        que incluye 'id' (el embedding_id/pk_id), 'content', y opcionalmente
        otros metadatos relevantes para BM25 si se quisieran usar para filtrar
        pre-indexación o post-búsqueda (aunque BM25 puro es sobre contenido).

        Args:
            company_id: El UUID de la compañía.

        Returns:
            Una lista de diccionarios, cada uno representando un chunk con al menos
            {'id': str, 'content': str}.
        """
        raise NotImplementedError