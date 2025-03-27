from typing import List, Dict, Any, Optional
from pymilvus import connections, Collection, FieldSchema, CollectionSchema, DataType, utility
import structlog

from app.core.config import settings

log = structlog.get_logger(__name__)

_collection: Optional[Collection] = None

def connect_milvus():
    """Establece la conexión con Milvus."""
    global _collection
    if _collection is None:
        try:
            log.info("Connecting to Milvus...", uri=settings.MILVUS_URI)
            connections.connect("default", uri=settings.MILVUS_URI)
            log.info("Connected to Milvus successfully.")
            _collection = _get_or_create_collection()
        except Exception as e:
            log.error("Failed to connect to Milvus", error=e, uri=settings.MILVUS_URI, exc_info=True)
            raise

def disconnect_milvus():
    """Desconecta de Milvus."""
    try:
        connections.disconnect("default")
        log.info("Disconnected from Milvus.")
    except Exception as e:
        log.error("Error disconnecting from Milvus", error=e, exc_info=True)

def _get_or_create_collection() -> Collection:
    """Obtiene la colección o la crea si no existe."""
    collection_name = settings.MILVUS_COLLECTION_NAME
    if utility.has_collection(collection_name):
        log.info(f"Collection '{collection_name}' already exists.")
        collection = Collection(name=collection_name)
        collection.load() # Asegurar que esté cargada en memoria para búsquedas
        return collection
    else:
        log.info(f"Collection '{collection_name}' not found. Creating...")
        # Definir esquema (asegúrate que coincida con tus datos y necesidades de búsqueda)
        # Usamos el ID de chunk de Postgres como PK en Milvus para facilitar la relación
        chunk_db_id = FieldSchema(name="chunk_db_id", dtype=DataType.VARCHAR, is_primary=True, max_length=36)
        doc_id_field = FieldSchema(name="document_id", dtype=DataType.VARCHAR, max_length=36)
        company_id_field = FieldSchema(name="company_id", dtype=DataType.VARCHAR, max_length=36, is_partition_key=False) # O True si particionas por compañía
        embedding_field = FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=settings.MILVUS_DIMENSION)
        # Podrías añadir otros campos de metadatos si necesitas filtrar por ellos en Milvus
        # text_field = FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=65535) # Opcional

        schema = CollectionSchema(
            fields=[chunk_db_id, doc_id_field, company_id_field, embedding_field],
            description="Document Chunks Embeddings",
            enable_dynamic_field=False # O True si quieres añadir campos no definidos en el schema
        )

        collection = Collection(name=collection_name, schema=schema)
        log.info(f"Collection '{collection_name}' created.")

        # Crear índice (ajusta según tus necesidades de performance/precisión)
        index_params = {
            "metric_type": "L2", # O "IP" para inner product / cosine similarity
            "index_type": "IVF_FLAT", # O HNSW, etc.
            "params": {"nlist": 1024} # Ajustar nlist
        }
        collection.create_index(field_name="embedding", index_params=index_params)
        log.info(f"Index created for field 'embedding' in collection '{collection_name}'.")
        collection.load()
        log.info(f"Collection '{collection_name}' loaded into memory.")
        return collection

def insert_embeddings(
    company_id: str,
    document_id: str,
    chunks_with_embeddings: List[Dict[str, Any]]
) -> List[str]:
    """Inserta embeddings y metadatos en Milvus."""
    if not _collection:
        raise ConnectionError("Milvus connection not established.")

    entities = []
    for chunk_data in chunks_with_embeddings:
        if 'embedding' not in chunk_data or 'db_id' not in chunk_data:
            log.warning("Skipping chunk due to missing embedding or db_id", chunk_data=chunk_data)
            continue
        entities.append({
            "chunk_db_id": str(chunk_data['db_id']), # PK, debe ser string
            "document_id": document_id,
            "company_id": company_id,
            "embedding": chunk_data['embedding'],
            # "text": chunk_data.get('content', '') # Opcional
        })

    if not entities:
        log.warning("No valid entities to insert into Milvus", document_id=document_id)
        return []

    try:
        log.info(f"Inserting {len(entities)} embeddings into Milvus", document_id=document_id)
        mutation_result = _collection.insert(entities)
        _collection.flush() # Asegura que los datos se escriban en disco
        log.info(f"Successfully inserted {len(mutation_result.primary_keys)} embeddings", document_id=document_id, insert_count=mutation_result.insert_count)
        # Devolver los IDs de Milvus (que son los chunk_db_id que pasamos)
        return [str(pk) for pk in mutation_result.primary_keys]
    except Exception as e:
        log.error("Failed to insert embeddings into Milvus", error=e, document_id=document_id, num_entities=len(entities), exc_info=True)
        raise

# Podrías añadir funciones para borrar documentos/chunks si es necesario