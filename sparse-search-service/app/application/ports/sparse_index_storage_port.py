# sparse-search-service/app/application/ports/sparse_index_storage_port.py
import abc
import uuid
from typing import Tuple, Optional

class SparseIndexStoragePort(abc.ABC):
    """
    Puerto abstracto para cargar y guardar archivos de índice BM25
    (el índice serializado y el mapa de IDs) desde/hacia un almacenamiento persistente.
    """

    @abc.abstractmethod
    async def load_index_files(self, company_id: uuid.UUID) -> Tuple[Optional[str], Optional[str]]:
        """
        Descarga los archivos de índice (dump BM25 y mapa de IDs JSON)
        desde el almacenamiento para una compañía específica.

        Args:
            company_id: El UUID de la compañía.

        Returns:
            Una tupla conteniendo las rutas a los archivos locales temporales:
            (local_bm2s_dump_path, local_id_map_path).
            Retorna (None, None) si los archivos no se encuentran, no se pueden descargar,
            o si ocurre cualquier error durante el proceso.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def save_index_files(self, company_id: uuid.UUID, local_bm2s_dump_path: str, local_id_map_path: str) -> None:
        """
        Guarda los archivos de índice locales (dump BM25 y mapa de IDs JSON)
        en el almacenamiento persistente para una compañía específica.

        Args:
            company_id: El UUID de la compañía.
            local_bm2s_dump_path: Ruta al archivo local del dump BM25.
            local_id_map_path: Ruta al archivo local del mapa de IDs JSON.

        Raises:
            Exception: Si ocurre un error durante la subida de los archivos.
        """
        raise NotImplementedError