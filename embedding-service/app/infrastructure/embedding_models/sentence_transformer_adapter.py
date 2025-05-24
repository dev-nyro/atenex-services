# embedding-service/app/infrastructure/embedding_models/sentence_transformer_adapter.py
import structlog
from typing import List, Tuple, Dict, Any, Optional
import asyncio
import time
import os
import numpy as np



from app.application.ports.embedding_model_port import EmbeddingModelPort
from app.core.config import settings 
from app.api.v1.schemas import TextType 

log = structlog.get_logger(__name__)

class SentenceTransformerAdapter(EmbeddingModelPort):
    _model: Optional[Any] = None  # type: ignore  # SentenceTransformer is imported locally
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
        # Import sentence_transformers and torch only when this adapter is instantiated
        global SentenceTransformer, torch
        try:
            from sentence_transformers import SentenceTransformer
            import torch
        except ImportError as e:
            log.critical("sentence_transformers or torch not installed. This adapter requires them.", error=str(e))
            raise
        self._model_name = settings.ST_MODEL_NAME
        self._model_dimension = settings.EMBEDDING_DIMENSION 
        self._device = settings.ST_MODEL_DEVICE 
        self._cache_dir = settings.ST_HF_CACHE_DIR
        self._batch_size = settings.ST_BATCH_SIZE
        self._normalize_embeddings = settings.ST_NORMALIZE_EMBEDDINGS
        
        log.info(
            "SentenceTransformerAdapter instance created (model not loaded yet)",
            configured_model_name=self._model_name,
            target_dimension=self._model_dimension,
            initial_configured_device=self._device, 
            batch_size=self._batch_size,
            normalize=self._normalize_embeddings,
        )

    async def initialize_model(self):
        if self._model_loaded:
            log.debug("SentenceTransformer model already initialized.", model_name=self._model_name)
            return

        init_log = log.bind(adapter="SentenceTransformerAdapter", action="initialize_model", model_name=self._model_name)
        
        
        final_device = self._device
        self._use_fp16 = False 
        if self._device.startswith("cuda"):
            if torch.cuda.is_available():
                init_log.info(f"CUDA is available. Checking capability for device: {self._device}")
                
                try:
                    current_cuda_device_for_check = torch.device(final_device)
                    major, _ = torch.cuda.get_device_capability(current_cuda_device_for_check)
                    init_log.info(f"CUDA device '{final_device}' capability: {major}.x")
                    if major >= 7: 
                        self._use_fp16 = settings.ST_USE_FP16
                        init_log.info(f"FP16 support confirmed. ST_USE_FP16: {self._use_fp16}.")
                    else:
                        init_log.warning(f"CUDA device '{final_device}' may have limited FP16 support. ST_USE_FP16 set to False.")
                        self._use_fp16 = False
                except Exception as e_cap:
                    init_log.error("Error getting CUDA device capability. Defaulting to CPU and no FP16.", error=str(e_cap), configured_device=self._device, exc_info=True)
                    final_device = "cpu"
                    self._use_fp16 = False
            else: 
                init_log.warning(f"ST_MODEL_DEVICE configured as '{self._device}' but CUDA is not available. Falling back to CPU.")
                final_device = "cpu"
                self._use_fp16 = False
        
        
        self._device = final_device
        init_log = init_log.bind(effective_device=self._device, use_fp16=self._use_fp16) 
        init_log.info("Initializing SentenceTransformer model with effective device settings...")
        
        start_time = time.perf_counter()
        
        try:
            if self._cache_dir:
                os.makedirs(self._cache_dir, exist_ok=True)
                init_log.info(f"Using HuggingFace cache directory: {self._cache_dir}")

            self._model = await asyncio.to_thread(
                SentenceTransformer,
                model_name_or_path=self._model_name,
                device=self._device, 
                cache_folder=self._cache_dir
            )

            if self._use_fp16 and self._device.startswith("cuda"): 
                init_log.info("Converting model to FP16 for CUDA device.")
                self._model = self._model.half() 

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

    async def embed_texts(self, texts: List[str], text_type: TextType = "passage") -> List[List[float]]:
        if not self._model_loaded or not self._model:
            log.error("SentenceTransformer model not loaded. Cannot generate embeddings.", model_error=self._model_load_error)
            raise ConnectionError(f"SentenceTransformer model is not available. Load error: {self._model_load_error}")

        if not texts:
            return []

        embed_log = log.bind(adapter="SentenceTransformerAdapter", action="embed_texts", num_texts=len(texts), text_type=text_type)
        
        if text_type == "query":
            prefix = "query: "
        elif text_type == "passage":
            prefix = "passage: "
        else: 
            log.warning("Unknown text_type received, defaulting to 'passage: ' prefix.", received_type=text_type)
            prefix = "passage: "
            
        prefixed_texts = [f"{prefix}{text}" for text in texts]
        embed_log.debug(f"Generating embeddings with SentenceTransformer using prefix '{prefix}'.")


        try:
            embeddings_array: np.ndarray = await asyncio.to_thread(
                self._model.encode, 
                sentences=prefixed_texts,
                batch_size=self._batch_size,
                convert_to_tensor=False, 
                convert_to_numpy=True,
                show_progress_bar=False, 
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
            "dimension": self._model_dimension, 
            "device": self._device, 
            "provider": "sentence_transformer"
        }

    async def health_check(self) -> Tuple[bool, str]:
        if self._model_loaded and self._model:
            try:
                _ = await asyncio.to_thread(
                    self._model.encode, 
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