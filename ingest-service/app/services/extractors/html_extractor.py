from bs4 import BeautifulSoup
import structlog

log = structlog.get_logger(__name__)

class HtmlExtractionError(Exception):
    pass

def extract_text_from_html(file_bytes: bytes, filename: str = "unknown.html", encoding: str = "utf-8") -> str:
    """Extrae texto de los bytes de un archivo HTML."""
    log.debug("Extracting text from HTML bytes", filename=filename)
    try:
        html = file_bytes.decode(encoding)
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator="\n")
        log.info("HTML extraction successful", filename=filename, length=len(text))
        return text
    except Exception as e:
        log.error("HTML extraction failed", filename=filename, error=str(e))
        raise HtmlExtractionError(f"Error extracting HTML: {filename}") from e
