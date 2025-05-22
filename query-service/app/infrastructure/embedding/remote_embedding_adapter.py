# query-service/app/infrastructure/embedding/remote_embedding_adapter.py
import structlog
from typing import List, Optional

from app.application.ports.embedding_port import EmbeddingPort
from app.infrastructure.clients.embedding_service_client import EmbeddingServiceClient
from app.core.config import settings 

log = structlog.get_logger(__name__)

class RemoteEmbeddingAdapter(EmbeddingPort):
    """
    Adaptador que utiliza EmbeddingServiceClient para generar embeddings
    llamando al servicio de embedding externo.
    """
    def __init__(self, client: EmbeddingServiceClient):
        self.client = client
        self._embedding_dimension: Optional[int] = None 
        self._expected_dimension = settings.EMBEDDING_DIMENSION 
        log.info("RemoteEmbeddingAdapter initialized", expected_dimension=self._expected_dimension)

    async def initialize(self):
        init_log = log.bind(adapter="RemoteEmbeddingAdapter", action="initialize")
        try:
            model_info = await self.client.get_model_info()
            if model_info and "dimension" in model_info:
                self._embedding_dimension = model_info["dimension"]
                init_log.info("Successfully retrieved embedding dimension from service.",
                              service_dimension=self._embedding_dimension,
                              service_model_name=model_info.get("model_name"))
                if self._embedding_dimension != self._expected_dimension:
                    init_log.warning("Embedding dimension mismatch!",
                                     configured_dimension=self._expected_dimension,
                                     service_dimension=self._embedding_dimension,
                                     message="Query service configured dimension does not match dimension reported by embedding service. "
                                             "This may cause issues with Milvus or other components. Ensure configurations are aligned.")
            else:
                init_log.warning("Could not retrieve embedding dimension from service. Will use configured dimension.",
                                 configured_dimension=self._expected_dimension)
        except Exception as e:
            init_log.error("Failed to retrieve embedding dimension during initialization.", error=str(e), exc_info=True)

    async def embed_query(self, query_text: str) -> List[float]:
        adapter_log = log.bind(adapter="RemoteEmbeddingAdapter", action="embed_query")
        if not query_text:
            adapter_log.warning("Empty query text provided.")
            raise ValueError("Query text cannot be empty.")
        try:
            # Especificar text_type="query" para las consultas de usuario
            embeddings = await self.client.generate_embeddings(texts=[query_text], text_type="query")
            if not embeddings or len(embeddings) != 1:
                adapter_log.error("Embedding service did not return a valid embedding for the query.", received_embeddings=embeddings)
                raise ValueError("Failed to get a valid embedding for the query.")

            embedding_vector = embeddings[0]
            
            service_dim = self.get_embedding_dimension() 
            if len(embedding_vector) != service_dim:
                adapter_log.error("Embedding dimension mismatch for query embedding.",
                                  expected_dim=service_dim,
                                  received_dim=len(embedding_vector))
                raise ValueError(f"Embedding dimension mismatch: expected {service_dim}, got {len(embedding_vector)}")

            adapter_log.debug("Query embedded successfully via remote service.")
            return embedding_vector
        except ConnectionError as e:
            adapter_log.error("Connection error while embedding query.", error=str(e))
            raise 
        except ValueError as e:
            adapter_log.error("Value error while embedding query.", error=str(e))
            raise 

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        adapter_log = log.bind(adapter="RemoteEmbeddingAdapter", action="embed_texts")
        if not texts:
            adapter_log.warning("No texts provided to embed_texts.")
            return []
        try:
            # Para lotes de texto genéricos (ej. passages), usar "passage" por defecto
            embeddings = await self.client.generate_embeddings(texts=texts, text_type="passage")
            if len(embeddings) != len(texts):
                adapter_log.error("Number of embeddings received does not match number of texts sent.",
                                  num_texts=len(texts), num_embeddings=len(embeddings))
                raise ValueError("Mismatch in number of embeddings received from service.")

            service_dim = self.get_embedding_dimension()
            if embeddings and len(embeddings[0]) != service_dim:
                 adapter_log.error("Embedding dimension mismatch for batch texts.",
                                   expected_dim=service_dim,
                                   received_dim=len(embeddings[0]))
                 raise ValueError(f"Embedding dimension mismatch: expected {service_dim}, got {len(embeddings[0])}")

            adapter_log.debug(f"Successfully embedded {len(texts)} texts via remote service.")
            return embeddings
        except ConnectionError as e:
            adapter_log.error("Connection error while embedding texts.", error=str(e))
            raise
        except ValueError as e:
            adapter_log.error("Value error while embedding texts.", error=str(e))
            raise

    def get_embedding_dimension(self) -> int:
        if self._embedding_dimension is not None:
            return self._embedding_dimension
        return self._expected_dimension

    async def health_check(self) -> bool:
        return await self.client.check_health()