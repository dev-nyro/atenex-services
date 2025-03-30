# ./app/pipelines/rag_pipeline.py
import structlog
import asyncio
import uuid # Importar uuid que se usa en run_rag_pipeline
# *** CORRECCIÓN: Añadir Optional y otros tipos necesarios de 'typing' ***
from typing import Dict, Any, List, Tuple, Optional

from haystack import Pipeline, Document
from haystack.components.embedders import OpenAITextEmbedder
from haystack.components.builders.prompt_builder import PromptBuilder
from milvus_haystack import MilvusDocumentStore, MilvusEmbeddingRetriever
from haystack.utils import Secret

# Importar desde la app local
from app.core.config import settings
# Importar db.postgres_client directamente para loguear la interacción
from app.db import postgres_client
from app.services.gemini_client import gemini_client # Importar instancia del cliente

log = structlog.get_logger(__name__)

# --- Component Initialization Functions ---

def get_milvus_document_store() -> MilvusDocumentStore:
    """Initializes the MilvusDocumentStore connection."""
    log.debug("Initializing MilvusDocumentStore for Query Service",
             uri=str(settings.MILVUS_URI),
             collection=settings.MILVUS_COLLECTION_NAME,
             embedding_dim=settings.EMBEDDING_DIMENSION,
             embedding_field=settings.MILVUS_EMBEDDING_FIELD,
             content_field=settings.MILVUS_CONTENT_FIELD,
             metadata_fields=settings.MILVUS_METADATA_FIELDS,
             # Corregido: company_id_field no es un parámetro de MilvusDocumentStore
             # Se usa en los filtros del retriever.
             # company_id_field=settings.MILVUS_COMPANY_ID_FIELD,
             search_params=settings.MILVUS_SEARCH_PARAMS)
    try:
        store = MilvusDocumentStore(
            uri=str(settings.MILVUS_URI),
            collection_name=settings.MILVUS_COLLECTION_NAME,
            dim=settings.EMBEDDING_DIMENSION,
            embedding_field=settings.MILVUS_EMBEDDING_FIELD,
            content_field=settings.MILVUS_CONTENT_FIELD,
            metadata_fields=settings.MILVUS_METADATA_FIELDS,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong",
        )
        log.info("MilvusDocumentStore initialized")
        return store
    except Exception as e:
        log.error("Failed to initialize MilvusDocumentStore", error=str(e), exc_info=True)
        raise RuntimeError("Could not connect to or initialize Milvus Document Store") from e


def get_openai_text_embedder() -> OpenAITextEmbedder:
    """Initializes the OpenAI Embedder for text (queries)."""
    log.debug("Initializing OpenAITextEmbedder", model=settings.OPENAI_EMBEDDING_MODEL)
    api_key_secret = Secret.from_env_var("QUERY_OPENAI_API_KEY")
    if not api_key_secret.resolve_value():
         log.warning("QUERY_OPENAI_API_KEY environment variable not found or empty for OpenAI Embedder.")
         # Considerar lanzar un error si la clave es estrictamente necesaria
         # raise ValueError("Missing OpenAI API Key for text embedder")

    return OpenAITextEmbedder(
        api_key=api_key_secret,
        model=settings.OPENAI_EMBEDDING_MODEL,
    )

def get_milvus_retriever(document_store: MilvusDocumentStore) -> MilvusEmbeddingRetriever:
    """Initializes the MilvusEmbeddingRetriever."""
    log.debug("Initializing MilvusEmbeddingRetriever")
    return MilvusEmbeddingRetriever(
        document_store=document_store,
    )

def get_prompt_builder() -> PromptBuilder:
    """Initializes the PromptBuilder with the RAG template."""
    log.debug("Initializing PromptBuilder", template_preview=settings.RAG_PROMPT_TEMPLATE[:100] + "...")
    return PromptBuilder(template=settings.RAG_PROMPT_TEMPLATE)

# --- Pipeline Construction ---

# Almacenar pipeline construido globalmente para reutilizar
# *** CORRECCIÓN: Se añadió Optional a la importación de typing arriba ***
_rag_pipeline_instance: Optional[Pipeline] = None

def build_rag_pipeline() -> Pipeline:
    """
    Builds the Haystack RAG pipeline by initializing and connecting components.
    """
    global _rag_pipeline_instance
    if _rag_pipeline_instance:
        log.debug("Returning existing RAG pipeline instance.")
        return _rag_pipeline_instance

    log.info("Building Haystack RAG pipeline...")
    rag_pipeline = Pipeline()

    try:
        # 1. Initialize components
        doc_store = get_milvus_document_store()
        text_embedder = get_openai_text_embedder()
        retriever = get_milvus_retriever(document_store=doc_store)
        prompt_builder = get_prompt_builder()
        # No añadimos el Gemini client al pipeline Haystack, se llamará después

        # 2. Add components to the pipeline
        rag_pipeline.add_component("text_embedder", text_embedder)
        rag_pipeline.add_component("retriever", retriever)
        rag_pipeline.add_component("prompt_builder", prompt_builder)

        # 3. Connect components
        rag_pipeline.connect("text_embedder.embedding", "retriever.query_embedding")
        rag_pipeline.connect("retriever.documents", "prompt_builder.documents")

        log.info("Haystack RAG pipeline built successfully.")
        _rag_pipeline_instance = rag_pipeline # Guardar instancia global
        return rag_pipeline

    except Exception as e:
        log.error("Failed to build Haystack RAG pipeline", error=str(e), exc_info=True)
        raise RuntimeError("Could not build the RAG pipeline") from e


# --- Pipeline Execution ---

async def run_rag_pipeline(
    query: str,
    company_id: str, # Pasar como string
    user_id: Optional[str], # Pasar como string o None
    top_k: Optional[int] = None
) -> Tuple[str, List[Document], Optional[uuid.UUID]]:
    """
    Runs the RAG pipeline for a given query and company_id.

    Args:
        query: The user's query string.
        company_id: The company ID (as string) to filter documents.
        user_id: The user ID (as string or None) for logging.
        top_k: Number of documents to retrieve (overrides default if provided).

    Returns:
        A tuple containing:
        - The generated answer string.
        - A list of retrieved Haystack Document objects.
        - The UUID of the logged query interaction, or None if logging failed.

    Raises:
        Exception: If any step in the pipeline fails critically.
    """
    run_log = log.bind(query=query, company_id=company_id, user_id=user_id or "N/A")
    run_log.info("Running RAG pipeline...")

    pipeline = build_rag_pipeline() # Obtener la instancia (construida o existente)

    # Determine retriever parameters
    retriever_top_k = top_k if top_k is not None else settings.RETRIEVER_TOP_K
    # Construir el filtro de Milvus correctamente usando el campo definido en config
    # La estructura exacta puede depender de milvus-haystack, pero usualmente es un dict
    # Milvus/Haystack esperan filtros en formato específico. Ver doc de MilvusEmbeddingRetriever.
    # Usualmente es una lista de condiciones o un diccionario complejo.
    # Probemos con un filtro simple de término:
    retriever_filters = {"company_id": company_id}
    # Si se necesitan filtros más complejos (AND/OR), la estructura sería diferente:
    # retriever_filters = {
    #     "bool": {
    #         "filter": [
    #             {"term": {settings.MILVUS_COMPANY_ID_FIELD: company_id}}
    #             # Ejemplo: {"range": {"created_at": {"gte": "2023-01-01"}}}
    #         ]
    #         # "must": [...], "should": [...] # Para condiciones AND/OR
    #     }
    # }
    run_log.debug("Retriever filters prepared", filters=retriever_filters, top_k=retriever_top_k)


    # Prepare inputs for the Haystack pipeline run
    pipeline_input = {
        "text_embedder": {"text": query},
        "retriever": {"filters": retriever_filters, "top_k": retriever_top_k},
        "prompt_builder": {"query": query} # Pasar query también al prompt builder
    }
    run_log.debug("Pipeline input prepared", input_data=pipeline_input)

    try:
        # Execute the Haystack pipeline (embedding, retrieval, prompt building)
        loop = asyncio.get_running_loop()
        pipeline_result = await loop.run_in_executor(
            None, # Use default ThreadPoolExecutor
            lambda: pipeline.run(pipeline_input, include_outputs_from=["retriever", "prompt_builder"])
        )
        run_log.info("Haystack pipeline (embed, retrieve, prompt) executed successfully.")

        # Extract results
        retrieved_docs: List[Document] = pipeline_result.get("retriever", {}).get("documents", [])
        prompt_builder_output = pipeline_result.get("prompt_builder", {})
        generated_prompt: Optional[str] = None

        # El prompt puede estar en diferentes formatos
        if "prompt" in prompt_builder_output:
             prompt_data = prompt_builder_output["prompt"]
             if isinstance(prompt_data, list): # Común con ChatPromptBuilder (lista de ChatMessage)
                 # Extraer contenido textual de los mensajes
                 text_parts = []
                 for msg in prompt_data:
                     if hasattr(msg, 'content') and isinstance(msg.content, str):
                          text_parts.append(msg.content)
                     # Podrías necesitar manejar otros tipos de contenido si usas roles/funciones
                 generated_prompt = "\n".join(text_parts)
             elif isinstance(prompt_data, str): # Si el builder devuelve un string simple
                  generated_prompt = prompt_data
             else:
                  run_log.warning("Unexpected prompt format from prompt_builder", prompt_type=type(prompt_data))
                  # Intentar convertir a string como fallback
                  generated_prompt = str(prompt_data)


        if not retrieved_docs:
            run_log.warning("No relevant documents found by retriever.")
            # Ajustar prompt si no hay documentos y no se generó un prompt alternativo
            if not generated_prompt:
                 generated_prompt = f"Pregunta: {query}\n\nNo se encontraron documentos relevantes. Intenta responder brevemente si es posible, o indica que no tienes información."


        if not generated_prompt:
             run_log.error("Failed to extract or generate prompt from pipeline output", output=prompt_builder_output)
             raise ValueError("Could not construct prompt for LLM.")

        run_log.debug("Generated prompt for LLM", prompt_preview=generated_prompt[:200] + "...")

        # Call Gemini LLM with the generated prompt
        answer = await gemini_client.generate_answer(generated_prompt)
        run_log.info("Answer generated by Gemini", answer_preview=answer[:100] + "...")

        # Log the interaction (asynchronously)
        log_id: Optional[uuid.UUID] = None
        try:
            doc_ids = [doc.id for doc in retrieved_docs]
            doc_scores = [doc.score for doc in retrieved_docs if doc.score is not None]
            user_uuid = uuid.UUID(user_id) if user_id else None

            # Llamar a la función de logging del cliente postgres
            log_id = await postgres_client.log_query_interaction(
                company_id=uuid.UUID(company_id),
                user_id=user_uuid,
                query=query,
                response=answer,
                retrieved_doc_ids=doc_ids,
                retrieved_doc_scores=doc_scores,
                metadata={"retriever_top_k": retriever_top_k}
            )
        except Exception as log_err:
             run_log.error("Failed to log query interaction to database", error=str(log_err), exc_info=True)

        return answer, retrieved_docs, log_id

    except Exception as e:
        run_log.exception("Error occurred during RAG pipeline execution")
        raise

# Función para verificar dependencias (llamada desde health check)
async def check_pipeline_dependencies() -> Dict[str, str]:
    """Checks critical dependencies for the pipeline (e.g., Milvus)."""
    results = {"milvus_connection": "pending"}
    try:
        # Intentar obtener el DocumentStore como prueba de conexión/configuración
        store = get_milvus_document_store()
        # Realizar una operación ligera para confirmar la conexión, como contar documentos
        # Ejecutar la operación síncrona en un thread para no bloquear
        count = await asyncio.to_thread(store.count_documents)
        results["milvus_connection"] = "ok"
        log.debug("Milvus dependency check successful (count documents)", count=count)
    except Exception as e:
        # Capturar cualquier excepción durante la inicialización o la operación
        error_msg = f"{type(e).__name__}: {str(e)}"
        results["milvus_connection"] = f"error: {error_msg[:100]}" # Limitar longitud del error
        log.warning("Milvus dependency check failed", error=error_msg, exc_info=False) # No loguear traceback completo aquí
    return results