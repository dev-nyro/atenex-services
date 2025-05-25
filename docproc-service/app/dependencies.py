from functools import lru_cache
from typing import Dict, Type, Optional 

from app.application.ports.extraction_port import ExtractionPort
from app.application.ports.chunking_port import ChunkingPort
from app.application.use_cases.process_document_use_case import ProcessDocumentUseCase

from app.infrastructure.extractors.pdf_adapter import PdfAdapter
from app.infrastructure.extractors.docx_adapter import DocxAdapter
from app.infrastructure.extractors.txt_adapter import TxtAdapter
from app.infrastructure.extractors.html_adapter import HtmlAdapter
from app.infrastructure.extractors.md_adapter import MdAdapter
from app.infrastructure.extractors.excel_adapter import ExcelAdapter
from app.infrastructure.chunkers.default_chunker_adapter import DefaultChunkerAdapter

from app.core.config import settings # Importa la instancia configurada
import structlog

log = structlog.get_logger(__name__)

# Los adaptadores individuales pueden seguir cacheados si su inicialización es costosa
# y no dependen de configuraciones que cambian después del inicio.
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

@lru_cache()
def get_excel_adapter() -> ExcelAdapter:
    return ExcelAdapter()

# No cachear esta función para asegurar que siempre use el estado más reciente de `settings`
# Aunque settings debería ser un singleton cargado al inicio, esto es para depuración extrema.
def get_all_extraction_adapters() -> Dict[str, ExtractionPort]:
    # Loguear el contenido de settings.SUPPORTED_CONTENT_TYPES aquí para depuración
    log.debug("get_all_extraction_adapters: Using settings.SUPPORTED_CONTENT_TYPES", 
              supported_types=settings.SUPPORTED_CONTENT_TYPES)
              
    adapters_definitions = {
        "application/pdf": get_pdf_adapter(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": get_docx_adapter(),
        "application/msword": get_docx_adapter(), 
        "text/plain": get_txt_adapter(),
        "text/html": get_html_adapter(),
        "text/markdown": get_md_adapter(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": get_excel_adapter(), 
        "application/vnd.ms-excel": get_excel_adapter(), 
    }
    
    # settings.SUPPORTED_CONTENT_TYPES ya está en minúsculas gracias al validador en config.py
    active_adapters = {
        ct_key.lower(): adapter_instance
        for ct_key, adapter_instance in adapters_definitions.items()
        if ct_key.lower() in settings.SUPPORTED_CONTENT_TYPES
    }
    log.debug("get_all_extraction_adapters: Filtered active adapters", active_adapter_keys=list(active_adapters.keys()))
    return active_adapters


class FlexibleExtractionPort(ExtractionPort):
    def __init__(self):
        # Obtener los adaptadores al instanciar, sin lru_cache en get_all_extraction_adapters
        self.adapters_map = get_all_extraction_adapters()
        self.log = log.bind(component="FlexibleExtractionPort")
        self.log.info("Initialized FlexibleExtractionPort with adapters", adapters_found=list(self.adapters_map.keys()))

    def extract_text(self, file_bytes: bytes, filename: str, content_type: str):
        content_type_lower = content_type.lower()
        self.log.debug("FlexibleExtractionPort: Attempting extraction", filename=filename, content_type=content_type_lower)
        
        adapter_to_use: Optional[ExtractionPort] = self.adapters_map.get(content_type_lower)
        
        if not adapter_to_use and content_type_lower == "application/msword":
             adapter_to_use = self.adapters_map.get("application/vnd.openxmlformats-officedocument.wordprocessingml.document")

        if adapter_to_use:
            self.log.info(f"Using adapter {type(adapter_to_use).__name__} for {content_type_lower}")
            return adapter_to_use.extract_text(file_bytes, filename, content_type_lower)
        else:
            self.log.warning("No suitable adapter found for content type in FlexibleExtractionPort", 
                             content_type_provided=content_type, 
                             content_type_lower=content_type_lower, 
                             available_adapters_in_map=list(self.adapters_map.keys()))
            from app.application.ports.extraction_port import UnsupportedContentTypeError
            raise UnsupportedContentTypeError(f"FlexibleExtractionPort: No configured adapter for content type: {content_type}")

# No cachear esta función por ahora
def get_flexible_extraction_port() -> ExtractionPort:
    return FlexibleExtractionPort()


@lru_cache()
def get_default_chunker_adapter() -> ChunkingPort:
    return DefaultChunkerAdapter()

# No cachear el use case principal si sus dependencias no están cacheadas y queremos la última config
def get_process_document_use_case() -> ProcessDocumentUseCase:
    extraction_port = get_flexible_extraction_port()
    chunking_port = get_default_chunker_adapter()
    
    log.info("Creating ProcessDocumentUseCase instance (dependencies potentially not cached)", 
             extraction_port_type=type(extraction_port).__name__,
             chunking_port_type=type(chunking_port).__name__)
    
    return ProcessDocumentUseCase(
        extraction_port=extraction_port,
        chunking_port=chunking_port
    )