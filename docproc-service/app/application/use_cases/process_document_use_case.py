import time
import structlog
from typing import List, Dict, Any, Optional, Tuple, Union

from app.core.config import settings
from app.domain.models import (
    ProcessedChunk,
    ProcessedDocumentMetadata,
    ProcessedChunkSourceMetadata,
    ProcessResponseData
)
from app.application.ports.extraction_port import ExtractionPort, UnsupportedContentTypeError, ExtractionError
from app.application.ports.chunking_port import ChunkingPort, ChunkingError

log = structlog.get_logger(__name__)

class ProcessDocumentUseCase:
    def __init__(
        self,
        extraction_port: ExtractionPort,
        chunking_port: ChunkingPort,
    ):
        self.extraction_port = extraction_port
        self.chunking_port = chunking_port
        self.log = log.bind(component="ProcessDocumentUseCase")

    async def execute(
        self,
        file_bytes: bytes,
        original_filename: str,
        content_type: str,
        # Parámetros de chunking pueden venir de la request o de la config del servicio
        chunk_size: Optional[int] = None, 
        chunk_overlap: Optional[int] = None,
        # IDs opcionales para tracing
        document_id_trace: Optional[str] = None, 
        company_id_trace: Optional[str] = None
    ) -> ProcessResponseData:
        
        start_time = time.perf_counter()
        
        use_case_log = self.log.bind(
            original_filename=original_filename, 
            content_type=content_type,
            document_id_trace=document_id_trace,
            company_id_trace=company_id_trace
        )
        use_case_log.info("Starting document processing")

        # Determinar parámetros de chunking
        effective_chunk_size = chunk_size if chunk_size is not None else settings.CHUNK_SIZE
        effective_chunk_overlap = chunk_overlap if chunk_overlap is not None else settings.CHUNK_OVERLAP

        # 1. Extracción de Texto
        try:
            extracted_data, extraction_meta = await self._run_extraction(file_bytes, original_filename, content_type)
        except UnsupportedContentTypeError as e:
            use_case_log.warning("Unsupported content type for extraction", error=str(e))
            raise  # Re-raise para que el endpoint lo maneje como 415
        except ExtractionError as e:
            use_case_log.error("Extraction failed", error=str(e))
            raise # Re-raise para que el endpoint lo maneje como 422 o 500

        # 2. Concatenación y Chunking
        all_chunks_data: List[ProcessedChunk] = []
        raw_text_length = 0

        if isinstance(extracted_data, str): # Para TXT, HTML, MD
            raw_text_length = len(extracted_data)
            if extracted_data.strip(): # Solo chunkear si hay contenido no vacío
                text_chunks = self._run_chunking(extracted_data, effective_chunk_size, effective_chunk_overlap)
                for text_chunk_content in text_chunks:
                    all_chunks_data.append(
                        ProcessedChunk(
                            text=text_chunk_content,
                            source_metadata=ProcessedChunkSourceMetadata() # Sin página para estos formatos
                        )
                    )
        elif isinstance(extracted_data, list): # Para PDF (lista de tuplas (page_num, page_text))
            current_page_text_for_chunking = ""
            for page_num, page_text in extracted_data:
                raw_text_length += len(page_text)
                if page_text.strip():
                    text_chunks_from_page = self._run_chunking(page_text, effective_chunk_size, effective_chunk_overlap)
                    for text_chunk_content in text_chunks_from_page:
                        all_chunks_data.append(
                            ProcessedChunk(
                                text=text_chunk_content,
                                source_metadata=ProcessedChunkSourceMetadata(page_number=page_num)
                            )
                        )
            # Alternativa: concatenar todo el texto del PDF y luego chunkear una vez.
            # Esto podría perder la granularidad de página para source_metadata de chunks que cruzan páginas.
            # La estrategia actual (chunkear página por página) es más simple para mantener metadatos de página.
        else:
            use_case_log.error("Unexpected data type from extraction port", type_received=type(extracted_data))
            raise ExtractionError("Internal error: Unexpected data type from extraction.")

        end_time = time.perf_counter()
        processing_time_ms = (end_time - start_time) * 1000

        doc_meta = ProcessedDocumentMetadata(
            original_filename=original_filename,
            content_type=content_type,
            total_pages_extracted=extraction_meta.get("total_pages_extracted"),
            raw_text_length_chars=raw_text_length,
            processing_time_ms=round(processing_time_ms, 2),
            num_chunks_generated=len(all_chunks_data)
        )

        use_case_log.info("Document processing finished successfully", 
                          num_chunks=len(all_chunks_data), 
                          processing_time_ms=doc_meta.processing_time_ms)
        
        return ProcessResponseData(document_metadata=doc_meta, chunks=all_chunks_data)

    async def _run_extraction(self, file_bytes: bytes, filename: str, content_type: str):
        # En un entorno real, podrías querer ejecutar esto en un ThreadPoolExecutor si es bloqueante
        # pero FastAPI maneja UploadFile de forma asíncrona y los extractores de PyMuPDF/docx
        # pueden ser CPU-bound pero no necesariamente bloquean el event loop si se manejan bien.
        # Por ahora, llamada directa.
        return self.extraction_port.extract_text(file_bytes, filename, content_type)

    def _run_chunking(self, text: str, chunk_size: int, chunk_overlap: int) -> List[str]:
        try:
            return self.chunking_port.chunk_text(text, chunk_size, chunk_overlap)
        except ChunkingError as e:
            self.log.error("Chunking failed during use case execution", error=str(e))
            # Decide si re-elevar o devolver lista vacía. Por ahora, re-elevar.
            raise

