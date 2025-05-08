# query-service/app/application/use_cases/ask_query_use_case.py
import structlog
import asyncio
import uuid
import re
from typing import Dict, Any, List, Tuple, Optional, Type, Set
from datetime import datetime, timezone, timedelta

# Import Ports and Domain Models
from app.application.ports import (
    ChatRepositoryPort, LogRepositoryPort, VectorStorePort, LLMPort,
    SparseRetrieverPort, RerankerPort, DiversityFilterPort, ChunkContentRepositoryPort
)
from app.domain.models import RetrievedChunk, ChatMessage

# Keep necessary components (Embedder, PromptBuilder)
try:
    import bm2s
except ImportError:
    bm2s = None

from haystack_integrations.components.embedders.fastembed import FastembedTextEmbedder
from haystack.components.builders.prompt_builder import PromptBuilder
from haystack import Document # Still needed for PromptBuilder context

from app.core.config import settings
from app.api.v1.schemas import RetrievedDocument as RetrievedDocumentSchema # For logging format
from app.utils.helpers import truncate_text
from fastapi import HTTPException, status

log = structlog.get_logger(__name__)

GREETING_REGEX = re.compile(r"^\s*(hola|hello|hi|buenos días|buenas tardes|buenas noches|hey|qué tal|hi there)\s*[\.,!?]*\s*$", re.IGNORECASE)

# RRF Constant
RRF_K = 60
# --- LLM_CORRECTION: Define a constant or setting for max chunks BEFORE reranking ---
MAX_CHUNKS_BEFORE_RERANK = 30 # Limit chunks sent to CPU reranker
# --- END CORRECTION ---

# --- LLM_FEATURE: Helper function for simple time delta formatting ---
def format_time_delta(dt: datetime) -> str:
    # ... (código existente sin cambios)
    now = datetime.now(timezone.utc)
    delta = now - dt
    if delta < timedelta(minutes=1):
        return "justo ahora"
    elif delta < timedelta(hours=1):
        minutes = int(delta.total_seconds() / 60)
        return f"hace {minutes} min" if minutes > 1 else "hace 1 min"
    elif delta < timedelta(days=1):
        hours = int(delta.total_seconds() / 3600)
        return f"hace {hours} h" if hours > 1 else "hace 1 h"
    else:
        days = delta.days
        return f"hace {days} días" if days > 1 else "hace 1 día"


class AskQueryUseCase:
    """
    Caso de uso para procesar una consulta de usuario, manejar el chat y ejecutar el pipeline RAG completo.
    """
    def __init__(self,
                 chat_repo: ChatRepositoryPort,
                 log_repo: LogRepositoryPort,
                 vector_store: VectorStorePort,
                 llm: LLMPort,
                 # Optional components for advanced RAG
                 sparse_retriever: Optional[SparseRetrieverPort] = None,
                 chunk_content_repo: Optional[ChunkContentRepositoryPort] = None,
                 reranker: Optional[RerankerPort] = None,
                 diversity_filter: Optional[DiversityFilterPort] = None):
        self.chat_repo = chat_repo
        self.log_repo = log_repo
        self.vector_store = vector_store
        self.llm = llm
        self.sparse_retriever = sparse_retriever if settings.BM25_ENABLED and bm2s is not None else None
        self.chunk_content_repo = chunk_content_repo
        self.reranker = reranker if settings.RERANKER_ENABLED else None
        self.diversity_filter = diversity_filter if settings.DIVERSITY_FILTER_ENABLED else None

        self._embedder = self._initialize_embedder()
        self._prompt_builder_rag = self._initialize_prompt_builder(settings.RAG_PROMPT_TEMPLATE)
        self._prompt_builder_general = self._initialize_prompt_builder(settings.GENERAL_PROMPT_TEMPLATE)

        log.info("AskQueryUseCase Initialized",
                 bm25_enabled=bool(self.sparse_retriever),
                 reranker_enabled=bool(self.reranker),
                 diversity_filter_enabled=settings.DIVERSITY_FILTER_ENABLED,
                 diversity_filter_type=type(self.diversity_filter).__name__ if self.diversity_filter else "None"
                 )
        if settings.BM25_ENABLED and bm2s is None:
             log.error("BM25_ENABLED is true in settings, but 'bm2s' library is not installed. Sparse retrieval will be skipped.")
        if self.sparse_retriever and not self.chunk_content_repo:
            log.error("SparseRetriever is enabled but ChunkContentRepositoryPort is missing!")
        if settings.RERANKER_ENABLED and self.reranker and not getattr(self.reranker, 'model', None):
             log.warning("RERANKER_ENABLED is true, but the reranker model failed to load during initialization.")


    def _initialize_embedder(self) -> FastembedTextEmbedder:
        # ... (código existente sin cambios)
        embedder_log = log.bind(component="FastembedTextEmbedder", model=settings.FASTEMBED_MODEL_NAME)
        embedder_log.debug("Initializing FastEmbed Embedder...")
        try:
            embedder = FastembedTextEmbedder(
                model=settings.FASTEMBED_MODEL_NAME,
                prefix=settings.FASTEMBED_QUERY_PREFIX
            )
            embedder_log.info("FastEmbed Embedder initialized.")
            return embedder
        except Exception as e:
            embedder_log.exception("Failed to initialize FastEmbed Embedder")
            raise RuntimeError(f"Could not initialize embedding model: {e}") from e

    def _initialize_prompt_builder(self, template: str) -> PromptBuilder:
        # ... (código existente sin cambios)
        log.debug("Initializing PromptBuilder...", template_start=template[:100]+"...")
        return PromptBuilder(template=template)

    async def _embed_query(self, query: str) -> List[float]:
        # ... (código existente sin cambios)
        embed_log = log.bind(action="embed_query_use_case")
        try:
            result = await asyncio.to_thread(self._embedder.run, text=query)
            embedding = result.get("embedding")
            if not embedding: raise ValueError("Embedding returned no vector.")
            if len(embedding) != settings.EMBEDDING_DIMENSION: raise ValueError("Embedding dimension mismatch.")
            embed_log.debug("Query embedded successfully", vector_dim=len(embedding))
            return embedding
        except Exception as e:
            embed_log.error("Embedding failed", error=str(e), exc_info=True)
            raise ConnectionError(f"Embedding service error: {e}") from e

    def _format_chat_history(self, messages: List[ChatMessage]) -> str:
        # ... (código existente sin cambios)
        if not messages:
            return ""
        history_str = []
        for msg in reversed(messages): # Show newest first in context? Or oldest? Let's try oldest first for chronology.
            role = "Usuario" if msg.role == 'user' else "Atenex"
            time_mark = format_time_delta(msg.created_at)
            history_str.append(f"{role} ({time_mark}): {msg.content}")
        # Reverse back to chronological for the LLM
        return "\n".join(reversed(history_str))

    async def _build_prompt(self, query: str, documents: List[Document], chat_history: Optional[str] = None) -> str:
        # ... (código existente sin cambios)
        builder_log = log.bind(action="build_prompt_use_case", num_docs=len(documents), history_included=bool(chat_history))
        prompt_data = {"query": query}
        try:
            if documents:
                prompt_builder = self._prompt_builder_rag
                prompt_data["documents"] = documents
                builder_log.debug("Using RAG prompt template.")
            else:
                prompt_builder = self._prompt_builder_general
                builder_log.debug("Using General prompt template.")

            if chat_history:
                prompt_data["chat_history"] = chat_history

            result = await asyncio.to_thread(prompt_builder.run, **prompt_data)
            prompt = result.get("prompt")
            if not prompt: raise ValueError("Prompt generation returned empty.")

            builder_log.debug("Prompt built successfully.")
            return prompt
        except Exception as e:
            builder_log.error("Prompt building failed", error=str(e), exc_info=True)
            raise ValueError(f"Prompt building error: {e}") from e

    def _reciprocal_rank_fusion(self,
                                dense_results: List[RetrievedChunk],
                                sparse_results: List[Tuple[str, float]],
                                k: int = RRF_K) -> Dict[str, float]:
        # ... (código existente sin cambios)
        fused_scores: Dict[str, float] = {}
        for rank, chunk in enumerate(dense_results):
            if chunk.id: # Ensure chunk has an ID
                 fused_scores[chunk.id] = fused_scores.get(chunk.id, 0.0) + 1.0 / (k + rank + 1)
        for rank, (chunk_id, _) in enumerate(sparse_results):
             if chunk_id: # Ensure chunk_id is valid
                 fused_scores[chunk_id] = fused_scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
        return fused_scores

    async def _fetch_content_for_fused_results(
        self,
        fused_scores: Dict[str, float],
        dense_map: Dict[str, RetrievedChunk],
        top_n: int
        ) -> List[RetrievedChunk]:
        # ... (código existente sin cambios)
        fetch_log = log.bind(action="fetch_content_for_fused", top_n=top_n, fused_count=len(fused_scores))
        if not fused_scores: return []

        sorted_chunk_ids = sorted(fused_scores.items(), key=lambda item: item[1], reverse=True)
        top_ids = [cid for cid, score in sorted_chunk_ids[:top_n]]
        fetch_log.debug("Top IDs after fusion", top_ids_count=len(top_ids))

        chunks_with_content: List[RetrievedChunk] = []
        ids_needing_content: List[str] = []
        final_scores: Dict[str, float] = dict(sorted_chunk_ids[:top_n])
        placeholder_map: Dict[str, RetrievedChunk] = {}

        for cid in top_ids:
            if not cid:
                 fetch_log.warning("Skipping invalid chunk ID found during fusion processing.")
                 continue
            if cid in dense_map and dense_map[cid].content:
                chunk = dense_map[cid]
                chunk.score = final_scores[cid]
                chunks_with_content.append(chunk)
            else:
                chunk_placeholder = dense_map.get(cid) or RetrievedChunk(id=cid, score=final_scores[cid], content=None, metadata={"retrieval_source": "sparse/fused" if cid not in dense_map else "dense_nocontent"})
                chunk_placeholder.score = final_scores[cid]
                chunks_with_content.append(chunk_placeholder)
                placeholder_map[cid] = chunk_placeholder
                ids_needing_content.append(cid)

        if ids_needing_content and self.chunk_content_repo:
             fetch_log.info("Fetching content for chunks missing content", count=len(ids_needing_content))
             try:
                 content_map = await self.chunk_content_repo.get_chunk_contents_by_ids(ids_needing_content)
                 for cid, content in content_map.items():
                     if cid in placeholder_map:
                          placeholder_map[cid].content = content
                          placeholder_map[cid].metadata["content_fetched"] = True
                 missing_after_fetch = [cid for cid in ids_needing_content if cid not in content_map]
                 if missing_after_fetch:
                      fetch_log.warning("Content not found for some chunks after fetch", missing_ids=missing_after_fetch)
             except Exception as e:
                 fetch_log.exception("Failed to fetch content for fused results", error=str(e))
        elif ids_needing_content: fetch_log.warning("Cannot fetch content for sparse/fused results, ChunkContentRepository not available.")

        final_chunks = [c for c in chunks_with_content if c.content]
        fetch_log.debug("Chunks remaining after content check", count=len(final_chunks))
        final_chunks.sort(key=lambda c: c.score or 0.0, reverse=True)
        return final_chunks


    async def execute(
        self, query: str, company_id: uuid.UUID, user_id: uuid.UUID,
        chat_id: Optional[uuid.UUID] = None, top_k: Optional[int] = None
    ) -> Tuple[str, List[RetrievedChunk], Optional[uuid.UUID], uuid.UUID]:
        """ Orchestrates the full RAG pipeline """
        exec_log = log.bind(use_case="AskQueryUseCase", company_id=str(company_id), user_id=str(user_id), query=truncate_text(query, 50))
        retriever_k = top_k if top_k is not None and 0 < top_k <= settings.RETRIEVER_TOP_K else settings.RETRIEVER_TOP_K
        exec_log = exec_log.bind(effective_retriever_k=retriever_k)

        pipeline_stages_used = ["dense_retrieval", "llm_generation"]
        final_chat_id: uuid.UUID
        log_id: Optional[uuid.UUID] = None
        chat_history_str: Optional[str] = None
        history_messages: List[ChatMessage] = [] # Keep track of messages retrieved

        try:
            # 1. Manage Chat State & Retrieve History
            # ... (código existente sin cambios)
            if chat_id:
                if not await self.chat_repo.check_chat_ownership(chat_id, user_id, company_id):
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chat not found or access denied.")
                final_chat_id = chat_id
                if settings.MAX_CHAT_HISTORY_MESSAGES > 0:
                    try:
                        history_messages = await self.chat_repo.get_chat_messages(
                            chat_id=final_chat_id, user_id=user_id, company_id=company_id,
                            limit=settings.MAX_CHAT_HISTORY_MESSAGES, offset=0
                        )
                        chat_history_str = self._format_chat_history(history_messages)
                        exec_log.info("Chat history retrieved and formatted", num_messages=len(history_messages))
                    except Exception as hist_err:
                        exec_log.error("Failed to retrieve chat history", error=str(hist_err))
            else:
                initial_title = f"Chat: {truncate_text(query, 40)}"
                final_chat_id = await self.chat_repo.create_chat(user_id=user_id, company_id=company_id, title=initial_title)
            exec_log = exec_log.bind(chat_id=str(final_chat_id))
            exec_log.info("Chat state managed", is_new=(not chat_id))

            # 2. Save User Message
            # ... (código existente sin cambios)
            await self.chat_repo.save_message(chat_id=final_chat_id, role='user', content=query)


            # 3. Handle Greetings
            # ... (código existente sin cambios)
            if GREETING_REGEX.match(query):
                answer = "¡Hola! ¿En qué puedo ayudarte hoy con la información de tus documentos?"
                await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=None)
                exec_log.info("Use case finished (greeting).")
                return answer, [], log_id, final_chat_id

            # --- RAG Pipeline ---
            exec_log.info("Proceeding with RAG pipeline...")

            # 4. Embed Query
            # ... (código existente sin cambios)
            query_embedding = await self._embed_query(query)

            # 5. Coarse Retrieval (Dense + Optional Sparse)
            # ... (código existente sin cambios)
            dense_task = self.vector_store.search(query_embedding, str(company_id), retriever_k)
            sparse_task = asyncio.create_task(asyncio.sleep(0))
            sparse_results: List[Tuple[str, float]] = []
            if self.sparse_retriever:
                 pipeline_stages_used.append("sparse_retrieval (bm2s)")
                 sparse_task = self.sparse_retriever.search(query, str(company_id), retriever_k)
            elif settings.BM25_ENABLED:
                 pipeline_stages_used.append("sparse_retrieval (skipped_no_lib)")
                 exec_log.warning("BM25 enabled but retriever instance is not available (likely missing library).")
            else:
                 pipeline_stages_used.append("sparse_retrieval (disabled)")

            dense_chunks, sparse_results_maybe = await asyncio.gather(dense_task, sparse_task)
            if self.sparse_retriever and isinstance(sparse_results_maybe, list):
                sparse_results = sparse_results_maybe
            exec_log.info("Retrieval phase completed", dense_count=len(dense_chunks), sparse_count=len(sparse_results), retriever_k=retriever_k)

            # 6. Fusion & Content Fetch
            # ... (código existente sin cambios)
            pipeline_stages_used.append("fusion (rrf)")
            dense_map = {c.id: c for c in dense_chunks if c.id}
            fused_scores = self._reciprocal_rank_fusion(dense_chunks, sparse_results)
            # Fetch enough to have options for reranker, but maybe not ALL possible chunks if fusion results are huge
            # Let's keep fetching MAX_CONTEXT_CHUNKS + buffer for now, as reranker is main bottleneck
            fusion_fetch_k = settings.MAX_CONTEXT_CHUNKS + 10
            combined_chunks_with_content = await self._fetch_content_for_fused_results(fused_scores, dense_map, fusion_fetch_k)
            exec_log.info("Fusion & Content Fetch completed", initial_fused_count=len(fused_scores), chunks_with_content=len(combined_chunks_with_content), fetch_limit=fusion_fetch_k)

            if not combined_chunks_with_content:
                 # ... (código existente sin cambios para caso sin documentos)
                 exec_log.warning("No chunks with content available after fusion/fetch.")
                 final_prompt = await self._build_prompt(query, [], chat_history=chat_history_str)
                 answer = await self.llm.generate(final_prompt)
                 await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=None)
                 try:
                    log_id = await self.log_repo.log_query_interaction(company_id=company_id, user_id=user_id, query=query, answer=answer, retrieved_documents_data=[], chat_id=final_chat_id, metadata={"pipeline_stages": pipeline_stages_used, "result": "no_docs_found"})
                 except Exception: exec_log.error("Failed to log interaction for no_docs case")
                 return answer, [], log_id, final_chat_id


            # 7. Reranking (Conditional & Optimized)
            chunks_to_process_further = combined_chunks_with_content
            if self.reranker and getattr(self.reranker, 'model', None):
                pipeline_stages_used.append("reranking (bge)")
                # --- LLM_CORRECTION: Limit chunks BEFORE reranking ---
                chunks_for_reranking = combined_chunks_with_content[:MAX_CHUNKS_BEFORE_RERANK]
                exec_log.info(f"Performing reranking on top {len(chunks_for_reranking)} chunks (limit: {MAX_CHUNKS_BEFORE_RERANK})...")
                reranked_top_chunks = await self.reranker.rerank(query, chunks_for_reranking)

                # Combine reranked top chunks with the rest (which were not reranked)
                # Keep the order: reranked first, then the rest in their original fused order
                remaining_chunks = [c for c in combined_chunks_with_content if c.id not in {rc.id for rc in reranked_top_chunks}]
                chunks_to_process_further = reranked_top_chunks + remaining_chunks
                # --- END CORRECTION ---
                exec_log.info("Reranking completed.", count_reranked=len(reranked_top_chunks), total_after_combining=len(chunks_to_process_further))
            elif self.reranker:
                 pipeline_stages_used.append("reranking (skipped_model_load_failed)")
                 exec_log.warning("Reranker enabled but model not loaded, skipping reranking step.")
            else:
                 pipeline_stages_used.append("reranking (disabled)")


            # 8. Apply Diversity Filter / Final Limit (Applied AFTER potential reranking)
            final_chunks_for_llm = chunks_to_process_further
            if self.diversity_filter:
                 k_final = settings.MAX_CONTEXT_CHUNKS
                 filter_type = type(self.diversity_filter).__name__
                 pipeline_stages_used.append(f"diversity_filter ({filter_type})")
                 exec_log.debug(f"Applying {filter_type} k={k_final}...", count=len(chunks_to_process_further))
                 # Pass the potentially re-ordered list (if reranked) to the filter
                 final_chunks_for_llm = await self.diversity_filter.filter(chunks_to_process_further, k_final)
                 exec_log.info(f"{filter_type} applied.", final_count=len(final_chunks_for_llm))
            else:
                 # Apply manual limit if diversity filter is disabled
                 # Use the list potentially re-ordered by reranker
                 final_chunks_for_llm = chunks_to_process_further[:settings.MAX_CONTEXT_CHUNKS]
                 exec_log.info(f"Diversity filter disabled. Truncating to MAX_CONTEXT_CHUNKS.", final_count=len(final_chunks_for_llm), limit=settings.MAX_CONTEXT_CHUNKS)

            # Ensure content exists for final chunks going to prompt builder
            final_chunks_for_llm = [c for c in final_chunks_for_llm if c.content]
            if not final_chunks_for_llm:
                 # ... (código existente sin cambios para caso sin documentos final)
                 exec_log.warning("No chunks with content remaining after final limiting/filtering.")
                 final_prompt = await self._build_prompt(query, [], chat_history=chat_history_str)
                 answer = await self.llm.generate(final_prompt)
                 await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=None)
                 try:
                    log_id = await self.log_repo.log_query_interaction(company_id=company_id, user_id=user_id, query=query, answer=answer, retrieved_documents_data=[], chat_id=final_chat_id, metadata={"pipeline_stages": pipeline_stages_used, "result": "no_docs_after_limit"})
                 except Exception: exec_log.error("Failed to log interaction for no_docs_after_limit case")
                 return answer, [], log_id, final_chat_id


            # 9. Build Prompt (with history)
            # ... (código existente sin cambios)
            haystack_docs_for_prompt = [
                Document(id=c.id, content=c.content, meta=c.metadata, score=c.score)
                for c in final_chunks_for_llm
            ]
            exec_log.info(f"Building RAG prompt with {len(haystack_docs_for_prompt)} final chunks and history.")
            final_prompt = await self._build_prompt(query, haystack_docs_for_prompt, chat_history=chat_history_str)

            # 10. Generate Answer
            # ... (código existente sin cambios)
            answer = await self.llm.generate(final_prompt)
            exec_log.info("LLM answer generated.", length=len(answer))

            # 11. Save Assistant Message & Limit Sources Shown
            # ... (código existente sin cambios)
            sources_to_show_count = settings.NUM_SOURCES_TO_SHOW
            assistant_sources = [
                {"chunk_id": c.id, "document_id": c.document_id, "file_name": c.file_name, "score": c.score, "preview": truncate_text(c.content, 150) if c.content else "Error: Contenido no disponible"}
                for c in final_chunks_for_llm[:sources_to_show_count]
            ]
            await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=assistant_sources or None)
            exec_log.info(f"Assistant message saved with top {len(assistant_sources)} sources.")


            # 12. Log Interaction
            # ... (código existente sin cambios)
            try:
                docs_for_log = [RetrievedDocumentSchema(**c.model_dump()).model_dump(exclude_none=True) for c in final_chunks_for_llm[:sources_to_show_count]]
                log_metadata = {
                    "pipeline_stages": pipeline_stages_used,
                    "retriever_k": retriever_k,
                    "fusion_fetch_k": fusion_fetch_k,
                    "max_context_chunks_limit": settings.MAX_CONTEXT_CHUNKS,
                    # --- LLM_CORRECTION: Log reranked count if applicable ---
                    "num_chunks_before_rerank": len(combined_chunks_with_content),
                    "num_chunks_actually_reranked": len(reranked_top_chunks) if self.reranker and getattr(self.reranker, 'model', None) else 0,
                    # --- END CORRECTION ---
                    "num_final_chunks_to_llm": len(final_chunks_for_llm),
                    "num_sources_shown": len(assistant_sources),
                    "chat_history_messages_included": len(history_messages),
                    "diversity_filter_enabled": settings.DIVERSITY_FILTER_ENABLED,
                    "reranker_enabled": settings.RERANKER_ENABLED,
                    "bm25_enabled": settings.BM25_ENABLED,
                 }
                log_id = await self.log_repo.log_query_interaction(
                    company_id=company_id, user_id=user_id, query=query, answer=answer,
                    retrieved_documents_data=docs_for_log, chat_id=final_chat_id, metadata=log_metadata
                )
                exec_log.info("Interaction logged successfully", db_log_id=str(log_id))
            except Exception as log_err:
                exec_log.error("Failed to log RAG interaction", error=str(log_err), exc_info=False)

            exec_log.info("Use case execution finished successfully.")
            return answer, final_chunks_for_llm[:sources_to_show_count], log_id, final_chat_id

        except ConnectionError as ce:
            # ... (código existente sin cambios)
             exec_log.error("Connection error during use case execution", error=str(ce), exc_info=True)
             raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"A dependency is unavailable: {ce}")
        except ValueError as ve:
             # ... (código existente sin cambios)
             exec_log.error("Value error during use case execution", error=str(ve), exc_info=True)
             raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Data processing error: {ve}")
        except HTTPException as http_exc: raise http_exc
        except Exception as e:
             # ... (código existente sin cambios)
             exec_log.exception("Unexpected error during use case execution")
             raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An internal error occurred: {type(e).__name__}")