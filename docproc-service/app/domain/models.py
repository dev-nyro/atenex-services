from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field

class ProcessedChunkSourceMetadata(BaseModel):
    """Metadatos originados durante el procesamiento en docproc-service para un chunk."""
    page_number: Optional[int] = Field(None, description="Número de página original del chunk (si aplica, ej. PDF).")
    # Se pueden añadir otros metadatos específicos del extractor o chunker aquí
    # por ejemplo, tipo de contenido original del bloque (ej. "table", "paragraph")
    # original_block_type: Optional[str] = None

class ProcessedChunk(BaseModel):
    """Representa un chunk de texto procesado."""
    text: str = Field(..., description="El contenido textual del chunk.")
    source_metadata: ProcessedChunkSourceMetadata = Field(default_factory=ProcessedChunkSourceMetadata, description="Metadatos asociados al origen del chunk.")

class ProcessedDocumentMetadata(BaseModel):
    """Metadatos generales sobre el documento procesado."""
    original_filename: str = Field(..., description="Nombre original del archivo procesado.")
    content_type: str = Field(..., description="Tipo MIME del archivo procesado.")
    total_pages_extracted: Optional[int] = Field(None, description="Número total de páginas de las que se extrajo texto (ej. para PDF).")
    raw_text_length_chars: int = Field(..., description="Longitud del texto crudo extraído en caracteres.")
    processing_time_ms: float = Field(..., description="Tiempo total de procesamiento en milisegundos.")
    num_chunks_generated: int = Field(..., description="Número total de chunks generados.")

class ProcessResponseData(BaseModel):
    """Datos contenidos en una respuesta exitosa del endpoint de procesamiento."""
    document_metadata: ProcessedDocumentMetadata
    chunks: List[ProcessedChunk]

class ProcessResponse(BaseModel):
    """Schema de respuesta para el endpoint POST /api/v1/process."""
    data: ProcessResponseData

    class Config:
        json_schema_extra = {
            "example": {
                "data": {
                    "document_metadata": {
                        "original_filename": "example_document.pdf",
                        "content_type": "application/pdf",
                        "total_pages_extracted": 10,
                        "raw_text_length_chars": 15000,
                        "processing_time_ms": 543.21,
                        "num_chunks_generated": 15
                    },
                    "chunks": [
                        {
                            "text": "Este es el contenido del primer chunk procesado...",
                            "source_metadata": {"page_number": 1}
                        },
                        {
                            "text": "Este es el contenido del segundo chunk procesado...",
                            "source_metadata": {"page_number": 1}
                        }
                    ]
                }
            }
        }