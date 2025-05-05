# query-service/app/infrastructure/rerankers/bge_reranker.py
import structlog
import asyncio
from typing import List, Optional, Tuple
import time

# LLM_REFACTOR_STEP_2: Use sentence-transformers for reranking
try:
    from sentence_transformers.cross_encoder import CrossEncoder
except ImportError:
    CrossEncoder = None # Handle optional dependency

from app.application.ports.retrieval_ports import RerankerPort
from app.domain.models import RetrievedChunk
from app.core.config import settings # To potentially get model name/path

log = structlog.get_logger(__name__)

# LLM_REFACTOR_STEP_4: Add config setting for reranker model
DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-base" # Default BGE reranker

class BGEReranker(RerankerPort):
    """
    Implementación de RerankerPort usando BAAI BGE Reranker
    con sentence-transformers.
    """
    def __init__(self, model_name_or_path: Optional[str] = None, device: Optional[str] = None):
        if CrossEncoder is None:
            log.error("sentence-transformers library not installed. Reranking is disabled. "
                      "Install with: poetry add sentence-transformers")
            raise ImportError("sentence-transformers library is required for BGEReranker.")

        self.model_name = model_name_or_path or settings.RERANKER_MODEL_NAME # Usa config o default
        self.device = device # Auto-detect if None ('cuda', 'mps', 'cpu')
        self.model: Optional[CrossEncoder] = None
        self._load_model() # Cargar modelo en la inicialización

    def _load_model(self):
        """Carga el modelo CrossEncoder."""
        init_log = log.bind(adapter="BGEReranker", action="load_model", model_name=self.model_name, device=self.device or "auto")
        init_log.info("Initializing BGE Reranker model...")
        start_time = time.time()
        try:
            # max_length puede necesitar ajuste basado en el modelo y longitud esperada de query+chunk
            self.model = CrossEncoder(self.model_name, max_length=512, device=self.device)
            load_time = time.time() - start_time
            init_log.info("BGE Reranker model loaded successfully.", duration_ms=load_time * 1000, effective_device=self.model.device)
        except Exception as e:
            init_log.exception("Failed to load BGE Reranker model")
            self.model = None # Ensure model is None if loading fails
            # No levantar error aquí necesariamente, podría intentarse de nuevo o fallar en rerank

    async def rerank(self, query: str, chunks: List[RetrievedChunk]) -> List[RetrievedChunk]:
        """Reordena chunks usando el modelo BGE CrossEncoder."""
        rerank_log = log.bind(adapter="BGEReranker", action="rerank", num_chunks=len(chunks))

        if not self.model:
             rerank_log.error("Reranker model not loaded. Cannot rerank.")
             # Devolver los chunks sin reordenar si el modelo no cargó
             return chunks

        if not chunks:
            rerank_log.debug("No chunks provided to rerank.")
            return []

        # Crear pares (query, chunk_content) para el modelo
        # Filtrar chunks sin contenido aquí por si acaso
        sentence_pairs: List[Tuple[str, str]] = []
        chunks_with_content: List[RetrievedChunk] = []
        for chunk in chunks:
            if chunk.content:
                sentence_pairs.append((query, chunk.content))
                chunks_with_content.append(chunk)
            else:
                rerank_log.warning("Skipping chunk during reranking due to missing content", chunk_id=chunk.id)

        if not sentence_pairs:
            rerank_log.warning("No chunks with content found to rerank.")
            return []

        rerank_log.debug(f"Reranking {len(sentence_pairs)} query-chunk pairs...")
        start_time = time.time()

        try:
            # Ejecutar predicción en thread separado ya que puede ser intensivo en CPU/GPU
            # El modelo de SBERT ya maneja batching interno si le pasas una lista
            scores = await asyncio.to_thread(self.model.predict, sentence_pairs, show_progress_bar=False) # Deshabilitar barra si corre en servidor
            predict_time = time.time() - start_time
            rerank_log.debug("Reranker prediction complete.", duration_ms=predict_time * 1000)

            # Asociar scores con los chunks originales (que tenían contenido)
            scored_chunks = list(zip(scores, chunks_with_content))

            # Ordenar los chunks por score descendente
            scored_chunks.sort(key=lambda x: x[0], reverse=True)

            # Extraer los chunks reordenados
            reranked_chunks = [chunk for score, chunk in scored_chunks]

            # Actualizar el score en los chunks reordenados (opcional)
            # for score, chunk in scored_chunks:
            #     chunk.score = float(score) # Sobrescribir score original con el del reranker

            rerank_log.info(f"Reranked {len(reranked_chunks)} chunks successfully.")
            return reranked_chunks

        except Exception as e:
            rerank_log.exception("Error during BGE reranking")
            # Devolver los chunks originales sin reordenar en caso de error
            return chunks_with_content # Devolver los que tenían contenido