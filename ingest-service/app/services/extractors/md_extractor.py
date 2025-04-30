import markdown
import html2text
import structlog

log = structlog.get_logger(__name__)

class MdExtractionError(Exception):
    pass

def extract_text_from_md(file_bytes: bytes, filename: str = "unknown.md", encoding: str = "utf-8") -> str:
    """Extrae texto de los bytes de un archivo Markdown."""
    log.debug("Extracting text from MD bytes", filename=filename)
    try:
        md_text = file_bytes.decode(encoding)
        html = markdown.markdown(md_text)
        text = html2text.html2text(html)
        log.info("MD extraction successful", filename=filename, length=len(text))
        return text
    except Exception as e:
        log.error("MD extraction failed", filename=filename, error=str(e))
        raise MdExtractionError(f"Error extracting MD: {filename}") from e
