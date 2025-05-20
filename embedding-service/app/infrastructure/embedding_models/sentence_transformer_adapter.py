# embedding-service/app/infrastructure/embedding_models/sentence_transformer_adapter.py
import structlog
from typing import List, Tuple, Dict, Any, Optional
import asyncio
import time
import os
import numpy as np

from sentence_transformers import SentenceTransformer
from sentence_transformers.util import semantic_search
import torch

from app.application.ports.embedding_model_port import EmbeddingModelPort
from app.core.config import settings # Import global settings

log = structlog.get_logger(__name__)

class SentenceTransformerAdapter(EmbeddingModelPort):
    """
    Adapter for SentenceTransformer models (e.g., E5).
    """
    _model: Optional[SentenceTransformer] = None
    _model_name: str
    _model_dimension: int
    _device: str
    _cache_dir: Optional[str]
    _batch_size: int
    _normalize_embeddings: bool
    _use_fp16: bool

    _model_loaded: bool = False
    _model_load_error: Optional[str] = None

    def __init__(self):
        self._model_name = settings.ST_MODEL_NAME
        self._model_dimension = settings.EMBEDDING_DIMENSION # This comes from global, validated config
        self._device = settings.ST_MODEL_DEVICE
        self._cache_dir = settings.ST_HF_CACHE_DIR
        self._batch_size = settings.ST_BATCH_SIZE
        self._normalize_embeddings = settings.ST_NORMALIZE_EMBEDDINGS
        
        # Determine if FP16 should be used
        self._use_fp16 = False
        if self._device.startswith("cuda"):
            if torch.cuda.is_available():
                # Basic check for FP16 support (Ampere and newer generally good)
                major, _ = torch.cuda.get_device_capability(torch.device(self._device))
                if major >= 7: # Volta, Turing, Ampere, Hopper etc.
                    self._use_fp16 = settings.ST_USE_FP16
                    log.info(f"CUDA device '{self._device}' supports FP16. ST_USE_FP16: {self._use_fp16}.")
                else:
                    log.warning(f"CUDA device '{self._device}' (capability {major}.x) may have limited FP16 support. ST_USE_FP16 set to False.")
                    self._use_fp16 = False # Force False if older GPU
            else: # CUDA specified but not available
                log.warning(f"ST_MODEL_DEVICE set to '{self._device}' but CUDA is not available. Falling back to CPU.")
                self._device = "cpu"
                self._use_fp16 = False # FP16 is for CUDA
        
        log.info(
            "SentenceTransformerAdapter initialized",
            configured_model_name=self._model_name,
            target_dimension=self._model_dimension,
            device=self._device,
            use_fp16=self._use_fp16,
            batch_size=self._batch_size,
            normalize=self._normalize_embeddings,
        )

    async def initialize_model(self):
        if self._model_loaded:
            log.debug("SentenceTransformer model already initialized.", model_name=self._model_name)
            return

        init_log = log.bind(adapter="SentenceTransformerAdapter", action="initialize_model", model_name=self._model_name, device=self._device)
        init_log.info("Initializing SentenceTransformer model...")
        start_time = time.perf_counter()
        
        try:
            # Ensure cache directory exists if specified
            if self._cache_dir:
                os.makedirs(self._cache_dir, exist_ok=True)
                init_log.info(f"Using HuggingFace cache directory: {self._cache_dir}")

            # SentenceTransformer loading is synchronous
            self._model = await asyncio.to_thread(
                SentenceTransformer,
                model_name_or_path=self._model_name,
                device=self._device,
                cache_folder=self._cache_dir
            )

            if self._use_fp16 and self._device.startswith("cuda"):
                init_log.info("Converting model to FP16 for CUDA device.")
                self._model = self._model.half() # type: ignore

            # Perform a test embedding to confirm dimension
            test_vector = self._model.encode(["test vector"], normalize_embeddings=self._normalize_embeddings)
            actual_dim = test_vector.shape[1]

            if actual_dim != self._model_dimension:
                self._model_load_error = (
                    f"SentenceTransformer Model dimension mismatch. Global EMBEDDING_DIMENSION is {self._model_dimension}, "
                    f"but model '{self._model_name}' produced {actual_dim} dimensions."
                )
                init_log.error(self._model_load_error)
                self._model = None
                self._model_loaded = False
                raise ValueError(self._model_load_error)

            self._model_loaded = True
            self._model_load_error = None
            duration_ms = (time.perf_counter() - start_time) * 1000
            init_log.info("SentenceTransformer model initialized and validated successfully.", duration_ms=duration_ms, actual_dimension=actual_dim)

        except Exception as e:
            self._model_load_error = f"Failed to load SentenceTransformer model '{self._model_name}': {str(e)}"
            init_log.critical(self._model_load_error, exc_info=True)
            self._model = None
            self._model_loaded = False
            raise ConnectionError(self._model_load_error) from e

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not self._model_loaded or not self._model:
            log.error("SentenceTransformer model not loaded. Cannot generate embeddings.", model_error=self._model_load_error)
            raise ConnectionError(f"SentenceTransformer model is not available. Load error: {self._model_load_error}")

        if not texts:
            return []

        embed_log = log.bind(adapter="SentenceTransformerAdapter", action="embed_texts", num_texts=len(texts))
        embed_log.debug("Generating embeddings with SentenceTransformer...")

        # E5 models expect "query: " or "passage: " prefix.
        # This service primarily embeds document chunks, so "passage: " is appropriate.
        # A more robust solution might involve the client specifying text_type.
        prefixed_texts = [f"passage: {text}" for text in texts]

        try:
            # model.encode is CPU/GPU-bound, run in thread pool if called from async context
            # However, SentenceTransformer handles its own parallelism with PyTorch.
            # For FastAPI, direct await on a threadpool call is good practice.
            
            # The encode method handles device placement and FP16 if model is configured.
            embeddings_array: np.ndarray = await asyncio.to_thread(
                self._model.encode, # type: ignore
                sentences=prefixed_texts,
                batch_size=self._batch_size,
                convert_to_tensor=False, # Get NumPy array
                convert_to_numpy=True,
                show_progress_bar=False, # Disable for server logs
                normalize_embeddings=self._normalize_embeddings,
            )
            
            embeddings_list = embeddings_array.tolist()
            embed_log.debug("SentenceTransformer embeddings generated successfully.")
            return embeddings_list
        except Exception as e:
            embed_log.exception("Error during SentenceTransformer embedding process")
            raise RuntimeError(f"SentenceTransformer embedding generation failed: {e}") from e

    def get_model_info(self) -> Dict[str, Any]:
        return {
            "model_name": self._model_name,
            "dimension": self._model_dimension, # This is the validated, final dimension
            "device": self._device,
            "provider": "sentence_transformer"
        }

    async def health_check(self) -> Tuple[bool, str]:
        if self._model_loaded and self._model:
            try:
                # Test with a short text
                _ = await asyncio.to_thread(
                    self._model.encode, #type: ignore
                    ["health check"], 
                    batch_size=1, 
                    normalize_embeddings=self._normalize_embeddings
                )
                return True, f"SentenceTransformer model '{self._model_name}' on '{self._device}' loaded and responsive."
            except Exception as e:
                log.error("SentenceTransformer model health check failed during test embedding", error=str(e), exc_info=True)
                return False, f"SentenceTransformer model '{self._model_name}' loaded but unresponsive: {str(e)}"
        elif self._model_load_error:
            return False, f"SentenceTransformer model '{self._model_name}' failed to load: {self._model_load_error}"
        else:
            return False, f"SentenceTransformer model '{self._model_name}' not loaded."