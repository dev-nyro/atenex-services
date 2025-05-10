import structlog
from typing import List

from app.application.ports.chunking_port import ChunkingPort, ChunkingError
from app.core.config import settings as service_settings # Use specific settings for docproc

log = structlog.get_logger(__name__)

class DefaultChunkerAdapter(ChunkingPort):
    """
    Adaptador de chunking por defecto, basado en división por palabras y solapamiento.
    Reutiliza la lógica de text_splitter.py del ingest-service.
    """

    def chunk_text(
        self,
        text_content: str,
        chunk_size: int, # Parameter passed from use case, originating from request or service default
        chunk_overlap: int
    ) -> List[str]:
        if not text_content or text_content.isspace():
            log.debug("DefaultChunkerAdapter: Empty or whitespace-only text provided, returning no chunks.")
            return []
        
        if chunk_size <= 0:
            raise ChunkingError(f"Chunk size must be positive. Received: {chunk_size}")
        if chunk_overlap < 0:
            raise ChunkingError(f"Chunk overlap must be non-negative. Received: {chunk_overlap}")
        if chunk_overlap >= chunk_size:
            raise ChunkingError(f"Chunk overlap ({chunk_overlap}) must be less than chunk size ({chunk_size}).")

        log.debug("DefaultChunkerAdapter: Splitting text into chunks", 
                  text_length=len(text_content), 
                  chunk_size=chunk_size, 
                  chunk_overlap=chunk_overlap)
        
        # Lógica de text_splitter.py (adaptada)
        # Asumimos que chunk_size y chunk_overlap se refieren a número de palabras.
        # Si se refiere a caracteres, la lógica debería ajustarse.
        # Por simplicidad y para coincidir con el text_splitter original, usamos palabras.
        
        words = text_content.split() # Split by whitespace
        if not words:
            log.debug("DefaultChunkerAdapter: Text content resulted in no words after split.")
            return []

        chunks: List[str] = []
        current_pos = 0
        
        while current_pos < len(words):
            end_pos = min(current_pos + chunk_size, len(words))
            chunk_words = words[current_pos:end_pos]
            chunks.append(" ".join(chunk_words))
            
            if end_pos == len(words): # Reached the end
                break
            
            current_pos += (chunk_size - chunk_overlap)
            if current_pos >= len(words): # Prevent infinite loop if step is too small or overlap too large making step 0 or negative.
                # This should not happen if overlap < size.
                log.warning("DefaultChunkerAdapter: Chunking step led to no progress, breaking loop.", current_pos=current_pos, num_words=len(words))
                break
        
        log.info("DefaultChunkerAdapter: Text split into chunks", num_chunks=len(chunks))
        return chunks