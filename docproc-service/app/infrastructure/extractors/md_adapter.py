import markdown
import html2text # To convert HTML generated from Markdown to clean text
import structlog
from typing import Tuple, Dict, Any, Union, List

from app.application.ports.extraction_port import ExtractionPort, ExtractionError, UnsupportedContentTypeError
from app.infrastructure.extractors.base_extractor import BaseExtractorAdapter

log = structlog.get_logger(__name__)

class MdAdapter(BaseExtractorAdapter):
    """Adaptador para extraer texto de archivos Markdown."""

    SUPPORTED_CONTENT_TYPES = ["text/markdown"]

    def extract_text(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str,
        encoding: str = "utf-8" # Default encoding
    ) -> Tuple[str, Dict[str, Any]]:
        if content_type not in self.SUPPORTED_CONTENT_TYPES:
            raise UnsupportedContentTypeError(f"MdAdapter does not support content type: {content_type}")

        log.debug("MdAdapter: Extracting text from MD bytes", filename=filename)
        extraction_metadata: Dict[str, Any] = {}
        try:
            md_text_content = file_bytes.decode(encoding)
            html = markdown.markdown(md_text_content)
            
            # Use html2text to get cleaner text from the rendered HTML
            text_maker = html2text.HTML2Text()
            text_maker.ignore_links = True # Example: ignore links, can be configured
            text_maker.ignore_images = True
            text = text_maker.handle(html)
            
            log.info("MdAdapter: MD extraction successful", filename=filename, length=len(text))
            return text, extraction_metadata
        except Exception as e:
            raise self._handle_extraction_error(e, filename, "MdAdapter")