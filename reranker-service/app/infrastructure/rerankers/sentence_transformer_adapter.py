# reranker-service/app/infrastructure/rerankers/sentence_transformer_adapter.py
import asyncio
import functools 
from typing import List, Tuple, Optional
from sentence_transformers import CrossEncoder # type: ignore
import structlog
import time
import os 

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
    _model_status: str = "unloaded" 

    def __init__(self):
        logger.debug("SentenceTransformerRerankerAdapter instance created.")

    @classmethod
    def load_model(cls):
        """
        Loads the CrossEncoder model based on settings.
        Applies FP16 optimization if on CUDA.
        """
        if cls._model_status == "loaded" and cls._model_name_loaded == settings.MODEL_NAME:
            logger.info("Reranker model already loaded and configured.", model_name=settings.MODEL_NAME)
            return

        cls._model_status = "loading"
        cls._model = None 
        init_log = logger.bind(
            adapter_action="load_model",
            model_name=settings.MODEL_NAME,
            device=settings.MODEL_DEVICE,
            configured_hf_cache_dir=settings.HF_CACHE_DIR
        )
        init_log.info("Attempting to load CrossEncoder model...")
        
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
                # trust_remote_code=True # Solo si es necesario para modelos específicos
            )
            
            # Optimización en GPU: FP16
            if settings.MODEL_DEVICE.startswith("cuda") and cls._model is not None:
                try:
                    cls._model.model.half() # type: ignore
                    init_log.info("CrossEncoder model converted to FP16 for GPU optimization.")
                except Exception as e_fp16:
                    init_log.warning("Failed to convert CrossEncoder model to FP16.", error_message=str(e_fp16))

            load_time = time.time() - start_time
            cls._model_name_loaded = settings.MODEL_NAME
            cls._model_status = "loaded"
            init_log.info("CrossEncoder model loaded successfully.", duration_seconds=round(load_time, 3))
        except Exception as e:
            cls._model_status = "error"
            cls._model = None 
            init_log.error("Failed to load CrossEncoder model.", error_message=str(e), exc_info=True)

    async def _predict_scores_async(self, query_doc_pairs: List[Tuple[str, str]]) -> List[float]:
        """
        Performs model prediction asynchronously in a thread pool.
        Uses TOKENIZER_WORKERS from settings.
        """
        if not self.is_ready() or SentenceTransformerRerankerAdapter._model is None:
            logger.error("Reranker model not loaded or not ready for prediction.")
            raise RuntimeError("Reranker model is not available for prediction.")

        predict_log = logger.bind(
            adapter_action="_predict_scores_async", 
            num_pairs=len(query_doc_pairs),
            tokenizer_workers_setting=settings.TOKENIZER_WORKERS # Log el valor de la config
            )
        
        # El número de workers para DataLoader vendrá directamente de la configuración.
        # Si es 0, la tokenización es secuencial en el hilo principal.
        # Si es >0 y mp.set_start_method('spawn') está activo para CUDA, funcionará.
        num_dataloader_workers = settings.TOKENIZER_WORKERS
        predict_log.debug(f"Starting asynchronous prediction with num_dataloader_workers={num_dataloader_workers}.")
        
        loop = asyncio.get_event_loop()
        try:
            predict_task_with_args = functools.partial(
                SentenceTransformerRerankerAdapter._model.predict,
                query_doc_pairs,  
                batch_size=settings.BATCH_SIZE,
                show_progress_bar=False,
                num_workers=num_dataloader_workers, # Usar el valor de settings
                activation_fct=None, 
                apply_softmax=False, 
                convert_to_numpy=True, 
                convert_to_tensor=False 
            )
            
            scores_numpy_array = await loop.run_in_executor(
                None,  
                predict_task_with_args 
            )
            
            scores = scores_numpy_array.tolist() 
            predict_log.debug("Prediction successful.")
            return scores
        except Exception as e:
            predict_log.error("Error during reranker model prediction.", error_message=str(e), exc_info=True)
            raise RuntimeError(f"Reranker prediction failed: {str(e)}") from e

    async def rerank(
        self, query: str, documents: List[DocumentToRerank]
    ) -> List[RerankedDocument]:
        rerank_log = logger.bind(
            adapter_action="rerank", 
            query_preview=query[:50]+"..." if len(query) > 50 else query,
            num_documents_input=len(documents)
        )
        rerank_log.debug("Starting rerank operation in adapter.")

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
                    score=score, 
                    metadata=doc.metadata 
                )
            )
        
        reranked_docs_with_scores.sort(key=lambda x: x.score, reverse=True)
        
        rerank_log.debug("Rerank operation completed by adapter.", num_documents_output=len(reranked_docs_with_scores))
        return reranked_docs_with_scores

    def get_model_name(self) -> str:
        # Devuelve el nombre del modelo que se intentó cargar según la configuración,
        # o el nombre del modelo cargado si tuvo éxito.
        return SentenceTransformerRerankerAdapter._model_name_loaded or settings.MODEL_NAME

    def is_ready(self) -> bool:
        return SentenceTransformerRerankerAdapter._model is not None and \
               SentenceTransformerRerankerAdapter._model_status == "loaded"

    @classmethod
    def get_model_status(cls) -> str:
        return cls._model_status