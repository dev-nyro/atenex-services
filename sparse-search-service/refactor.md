# Plan de Refactorización: `sparse-search-service` v1.0.0

**Objetivo Principal:** Eliminar la indexación BM25 on-demand para mejorar drásticamente la latencia y reducir la carga en PostgreSQL. Los índices BM25 se precalcularán y almacenarán en Google Cloud Storage (GCS), con un caché LRU en memoria en cada réplica del servicio.

## 1. Arquitectura Propuesta

```mermaid
graph LR
    subgraph Cliente [Usuario via Query Service]
        QS[Query Service] -->|HTTP POST /api/v1/search| SSS_API[Sparse Search API]
    end

    subgraph SparseSearchService [sparse-search-service]
        direction LR
        SSS_API --> SSS_UC[LoadAndSearchIndexUseCase]
        SSS_UC --> SSS_LRUCache[(LRU Cache<br/>BM25 Instances + ID Maps)]
        SSS_LRUCache -- Cache Miss --> SSS_GCSStore[GCSIndexStorageAdapter]
        SSS_GCSStore -- Loads Index --> SSS_BM25Adapter[BM25Adapter]
        SSS_BM25Adapter -- Populates --> SSS_LRUCache
        SSS_LRUCache -- Cache Hit / Populated --> SSS_BM25Adapter
        SSS_BM25Adapter -- Performs Search --> SSS_UC
        SSS_UC --> SSS_API
    end
    
    subgraph GCS [Google Cloud Storage]
        direction TB
        BucketIndices["atenex-sparse-indices<br/>(Nuevo Bucket GCS)<br/>indices/{company_id}/bm25_index.bm2s<br/>indices/{company_id}/id_map.json"]
    end
    
    SSS_GCSStore <--> BucketIndices

    subgraph KubernetesCronJob [K8s CronJob: Index Builder]
        direction TB
        CronJobScript["index_builder_cronjob.py<br/>(dentro de la imagen del servicio)"]
        CronJobScript -- Lee Chunks --> PG_DB[(PostgreSQL<br/>'atenex' DB - Tabla 'document_chunks')]
        CronJobScript -- Construye y Dumpea --> SSS_BM25Adapter_BuilderTool[BM25Adapter (Herramienta)]
        SSS_BM25Adapter_BuilderTool -- Serializa --> LocalFiles["Índice Local<br/>(bm25_index.bm2s, id_map.json)"]
        CronJobScript -- Usa GCS Client --> BucketIndices
    end
    
    style SSS_LRUCache fill:#e6ffe6
    style BucketIndices fill:#ffe0b3
```

**Componentes Clave y Flujo:**

1.  **Query Service (Cliente):** Llama al endpoint `/api/v1/search` del `sparse-search-service` (sin cambios para el cliente).
2.  **Sparse Search API (`search_endpoint.py`):** Recibe la solicitud y la delega al `LoadAndSearchIndexUseCase`.
3.  **`LoadAndSearchIndexUseCase` (Nuevo/Modificado):**
    *   Intenta obtener el par `(BM25_instance, id_map)` del **LRU Cache** para la `company_id`.
    *   **Cache Hit:** Pasa la instancia BM25 y el `id_map` al `BM25Adapter` para la búsqueda.
    *   **Cache Miss:**
        *   Utiliza `GCSIndexStorageAdapter` para descargar `bm25_index.bm2s` y `id_map.json` desde GCS para la `company_id`.
        *   Si los archivos no existen en GCS (o hay error de descarga), se loguea la situación y se devuelven resultados vacíos (no se recurre a indexación on-demand).
        *   Si se descargan correctamente:
            *   El `BM25Adapter` (o el UseCase directamente) carga el índice (`bm2s.BM25.load()`) y el `id_map`.
            *   El par `(BM25_instance, id_map)` se almacena en el **LRU Cache**.
            *   Se procede con la búsqueda usando la instancia cargada.
4.  **LRU Cache (`cachetools.LRUCache`):** Almacena en memoria las instancias de `bm2s.BM25` ya cargadas y sus correspondientes `id_map`. Tendrá un tamaño máximo configurable.
5.  **`GCSIndexStorageAdapter` (Nuevo):** Adaptador para interactuar con GCS. Responsable de descargar y subir los archivos de índice (`bm25_index.bm2s`, `id_map.json`).
6.  **`BM25Adapter` (Modificado):**
    *   Su método `search` ya no construirá el índice. Recibirá una instancia de `bm2s.BM25` ya cargada y el `id_map` para realizar la búsqueda.
    *   Se añadirán métodos estáticos o auxiliares `dump_index(bm2s_instance, path)` y `load_index(path)` que serán utilizados tanto por el builder como por el runtime.
7.  **Bucket GCS `atenex-sparse-indices` (Nuevo):** Un bucket dedicado para almacenar los índices BM25 serializados y los `id_map.json`. Estructura: `indices/{company_id}/bm25_index.bm2s` y `indices/{company_id}/id_map.json`.
8.  **K8s CronJob (`index_builder_cronjob.py`):**
    *   Un script Python que se ejecuta periódicamente (ej. cada X horas).
    *   Se empaqueta dentro de la misma imagen Docker del `sparse-search-service`.
    *   Para una `company_id` (o todas):
        *   Utiliza `PostgresChunkContentRepository` (existente) para obtener todos los `embedding_id` y `content` de los chunks procesados.
        *   Construye el `corpus_texts` (lista de contenidos) y el `id_map` (lista de `embedding_id` en el mismo orden que `corpus_texts`).
        *   Utiliza `bm2s.BM25()` para indexar `corpus_texts`.
        *   Dumpea el índice (`retriever.dump()`) y guarda el `id_map` como JSON en archivos locales temporales.
        *   Utiliza `GCSIndexStorageAdapter` (o un cliente GCS directo en el script) para subir estos dos archivos a la ruta correcta en el bucket GCS, sobrescribiendo los existentes para esa compañía.

## 2. Proceso de Construcción Offline de Índices (CronJob)

El script `index_builder_cronjob.py` (a crear en `app/jobs/` o similar):

```python
# Pseudocódigo para app/jobs/index_builder_cronjob.py

# import argparse
# import asyncio
# import json
# import os
# import tempfile
# import uuid
# from pathlib import Path

# import bm2s
# import structlog

# from app.core.config import settings_for_builder # Una instancia de Settings adaptada
# from app.infrastructure.persistence.postgres_repositories import PostgresChunkContentRepository
# from app.infrastructure.persistence import postgres_connector # Para inicializar/cerrar pool
# from app.infrastructure.gcs_index_storage_adapter import GCSIndexStorageAdapter # Para subir

# log = structlog.get_logger("index_builder_cronjob")

# async def build_and_upload_index_for_company(company_id_str: str, repo: PostgresChunkContentRepository, gcs_adapter: GCSIndexStorageAdapter):
#     log.info("Starting index build for company", company_id=company_id_str)
#     company_uuid = uuid.UUID(company_id_str)

#     # 1. Obtener chunks de PostgreSQL
#     # chunks_data: List[Dict[str, Any]] con {'id': embedding_id, 'content': content}
#     chunks_data = await repo.get_chunks_with_metadata_by_company(company_uuid)
#     if not chunks_data:
#         log.warning("No chunks found for company. Skipping index build.", company_id=company_id_str)
#         return

#     corpus_texts = [chunk['content'] for chunk in chunks_data]
#     # id_map ESENCIAL: lista de chunk_ids (embedding_id) en el MISMO ORDEN que corpus_texts
#     id_map = [chunk['id'] for chunk in chunks_data]

#     if not corpus_texts:
#         log.warning("Corpus is empty after extracting content. Skipping.", company_id=company_id_str)
#         return

#     log.info(f"Building BM25 index for {len(corpus_texts)} chunks...", company_id=company_id_str)
#     retriever = bm2s.BM25() # Usar defaults de bm2s, o configurar k1 y b desde settings
#     retriever.index(corpus_texts)
#     log.info("BM25 index built.", company_id=company_id_str)

#     with tempfile.TemporaryDirectory() as tmpdir:
#         bm2s_file_path = Path(tmpdir) / "bm25_index.bm2s"
#         id_map_file_path = Path(tmpdir) / "id_map.json"

#         # 2. Dumpear índice y guardar id_map
#         retriever.dump(str(bm2s_file_path))
#         with open(id_map_file_path, 'w') as f:
#             json.dump(id_map, f)
#         log.info("Index and ID map saved to temporary local files.", company_id=company_id_str)

#         # 3. Subir a GCS usando GCSIndexStorageAdapter (o un cliente GCS directo)
#         # GCSIndexStorageAdapter necesitaría un método save_index(company_id, local_bm2s_path, local_id_map_path)
#         await gcs_adapter.save_index_files(company_id_str, str(bm2s_file_path), str(id_map_file_path))
#         log.info("Index and ID map uploaded to GCS successfully.", company_id=company_id_str)

# async def main_builder(target_company_id: str):
#     # Configurar logging para el builder
#     # Inicializar pool de DB
#     await postgres_connector.get_db_pool() # Usar el pool del servicio para el builder

#     repo = PostgresChunkContentRepository()
#     gcs_adapter = GCSIndexStorageAdapter(bucket_name=settings_for_builder.SPARSE_INDEX_GCS_BUCKET_NAME)

#     if target_company_id.upper() == "ALL":
#         log.info("Building indices for ALL companies...")
#         # Lógica para obtener todas las company_id activas de la tabla 'companies' o 'documents'
#         # unique_company_ids = await repo.get_all_active_company_ids() # Necesitaría un nuevo método en el repo
#         # for comp_id in unique_company_ids:
#         #     await build_and_upload_index_for_company(str(comp_id), repo, gcs_adapter)
#         log.warning("Building for 'ALL' companies not fully implemented yet. Requires fetching all company IDs.")
#     else:
#         await build_and_upload_index_for_company(target_company_id, repo, gcs_adapter)

#     # Cerrar pool de DB
#     await postgres_connector.close_db_pool()
#     log.info("Index building process finished.")

# if __name__ == "__main__":
#     # Lógica de Argparse para --company-id
#     # asyncio.run(main_builder(parsed_args.company_id))
```

## 3. Flujo de Búsqueda en Runtime

1.  **`search_endpoint.py`:**
    *   Recibe la solicitud HTTP.
    *   Llama a `LoadAndSearchIndexUseCase.execute()`.
2.  **`LoadAndSearchIndexUseCase.execute()`:**
    *   `log = structlog.get_logger().bind(company_id=company_id_str, query=query)`
    *   Intenta obtener `(bm2s_instance, id_map)` del `LRUCache` usando `company_id` como clave.
    *   **Cache Hit:**
        *   `log.info("LRU cache hit for BM25 index.")`
        *   Llama a `BM25Adapter.search(query, bm2s_instance, id_map, top_k)`.
        *   Devuelve resultados.
    *   **Cache Miss:**
        *   `log.info("LRU cache miss. Attempting to load BM25 index from GCS.")`
        *   Llama a `GCSIndexStorageAdapter.load_index_files(company_id)`:
            *   Este método descarga `bm2s_index.bm2s` y `id_map.json` de GCS a archivos temporales locales.
            *   Si falla la descarga (ej. archivos no existen), devuelve `(None, None)`.
        *   Si `load_index_files` devuelve `(None, None)`:
            *   `log.warning("Index files not found in GCS or download failed. Returning empty results.")`
            *   Retorna `[]`. **No hay fallback a indexación on-demand.**
        *   Si los archivos se descargan:
            *   `bm2s_instance = BM25Adapter.load_index(local_bm2s_path)`
            *   `id_map = json.load(open(local_id_map_path))`
            *   `log.info("BM25 index and ID map loaded from GCS files.")`
            *   Almacena `(bm2s_instance, id_map)` en `LRUCache` para `company_id`.
            *   Llama a `BM25Adapter.search(query, bm2s_instance, id_map, top_k)`.
            *   Devuelve resultados.
3.  **`BM25Adapter.search()` (Modificado):**
    *   Recibe `query_text: str`, `bm2s_instance: bm2s.BM25`, `id_map: List[str]`, `top_k: int`.
    *   Tokeniza `query_text`.
    *   Usa `bm2s_instance.retrieve(tokenized_query, k=top_k)` para obtener `doc_indices` y `scores`.
    *   Mapea los `doc_indices` a los `chunk_id` reales usando el `id_map` (ej. `actual_chunk_id = id_map[doc_idx]`).
    *   Retorna `List[SparseSearchResultItem]`.

## 4. Cambios de Código Detallados

### `app/core/config.py`
*   Añadir nuevas variables de configuración:
    *   `SPARSE_INDEX_GCS_BUCKET_NAME: str` (Ej: `"atenex-sparse-indices"`)
    *   `SPARSE_INDEX_CACHE_MAX_ITEMS: int = Field(default=50, description="Max number of company indexes in LRU cache.")`
    *   `SPARSE_INDEX_CACHE_TTL_SECONDS: int = Field(default=3600, description="TTL for items in LRU cache (seconds).")`
    *   (Opcional) `SPARSE_BM2S_K1: float = Field(default=1.5)` (si se quieren configurar parámetros de BM25)
    *   (Opcional) `SPARSE_BM2S_B: float = Field(default=0.75)`

### `app/application/ports/`
*   Crear `sparse_index_storage_port.py`:
    ```python
    # app/application/ports/sparse_index_storage_port.py
    import abc
    from typing import Tuple, Optional, List, Any
    # from bm2s import BM25 # Evitar importación directa de bm2s aquí si es posible

    class SparseIndexStoragePort(abc.ABC):
        @abc.abstractmethod
        async def load_index_files(self, company_id: str) -> Tuple[Optional[str], Optional[str]]:
            """
            Downloads index files (BM25 dump and ID map) from storage for a company.
            Returns paths to local temporary files.
            (local_bm2s_path, local_id_map_path) or (None, None) if not found/error.
            """
            raise NotImplementedError

        @abc.abstractmethod
        async def save_index_files(self, company_id: str, local_bm2s_path: str, local_id_map_path: str) -> None:
            """Saves local index files to storage for a company."""
            raise NotImplementedError
    ```

### `app/infrastructure/storage/` (Nuevo directorio)
*   Crear `gcs_index_storage_adapter.py`:
    *   Usará `google-cloud-storage` de forma asíncrona (`loop.run_in_executor`).
    *   Implementará `SparseIndexStoragePort`.
    *   `load_index_files`: descarga `indices/{company_id}/bm25_index.bm2s` y `indices/{company_id}/id_map.json` a un directorio temporal local. Devuelve las rutas locales.
    *   `save_index_files`: sube los archivos locales a las rutas GCS correspondientes.

### `app/infrastructure/cache/` (Nuevo directorio)
*   Crear `index_lru_cache.py`:
    *   Usará `cachetools.TTLCache` o `cachetools.LRUCache`. Si es LRU, no necesita TTL directamente en la clase, sino que el tamaño máximo lo gestiona. Para TTL, `TTLCache` es mejor.
    *   Wrapper simple alrededor del caché para gestionar instancias de `bm2s.BM25` y `id_map`.
    *   Métodos `get(company_id)` y `put(company_id, bm2s_instance, id_map)`.
    *   Se inicializará en `main.py` (lifespan) y se pasará al UseCase.

### `app/application/use_cases/sparse_search_use_case.py`
*   Renombrar o crear `LoadAndSearchIndexUseCase`.
*   Modificar constructor para aceptar `index_cache: IndexLRUCache` y `index_storage: SparseIndexStoragePort`.
*   Reescribir `execute()` siguiendo el flujo descrito en la sección 3.

### `app/infrastructure/sparse_retrieval/bm25_adapter.py`
*   Modificar `search()`:
    *   Ya no construye el índice (`retriever.index(corpus_texts)`).
    *   Recibirá `bm2s_instance: bm2s.BM25` y `id_map: List[str]` como parámetros.
    *   Usará `bm2s_instance.retrieve()` y luego mapeará los índices con `id_map`.
*   Añadir métodos estáticos/auxiliares (o el UseCase los llama directamente):
    *   `def load_bm2s_from_file(file_path: str) -> bm2s.BM25:`
        *   Crea una instancia `bm2s.BM25()` y llama a `instance.load(file_path)`.
    *   `def dump_bm2s_to_file(instance: bm2s.BM25, file_path: str):`
        *   Llama a `instance.dump(file_path)`.

### `app/main.py` (Lifespan)
*   Inicializar `GCSIndexStorageAdapter`.
*   Inicializar `IndexLRUCache` con `maxsize` y `ttl` desde `settings`.
*   Pasar estas instancias al constructor de `LoadAndSearchIndexUseCase`.
*   Actualizar `set_global_dependencies` y los getters en `app/dependencies.py`.

### `app/jobs/index_builder_cronjob.py` (Nuevo)
*   Script standalone como se describió anteriormente.
*   Necesitará su propia lógica para cargar `settings` (o una versión simplificada de ellas, principalmente DB y GCS config).
*   Usará `PostgresChunkContentRepository`, `BM25Adapter.dump_bm2s_to_file`, y `GCSIndexStorageAdapter.save_index_files`.

### `Dockerfile`
*   Asegurar que `google-cloud-storage` y `cachetools` estén en `pyproject.toml` y se instalen.
*   El `index_builder_cronjob.py` debe estar en la imagen. Se puede invocar con `CMD ["python", "app/jobs/index_builder_cronjob.py", "--company-id", "ALL"]` o similar.

### `k8s/sparse-search-service-cronjob.yaml` (Nuevo)
*   Definición de un `CronJob` de Kubernetes.
*   Usará la misma imagen que el `sparse-search-service`.
*   El `command` del contenedor ejecutará el script `index_builder_cronjob.py`.
*   Se deben montar/inyectar las variables de entorno necesarias (DB_PASSWORD, GCS_BUCKET_NAME, etc.) y las credenciales de GCS (Service Account).
    ```yaml
    # Ejemplo k8s/sparse-search-service-cronjob.yaml
    apiVersion: batch/v1
    kind: CronJob
    metadata:
      name: sparse-search-index-builder
      namespace: nyro-develop
    spec:
      schedule: "0 */6 * * *" # Cada 6 horas
      jobTemplate:
        spec:
          template:
            spec:
              serviceAccountName: sparse-search-service-sa # SA con permisos a GCS y PG
              containers:
              - name: index-builder
                image: TU_REGISTRO/sparse-search-service:TAG_ACTUAL
                command: ["python", "-m", "app.jobs.index_builder_cronjob"] # Asumiendo que es un módulo ejecutable
                args:
                - "--company-id"
                - "ALL" # O se puede parametrizar para correr por compañía individual si es necesario
                envFrom:
                - configMapRef:
                    name: sparse-search-service-config
                - secretRef:
                    name: sparse-search-service-secrets # O el secret general de DB
                # env:
                # - name: GOOGLE_APPLICATION_CREDENTIALS
                #   value: "/var/secrets/google/key.json" # Si se monta el SA key
                # volumeMounts:
                # - name: google-cloud-key
                #   mountPath: /var/secrets/google
                #   readOnly: true
              restartPolicy: OnFailure
              # volumes:
              # - name: google-cloud-key
              #   secret:
              #     secretName: gcp-sa-key-for-sparse-indexer # Secret con la key del SA para GCS
    ```

## 5. Configuración Adicional

*   **`SPARSE_INDEX_GCS_BUCKET_NAME`**: Nombre del nuevo bucket GCS. Ej: `atenex-sparse-indices-prod` / `atenex-sparse-indices-dev`.
*   **IAM para GCS:**
    *   El Service Account del pod `sparse-search-service` necesita permisos `storage.objectViewer` (o `storage.objects.get`) sobre el bucket `SPARSE_INDEX_GCS_BUCKET_NAME`.
    *   El Service Account del `CronJob` (o el SA del `sparse-search-service` si es el mismo) necesita permisos `storage.objectCreator`, `storage.objectViewer`, `storage.objectDeleter` (o `storage.objects.create`, `storage.objects.get`, `storage.objects.delete`) sobre el bucket `SPARSE_INDEX_GCS_BUCKET_NAME`. Se recomienda un SA dedicado para el builder con permisos más amplios.

## 6. Simplificación del CronJob (Respecto al plan original)

Para la primera iteración, el CronJob puede ser simple:
*   Se ejecuta cada N horas (configurable).
*   Obtiene la lista de TODAS las `company_id` activas (desde una tabla `companies` si existe, o un `SELECT DISTINCT company_id FROM documents WHERE status = 'processed'`).
*   Itera sobre cada `company_id` y reconstruye y sube su índice.
*   Esto es idempotente. Si no hay cambios en los chunks de una compañía, el índice será el mismo.

No es necesario el callback desde `ingest-service` para la V1 de este refactor, el CronJob periódico es suficiente para mantener los índices razonablemente actualizados.

---

## Checklist de Refactorización

**A. Configuración y Setup**
*   [ ] Definir y crear nuevo bucket GCS: `atenex-sparse-indices` (o similar).
*   [ ] Configurar IAM para el Service Account de `sparse-search-service` (lectura del bucket de índices).
*   [ ] Configurar IAM para el Service Account del CronJob (lectura/escritura del bucket de índices).
*   [ ] Añadir nuevas variables de entorno a `app/core/config.py` (`SPARSE_INDEX_GCS_BUCKET_NAME`, `SPARSE_INDEX_CACHE_MAX_ITEMS`, `SPARSE_INDEX_CACHE_TTL_SECONDS`).
*   [ ] Añadir `google-cloud-storage` y `cachetools` a `pyproject.toml`.

**B. Adaptadores y Puertos**
*   [ ] Crear `app/application/ports/sparse_index_storage_port.py` con `SparseIndexStoragePort`.
*   [ ] Crear `app/infrastructure/storage/gcs_index_storage_adapter.py` implementando `SparseIndexStoragePort`.
*   [ ] Crear `app/infrastructure/cache/index_lru_cache.py` con la lógica del caché LRU/TTL.

**C. Lógica de Negocio (Use Case)**
*   [ ] Crear/Modificar `LoadAndSearchIndexUseCase` en `app/application/use_cases/`.
    *   [ ] Implementar lógica de cache-lookup.
    *   [ ] Implementar lógica de carga desde GCS en caso de cache-miss usando `GCSIndexStorageAdapter`.
    *   [ ] Cargar el índice (`bm2s.BM25.load()`) y `id_map.json`.
    *   [ ] Almacenar en caché.
    *   [ ] Delegar búsqueda al `BM25Adapter` con la instancia cargada.

**D. BM25 Adapter**
*   [ ] Modificar `BM25Adapter.search()` para aceptar `bm2s_instance` y `id_map` (ya no construye índice).
*   [ ] Añadir/usar métodos auxiliares `load_bm2s_from_file()` y `dump_bm2s_to_file()`.

**E. Endpoints y Main App**
*   [ ] Actualizar `app/dependencies.py` para los nuevos adaptadores y el caché.
*   [ ] Actualizar `app/main.py` (lifespan) para inicializar `GCSIndexStorageAdapter` y `IndexLRUCache`, y pasarlos al `LoadAndSearchIndexUseCase`.
*   [ ] Asegurar que `search_endpoint.py` usa el `LoadAndSearchIndexUseCase` actualizado.

**F. Index Builder (CronJob)**
*   [ ] Crear script `app/jobs/index_builder_cronjob.py`.
    *   [ ] Conectar a PostgreSQL y obtener chunks por compañía (reusar `PostgresChunkContentRepository`).
    *   [ ] Construir `corpus_texts` y `id_map`.
    *   [ ] Indexar con `bm2s.BM25()`.
    *   [ ] Dumpear índice y `id_map` a archivos locales.
    *   [ ] Subir archivos a GCS usando `GCSIndexStorageAdapter` (o cliente GCS directo).
*   [ ] Actualizar `Dockerfile` para incluir el script del builder y sus dependencias.
*   [ ] Crear manifiesto Kubernetes `k8s/sparse-search-service-cronjob.yaml`.

**G. Limpieza y Mantenimiento (Post-Refactor)**
*   [ ] Eliminar el código de indexación on-demand de `BM25Adapter` y `SparseSearchUseCase` original.
*   [ ] Revisar logs y métricas de rendimiento tras el despliegue.