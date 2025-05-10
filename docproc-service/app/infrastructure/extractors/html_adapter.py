from bs4 import BeautifulSoup
import structlog
from typing import Tuple, Dict, Any, Union, List

from app.application.ports.extraction_port import ExtractionPort, ExtractionError, UnsupportedContentTypeError
from app.infrastructure.extractors.base_extractor import BaseExtractorAdapter

log = structlog.get_logger(__name__)

class HtmlAdapter(BaseExtractorAdapter):
    """Adaptador para extraer texto de archivos HTML."""

    SUPPORTED_CONTENT_TYPES = ["text/html"]

    def extract_text(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str,
        encoding: str = "utf-8" # Default encoding for HTML
    ) -> Tuple[str, Dict[str, Any]]:
        if content_type not in self.SUPPORTED_CONTENT_TYPES:
            raise UnsupportedContentTypeError(f"HtmlAdapter does not support content type: {content_type}")

        log.debug("HtmlAdapter: Extracting text from HTML bytes", filename=filename)
        extraction_metadata: Dict[str, Any] = {}
        try:
            # BeautifulSoup typically handles encoding detection well, but providing a hint can help.
            html_content = file_bytes.decode(encoding, errors='replace') # Replace errors to avoid decode failure
            soup = BeautifulSoup(html_content, "html.parser")
            
            # Remove script and style elements
            for script_or_style in soup(["script", "style"]):
                script_or_style.decompose()
            
            text = soup.get_text(separator="\n", strip=True)
            
            extraction_metadata["title_extracted"] = soup.title.string if soup.title else None
            log.info("HtmlAdapter: HTML extraction successful", filename=filename, length=len(text))
            return text, extraction_metadata
        except Exception as e:
            raise self._handle_extraction_error(e, filename, "HtmlAdapter")