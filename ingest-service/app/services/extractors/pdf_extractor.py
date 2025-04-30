import fitz  # PyMuPDF
import structlog

log = structlog.get_logger(__name__)

class PdfExtractionError(Exception):
    pass

def extract_text_from_pdf(file_bytes: bytes, filename: str = "unknown.pdf") -> str:
    """Extrae texto plano de los bytes de un PDF usando PyMuPDF."""
    log.debug("Extracting text from PDF bytes", filename=filename)
    try:
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            text_content = ""
            for page_num, page in enumerate(doc):
                try:
                    text_content += page.get_text("text") + "\n"
                except Exception as page_err:
                    log.warning("Error extracting text from PDF page", filename=filename, page=page_num + 1, error=str(page_err))
            log.info("PDF extraction successful", filename=filename, num_pages=len(doc))
            return text_content
    except Exception as e:
        log.error("PDF extraction failed", filename=filename, error=str(e))
        raise PdfExtractionError(f"Error extracting PDF: {filename}") from e
