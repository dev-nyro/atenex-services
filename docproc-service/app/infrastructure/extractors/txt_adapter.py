import structlog
from typing import Tuple, Dict, Any, Union, List

from app.application.ports.extraction_port import ExtractionPort, ExtractionError, UnsupportedContentTypeError
from app.infrastructure.extractors.base_extractor import BaseExtractorAdapter

log = structlog.get_logger(__name__)

class TxtAdapter(BaseExtractorAdapter):
    """Adaptador para extraer texto de archivos TXT."""

    SUPPORTED_CONTENT_TYPES = ["text/plain"]

    def extract_text(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str,
        encoding: str = "utf-8" # Default encoding
    ) -> Tuple[str, Dict[str, Any]]:
        if content_type not in self.SUPPORTED_CONTENT_TYPES:
            raise UnsupportedContentTypeError(f"TxtAdapter does not support content type: {content_type}")

        log.debug("TxtAdapter: Extracting text from TXT bytes", filename=filename, encoding=encoding)
        extraction_metadata: Dict[str, Any] = {}
        try:
            # Try common encodings if default utf-8 fails
            encodings_to_try = [encoding, 'latin-1', 'iso-8859-1', 'cp1252']
            text = None
            for enc in encodings_to_try:
                try:
                    text = file_bytes.decode(enc)
                    log.info(f"TxtAdapter: Successfully decoded with {enc}", filename=filename)
                    extraction_metadata["encoding_used"] = enc
                    break
                except UnicodeDecodeError:
                    log.debug(f"TxtAdapter: Failed to decode with {enc}, trying next.", filename=filename)
                    continue
            
            if text is None:
                log.error("TxtAdapter: Could not decode TXT file with tried encodings.", filename=filename)
                raise ExtractionError(f"Could not decode TXT file {filename} with tried encodings.")

            log.info("TxtAdapter: TXT extraction successful", filename=filename, length=len(text))
            return text, extraction_metadata
        except Exception as e:
            if not isinstance(e, ExtractionError): # Avoid re-wrapping known ExtractionError
                raise self._handle_extraction_error(e, filename, "TxtAdapter")
            raise e