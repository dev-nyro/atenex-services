from functools import lru_cache
from typing import Dict, Type

from app.application.ports.extraction_port import ExtractionPort
from app.application.ports.chunking_port import ChunkingPort
from app.application.use_cases.process_document_use_case import ProcessDocumentUseCase

from app.infrastructure.extractors.pdf_adapter import PdfAdapter
from app.infrastructure.extractors.docx_adapter import DocxAdapter
from app.infrastructure.extractors.txt_adapter import TxtAdapter
from app.infrastructure.extractors.html_adapter import HtmlAdapter
from app.infrastructure.extractors.md_adapter import MdAdapter
from app.infrastructure.chunkers.default_chunker_adapter import DefaultChunkerAdapter

from app.core.config import settings
import structlog

log = structlog.get_logger(__name__)

class CompositeExtractionAdapter(ExtractionPort):
    """
    Un adaptador de extracción compuesto que delega al adaptador apropiado
    basado en el content_type.
    """
    def __init__(self, adapters: Dict[str, ExtractionPort]):
        self.adapters = adapters
        self.log = log.bind(component="CompositeExtractionAdapter")

    def extract_text(self, file_bytes: bytes, filename: str, content_type: str):
        self.log.debug("Attempting extraction", filename=filename, content_type=content_type)
        
        # Find adapter that supports this content type
        selected_adapter: Optional[ExtractionPort] = None
        for adapter_instance in self.adapters.values():
            # Adapters should ideally expose their supported types or be selected more directly
            # For now, we assume adapters raise UnsupportedContentTypeError if they can't handle it
            # A better approach would be to register adapters with their supported MIME types.
            # Here, we try them or have specific logic.
            # For this example, we'll rely on a pre-defined mapping in the factory.
            # This Composite adapter is more of a dispatcher.
            # A simpler way in the factory is to just pick the right one.
            # Let's re-think: the factory should provide the *specific* adapter,
            # or this composite adapter needs a way to know which one to call.

            # Simplified: Assume the ProcessDocumentUseCase will choose one.
            # This composite is not strictly needed if the use case itself can select.
            # However, to keep the use case clean from knowing *all* adapters,
            # a factory function for ExtractionPort in dependencies.py is better.

            # Let's refine: This Composite is not used. Instead, a factory creates the right one.
            # Keeping this class for conceptual reference, but get_extraction_port is the way.
            pass # This class will not be directly used as a port implementation for DI to use case

        # This method will not be called if the factory directly provides the correct adapter.
        # This is more for a scenario where the UseCase gets *this* composite.
        # For now, it's unused.
        raise NotImplementedError("CompositeExtractionAdapter.extract_text should not be called directly if factory provides specific adapter.")


@lru_cache()
def get_pdf_adapter() -> PdfAdapter:
    return PdfAdapter()

@lru_cache()
def get_docx_adapter() -> DocxAdapter:
    return DocxAdapter()

@lru_cache()
def get_txt_adapter() -> TxtAdapter:
    return TxtAdapter()

@lru_cache()
def get_html_adapter() -> HtmlAdapter:
    return HtmlAdapter()

@lru_cache()
def get_md_adapter() -> MdAdapter:
    return MdAdapter()


# Factory function to provide the correct ExtractionPort based on content_type
# This is not directly injectable into FastAPI's Depends if content_type is runtime data.
# So, the ProcessDocumentUseCase will need access to all relevant adapters
# or a way to request one. Let's provide all to the use case for now, or make the use case resolve it.

# For simplicity, the UseCase will have a mapping or similar logic.
# Or, the endpoint can determine the adapter and pass it.
# Let's make the UseCase smarter with a dictionary of available extractors.

@lru_cache()
def get_all_extraction_adapters() -> Dict[str, ExtractionPort]:
    """Returns a dictionary of all configured extraction adapters, keyed by a representative content type."""
    # This is a simplified mapping. A more robust solution might involve
    # querying adapters for their supported types.
    adapters = {
        "application/pdf": get_pdf_adapter(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": get_docx_adapter(),
        "application/msword": get_docx_adapter(), # DOCX adapter might handle some .doc
        "text/plain": get_txt_adapter(),
        "text/html": get_html_adapter(),
        "text/markdown": get_md_adapter(),
    }
    # Filter by configured supported types
    return {
        ct: adapter for ct, adapter in adapters.items() if ct in settings.SUPPORTED_CONTENT_TYPES
    }


class FlexibleExtractionPort(ExtractionPort):
    """
    An ExtractionPort implementation that dynamically selects the correct adapter.
    This is what will be injected into the UseCase.
    """
    def __init__(self):
        self.adapters_map = get_all_extraction_adapters()
        self.log = log.bind(component="FlexibleExtractionPort")
        self.log.info("Initialized FlexibleExtractionPort with adapters", adapters=list(self.adapters_map.keys()))


    def extract_text(self, file_bytes: bytes, filename: str, content_type: str):
        self.log.debug("FlexibleExtractionPort: Attempting extraction", filename=filename, content_type=content_type)
        
        adapter_to_use = None
        # Prioritize exact match
        if content_type in self.adapters_map:
            adapter_to_use = self.adapters_map[content_type]
        else:
            # Fallback for similar types, e.g. if only specific docx mimetype is registered
            # but a more generic one is passed. This logic can be expanded.
            if content_type.startswith("text/"): # Generic text might be plain
                 adapter_to_use = self.adapters_map.get("text/plain")

        if adapter_to_use:
            self.log.info(f"Using adapter {type(adapter_to_use).__name__} for {content_type}")
            return adapter_to_use.extract_text(file_bytes, filename, content_type)
        else:
            self.log.warning("No suitable adapter found for content type", content_type=content_type)
            from app.application.ports.extraction_port import UnsupportedContentTypeError
            raise UnsupportedContentTypeError(f"No configured adapter for content type: {content_type}")


@lru_cache()
def get_flexible_extraction_port() -> ExtractionPort:
    return FlexibleExtractionPort()


@lru_cache()
def get_default_chunker_adapter() -> ChunkingPort:
    return DefaultChunkerAdapter()

@lru_cache()
def get_process_document_use_case() -> ProcessDocumentUseCase:
    # This is where specific adapters are chosen or a composite/flexible one is passed
    extraction_port = get_flexible_extraction_port()
    chunking_port = get_default_chunker_adapter()
    
    log.info("Creating ProcessDocumentUseCase instance", 
             extraction_port_type=type(extraction_port).__name__,
             chunking_port_type=type(chunking_port).__name__)
    
    return ProcessDocumentUseCase(
        extraction_port=extraction_port,
        chunking_port=chunking_port
    )