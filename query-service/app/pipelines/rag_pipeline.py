# ./app/pipelines/rag_pipeline.py
import structlog
import asyncio
import uuid
from typing import Dict, Any, List, Tuple, Optional

# Importar excepciones específicas si es necesario
from pymilvus.exceptions import MilvusException

from haystack import Pipeline, Document
from haystack.components.embedders import OpenAITextEmbedder
from haystack.components.builders.prompt_builder import PromptBuilder
from milvus_haystack import MilvusDocumentStore, MilvusEmbeddingRetriever
from haystack.utils import Secret

from app.core.config import settings
from app.db import postgres_client
from app.services.gemini_client import gemini_client

log = structlog.get_logger(__name__)

# --- Component Initialization Functions ---

def get_milvus_document_store() -> MilvusDocumentStore:
    """Initializes the MilvusDocumentStore connection."""
    connection_uri = str(settings.MILVUS_URI)
    # Definir un timeout explícito para la conexión (en segundos)
    connection_timeout = 30.0 # Ajustable, 30 segundos es un valor generoso para la conexión inicial

    log.debug("Initializing MilvusDocumentStore for Query Service",
             connection_uri=connection_uri,
             collection=settings.MILVUS_COLLECTION_NAME,
             search_params=settings.MILVUS_SEARCH_PARAMS,
             connection_timeout=connection_timeout)
    try:
        # *** CORRECCIÓN: Añadir timeout a connection_args ***
        store = MilvusDocumentStore(
            connection_args={
                "uri": connection_uri,
                "timeout": connection_timeout # Añadir timeout explícito
                # Si Milvus requiere auth, añadir aquí:
                # "user": "your_milvus_user",
                # "password": "your_milvus_password",
                # "token": "your_milvus_token",
            },
            collection_name=settings.MILVUS_COLLECTION_NAME,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong",
        )
        # Intentar una operación simple para forzar la conexión real y validarla
        log.debug("Attempting to verify connection by counting documents...")
        store.count_documents() # Esta llamada forzará la conexión si no se hizo ya
        log.info("MilvusDocumentStore initialized and connection verified successfully")
        return store
    except MilvusException as e:
        # Capturar específicamente errores de Milvus
        log.error(
            "Failed to initialize or connect to MilvusDocumentStore",
            error_code=e.code,
            error_message=e.message,
            connection_uri=connection_uri,
            collection=settings.MILVUS_COLLECTION_NAME,
            exc_info=True # Incluir traceback completo para MilvusException
        )
        # Lanzar un error más descriptivo
        raise RuntimeError(
            f"Could not connect to Milvus at {connection_uri}. "
            f"Error code {e.code}: {e.message}. "
            "Check Milvus service status, network connectivity/policies between namespaces, and credentials."
        ) from e
    except Exception as e:
        # Capturar otros errores inesperados
        log.error("Unexpected error during MilvusDocumentStore initialization", error=str(e), exc_info=True)
        raise RuntimeError(f"Unexpected error initializing Milvus Document Store: {e}") from e


# --- get_openai_text_embedder, get_milvus_retriever, get_prompt_builder (Sin cambios respecto a la versión funcional anterior) ---
def get_openai_text_embedder() -> OpenAITextEmbedder:
    """Initializes the OpenAI Embedder for text (queries)."""
    log.debug("Initializing OpenAITextEmbedder", model=settings.OPENAI_EMBEDDING_MODEL)
    api_key_secret = Secret.from_env_var("QUERY_OPENAI_API_KEY")
    if not api_key_secret.resolve_value():
         log.warning("QUERY_OPENAI_API_KEY environment variable not found or empty for OpenAI Embedder.")

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
# (Sin cambios respecto a la versión funcional anterior)
_rag_pipeline_instance: Optional[Pipeline] = None

def build_rag_pipeline() -> Pipeline:
    """
    Builds the Haystack RAG pipeline by initializing and connecting components.
    Caches the pipeline instance globally after the first successful build.
    """
    global _rag_pipeline_instance
    if _rag_pipeline_instance:
        log.debug("Returning existing RAG pipeline instance.")
        return _rag_pipeline_instance

    log.info("Building Haystack RAG pipeline...")
    rag_pipeline = Pipeline()

    try:
        doc_store = get_milvus_document_store() # Ahora debería fallar aquí si hay problemas
        text_embedder = get_openai_text_embedder()
        retriever = get_milvus_retriever(document_store=doc_store)
        prompt_builder = get_prompt_builder()

        rag_pipeline.add_component("text_embedder", text_embedder)
        rag_pipeline.add_component("retriever", retriever)
        rag_pipeline.add_component("prompt_builder", prompt_builder)

        rag_pipeline.connect("text_embedder.embedding", "retriever.query_embedding")
        rag_pipeline.connect("retriever.documents", "prompt_builder.documents")

        log.info("Haystack RAG pipeline built successfully.")
        _rag_pipeline_instance = rag_pipeline
        return rag_pipeline

    except Exception as e:
        log.error("Failed to build Haystack RAG pipeline", error=str(e), exc_info=True)
        raise RuntimeError("Could not build the RAG pipeline") from e


# --- Pipeline Execution ---
# (Sin cambios respecto a la versión funcional anterior)
async def run_rag_pipeline(
    query: str,
    company_id: str,
    user_id: Optional[str],
    top_k: Optional[int] = None
) -> Tuple[str, List[Document], Optional[uuid.UUID]]:
    """
    Runs the RAG pipeline for a given query and company_id.
    """
    run_log = log.bind(query=query, company_id=company_id, user_id=user_id or "N/A")
    run_log.info("Running RAG pipeline...")

    try:
        pipeline = build_rag_pipeline()
    except Exception as build_err:
         run_log.error("Failed to get or build RAG pipeline for execution", error=str(build_err))
         raise HTTPException(status_code=503, detail="RAG pipeline is not available.")


    retriever_top_k = top_k if top_k is not None else settings.RETRIEVER_TOP_K
    retriever_filters = {settings.MILVUS_COMPANY_ID_FIELD: company_id}
    run_log.debug("Retriever filters prepared", filters=retriever_filters, top_k=retriever_top_k)

    pipeline_input = {
        "text_embedder": {"text": query},
        "retriever": {"filters": retriever_filters, "top_k": retriever_top_k},
        "prompt_builder": {"query": query}
    }
    run_log.debug("Pipeline input prepared", input_data=pipeline_input)

    try:
        loop = asyncio.get_running_loop()
        pipeline_result = await loop.run_in_executor(
            None,
            lambda: pipeline.run(pipeline_input, include_outputs_from=["retriever", "prompt_builder"])
        )
        run_log.info("Haystack pipeline (embed, retrieve, prompt) executed successfully.")

        retrieved_docs: List[Document] = pipeline_result.get("retriever", {}).get("documents", [])
        prompt_builder_output = pipeline_result.get("prompt_builder", {})
        generated_prompt: Optional[str] = None

        if "prompt" in prompt_builder_output:
             prompt_data = prompt_builder_output["prompt"]
             if isinstance(prompt_data, list):
                 text_parts = [msg.content for msg in prompt_data if hasattr(msg, 'content') and isinstance(msg.content, str)]
                 generated_prompt = "\n".join(text_parts)
             elif isinstance(prompt_data, str):
                  generated_prompt = prompt_data
             else:
                  run_log.warning("Unexpected prompt format from prompt_builder", prompt_type=type(prompt_data))
                  generated_prompt = str(prompt_data)

        if not retrieved_docs:
            run_log.warning("No relevant documents found by retriever.")
            if not generated_prompt:
                 generated_prompt = f"Pregunta: {query}\n\nNo se encontraron documentos relevantes. Intenta responder brevemente si es posible, o indica que no tienes información."

        if not generated_prompt:
             run_log.error("Failed to extract or generate prompt from pipeline output", output=prompt_builder_output)
             raise ValueError("Could not construct prompt for LLM.")

        run_log.debug("Generated prompt for LLM", prompt_preview=generated_prompt[:200] + "...")

        answer = await gemini_client.generate_answer(generated_prompt)
        run_log.info("Answer generated by Gemini", answer_preview=answer[:100] + "...")

        log_id: Optional[uuid.UUID] = None
        try:
            doc_ids = [doc.id for doc in retrieved_docs]
            doc_scores = [doc.score for doc in retrieved_docs if doc.score is not None]
            user_uuid = uuid.UUID(user_id) if user_id else None
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
        raise HTTPException(status_code=500, detail=f"Error processing query: {type(e).__name__}")


# --- check_pipeline_dependencies (Sin cambios respecto a la versión funcional anterior) ---
async def check_pipeline_dependencies() -> Dict[str, str]:
    """Checks critical dependencies for the pipeline (e.g., Milvus)."""
    results = {"milvus_connection": "pending"}
    try:
        store = get_milvus_document_store() # Ahora debería funcionar o lanzar error claro
        count = await asyncio.to_thread(store.count_documents)
        results["milvus_connection"] = "ok"
        log.debug("Milvus dependency check successful (count documents)", count=count)
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        results["milvus_connection"] = f"error: {error_msg[:100]}" # Limitar longitud
        log.warning("Milvus dependency check failed", error=error_msg, exc_info=False)
    return results