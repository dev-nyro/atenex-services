import fitz  # PyMuPDF
import structlog
from typing import List, Tuple, Dict, Any, Union

from app.application.ports.extraction_port import ExtractionPort, ExtractionError, UnsupportedContentTypeError
from app.infrastructure.extractors.base_extractor import BaseExtractorAdapter


log = structlog.get_logger(__name__)

class PdfAdapter(BaseExtractorAdapter):
    """Adaptador para extraer texto de archivos PDF usando PyMuPDF."""

    SUPPORTED_CONTENT_TYPES = ["application/pdf"]

    def extract_text(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str
    ) -> Tuple[List[Tuple[int, str]], Dict[str, Any]]:
        if content_type not in self.SUPPORTED_CONTENT_TYPES:
            raise UnsupportedContentTypeError(f"PdfAdapter does not support content type: {content_type}")

        log.debug("PdfAdapter: Extracting text and pages from PDF bytes", filename=filename)
        pages_content: List[Tuple[int, str]] = []
        extraction_metadata: Dict[str, Any] = {"total_pages_extracted": 0}
        total_pages_in_doc = 0

        try:
            with fitz.open(stream=file_bytes, filetype="pdf") as doc:
                total_pages_in_doc = len(doc)
                log.info("PdfAdapter: Processing PDF document", filename=filename, num_pages_in_doc=total_pages_in_doc)
                for page_num_zero_based, page in enumerate(doc):
                    page_num_one_based = page_num_zero_based + 1
                    try:
                        page_text = page.get_text("text")
                        if page_text and not page_text.isspace():
                            pages_content.append((page_num_one_based, page_text))
                            log.debug("PdfAdapter: Extracted text from page", page=page_num_one_based, length=len(page_text))
                        else:
                            log.debug("PdfAdapter: Skipping empty or whitespace-only page", page=page_num_one_based)
                    except Exception as page_err:
                        log.warning("PdfAdapter: Error extracting text from PDF page", filename=filename, page=page_num_one_based, error=str(page_err))
                
                extraction_metadata["total_pages_extracted"] = len(pages_content)
                log.info("PdfAdapter: PDF extraction successful", filename=filename, pages_with_text=len(pages_content), total_doc_pages=total_pages_in_doc)
                return pages_content, extraction_metadata
        except Exception as e:
            raise self._handle_extraction_error(e, filename, "PdfAdapter")