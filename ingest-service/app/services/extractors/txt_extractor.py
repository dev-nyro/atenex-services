import structlog

log = structlog.get_logger(__name__)

class TxtExtractionError(Exception):
    pass

def extract_text_from_txt(file_bytes: bytes, filename: str = "unknown.txt", encoding: str = "utf-8") -> str:
    """Extrae texto de los bytes de un archivo TXT."""
    log.debug("Extracting text from TXT bytes", filename=filename)
    try:
        text = file_bytes.decode(encoding)
        log.info("TXT extraction successful", filename=filename, length=len(text))
        return text
    except Exception as e:
        log.error("TXT extraction failed", filename=filename, error=str(e))
        raise TxtExtractionError(f"Error extracting TXT: {filename}") from e
