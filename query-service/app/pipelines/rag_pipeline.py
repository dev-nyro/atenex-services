# ./app/pipelines/rag_pipeline.py
import structlog
import asyncio
from typing import Dict, Any, List, Tuple

from haystack import Pipeline, Document
from haystack.components.embedders import OpenAITextEmbedder
from haystack.components.builders.prompt_builder import PromptBuilder
from milvus_haystack import MilvusDocumentStore, MilvusEmbeddingRetriever
from haystack.utils import Secret

from app.core.config import settings
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
             company_id_field=settings.MILVUS_COMPANY_ID_FIELD,
             search_params=settings.MILVUS_SEARCH_PARAMS)
    try:
        store = MilvusDocumentStore(
            uri=str(settings.MILVUS_URI),
            collection_name=settings.MILVUS_COLLECTION_NAME,
            dim=settings.EMBEDDING_DIMENSION, # Asegurar que la dimensión es correcta
            embedding_field=settings.MILVUS_EMBEDDING_FIELD,
            content_field=settings.MILVUS_CONTENT_FIELD,
            metadata_fields=settings.MILVUS_METADATA_FIELDS, # Campos a recuperar
             # Parámetros específicos de búsqueda (ajustar según sea necesario)
            search_params=settings.MILVUS_SEARCH_PARAMS,
            # Consistency level fuerte para asegurar leer escrituras recientes si es necesario
            # Podría relajarse a 'Bounded' o 'Eventually' para rendimiento si la consistencia inmediata no es crítica
            consistency_level="Strong",
        )
        # Podríamos añadir un ping o verificación aquí si MilvusDocumentStore lo soporta
        log.info("MilvusDocumentStore initialized")
        return store
    except Exception as e:
        log.error("Failed to initialize MilvusDocumentStore", error=str(e), exc_info=True)
        raise RuntimeError("Could not connect to or initialize Milvus Document Store") from e


def get_openai_text_embedder() -> OpenAITextEmbedder:
    """Initializes the OpenAI Embedder for text (queries)."""
    log.debug("Initializing OpenAITextEmbedder", model=settings.OPENAI_EMBEDDING_MODEL)
    # Haystack usa OPENAI_API_KEY de env vars por defecto si Secret no se pasa explícitamente
    # pero es mejor ser explícito usando Secret.from_env o Secret.from_token
    api_key_secret = Secret.from_env_var("QUERY_OPENAI_API_KEY") # Leer desde la variable correcta
    if not api_key_secret.resolve_value(): # Verificar si la clave se pudo resolver
         log.warning("QUERY_OPENAI_API_KEY environment variable not found or empty for OpenAI Embedder.")
         # Considerar lanzar un error si la clave es estrictamente necesaria
         # raise ValueError("Missing OpenAI API Key for text embedder")

    return OpenAITextEmbedder(
        api_key=api_key_secret,
        model=settings.OPENAI_EMBEDDING_MODEL,
        # Podríamos añadir prefijo/sufijo si es necesario para el modelo
        # prefix="query: ",
    )

def get_milvus_retriever(document_store: MilvusDocumentStore) -> MilvusEmbeddingRetriever:
    """Initializes the MilvusEmbeddingRetriever."""
    log.debug("Initializing MilvusEmbeddingRetriever")
    return MilvusEmbeddingRetriever(
        document_store=document_store,
        # top_k se pasará dinámicamente en pipeline.run()
        # filters también se pasarán dinámicamente
    )

def get_prompt_builder() -> PromptBuilder:
    """Initializes the PromptBuilder with the RAG template."""
    log.debug("Initializing PromptBuilder", template_preview=settings.RAG_PROMPT_TEMPLATE[:100] + "...")
    return PromptBuilder(template=settings.RAG_PROMPT_TEMPLATE)

# --- Pipeline Construction ---

# Almacenar pipeline construido globalmente para reutilizar (si es seguro y deseado)
# Esto evita reconstruirlo en cada request, pero asume que los componentes son reutilizables
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
        # La salida del prompt_builder se usará para llamar a Gemini externamente

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
    retriever_filters = {
        "bool": {
             "filter": [
                 {"term": {"company_id": company_id}}
                 # Puedes añadir más filtros aquí si es necesario, usando la sintaxis de Milvus/Haystack
                 # Ejemplo: {"range": {"created_at": {"gte": "2023-01-01"}}}
             ]
         }
     }


    # Prepare inputs for the Haystack pipeline run
    pipeline_input = {
        "text_embedder": {"text": query},
        "retriever": {"filters": retriever_filters, "top_k": retriever_top_k},
        "prompt_builder": {"query": query} # Pasar query también al prompt builder
    }
    run_log.debug("Pipeline input prepared", input_data=pipeline_input)

    try:
        # Execute the Haystack pipeline (embedding, retrieval, prompt building)
        # Pipeline.run is synchronous, run it in a thread to avoid blocking async event loop
        loop = asyncio.get_running_loop()
        pipeline_result = await loop.run_in_executor(
            None, # Use default ThreadPoolExecutor
            lambda: pipeline.run(pipeline_input, include_outputs_from=["retriever", "prompt_builder"]) # Incluir outputs específicos
        )
        run_log.info("Haystack pipeline (embed, retrieve, prompt) executed successfully.")

        # Extract results
        retrieved_docs: List[Document] = pipeline_result.get("retriever", {}).get("documents", [])
        prompt_builder_output = pipeline_result.get("prompt_builder", {})
        generated_prompt: Optional[str] = None

        # El prompt puede estar en diferentes formatos dependiendo del builder/output
        if "prompt" in prompt_builder_output:
             # Si es una lista de ChatMessage (común con ChatPromptBuilder), unir contenido
             if isinstance(prompt_builder_output["prompt"], list):
                 generated_prompt = "\n".join([msg.content for msg in prompt_builder_output["prompt"] if hasattr(msg, 'content')])
             elif isinstance(prompt_builder_output["prompt"], str):
                  generated_prompt = prompt_builder_output["prompt"]


        if not retrieved_docs:
            run_log.warning("No relevant documents found by retriever.")
            # Decidir qué hacer: ¿Intentar responder sin contexto? ¿Devolver mensaje específico?
            # Por ahora, intentaremos responder indicando la falta de contexto.
            # Podríamos crear un prompt alternativo o dejar que Gemini diga "No sé".
            # Ajustamos el prompt para reflejar esto si es necesario, o confiamos en la plantilla por defecto.
            if not generated_prompt: # Si el prompt builder no generó nada (raro)
                 generated_prompt = f"Pregunta: {query}\n\nNo se encontraron documentos relevantes. Intenta responder brevemente si es posible, o indica que no tienes información."


        if not generated_prompt:
             run_log.error("Failed to extract generated prompt from pipeline output", output=prompt_builder_output)
             raise ValueError("Could not construct prompt for LLM.")

        run_log.debug("Generated prompt for LLM", prompt_preview=generated_prompt[:200] + "...")

        # Call Gemini LLM with the generated prompt
        answer = await gemini_client.generate_answer(generated_prompt)
        run_log.info("Answer generated by Gemini", answer_preview=answer[:100] + "...")

        # Log the interaction (asynchronously)
        log_id: Optional[uuid.UUID] = None
        try:
            # Preparar datos para logging
            doc_ids = [doc.id for doc in retrieved_docs]
            doc_scores = [doc.score for doc in retrieved_docs if doc.score is not None] # Asegurarse de que score no es None
            # Convertir user_id a UUID si existe
            user_uuid = uuid.UUID(user_id) if user_id else None
            log_id = await postgres_client.log_query_interaction(
                company_id=uuid.UUID(company_id), # Convertir a UUID
                user_id=user_uuid,
                query=query,
                response=answer,
                retrieved_doc_ids=doc_ids,
                retrieved_doc_scores=doc_scores,
                metadata={"retriever_top_k": retriever_top_k} # Ejemplo de metadata adicional
            )
        except Exception as log_err:
             run_log.error("Failed to log query interaction to database", error=str(log_err), exc_info=True)
             # No fallar la respuesta al usuario, pero loguear el error. log_id será None.

        return answer, retrieved_docs, log_id

    except Exception as e:
        run_log.exception("Error occurred during RAG pipeline execution")
        raise # Re-lanzar para que el endpoint lo maneje

# Función para verificar dependencias (llamada desde health check)
async def check_pipeline_dependencies() -> Dict[str, str]:
    results = {"milvus_connection": "pending"}
    try:
        store = get_milvus_document_store()
        # Intentar una operación simple, como contar documentos (si existe y es rápida)
        # o simplemente verificar la conexión si el store lo permite.
        # MilvusDocumentStore no tiene un método 'ping' directo expuesto.
        # Podríamos intentar contar documentos con un límite muy bajo.
        count = await asyncio.to_thread(store.count_documents) # Ejecutar sync en thread
        results["milvus_connection"] = "ok"
        log.debug("Milvus dependency check successful (count documents)")
    except Exception as e:
        results["milvus_connection"] = f"error: {type(e).__name__}"
        log.warning("Milvus dependency check failed", error=str(e))
    return results