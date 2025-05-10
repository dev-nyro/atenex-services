import structlog
from app.application.ports.extraction_port import ExtractionPort, ExtractionError

log = structlog.get_logger(__name__)

class BaseExtractorAdapter(ExtractionPort):
    """
    Clase base para adaptadores de extracción con logging común.
    """
    def _handle_extraction_error(self, e: Exception, filename: str, adapter_name: str) -> ExtractionError:
        log.error(f"{adapter_name} extraction failed", filename=filename, error=str(e), exc_info=True)
        raise ExtractionError(f"Error extracting with {adapter_name} for {filename}: {e}") from e