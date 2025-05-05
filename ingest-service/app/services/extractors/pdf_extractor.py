# ingest-service/app/services/extractors/pdf_extractor.py
import fitz  # PyMuPDF
import structlog
from typing import List, Tuple, Union # Added Tuple and Union

log = structlog.get_logger(__name__)

class PdfExtractionError(Exception):
    pass

def extract_text_from_pdf(file_bytes: bytes, filename: str = "unknown.pdf") -> List[Tuple[int, str]]:
    """
    Extrae texto plano de los bytes de un PDF usando PyMuPDF,
    devolviendo una lista de tuplas (page_number, page_text).
    """
    log.debug("Extracting text and pages from PDF bytes", filename=filename)
    pages_content: List[Tuple[int, str]] = []
    try:
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            log.info("Processing PDF document", filename=filename, num_pages=len(doc))
            for page_num_zero_based, page in enumerate(doc):
                page_num_one_based = page_num_zero_based + 1
                try:
                    page_text = page.get_text("text")
                    if page_text and not page_text.isspace():
                         pages_content.append((page_num_one_based, page_text))
                         log.debug("Extracted text from page", page=page_num_one_based, length=len(page_text))
                    else:
                         log.debug("Skipping empty or whitespace-only page", page=page_num_one_based)

                except Exception as page_err:
                    log.warning("Error extracting text from PDF page", filename=filename, page=page_num_one_based, error=str(page_err))
            log.info("PDF extraction successful", filename=filename, num_pages_processed=len(pages_content))
            return pages_content
    except Exception as e:
        log.error("PDF extraction failed", filename=filename, error=str(e))
        raise PdfExtractionError(f"Error extracting PDF: {filename}") from e

# Keep original function signature for compatibility if needed elsewhere,
# but adapt its use or mark as deprecated if only page-aware version is used now.
# def extract_text_from_pdf_combined(file_bytes: bytes, filename: str = "unknown.pdf") -> str:
#     pages_data = extract_text_from_pdf(file_bytes, filename)
#     return "\n".join([text for _, text in pages_data])