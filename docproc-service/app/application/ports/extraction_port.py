from abc import ABC, abstractmethod
from typing import List, Tuple, Union, Any, Dict # <--- AÑADIR Dict AQUÍ

class ExtractionError(Exception):
    """Base exception for extraction errors."""
    pass

class UnsupportedContentTypeError(ExtractionError):
    """Exception raised when a content type is not supported for extraction."""
    pass

class ExtractionPort(ABC):
    """
    Interface (Port) para la extracción de texto de documentos.
    """

    @abstractmethod
    def extract_text(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str
    ) -> Tuple[Union[str, List[Tuple[int, str]]], Dict[str, Any]]: # Ahora Dict es conocido
        """
        Extrae texto de los bytes de un archivo.

        Args:
            file_bytes: Contenido del archivo en bytes.
            filename: Nombre original del archivo (para logging y metadatos).
            content_type: Tipo MIME del archivo.

        Returns:
            Una tupla conteniendo:
            - El texto extraído. Puede ser un string único (para formatos sin páginas como TXT, MD, HTML)
              o una lista de tuplas (page_number, page_text) para formatos con páginas (como PDF).
            - Un diccionario con metadatos de la extracción (ej. {'total_pages_extracted': 10}).

        Raises:
            UnsupportedContentTypeError: Si el content_type no es soportado.
            ExtractionError: Para otros errores durante la extracción.
        """
        pass