# query-service/app/pipelines/rag_pipeline.py
import structlog
import asyncio
import uuid
from typing import Dict, Any, List, Tuple, Optional

# --- Direct pymilvus Imports ---
from pymilvus import Collection, connections, utility, MilvusException, DataType # Added DataType
# -----------------------------
from fastapi import HTTPException, status
from haystack import Document # Keep Haystack Document import
# Import FastEmbed Text Embedder for Haystack
from haystack_integrations.components.embedders.fastembed import FastembedTextEmbedder
from haystack.components.builders.prompt_builder import PromptBuilder
# --- REMOVED Milvus Haystack Imports ---
# from milvus_haystack import MilvusDocumentStore, MilvusEmbeddingRetriever
# ------------------------------------
from haystack.utils import Secret

from app.core.config import settings
from app.db import postgres_client
from app.services.gemini_client import gemini_client # Keep Gemini client import
from app.api.v1.schemas import RetrievedDocument # Import schema for logging

log = structlog.get_logger(__name__)

# --- Milvus Connection Management (using pymilvus) ---
_milvus_collection: Optional[Collection] = None
_milvus_connected = False

def _ensure_milvus_connection_and_collection() -> Collection:
    """Ensures connection to Milvus and returns the Collection object using pymilvus."""
    global _milvus_collection, _milvus_connected
    alias = "query_service_pymilvus" # Unique alias for this service
    if not _milvus_connected or alias not in connections.list_connections():
        uri = str(settings.MILVUS_URI)
        connect_log = log.bind(component="PymilvusConnection", uri=uri, alias=alias)
        connect_log.info("Connecting to Milvus...")
        try:
            connections.connect(alias=alias, uri=uri, timeout=settings.MILVUS_GRPC_TIMEOUT) # Use timeout from config
            _milvus_connected = True
            connect_log.info("Connected to Milvus successfully.")
        except MilvusException as e:
            connect_log.error("Failed to connect to Milvus.", error=str(e))
            _milvus_connected = False
            raise ConnectionError(f"Milvus connection failed: {e}") from e
        except Exception as e:
            connect_log.error("Unexpected error connecting to Milvus.", error=str(e))
            _milvus_connected = False
            raise ConnectionError(f"Unexpected Milvus connection error: {e}") from e

    if _milvus_collection is None:
        collection_name = settings.MILVUS_COLLECTION_NAME
        collection_log = log.bind(component="PymilvusCollection", collection=collection_name, alias=alias)
        try:
            if not utility.has_collection(collection_name, using=alias):
                 collection_log.error("Milvus collection does not exist. It should be created by the ingest service.")
                 # Raise an error because query service shouldn't create the collection
                 raise RuntimeError(f"Milvus collection '{collection_name}' not found.")

            collection = Collection(name=collection_name, using=alias)
            # Load the collection into memory for searching
            collection_log.info("Loading Milvus collection into memory...")
            collection.load()
            collection_log.info("Milvus collection loaded successfully.")
            _milvus_collection = collection

        except MilvusException as e:
            collection_log.error("Failed to get or load Milvus collection", error=str(e))
            raise RuntimeError(f"Milvus collection access error: {e}") from e
        except Exception as e:
             collection_log.exception("Unexpected error accessing Milvus collection")
             raise RuntimeError(f"Unexpected error accessing Milvus collection: {e}") from e


    if not isinstance(_milvus_collection, Collection):
        log.critical("Milvus collection object is unexpectedly None or invalid type after initialization attempt.")
        raise RuntimeError("Failed to obtain a valid Milvus collection object.")

    return _milvus_collection

# --- Component Initialization Functions (Embedder, PromptBuilder - No Change) ---

def get_fastembed_text_embedder() -> FastembedTextEmbedder:
    """Initializes and returns a FastembedTextEmbedder instance."""
    embedder_log = log.bind(
        component="FastembedTextEmbedder",
        model=settings.FASTEMBED_MODEL_NAME,
        prefix=settings.FASTEMBED_QUERY_PREFIX,
        dimension=settings.EMBEDDING_DIMENSION
    )
    embedder_log.debug("Initializing...")
    embedder = FastembedTextEmbedder(
        model=settings.FASTEMBED_MODEL_NAME,
        prefix=settings.FASTEMBED_QUERY_PREFIX
    )
    embedder_log.info("Initialization successful.")
    return embedder

def get_prompt_builder(template: str) -> PromptBuilder:
    """Initializes PromptBuilder with a given template."""
    log.debug("Initializing PromptBuilder...")
    return PromptBuilder(template=template)

# --- Pipeline Execution Logic ---

async def embed_query(query: str) -> List[float]:
    """Embeds the user query using FastEmbed."""
    embed_log = log.bind(action="embed_query")
    try:
        embedder = get_fastembed_text_embedder()
        await asyncio.to_thread(embedder.warm_up)
        result = await asyncio.to_thread(embedder.run, text=query)
        embedding = result.get("embedding")
        if not embedding:
            raise ValueError("Embedding process returned no embedding vector.")
        if len(embedding) != settings.EMBEDDING_DIMENSION:
            embed_log.error("Embedding dimension mismatch!",
                            expected=settings.EMBEDDING_DIMENSION,
                            actual=len(embedding),
                            model=settings.FASTEMBED_MODEL_NAME)
            raise ValueError(f"Embedding dimension mismatch: expected {settings.EMBEDDING_DIMENSION}, got {len(embedding)}")
        embed_log.info("Query embedded successfully", vector_dim=len(embedding))
        return embedding
    except Exception as e:
        embed_log.error("Embedding failed", error=str(e), exc_info=True)
        raise ConnectionError(f"Embedding service error: {e}") from e

# --- REFACTORED: Retrieve documents using pymilvus ---
async def search_milvus_directly(embedding: List[float], company_id: str, top_k: int) -> List[Document]:
    """
    Retrieves relevant documents directly from Milvus using pymilvus search.
    Converts results to Haystack Document objects.
    """
    search_log = log.bind(action="search_milvus_directly", company_id=company_id, top_k=top_k)
    try:
        collection = _ensure_milvus_connection_and_collection()

        # Define search parameters
        search_params = settings.MILVUS_SEARCH_PARAMS

        # Define the filter expression
        filter_expr = f'{settings.MILVUS_COMPANY_ID_FIELD} == "{company_id}"'
        search_log.debug("Using filter expression", expr=filter_expr)

        # Define output fields
        output_fields = [
            settings.MILVUS_CONTENT_FIELD,
            settings.MILVUS_COMPANY_ID_FIELD, # Keep for potential verification
            settings.MILVUS_DOCUMENT_ID_FIELD,
            settings.MILVUS_FILENAME_FIELD,
            # Add other metadata fields from MILVUS_METADATA_FIELDS if needed
        ]
        # Ensure PK field is not requested if auto_id=True (it comes in hit.id)
        # If PK is not auto_id and needed, add it here.

        search_log.info("Performing Milvus vector search...", vector_field=settings.MILVUS_EMBEDDING_FIELD)

        # Execute search asynchronously using run_in_executor
        loop = asyncio.get_running_loop()
        search_results = await loop.run_in_executor(
            None, # Use default executor
            lambda: collection.search(
                data=[embedding], # Search data expects a list of vectors
                anns_field=settings.MILVUS_EMBEDDING_FIELD,
                param=search_params,
                limit=top_k,
                expr=filter_expr,
                output_fields=output_fields,
                consistency_level="Strong" # Or match consistency level used in ingest
            )
        )

        search_log.info(f"Milvus search completed. Found hits: {len(search_results[0]) if search_results else 0}")

        # Convert Milvus results to Haystack Documents
        haystack_documents: List[Document] = []
        if search_results and search_results[0]:
            for hit in search_results[0]:
                entity = hit.entity # Contains the output fields
                content = entity.get(settings.MILVUS_CONTENT_FIELD, "")
                metadata = {
                    # Include all retrieved output fields (except content/embedding) as metadata
                    field: entity.get(field) for field in output_fields if field != settings.MILVUS_CONTENT_FIELD
                }
                # Add standard fields if not already present
                metadata.setdefault("company_id", entity.get(settings.MILVUS_COMPANY_ID_FIELD))
                metadata.setdefault("document_id", entity.get(settings.MILVUS_DOCUMENT_ID_FIELD))
                metadata.setdefault("file_name", entity.get(settings.MILVUS_FILENAME_FIELD))

                doc = Document(
                    id=str(hit.id), # Use Milvus primary key as Haystack ID
                    content=content,
                    meta=metadata,
                    score=hit.score # Or hit.distance depending on metric
                )
                haystack_documents.append(doc)

        search_log.info(f"Converted {len(haystack_documents)} Milvus hits to Haystack Documents.")
        return haystack_documents

    except MilvusException as me:
         search_log.error("Milvus search failed", error_code=me.code, error_message=me.message, exc_info=False)
         raise ConnectionError(f"Vector DB search error (Collection: {settings.MILVUS_COLLECTION_NAME}, Milvus code: {me.code})") from me
    except Exception as e:
        search_log.exception("Direct Milvus search failed")
        raise ConnectionError(f"Milvus search service error: {e}") from e
# --- End Refactored Search ---


async def build_prompt(query: str, documents: List[Document]) -> str:
    """Builds the final prompt for the LLM, selecting template based on retrieved documents."""
    build_log = log.bind(action="build_prompt", num_docs=len(documents))
    try:
        if documents:
            template = settings.RAG_PROMPT_TEMPLATE
            prompt_builder = get_prompt_builder(template)
            result = await asyncio.to_thread(prompt_builder.run, query=query, documents=documents)
            build_log.info("RAG prompt built")
        else:
            template = settings.GENERAL_PROMPT_TEMPLATE
            prompt_builder = get_prompt_builder(template)
            result = await asyncio.to_thread(prompt_builder.run, query=query)
            build_log.info("General prompt built (no documents retrieved)")

        prompt = result.get("prompt")
        if not prompt:
            raise ValueError("Prompt generation returned empty prompt.")
        return prompt
    except Exception as e:
        build_log.error("Prompt building failed", error=str(e), exc_info=True)
        raise ValueError(f"Prompt building error: {e}") from e

async def generate_llm_answer(prompt: str) -> str:
    """Generates the final answer using the Gemini client."""
    llm_log = log.bind(action="generate_llm_answer", model=settings.GEMINI_MODEL_NAME)
    try:
        answer = await gemini_client.generate_answer(prompt)
        llm_log.info("Answer generated successfully", answer_length=len(answer))
        return answer
    except Exception as e:
        llm_log.error("LLM generation failed", error=str(e), exc_info=True)
        raise ConnectionError(f"LLM service error: {e}") from e

async def run_rag_pipeline(
    query: str,
    company_id: str,
    user_id: Optional[str],
    top_k: Optional[int] = None,
    chat_id: Optional[uuid.UUID] = None
) -> Tuple[str, List[Document], Optional[uuid.UUID]]:
    """
    Orchestrates the RAG pipeline steps: embed, retrieve (using pymilvus), build prompt, generate answer, log interaction.
    """
    run_log = log.bind(company_id=company_id, user_id=user_id or "N/A", chat_id=str(chat_id) if chat_id else "N/A", query=query[:50]+"...")
    run_log.info("Executing RAG pipeline (using direct pymilvus search)")
    retriever_k = top_k or settings.RETRIEVER_TOP_K
    log_id: Optional[uuid.UUID] = None

    try:
        # 1. Embed Query
        query_embedding = await embed_query(query)

        # --- REPLACED Haystack Retriever with direct pymilvus search ---
        # 2. Retrieve Documents using pymilvus
        retrieved_docs = await search_milvus_directly(query_embedding, company_id, retriever_k)
        # ------------------------------------------------------------

        # 3. Build Prompt
        final_prompt = await build_prompt(query, retrieved_docs)

        # 4. Generate Answer
        answer = await generate_llm_answer(final_prompt)

        # 5. Log Interaction (Best effort)
        try:
            docs_for_log = [RetrievedDocument.from_haystack_doc(d).model_dump(exclude_none=True)
                            for d in retrieved_docs]
            log_id = await postgres_client.log_query_interaction(
                company_id=uuid.UUID(company_id),
                user_id=uuid.UUID(user_id) if user_id else None,
                query=query, answer=answer,
                retrieved_documents_data=docs_for_log,
                chat_id=chat_id,
                metadata={"top_k": retriever_k, "llm_model": settings.GEMINI_MODEL_NAME, "embedder_model": settings.FASTEMBED_MODEL_NAME, "num_retrieved": len(retrieved_docs)}
            )
            run_log.info("Interaction logged successfully", db_log_id=str(log_id))
        except Exception as log_err:
            run_log.error("Failed to log RAG interaction to DB", error=str(log_err), exc_info=False)

        run_log.info("RAG pipeline completed successfully")
        return answer, retrieved_docs, log_id

    except ConnectionError as ce:
        run_log.error("Connection error during RAG pipeline", error=str(ce), exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"A dependency is unavailable: {ce}")
    except ValueError as ve:
        run_log.error("Value error during RAG pipeline", error=str(ve), exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Data processing error: {ve}")
    except Exception as e:
        run_log.exception("Unexpected error during RAG pipeline execution")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An internal error occurred.")

# --- Dependency Check Function ---
async def check_pipeline_dependencies() -> Dict[str, str]:
    """Checks the status of pipeline dependencies (Milvus via pymilvus, Gemini)."""
    results = {"milvus_connection": "pending", "gemini_api": "pending", "fastembed_model": "pending"}
    check_log = log.bind(action="check_dependencies")

    # Check Milvus (using direct pymilvus connection)
    try:
        # Attempt to get collection - this implicitly checks connection
        _ = _ensure_milvus_connection_and_collection()
        results["milvus_connection"] = "ok"
        check_log.debug("Milvus dependency check via pymilvus: Connection ok, collection loaded.")
    except MilvusException as me:
        results["milvus_connection"] = f"error: MilvusException (code={me.code}, msg={me.message})"
        check_log.warning("Milvus dependency check via pymilvus failed", error_code=me.code, error_message=me.message, exc_info=False)
    except RuntimeError as re: # Catch errors from _ensure_milvus...
         results["milvus_connection"] = f"error: RuntimeError during connection/load ({re})"
         check_log.warning("Milvus dependency check via pymilvus failed", error=str(re), exc_info=False)
    except Exception as e:
        results["milvus_connection"] = f"error: Unexpected {type(e).__name__}"
        check_log.warning("Milvus dependency check via pymilvus failed", error=str(e), exc_info=True)

    # Check Gemini (API Key Presence)
    if settings.GEMINI_API_KEY.get_secret_value():
        results["gemini_api"] = "key_present"
        check_log.debug("Gemini dependency check: API Key is present.")
    else:
        results["gemini_api"] = "key_missing"
        check_log.warning("Gemini dependency check: API Key is MISSING.")

    # Check FastEmbed (Model loading is lazy, just check config)
    if settings.FASTEMBED_MODEL_NAME and settings.EMBEDDING_DIMENSION:
         results["fastembed_model"] = f"configured ({settings.FASTEMBED_MODEL_NAME}, dim={settings.EMBEDDING_DIMENSION})"
         check_log.debug("FastEmbed dependency check: Model configured.", model=settings.FASTEMBED_MODEL_NAME, dim=settings.EMBEDDING_DIMENSION)
    else:
         results["fastembed_model"] = "config_missing"
         check_log.error("FastEmbed dependency check: Model name or dimension MISSING in configuration.")

    return results