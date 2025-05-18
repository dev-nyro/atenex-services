# embedding-service/app/infrastructure/embedding_models/fastembed_adapter.py
import structlog
from typing import List, Tuple, Dict, Any, Optional
import asyncio
import time

from fastembed import TextEmbedding 

from app.application.ports.embedding_model_port import EmbeddingModelPort
from app.core.config import settings

log = structlog.get_logger(__name__)

class FastEmbedAdapter(EmbeddingModelPort):
    """
    Adapter for FastEmbed library.
    Note: This adapter is currently not the primary one used by default.
    """
    _model: Optional[TextEmbedding] = None
    _model_name: str
    _model_dimension: int # This will be set from global settings.EMBEDDING_DIMENSION
    _model_loaded: bool = False
    _model_load_error: Optional[str] = None

    def __init__(self):
        self._model_name = settings.FASTEMBED_MODEL_NAME
        # IMPORTANT: This adapter will use the global EMBEDDING_DIMENSION from settings.
        # If settings.EMBEDDING_DIMENSION is configured for OpenAI (e.g., 1536),
        # and this FastEmbed model (e.g., all-MiniLM-L6-v2 -> 384) has a different dimension,
        # the health check/validation within initialize_model WILL LIKELY FAIL.
        # This is expected if the service is globally configured for a different provider like OpenAI.
        self._model_dimension = settings.EMBEDDING_DIMENSION
        log.info("FastEmbedAdapter initialized", configured_model_name=self._model_name, expected_dimension_from_global_settings=self._model_dimension)


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
            # TextEmbedding initialization is synchronous
            self._model = TextEmbedding(
                model_name=self._model_name,
                cache_dir=settings.FASTEMBED_CACHE_DIR,
                threads=settings.FASTEMBED_THREADS,
                max_length=settings.FASTEMBED_MAX_LENGTH,
            )
            
            # Perform a test embedding to confirm dimension and successful loading
            # The .embed() method is CPU-bound, so run in a thread pool if called from async context
            test_embeddings_generator = await asyncio.to_thread(self._model.embed, ["test vector"])
            test_embeddings_list = list(test_embeddings_generator) 

            if not test_embeddings_list or not test_embeddings_list[0].any():
                raise ValueError("Test embedding with FastEmbed failed or returned empty result.")

            actual_dim = len(test_embeddings_list[0])
            
            # Validate against the dimension this adapter was initialized with (from global settings)
            if actual_dim != self._model_dimension:
                self._model_load_error = (
                    f"FastEmbed Model dimension mismatch. Global EMBEDDING_DIMENSION is {self._model_dimension}, "
                    f"but FastEmbed model '{self._model_name}' produced {actual_dim} dimensions."
                )
                init_log.error(self._model_load_error)
                self._model = None 
                self._model_loaded = False # Ensure model is not marked as loaded
                # This exception will be caught and handled by the main startup logic
                raise ValueError(self._model_load_error) 

            self._model_loaded = True
            self._model_load_error = None
            duration_ms = (time.perf_counter() - start_time) * 1000
            init_log.info("FastEmbed model initialized and validated successfully.", duration_ms=duration_ms, actual_dimension=actual_dim)

        except Exception as e:
            self._model_load_error = f"Failed to load FastEmbed model '{self._model_name}': {str(e)}"
            init_log.critical(self._model_load_error, exc_info=True)
            self._model = None
            self._model_loaded = False
            # Propagate as ConnectionError to indicate a critical startup failure for this adapter
            raise ConnectionError(self._model_load_error) from e


    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not self._model_loaded or not self._model:
            log.error("FastEmbed model not loaded. Cannot generate embeddings.", model_error=self._model_load_error)
            raise ConnectionError(f"FastEmbed model is not available. Load error: {self._model_load_error}")

        if not texts:
            return []

        embed_log = log.bind(adapter="FastEmbedAdapter", action="embed_texts", num_texts=len(texts))
        embed_log.debug("Generating embeddings with FastEmbed...")
        try:
            # FastEmbed's embed method is CPU-bound; run in thread pool.
            embeddings_generator = await asyncio.to_thread(self._model.embed, texts, batch_size=128)
            embeddings_list = [emb.tolist() for emb in embeddings_generator]

            embed_log.debug("FastEmbed embeddings generated successfully.")
            return embeddings_list
        except Exception as e:
            embed_log.exception("Error during FastEmbed embedding process")
            raise RuntimeError(f"FastEmbed embedding generation failed: {e}") from e

    def get_model_info(self) -> Dict[str, Any]:
        # Returns the dimension this adapter *expects* based on global config,
        # and the model name it's configured to use.
        # Actual dimension is validated during init.
        return {
            "model_name": self._model_name,
            "dimension": self._model_dimension, 
        }

    async def health_check(self) -> Tuple[bool, str]:
        if self._model_loaded and self._model:
            try:
                # Test with a short text
                test_embeddings_generator = await asyncio.to_thread(self._model.embed, ["health check"], batch_size=1)
                _ = list(test_embeddings_generator) 
                return True, f"FastEmbed model '{self._model_name}' loaded and responsive."
            except Exception as e:
                log.error("FastEmbed model health check failed during test embedding", error=str(e), exc_info=True)
                return False, f"FastEmbed model '{self._model_name}' loaded but unresponsive: {str(e)}"
        elif self._model_load_error:
            return False, f"FastEmbed model '{self._model_name}' failed to load: {self._model_load_error}"
        else:
            return False, f"FastEmbed model '{self._model_name}' not loaded."