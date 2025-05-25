from functools import lru_cache
from typing import Dict, Type, Optional # Optional importado

from app.application.ports.extraction_port import ExtractionPort
from app.application.ports.chunking_port import ChunkingPort
from app.application.use_cases.process_document_use_case import ProcessDocumentUseCase

from app.infrastructure.extractors.pdf_adapter import PdfAdapter
from app.infrastructure.extractors.docx_adapter import DocxAdapter
from app.infrastructure.extractors.txt_adapter import TxtAdapter
from app.infrastructure.extractors.html_adapter import HtmlAdapter
from app.infrastructure.extractors.md_adapter import MdAdapter
from app.infrastructure.extractors.excel_adapter import ExcelAdapter # NUEVO
from app.infrastructure.chunkers.default_chunker_adapter import DefaultChunkerAdapter

from app.core.config import settings
import structlog

log = structlog.get_logger(__name__)

# Eliminada la clase CompositeExtractionAdapter ya que no se usa

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
def get_excel_adapter() -> ExcelAdapter: # NUEVO
    return ExcelAdapter()


@lru_cache()
def get_all_extraction_adapters() -> Dict[str, ExtractionPort]:
    adapters = {
        "application/pdf": get_pdf_adapter(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": get_docx_adapter(),
        "application/msword": get_docx_adapter(), 
        "text/plain": get_txt_adapter(),
        "text/html": get_html_adapter(),
        "text/markdown": get_md_adapter(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": get_excel_adapter(), # XLSX
        "application/vnd.ms-excel": get_excel_adapter(), # XLS
    }
    # Filter by configured supported types (que ahora están en minúsculas en config)
    return {
        ct.lower(): adapter for ct, adapter in adapters.items() if ct.lower() in settings.SUPPORTED_CONTENT_TYPES
    }


class FlexibleExtractionPort(ExtractionPort):
    def __init__(self):
        self.adapters_map = get_all_extraction_adapters()
        self.log = log.bind(component="FlexibleExtractionPort")
        self.log.info("Initialized FlexibleExtractionPort with adapters", adapters=list(self.adapters_map.keys()))


    def extract_text(self, file_bytes: bytes, filename: str, content_type: str):
        content_type_lower = content_type.lower() # Comparar en minúsculas
        self.log.debug("FlexibleExtractionPort: Attempting extraction", filename=filename, content_type=content_type_lower)
        
        adapter_to_use: Optional[ExtractionPort] = self.adapters_map.get(content_type_lower)
        
        # Fallback simple para application/msword si solo está registrado el de docx.
        # Esto es solo un ejemplo, la lógica de `get_all_extraction_adapters` ya debería manejar esto.
        if not adapter_to_use and content_type_lower == "application/msword":
             adapter_to_use = self.adapters_map.get("application/vnd.openxmlformats-officedocument.wordprocessingml.document")


        if adapter_to_use:
            self.log.info(f"Using adapter {type(adapter_to_use).__name__} for {content_type_lower}")
            return adapter_to_use.extract_text(file_bytes, filename, content_type_lower) # Pasar content_type original o lower? Pasamos lower para consistencia interna del adapter
        else:
            self.log.warning("No suitable adapter found for content type", content_type_provided=content_type, content_type_lower=content_type_lower, available_adapters=list(self.adapters_map.keys()))
            from app.application.ports.extraction_port import UnsupportedContentTypeError # Importación local
            raise UnsupportedContentTypeError(f"No configured adapter for content type: {content_type}")


@lru_cache()
def get_flexible_extraction_port() -> ExtractionPort:
    return FlexibleExtractionPort()


@lru_cache()
def get_default_chunker_adapter() -> ChunkingPort:
    return DefaultChunkerAdapter()

@lru_cache()
def get_process_document_use_case() -> ProcessDocumentUseCase:
    extraction_port = get_flexible_extraction_port()
    chunking_port = get_default_chunker_adapter()
    
    log.info("Creating ProcessDocumentUseCase instance", 
             extraction_port_type=type(extraction_port).__name__,
             chunking_port_type=type(chunking_port).__name__)
    
    return ProcessDocumentUseCase(
        extraction_port=extraction_port,
        chunking_port=chunking_port
    )