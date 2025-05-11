# embedding-service/app/application/use_cases/embed_texts_use_case.py
import structlog
from typing import List, Tuple, Dict, Any

from app.application.ports.embedding_model_port import EmbeddingModelPort
from app.api.v1.schemas import ModelInfo # For response structure

log = structlog.get_logger(__name__)

class EmbedTextsUseCase:
    """
    Use case for generating embeddings for a list of texts.
    """
    def __init__(self, embedding_model: EmbeddingModelPort):
        self.embedding_model = embedding_model
        log.info("EmbedTextsUseCase initialized", model_adapter=type(embedding_model).__name__)

    async def execute(self, texts: List[str]) -> Tuple[List[List[float]], ModelInfo]:
        """
        Executes the embedding generation process.

        Args:
            texts: A list of strings to embed.

        Returns:
            A tuple containing:
                - A list of embeddings (list of lists of floats).
                - ModelInfo object containing details about the embedding model.

        Raises:
            ValueError: If no texts are provided.
            Exception: If embedding generation fails.
        """
        if not texts:
            log.warning("EmbedTextsUseCase executed with no texts.")
            # Return empty list and model info, consistent with schema
            model_info_dict = self.embedding_model.get_model_info()
            return [], ModelInfo(**model_info_dict)


        use_case_log = log.bind(num_texts=len(texts))
        use_case_log.info("Executing embedding generation for texts")

        try:
            embeddings = await self.embedding_model.embed_texts(texts)
            model_info_dict = self.embedding_model.get_model_info()
            model_info_obj = ModelInfo(**model_info_dict)

            use_case_log.info("Successfully generated embeddings", num_embeddings=len(embeddings))
            return embeddings, model_info_obj
        except Exception as e:
            use_case_log.exception("Failed to generate embeddings in use case")
            # Re-raise to be handled by the endpoint or a global exception handler
            raise