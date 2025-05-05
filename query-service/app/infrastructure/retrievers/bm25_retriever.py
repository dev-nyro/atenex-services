# query-service/app/infrastructure/retrievers/bm25_retriever.py
import structlog
import asyncio
from typing import List, Tuple, Dict, Optional
import uuid
import time

# LLM_REFACTOR_STEP_2: Implement BM25 Adapter using bm25s
try:
    import bm2s
except ImportError:
    bm2s = None # Handle optional dependency

from app.application.ports.retrieval_ports import SparseRetrieverPort
from app.application.ports.repository_ports import ChunkContentRepositoryPort # To get content

log = structlog.get_logger(__name__)

class BM25sRetriever(SparseRetrieverPort):
    """
    Implementación de SparseRetrieverPort usando la librería bm25s.
    Este retriever construye un índice en memoria por consulta,
    lo cual puede ser intensivo en memoria y CPU para grandes volúmenes de datos.
    """

    def __init__(self, chunk_content_repo: ChunkContentRepositoryPort):
        if bm2s is None:
            log.error("bm2s library not installed. BM25 retrieval is disabled. "
                      "Install with: poetry add bm2s")
            raise ImportError("bm2s library is required for BM25sRetriever.")
        self.chunk_content_repo = chunk_content_repo
        log.info("BM25sRetriever initialized.")

    async def search(self, query: str, company_id: str, top_k: int) -> List[Tuple[str, float]]:
        """
        Busca chunks usando BM25s. Recupera todo el contenido de la compañía,
        construye el índice, tokeniza y busca.
        """
        search_log = log.bind(adapter="BM25sRetriever", action="search", company_id=company_id, top_k=top_k)
        search_log.debug("Starting BM25 search...")

        start_time = time.time()
        try:
            # 1. Obtener contenido de los chunks para la compañía
            search_log.info("Fetching chunk contents for BM25 index...")
            # Convert company_id string back to UUID for repository call
            try:
                company_uuid = uuid.UUID(company_id)
            except ValueError:
                 search_log.error("Invalid company_id format for UUID conversion", provided_id=company_id)
                 return []

            contents_map: Dict[str, str] = await self.chunk_content_repo.get_chunk_contents_by_company(company_uuid)

            if not contents_map:
                search_log.warning("No chunk content found for company to build BM25 index.")
                return []

            fetch_time = time.time()
            search_log.info(f"Fetched content for {len(contents_map)} chunks.", duration_ms=(fetch_time - start_time) * 1000)

            # Preparar corpus y mapeo de IDs
            # chunk_ids_list = list(contents_map.keys()) # Mantener el orden
            # corpus = [contents_map[cid] for cid in chunk_ids_list]

            # Crear listas separadas para asegurar correspondencia índice <-> ID
            chunk_ids_list = []
            corpus = []
            for cid, content in contents_map.items():
                 if content and isinstance(content, str): # Asegurar que hay contenido y es string
                     chunk_ids_list.append(cid)
                     corpus.append(content)
                 else:
                    search_log.warning("Skipping chunk due to missing or invalid content", chunk_id=cid)

            if not corpus:
                 search_log.warning("Corpus is empty after filtering invalid content.")
                 return []

            # 2. Tokenizar (simple split por ahora, mejorar si es necesario)
            search_log.debug("Tokenizing query and corpus...")
            query_tokens = query.lower().split()
            # Usar bm2s para tokenizar el corpus (más eficiente)
            corpus_tokens = bm2s.tokenize(corpus) # bm2s tiene su propio tokenizador eficiente
            tokenize_time = time.time()
            search_log.debug("Tokenization complete.", duration_ms=(tokenize_time - fetch_time) * 1000)

            # 3. Crear y entrenar el índice BM25s
            search_log.debug("Indexing corpus with BM25s...")
            retriever = bm2s.BM25()
            retriever.index(corpus_tokens)
            index_time = time.time()
            search_log.debug("BM25s indexing complete.", duration_ms=(index_time - tokenize_time) * 1000)

            # 4. Realizar la búsqueda
            search_log.debug("Performing BM25s retrieval...")
            # `retrieve` devuelve (doc_indices, scores) para CADA consulta (aquí solo una)
            # k es el número máximo a recuperar por consulta.
            results_indices, results_scores = retriever.retrieve(
                bm2s.tokenize(query), # Tokenizar la consulta con bm2s también
                k=top_k
                )

            # Como solo hay una consulta, tomamos el primer elemento
            doc_indices = results_indices[0]
            scores = results_scores[0]
            retrieval_time = time.time()
            search_log.debug("BM25s retrieval complete.", duration_ms=(retrieval_time - index_time) * 1000, hits_found=len(doc_indices))

            # 5. Mapear resultados a (chunk_id, score)
            final_results: List[Tuple[str, float]] = []
            for i, score in zip(doc_indices, scores):
                if i < len(chunk_ids_list): # Check boundary
                     original_chunk_id = chunk_ids_list[i]
                     final_results.append((original_chunk_id, float(score))) # Asegurar float
                else:
                    search_log.error("BM25 returned index out of bounds", index=i, list_size=len(chunk_ids_list))


            total_time = time.time() - start_time
            search_log.info(f"BM25 search finished. Returning {len(final_results)} results.", total_duration_ms=total_time * 1000)

            # Devolver ordenado por score descendente (BM25s ya lo devuelve así)
            return final_results

        except ImportError:
             log.error("bm2s library is not available. Cannot perform BM25 search.")
             return []
        except Exception as e:
            search_log.exception("Error during BM25 search")
            # No relanzar ConnectionError aquí, ya que es un error interno de procesamiento
            return [] # Devolver vacío en caso de error interno