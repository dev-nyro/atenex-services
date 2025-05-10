from abc import ABC, abstractmethod
from typing import List

class ChunkingError(Exception):
    """Base exception for chunking errors."""
    pass

class ChunkingPort(ABC):
    """
    Interface (Port) para la división de texto en fragmentos (chunks).
    """

    @abstractmethod
    def chunk_text(
        self,
        text_content: str,
        chunk_size: int,
        chunk_overlap: int
    ) -> List[str]:
        """
        Divide un bloque de texto en fragmentos más pequeños.

        Args:
            text_content: El texto a dividir.
            chunk_size: El tamaño deseado para cada chunk (en alguna unidad, ej. tokens o palabras).
            chunk_overlap: El tamaño del solapamiento entre chunks consecutivos.

        Returns:
            Una lista de strings, donde cada string es un chunk.

        Raises:
            ChunkingError: Si ocurre un error durante el chunking.
        """
        pass