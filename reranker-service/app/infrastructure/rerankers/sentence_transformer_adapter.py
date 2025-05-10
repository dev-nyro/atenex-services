# reranker-service/app/infrastructure/rerankers/sentence_transformer_adapter.py
import asyncio
from typing import List, Tuple, Optional
from sentence_transformers import CrossEncoder # type: ignore
import structlog
import time
import os # For Hugging Face cache directory environment variable

from app.application.ports.reranker_model_port import RerankerModelPort
from app.domain.models import DocumentToRerank, RerankedDocument
from app.core.config import settings

logger = structlog.get_logger(__name__)

class SentenceTransformerRerankerAdapter(RerankerModelPort):
    """
    Adapter for sentence-transformers CrossEncoder models.
    Manages model loading and prediction.
    The model instance and status are class-level to act as a singleton
    managed by the lifespan.
    """
    _model: Optional[CrossEncoder] = None
    _model_name_loaded: Optional[str] = None
    _model_status: str = "unloaded" # States: unloaded, loading, loaded, error

    def __init__(self):
        # Initialization logic (if any beyond model loading) can go here.
        # Model loading is handled by the static `load_model` method,
        # typically called during application lifespan.
        logger.debug("SentenceTransformerRerankerAdapter instance created.")

    @classmethod
    def load_model(cls):
        """
        Loads the CrossEncoder model based on settings.
        This method is intended to be called once, e.g., during application startup.
        """
        if cls._model_status == "loaded" and cls._model_name_loaded == settings.MODEL_NAME:
            logger.info("Reranker model already loaded and configured.", model_name=settings.MODEL_NAME)
            return

        cls._model_status = "loading"
        cls._model = None # Ensure model is None while loading a new one or retrying
        init_log = logger.bind(
            adapter_action="load_model",
            model_name=settings.MODEL_NAME,
            device=settings.MODEL_DEVICE,
            configured_hf_cache_dir=settings.HF_CACHE_DIR
        )
        init_log.info("Attempting to load CrossEncoder model...")
        
        # Set Hugging Face cache directory if specified
        if settings.HF_CACHE_DIR:
            os.environ['HF_HOME'] = settings.HF_CACHE_DIR
            os.environ['TRANSFORMERS_CACHE'] = settings.HF_CACHE_DIR
            init_log.info(f"Set HF_HOME/TRANSFORMERS_CACHE to: {settings.HF_CACHE_DIR}")

        start_time = time.time()
        try:
            cls._model = CrossEncoder(
                model_name=settings.MODEL_NAME,
                max_length=settings.MAX_SEQ_LENGTH,
                device=settings.MODEL_DEVICE,
                # trust_remote_code=True # Might be needed for some models, use with caution
            )
            load_time = time.time() - start_time
            cls._model_name_loaded = settings.MODEL_NAME
            cls._model_status = "loaded"
            init_log.info("CrossEncoder model loaded successfully.", duration_seconds=round(load_time, 3))
        except Exception as e:
            cls._model_status = "error"
            cls._model = None # Ensure model is None on error
            init_log.error("Failed to load CrossEncoder model.", error_message=str(e), exc_info=True)
            # Depending on application requirements, this could raise an error
            # to prevent the service from starting in a non-functional state.
            # For now, it logs the error and the health check will reflect this.

    async def _predict_scores_async(self, query_doc_pairs: List[Tuple[str, str]]) -> List[float]:
        """
        Performs model prediction asynchronously in a thread pool.
        """
        if not self.is_ready() or SentenceTransformerRerankerAdapter._model is None:
            logger.error("Reranker model not loaded or not ready for prediction.")
            raise RuntimeError("Reranker model is not available for prediction.")

        predict_log = logger.bind(adapter_action="_predict_scores_async", num_pairs=len(query_doc_pairs))
        predict_log.debug("Starting asynchronous prediction.")
        
        loop = asyncio.get_event_loop()
        try:
            # model.predict is CPU/GPU bound, so run it in the default executor (ThreadPoolExecutor)
            scores_tensor = await loop.run_in_executor(
                None, 
                SentenceTransformerRerankerAdapter._model.predict, # Call the class-level model
                query_doc_pairs,
                settings.BATCH_SIZE,
                False,  # show_progress_bar
                None,   # activation_fct (default is fine for most rerankers)
                False,  # convert_to_numpy (scores are usually numpy arrays)
                False   # convert_to_tensor (predict returns numpy by default)
            )
            # Ensure scores are Python floats
            scores = [float(score) for score in scores_tensor]
            predict_log.debug("Prediction successful.")
            return scores
        except Exception as e:
            predict_log.error("Error during reranker model prediction.", error_message=str(e), exc_info=True)
            raise RuntimeError(f"Reranker prediction failed: {e}") from e

    async def rerank(
        self, query: str, documents: List[DocumentToRerank]
    ) -> List[RerankedDocument]:
        """
        Reranks documents based on the query using the loaded CrossEncoder model.
        """
        rerank_log = logger.bind(
            adapter_action="rerank", 
            query_preview=query[:50]+"..." if len(query) > 50 else query,
            num_documents_input=len(documents)
        )
        rerank_log.info("Starting rerank operation.")

        if not documents:
            rerank_log.debug("No documents provided for reranking.")
            return []

        if not self.is_ready():
            rerank_log.error("Attempted to rerank when model is not ready.")
            raise RuntimeError("Reranker model is not available or failed to load.")

        query_doc_pairs: List[Tuple[str, str]] = []
        valid_documents_for_reranking: List[DocumentToRerank] = []

        for doc in documents:
            if doc.text and isinstance(doc.text, str) and doc.text.strip():
                query_doc_pairs.append((query, doc.text))
                valid_documents_for_reranking.append(doc)
            else:
                rerank_log.warning("Skipping document due to empty or invalid text.", document_id=doc.id)
        
        if not valid_documents_for_reranking:
            rerank_log.warning("No valid documents with text found for reranking.")
            return []

        rerank_log.debug(f"Processing {len(valid_documents_for_reranking)} documents for reranking.")
        scores = await self._predict_scores_async(query_doc_pairs)

        reranked_docs_with_scores: List[RerankedDocument] = []
        for doc, score in zip(valid_documents_for_reranking, scores):
            reranked_docs_with_scores.append(
                RerankedDocument(
                    id=doc.id,
                    text=doc.text, 
                    score=score, # Score from the reranker
                    metadata=doc.metadata # Preserve original metadata
                )
            )
        
        # Sort by the new reranker score in descending order
        reranked_docs_with_scores.sort(key=lambda x: x.score, reverse=True)
        
        rerank_log.info("Rerank operation completed.", num_documents_output=len(reranked_docs_with_scores))
        return reranked_docs_with_scores

    def get_model_name(self) -> str:
        return settings.MODEL_NAME # Returns the configured model name

    def is_ready(self) -> bool:
        # Check the class-level status
        return SentenceTransformerRerankerAdapter._model is not None and \
               SentenceTransformerRerankerAdapter._model_status == "loaded"

    @classmethod
    def get_model_status(cls) -> str:
        """Class method to get the current model loading status."""
        return cls._model_status