# embedding-service/app/infrastructure/embedding_models/fastembed_adapter.py
import structlog
from typing import List, Tuple, Dict, Any, Optional
import asyncio
import time

# CORREGIDO: Importar solo TextEmbedding o lo que sea necesario de la API actual de fastembed
from fastembed import TextEmbedding # Qdrant/FastEmbed
# DefaultEmbedding y EmbeddingModel ya no parecen ser parte de la API pública
# de las versiones recientes de fastembed (>=0.3.0)

from app.application.ports.embedding_model_port import EmbeddingModelPort
from app.core.config import settings

log = structlog.get_logger(__name__)

class FastEmbedAdapter(EmbeddingModelPort):
    """
    Adapter for FastEmbed library.
    """
    _model: Optional[TextEmbedding] = None # FastEmbed's TextEmbedding instance
    _model_name: str
    _model_dimension: int
    _model_loaded: bool = False
    _model_load_error: Optional[str] = None

    def __init__(self):
        self._model_name = settings.FASTEMBED_MODEL_NAME
        self._model_dimension = settings.EMBEDDING_DIMENSION
        # Model loading is deferred to an async method, typically called during startup.

    async def initialize_model(self):
        """
        Initializes and loads the FastEmbed model.
        This should be called during service startup (e.g., lifespan).
        """
        if self._model_loaded:
            log.debug("FastEmbed model already initialized.", model_name=self._model_name)
            return

        init_log = log.bind(adapter="FastEmbedAdapter", action="initialize_model", model_name=self._model_name)
        init_log.info("Initializing FastEmbed model...")
        start_time = time.perf_counter()
        try:
            self._model = await asyncio.to_thread(
                TextEmbedding, # Usar la clase principal TextEmbedding
                model_name=self._model_name,
                cache_dir=settings.FASTEMBED_CACHE_DIR,
                threads=settings.FASTEMBED_THREADS,
                max_length=settings.FASTEMBED_MAX_LENGTH,
            )
            # Perform a test embedding to confirm dimension and successful loading
            # FastEmbed.embed() devuelve un generador de numpy arrays
            test_embeddings_generator = self._model.embed(["test vector"])
            test_embeddings = list(test_embeddings_generator) # Convertir el generador a lista

            if not test_embeddings or not test_embeddings[0].any(): # .any() para numpy array
                raise ValueError("Test embedding failed or returned empty result.")

            actual_dim = len(test_embeddings[0])
            if actual_dim != self._model_dimension:
                self._model_load_error = (
                    f"Model dimension mismatch. Expected {self._model_dimension}, "
                    f"got {actual_dim} for model {self._model_name}."
                )
                init_log.error(self._model_load_error)
                self._model = None # Ensure model is not used
                raise ValueError(self._model_load_error)

            self._model_loaded = True
            self._model_load_error = None
            duration_ms = (time.perf_counter() - start_time) * 1000
            init_log.info("FastEmbed model initialized and validated successfully.", duration_ms=duration_ms, dimension=actual_dim)

        except Exception as e:
            self._model_load_error = f"Failed to load FastEmbed model '{self._model_name}': {str(e)}"
            init_log.critical(self._model_load_error, exc_info=True)
            self._model = None
            self._model_loaded = False
            raise ConnectionError(self._model_load_error) from e


    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not self._model_loaded or not self._model:
            log.error("FastEmbed model not loaded. Cannot generate embeddings.", model_error=self._model_load_error)
            raise ConnectionError("Embedding model is not available.")

        embed_log = log.bind(adapter="FastEmbedAdapter", action="embed_texts", num_texts=len(texts))
        embed_log.debug("Generating embeddings...")
        try:
            embeddings_generator = await asyncio.to_thread(self._model.embed, texts, batch_size=128)
            embeddings_list = [emb.tolist() for emb in embeddings_generator] # Convertir numpy arrays a listas

            embed_log.debug("Embeddings generated successfully.")
            return embeddings_list
        except Exception as e:
            embed_log.exception("Error during FastEmbed embedding process")
            raise RuntimeError(f"Embedding generation failed: {e}") from e

    def get_model_info(self) -> Dict[str, Any]:
        return {
            "model_name": self._model_name,
            "dimension": self._model_dimension,
        }

    async def health_check(self) -> Tuple[bool, str]:
        if self._model_loaded and self._model:
            try:
                _ = list(self._model.embed(["health check"], batch_size=1)) # Convertir generador a lista
                return True, "Model loaded and responsive."
            except Exception as e:
                log.error("Model health check failed during test embedding", error=str(e))
                return False, f"Model loaded but unresponsive: {str(e)}"
        elif self._model_load_error:
            return False, f"Model failed to load: {self._model_load_error}"
        else:
            return False, "Model not loaded."