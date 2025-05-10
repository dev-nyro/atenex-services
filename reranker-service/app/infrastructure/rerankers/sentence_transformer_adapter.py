# reranker-service/app/infrastructure/rerankers/sentence_transformer_adapter.py
import asyncio
from typing import List, Tuple, Callable, Optional
from sentence_transformers import CrossEncoder
import structlog
import time

from app.application.ports.reranker_model_port import RerankerModelPort
from app.domain.models import DocumentToRerank, RerankedDocument
from app.core.config import settings

logger = structlog.get_logger(__name__)

class SentenceTransformerRerankerAdapter(RerankerModelPort):
    _model: Optional[CrossEncoder] = None
    _model_name_loaded: Optional[str] = None
    _model_status: str = "unloaded" # unloaded, loading, loaded, error

    def __init__(self):
        # Model loading is deferred to an explicit load method or lifespan
        pass

    def load_model(self):
        """Loads the CrossEncoder model."""
        if SentenceTransformerRerankerAdapter._model_status == "loaded" and \
           SentenceTransformerRerankerAdapter._model_name_loaded == settings.MODEL_NAME:
            logger.info("Reranker model already loaded.", model_name=settings.MODEL_NAME)
            return

        SentenceTransformerRerankerAdapter._model_status = "loading"
        init_log = logger.bind(
            adapter="SentenceTransformerRerankerAdapter",
            action="load_model",
            model_name=settings.MODEL_NAME,
            device=settings.MODEL_DEVICE,
            cache_dir=settings.HF_CACHE_DIR
        )
        init_log.info("Initializing CrossEncoder model...")
        start_time = time.time()
        try:
            SentenceTransformerRerankerAdapter._model = CrossEncoder(
                model_name=settings.MODEL_NAME,
                max_length=settings.MAX_SEQ_LENGTH,
                device=settings.MODEL_DEVICE,
                # model_kwargs={'cache_dir': settings.HF_CACHE_DIR} if settings.HF_CACHE_DIR else {} # Newer sentence-transformers might not need model_kwargs for cache_dir
            )
            # For cache_dir with newer sentence-transformers, it often respects TRANSFORMERS_CACHE or HF_HOME env vars
            # or you can pass cache_folder to some specific model classes if they support it.
            # CrossEncoder itself might use the default Hugging Face cache.
            
            load_time = time.time() - start_time
            SentenceTransformerRerankerAdapter._model_name_loaded = settings.MODEL_NAME
            SentenceTransformerRerankerAdapter._model_status = "loaded"
            init_log.info("CrossEncoder model loaded successfully.", duration_ms=load_time * 1000)
        except Exception as e:
            SentenceTransformerRerankerAdapter._model_status = "error"
            SentenceTransformerRerankerAdapter._model = None
            init_log.error("Failed to load CrossEncoder model.", error_message=str(e), exc_info=True)
            # Optionally raise an exception here if loading is critical for startup
            # raise RuntimeError(f"Failed to load CrossEncoder model: {e}") from e

    async def _predict_scores_async(self, query_doc_pairs: List[Tuple[str, str]]) -> List[float]:
        if SentenceTransformerRerankerAdapter._model is None or SentenceTransformerRerankerAdapter._model_status != "loaded":
            logger.error("Reranker model not loaded or not ready for prediction.")
            raise RuntimeError("Reranker model is not available for prediction.")

        loop = asyncio.get_event_loop()
        try:
            scores = await loop.run_in_executor(
                None,  # Uses the default ThreadPoolExecutor
                SentenceTransformerRerankerAdapter._model.predict,
                query_doc_pairs,
                settings.BATCH_SIZE, # batch_size
                True, # show_progress_bar (set to False in prod)
                None, # activation_fct
                False, # convert_to_numpy
                True # convert_to_tensor (might not be needed if model handles it)
            )
            return [float(score) for score in scores]
        except Exception as e:
            logger.error("Error during reranker model prediction", exc_info=True, query_doc_pairs_count=len(query_doc_pairs))
            raise RuntimeError(f"Reranker prediction failed: {e}") from e

    async def rerank(
        self, query: str, documents: List[DocumentToRerank]
    ) -> List[RerankedDocument]:
        if not documents:
            return []

        if SentenceTransformerRerankerAdapter._model is None or SentenceTransformerRerankerAdapter._model_status != "loaded":
            logger.error("Attempted to rerank with model not loaded/ready.")
            # Fallback: return documents unsorted or raise specific error
            # For now, let's raise to make it explicit
            raise RuntimeError("Reranker model is not available.")


        query_doc_pairs: List[Tuple[str, str]] = [(query, doc.text) for doc in documents]
        
        scores = await self._predict_scores_async(query_doc_pairs)

        reranked_docs_with_scores = []
        for doc, score in zip(documents, scores):
            reranked_docs_with_scores.append(
                RerankedDocument(
                    id=doc.id,
                    text=doc.text, 
                    score=score,
                    metadata=doc.metadata
                )
            )
        
        reranked_docs_with_scores.sort(key=lambda x: x.score, reverse=True)
        return reranked_docs_with_scores

    def get_model_name(self) -> str:
        return settings.MODEL_NAME

    def is_ready(self) -> bool:
        return SentenceTransformerRerankerAdapter._model is not None and \
               SentenceTransformerRerankerAdapter._model_status == "loaded"

    @classmethod
    def get_model_status(cls) -> str:
        return cls._model_status