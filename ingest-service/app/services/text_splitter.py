import re
from typing import List
import structlog
from app.core.config import settings

log = structlog.get_logger(__name__)

CHUNK_SIZE = getattr(settings, 'SPLITTER_CHUNK_SIZE', 1000)
CHUNK_OVERLAP = getattr(settings, 'SPLITTER_CHUNK_OVERLAP', 250)

def split_text(text: str) -> List[str]:
    """
    Divide el texto en chunks de tamaño CHUNK_SIZE con solapamiento CHUNK_OVERLAP.
    """
    log.debug("Splitting text into chunks", chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + CHUNK_SIZE, len(words))
        chunk = " ".join(words[start:end])
        chunks.append(chunk)
        if end == len(words):
            break
        start += CHUNK_SIZE - CHUNK_OVERLAP
    log.info("Text split into chunks", num_chunks=len(chunks))
    return chunks
