# embedding-service/app/infrastructure/embedding_models/instructor_adapter.py
import structlog
import asyncio
import time
import os
from typing import List, Tuple, Dict, Any, Optional
import numpy as np

# LLM_FLAG: INSTRUCTOR_IMPORTS_START
# Defer import until initialization or if INSTRUCTOR is active provider
_InstructorEmbedding_imported = False
_torch_imported_for_instructor = False
try:
    if os.getenv("EMBEDDING_ACTIVE_EMBEDDING_PROVIDER", "").lower() == "instructor":
        from InstructorEmbedding import INSTRUCTOR as InstructorModelClass # Renamed for clarity
        _InstructorEmbedding_imported = True
        import torch
        _torch_imported_for_instructor = True
except ImportError as e:
    # This warning will also be logged by config loader if torch itself fails globally
    structlog.get_logger(__name__).warning(
        "InstructorEmbedding or torch not installed. InstructorAdapter will fail if used.",
        error=str(e)
    )
# LLM_FLAG: INSTRUCTOR_IMPORTS_END


from app.application.ports.embedding_model_port import EmbeddingModelPort
from app.core.config import settings

log = structlog.get_logger(__name__)

class InstructorAdapter(EmbeddingModelPort):
    _model: Optional[Any] = None # InstructorModelClass
    _model_name: str
    _model_dimension: int # This comes from global settings.EMBEDDING_DIMENSION
    _device: str
    _cache_dir: Optional[str]
    _batch_size: int
    _normalize_embeddings: bool

    _model_loaded: bool = False
    _model_load_error: Optional[str] = None

    def __init__(self):
        global _InstructorEmbedding_imported, _torch_imported_for_instructor
        if not _InstructorEmbedding_imported:
            log.critical("InstructorEmbedding library not found. Please install it to use InstructorAdapter: 'poetry install --with st'")
            # This adapter might be instantiated even if not active, so don't raise error here,
            # but initialization will fail if it's the active provider.
        if not _torch_imported_for_instructor:
            log.critical("PyTorch library not found. Please install it to use InstructorAdapter.")


        self._model_name = settings.INSTRUCTOR_MODEL_NAME
        self._model_dimension = settings.EMBEDDING_DIMENSION # Validated in config.py
        self._device = settings.INSTRUCTOR_MODEL_DEVICE # Validated in config.py for CUDA availability
        self._cache_dir = settings.INSTRUCTOR_HF_CACHE_DIR
        self._batch_size = settings.INSTRUCTOR_BATCH_SIZE
        self._normalize_embeddings = settings.INSTRUCTOR_NORMALIZE_EMBEDDINGS
        
        log.info(
            "InstructorAdapter instance created (model not loaded yet)",
            configured_model_name=self._model_name,
            target_dimension=self._model_dimension,
            initial_configured_device=self._device,
            batch_size=self._batch_size,
            normalize=self._normalize_embeddings,
            cache_dir=self._cache_dir
        )

    async def initialize_model(self):
        if self._model_loaded:
            log.debug("INSTRUCTOR model already initialized.", model_name=self._model_name)
            return

        if not _InstructorEmbedding_imported or not _torch_imported_for_instructor:
            self._model_load_error = "InstructorEmbedding library or PyTorch not available."
            log.critical(self._model_load_error)
            raise ConnectionError(self._model_load_error)

        init_log = log.bind(adapter="InstructorAdapter", action="initialize_model", model_name=self._model_name, device=self._device)
        init_log.info("Initializing INSTRUCTOR model...")
        
        start_time = time.perf_counter()
        
        try:
            if self._cache_dir:
                os.makedirs(self._cache_dir, exist_ok=True)
                init_log.info(f"Using HuggingFace cache directory for INSTRUCTOR: {self._cache_dir}")

            # INSTRUCTOR model loading is synchronous, run in thread pool
            self._model = await asyncio.to_thread(
                InstructorModelClass, # Use the imported and renamed class
                model_name_or_path=self._model_name,
                device=self._device,
                cache_folder=self._cache_dir
            )

            # Test embedding to confirm dimension (must match settings.EMBEDDING_DIMENSION)
            # Example instruction pair based on INSTRUCTOR usage
            test_instruction_pair = [["Represent the test query for model validation:", "hello world"]]
            test_vector_array = await asyncio.to_thread(
                self._model.encode,
                test_instruction_pair,
                batch_size=1,
                normalize_embeddings=self._normalize_embeddings,
                # convert_to_numpy=True # Default is numpy array
            )
            
            if not isinstance(test_vector_array, np.ndarray) or test_vector_array.ndim != 2:
                 raise ValueError(f"Test embedding with INSTRUCTOR failed or returned unexpected type: {type(test_vector_array)}")

            actual_dim = test_vector_array.shape[1]

            if actual_dim != self._model_dimension:
                self._model_load_error = (
                    f"INSTRUCTOR Model dimension mismatch. Global EMBEDDING_DIMENSION is {self._model_dimension}, "
                    f"but model '{self._model_name}' on device '{self._device}' produced {actual_dim} dimensions."
                )
                init_log.error(self._model_load_error)
                self._model = None
                self._model_loaded = False
                raise ValueError(self._model_load_error)

            self._model_loaded = True
            self._model_load_error = None
            duration_ms = (time.perf_counter() - start_time) * 1000
            init_log.info("INSTRUCTOR model initialized and validated successfully.", duration_ms=duration_ms, actual_dimension=actual_dim)

        except Exception as e:
            self._model_load_error = f"Failed to load INSTRUCTOR model '{self._model_name}': {str(e)}"
            init_log.critical(self._model_load_error, exc_info=True)
            self._model = None
            self._model_loaded = False
            raise ConnectionError(self._model_load_error) from e

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        # text_type and task_instruction are not part of the port's method signature.
        # This adapter will use a default instruction strategy.
        # If customization is needed per request, the port and use_case would need changes.
        if not self._model_loaded or not self._model:
            log.error("INSTRUCTOR model not loaded. Cannot generate embeddings.", model_error=self._model_load_error)
            raise ConnectionError(f"INSTRUCTOR model is not available. Load error: {self._model_load_error}")

        if not texts:
            return []

        # Default instruction strategy: Use "Represent the passage:" for all texts.
        # This is a common generic instruction. For specific tasks (query, document),
        # this could be made configurable or more intelligent if requirements evolve.
        default_instruction_prefix = "Represent the general text for embedding: "
        
        # Create instruction-text pairs
        # Format: [[instruction, text], [instruction, text], ...]
        instruction_text_pairs = [[default_instruction_prefix, text] for text in texts]

        embed_log = log.bind(adapter="InstructorAdapter", action="embed_texts", num_texts=len(texts), num_pairs=len(instruction_text_pairs))
        embed_log.debug("Generating embeddings with INSTRUCTOR...")

        try:
            # .encode is CPU/GPU-bound; run in thread pool
            embeddings_array: np.ndarray = await asyncio.to_thread(
                self._model.encode,
                instruction_text_pairs,
                batch_size=self._batch_size,
                normalize_embeddings=self._normalize_embeddings,
                show_progress_bar=False, # Typically false for server-side
                # convert_to_numpy=True # is default
            )
            
            embeddings_list = embeddings_array.tolist()
            embed_log.debug("INSTRUCTOR embeddings generated successfully.")
            return embeddings_list
        except Exception as e:
            embed_log.exception("Error during INSTRUCTOR embedding process")
            raise RuntimeError(f"INSTRUCTOR embedding generation failed: {e}") from e

    def get_model_info(self) -> Dict[str, Any]:
        return {
            "model_name": self._model_name,
            "dimension": self._model_dimension,
            "device": self._device, # Effective device after validation
            "provider": "instructor"
        }

    async def health_check(self) -> Tuple[bool, str]:
        if self._model_loaded and self._model:
            try:
                # Perform a quick test encode
                test_pair = [["Represent the health check query:", "ping"]]
                _ = await asyncio.to_thread(
                    self._model.encode,
                    test_pair,
                    batch_size=1,
                    normalize_embeddings=self._normalize_embeddings
                )
                return True, f"INSTRUCTOR model '{self._model_name}' on '{self._device}' loaded and responsive."
            except Exception as e:
                log.error("INSTRUCTOR model health check failed during test embedding", error=str(e), exc_info=True)
                return False, f"INSTRUCTOR model '{self._model_name}' loaded but unresponsive: {str(e)}"
        elif self._model_load_error:
            return False, f"INSTRUCTOR model '{self._model_name}' failed to load: {self._model_load_error}"
        else:
            return False, f"INSTRUCTOR model '{self._model_name}' not loaded (or library missing)."