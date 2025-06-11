## Plan de Refactorización Completo para `ingest-service` (Arquitectura Hexagonal)

El objetivo es reestructurar el `ingest-service` para que siga los principios de la Arquitectura Hexagonal (también conocida como Puertos y Adaptadores). Esto mejorará la mantenibilidad, testabilidad y permitirá una clara separación de la lógica de negocio de las preocupaciones de infraestructura.

**Principios Clave de la Arquitectura Hexagonal a Aplicar:**

1.  **Núcleo de la Aplicación (Dominio y Casos de Uso):** Contiene la lógica de negocio pura, sin dependencias de frameworks o infraestructura externa (bases de datos, colas de mensajes, APIs externas).
2.  **Puertos (Interfaces):** Definen los contratos (interfaces) que el núcleo de la aplicación utiliza para interactuar con el mundo exterior. Hay puertos de entrada (para ser llamados por la API o workers) y puertos de salida (para acceder a bases de datos, almacenamiento, servicios externos).
3.  **Adaptadores (Implementaciones):** Implementan los puertos. Hay adaptadores primarios (o "driving") que invocan el núcleo (e.g., controladores API, workers Celery) y adaptadores secundarios (o "driven") que son invocados por el núcleo (e.g., repositorios de base de datos, clientes de servicios externos).

---

### Nueva Estructura de Directorios Propuesta para `ingest-service/app/`

```
ingest-service/
├── app/
│   ├── __init__.py
│   ├── api/v1/                         # Capa API (Adaptador Primario)
│   │   ├── __init__.py
│   │   ├── endpoints/ingest_endpoint.py # Controladores FastAPI (antes ingest.py)
│   │   └── schemas.py                   # DTOs para la API (ya existe, mantener)
│   │
│   ├── application/                    # Núcleo - Lógica de Aplicación
│   │   ├── __init__.py
│   │   ├── ports/                      # Interfaces (Puertos)
│   │   │   ├── __init__.py
│   │   │   ├── document_repository_port.py
│   │   │   ├── chunk_repository_port.py
│   │   │   ├── file_storage_port.py
│   │   │   ├── vector_store_port.py
│   │   │   ├── docproc_service_port.py
│   │   │   ├── embedding_service_port.py
│   │   │   └── task_queue_port.py
│   │   │
│   │   └── use_cases/                  # Casos de Uso (Lógica de Negocio)
│   │       ├── __init__.py
│   │       ├── upload_document_use_case.py
│   │       ├── process_document_use_case.py
│   │       ├── get_document_status_use_case.py
│   │       ├── list_documents_use_case.py
│   │       ├── retry_document_use_case.py
│   │       ├── delete_document_use_case.py
│   │       └── get_document_stats_use_case.py
│   │
│   ├── core/                           # Configuración, Logging (ya existe, mantener)
│   │   ├── __init__.py
│   │   ├── config.py
│   │   └── logging_config.py
│   │
│   ├── domain/                         # Núcleo - Modelos de Dominio
│   │   ├── __init__.py
│   │   ├── entities.py                 # Clases de dominio (e.g., Document, Chunk)
│   │   └── enums.py                    # Enums (e.g., DocumentStatus, antes en models/domain.py)
│   │
│   ├── infrastructure/                 # Capa de Infraestructura (Adaptadores Secundarios y Primarios)
│   │   ├── __init__.py
│   │   ├── persistence/                # Adaptadores de Base de Datos
│   │   │   ├── __init__.py
│   │   │   ├── postgres_connector.py      # Lógica de conexión (async y sync)
│   │   │   ├── postgres_document_repository.py # Implementa DocumentRepositoryPort
│   │   │   └── postgres_chunk_repository.py    # Implementa ChunkRepositoryPort (sync)
│   │   │
│   │   ├── storage/                    # Adaptadores de Almacenamiento de Archivos
│   │   │   ├── __init__.py
│   │   │   └── gcs_adapter.py              # Implementa FileStoragePort (usando GCSClient)
│   │   │
│   │   ├── vectorstore/                # Adaptadores de Base de Datos Vectorial
│   │   │   ├── __init__.py
│   │   │   └── milvus_adapter.py           # Implementa VectorStorePort (usando Pymilvus)
│   │   │
│   │   ├── clients/                    # Adaptadores para Servicios Externos
│   │   │   ├── __init__.py
│   │   │   ├── remote_docproc_adapter.py   # Implementa DocProcServicePort
│   │   │   └── remote_embedding_adapter.py # Implementa EmbeddingServicePort
│   │   │
│   │   └── tasks/                      # Adaptadores Primarios para Tareas Asíncronas
│   │       ├── __init__.py
│   │       ├── celery_app_config.py        # Configuración de Celery (antes celery_app.py)
│   │       ├── celery_task_adapter.py    # Implementa TaskQueuePort
│   │       └── celery_worker.py          # Definición de la tarea Celery que llama a ProcessDocumentUseCase
│   │
│   ├── main.py                         # Entrypoint FastAPI (ya existe, adaptar lifespan)
│   └── dependencies.py                 # (Opcional) Para inyección de dependencias compleja
│
├── pyproject.toml
└── README.md
```

---

### Detalles del Plan de Refactorización:

**1. Capa de Dominio (`app/domain/`)**

*   **`app/domain/enums.py`**:
    *   Mover `DocumentStatus` y `ChunkVectorStatus` de `app/models/domain.py` a este nuevo archivo.
*   **`app/domain/entities.py`**:
    *   Crear una entidad `Document`:
        ```python
        # app/domain/entities.py
        import uuid
        from datetime import datetime
        from typing import Optional, Dict, Any
        from pydantic import BaseModel, Field # Pydantic puede usarse para modelos de dominio si son simples
        from app.domain.enums import DocumentStatus

        class Document(BaseModel):
            id: uuid.UUID
            company_id: uuid.UUID
            file_name: str
            file_type: str
            file_path: Optional[str] = None
            metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)
            status: DocumentStatus
            chunk_count: Optional[int] = 0
            error_message: Optional[str] = None
            uploaded_at: Optional[datetime] = None
            updated_at: Optional[datetime] = None
            # Atributos para verificaciones en vivo (opcionalmente parte del dominio o DTOs de salida)
            gcs_exists: Optional[bool] = None
            milvus_chunk_count_live: Optional[int] = None

            class Config:
                from_attributes = True # Para compatibilidad con ORM si se usa directo
        ```
    *   Crear una entidad `Chunk`:
        ```python
        # app/domain/entities.py (continuación)
        import uuid
        from datetime import datetime
        from typing import Optional, List
        from pydantic import BaseModel, Field
        from app.domain.enums import ChunkVectorStatus

        class ChunkMetadata(BaseModel): # Podría ser DocumentChunkMetadata de models/domain.py
            page: Optional[int] = None
            title: Optional[str] = None
            tokens: Optional[int] = None
            content_hash: Optional[str] = Field(None, max_length=64)

        class Chunk(BaseModel):
            id: Optional[uuid.UUID] = None # ID de la tabla de chunks
            document_id: uuid.UUID
            company_id: uuid.UUID
            chunk_index: int
            content: str
            metadata: ChunkMetadata = Field(default_factory=ChunkMetadata)
            embedding_id: Optional[str] = None # PK de Milvus
            vector_status: ChunkVectorStatus = ChunkVectorStatus.PENDING
            # Opcional: El embedding mismo si se maneja en el dominio, o dejarlo en la capa de infraestructura
            # embedding: Optional[List[float]] = None
            created_at: Optional[datetime] = None

            class Config:
                from_attributes = True
        ```
    *   Eliminar `app/models/domain.py` o mover su contenido restante.

**2. Capa de Aplicación (`app/application/`)**

*   **Puertos (`app/application/ports/`)**: Definir interfaces Python puras (usando `abc.ABC` y `abc.abstractmethod`).
    *   `document_repository_port.py`:
        *   `DocumentRepositoryPort(ABC)`:
            *   `save(document: Document) -> None` (async y sync)
            *   `find_by_id(doc_id: uuid.UUID, company_id: uuid.UUID) -> Optional[Document]` (async y sync)
            *   `find_by_name_and_company(filename: str, company_id: uuid.UUID) -> Optional[Document]` (async)
            *   `update_status(doc_id: uuid.UUID, status: DocumentStatus, chunk_count: Optional[int] = None, error_message: Optional[str] = None) -> bool` (async y sync)
            *   `list_paginated(company_id: uuid.UUID, limit: int, offset: int) -> Tuple[List[Document], int]` (async)
            *   `delete(doc_id: uuid.UUID, company_id: uuid.UUID) -> bool` (async)
            *   `get_stats(company_id: uuid.UUID, from_date: Optional[date], to_date: Optional[date], status_filter: Optional[DocumentStatus]) -> Dict[str, Any]` (async)
    *   `chunk_repository_port.py`:
        *   `ChunkRepositoryPort(ABC)`:
            *   `save_bulk(chunks: List[Chunk]) -> int` (sync) - Usado por el worker.
    *   `file_storage_port.py`:
        *   `FileStoragePort(ABC)`:
            *   `upload(object_name: str, data: bytes, content_type: str) -> str` (async y sync)
            *   `download(object_name: str, target_path: str) -> None` (sync)
            *   `exists(object_name: str) -> bool` (async y sync)
            *   `delete(object_name: str) -> None` (async y sync)
    *   `vector_store_port.py`:
        *   `VectorStorePort(ABC)`:
            *   `index_chunks(chunks_data: List[Dict], embeddings: List[List[float]], filename: str, company_id: str, document_id: str, delete_existing: bool) -> Tuple[int, List[str], List[Dict]]` (sync)
            *   `delete_document_chunks(company_id: str, document_id: str) -> int` (sync y async)
            *   `count_document_chunks(company_id: str, document_id: str) -> int` (sync y async)
            *   `ensure_collection_and_indexes() -> None` (sync)
    *   `docproc_service_port.py`:
        *   `DocProcServicePort(ABC)`:
            *   `process_document(file_bytes: bytes, original_filename: str, content_type: str, document_id: Optional[str], company_id: Optional[str]) -> Dict[str, Any]` (sync, ya que el worker es sync)
    *   `embedding_service_port.py`:
        *   `EmbeddingServicePort(ABC)`:
            *   `get_embeddings(texts: List[str], text_type: str = "passage") -> Tuple[List[List[float]], Dict[str, Any]]` (sync)
    *   `task_queue_port.py`:
        *   `TaskQueuePort(ABC)`:
            *   `enqueue_process_document_task(document_id: str, company_id: str, filename: str, content_type: str) -> str` (async) - Devuelve `task_id`.

*   **Casos de Uso (`app/application/use_cases/`)**: Clases que implementan la lógica de negocio, usando los puertos.
    *   `upload_document_use_case.py -> UploadDocumentUseCase`:
        *   Constructor inyecta `DocumentRepositoryPort` (async), `FileStoragePort` (async), `TaskQueuePort` (async).
        *   Lógica del actual endpoint `POST /upload` hasta antes de la llamada a Celery. Valida, crea registro DB (PENDING), sube a GCS, actualiza DB (UPLOADED), encola tarea.
    *   `process_document_use_case.py -> ProcessDocumentUseCase`:
        *   Constructor inyecta `DocumentRepositoryPort` (sync), `ChunkRepositoryPort` (sync), `FileStoragePort` (sync), `DocProcServicePort` (sync), `EmbeddingServicePort` (sync), `VectorStorePort` (sync).
        *   Toda la lógica de la tarea Celery `process_document_standalone`. Actualiza estado a PROCESSING, descarga de GCS, llama a DocProc, llama a Embedding, indexa en Milvus, guarda chunks en PG, actualiza estado final.
    *   `get_document_status_use_case.py -> GetDocumentStatusUseCase`:
        *   Constructor inyecta `DocumentRepositoryPort` (async), `FileStoragePort` (async), `VectorStorePort` (async).
        *   Lógica del actual endpoint `GET /status/{document_id}`. Obtiene de DB, verifica GCS y Milvus.
    *   `list_documents_use_case.py -> ListDocumentsUseCase`:
        *   Similar a `GetDocumentStatusUseCase` pero para listas, incluyendo verificaciones en vivo.
    *   `retry_document_use_case.py -> RetryDocumentUseCase`:
        *   Constructor inyecta `DocumentRepositoryPort` (async), `TaskQueuePort` (async).
        *   Lógica del actual endpoint `POST /retry/{document_id}`.
    *   `delete_document_use_case.py -> DeleteDocumentUseCase`:
        *   Constructor inyecta `DocumentRepositoryPort` (async), `FileStoragePort` (async), `VectorStorePort` (async).
        *   Lógica de `DELETE /{document_id}` y de la parte de un solo documento en `DELETE /bulk`.
    *   `get_document_stats_use_case.py -> GetDocumentStatsUseCase`:
        *   Constructor inyecta `DocumentRepositoryPort` (async).
        *   Lógica del actual endpoint `GET /stats`.

**3. Capa de API (`app/api/v1/`)**

*   **`endpoints/ingest_endpoint.py`**:
    *   Importará los casos de uso y los DTOs de `schemas.py`.
    *   Cada endpoint FastAPI será mucho más delgado:
        1.  Recibe la solicitud HTTP y valida los datos de entrada (headers, body, DTOs).
        2.  Inyecta y llama al caso de uso apropiado.
        3.  Maneja la respuesta del caso de uso, la transforma a un DTO de respuesta si es necesario, y la devuelve.
    *   Las funciones helper de Milvus (`_get_milvus_collection_sync`, `_get_milvus_chunk_count_sync`, `_delete_milvus_sync`) se moverán al adaptador de Milvus.
    *   La dependencia `get_gcs_client` será reemplazada por la inyección del `FileStoragePort` (y su adaptador GCS) en los casos de uso.
    *   La dependencia `get_db_conn` será reemplazada por la inyección del `DocumentRepositoryPort` (y su adaptador PG) en los casos de uso.
    *   El endpoint `DELETE /bulk` iterará llamando a `DeleteDocumentUseCase` para cada ID, o se crea un `BulkDeleteDocumentsUseCase`.
*   **`schemas.py`**:
    *   Se mantiene como está, ya que define los DTOs para la comunicación API.
    *   Se pueden añadir nuevos DTOs si los casos de uso devuelven estructuras más complejas que necesitan ser mapeadas.

**4. Capa de Infraestructura (`app/infrastructure/`)**

*   **Persistencia (`app/infrastructure/persistence/`)**:
    *   `postgres_connector.py`:
        *   Contendrá las funciones `get_db_pool`, `close_db_pool`, `check_db_connection` (async).
        *   Contendrá las funciones `get_sync_engine`, `dispose_sync_engine` (sync).
        *   Definición del `document_chunks_table` SQLAlchemy.
    *   `postgres_document_repository.py -> PostgresDocumentRepositoryAdapter`:
        *   Implementa `DocumentRepositoryPort`.
        *   Utiliza `postgres_connector.py` para obtener conexiones/engine.
        *   Contendrá las funciones CRUD para la tabla `documents` (las funciones async y sync equivalentes a las actuales en `db/postgres_client.py` para `documents`).
    *   `postgres_chunk_repository.py -> PostgresChunkRepositoryAdapter`:
        *   Implementa `ChunkRepositoryPort`.
        *   Solo métodos síncronos (para el worker Celery).
        *   Contendrá la función `bulk_insert_chunks_sync` del `db/postgres_client.py`.
*   **Almacenamiento (`app/infrastructure/storage/`)**:
    *   `gcs_adapter.py -> GCSAdapter`:
        *   Implementa `FileStoragePort`.
        *   Internamente, utilizará una instancia de `GCSClient` (el actual `app/services/gcs_client.py`). Se puede mover `gcs_client.py` aquí y renombrarlo o mantenerlo como un cliente interno del adaptador.
        *   Deberá proveer implementaciones async y sync para los métodos del puerto.
*   **VectorStore (`app/infrastructure/vectorstore/`)**:
    *   `milvus_adapter.py -> MilvusAdapter`:
        *   Implementa `VectorStorePort`.
        *   Absorberá la lógica de `app/services/ingest_pipeline.py` y los helpers de Milvus en `app/api/v1/endpoints/ingest.py`.
        *   Gestionará la conexión a Milvus (`_ensure_milvus_connection_and_collection_for_pipeline`, etc.).
        *   Implementará métodos sync y async para los puertos según sea necesario.
*   **Clientes Externos (`app/infrastructure/clients/`)**:
    *   `remote_docproc_adapter.py -> RemoteDocProcAdapter`:
        *   Implementa `DocProcServicePort`.
        *   Adaptará `app/services/clients/docproc_service_client.py`.
    *   `remote_embedding_adapter.py -> RemoteEmbeddingAdapter`:
        *   Implementa `EmbeddingServicePort`.
        *   Adaptará `app/services/clients/embedding_service_client.py`.
*   **Tareas Asíncronas (`app/infrastructure/tasks/`)**:
    *   `celery_app_config.py`: Mover la configuración de `Celery` de `app/tasks/celery_app.py`.
    *   `celery_task_adapter.py -> CeleryTaskAdapter`:
        *   Implementa `TaskQueuePort`.
        *   Contendrá la lógica para enviar tareas a Celery.
    *   `celery_worker.py`:
        *   Definirá la tarea Celery `@celery_app.task(...) def process_document_task(...)`.
        *   Esta tarea:
            1.  Instanciará el `ProcessDocumentUseCase`.
            2.  Inyectará las implementaciones síncronas de los puertos necesarios (e.g., `PostgresDocumentRepositoryAdapter(sync_engine)`, `GCSAdapter(sync_mode=True)`, etc.). Los recursos como `sync_engine` y `gcs_client_global` se obtendrán del `worker_process_init` como ahora.
            3.  Llamará a `process_document_use_case.execute(...)`.

**5. Adaptación de `main.py` y Otros Archivos Core**

*   `app/main.py`:
    *   El `lifespan` manager necesitará inicializar el `postgres_connector` (para el pool async) y cualquier otro recurso global para los adaptadores async.
    *   La inclusión del router seguirá siendo `app.include_router(ingest_endpoint.router, ...)`.
*   `app/core/config.py` y `app/core/logging_config.py`: Se mantienen, pueden requerir ajustes menores si algunas configuraciones se mueven o cambian de nombre (e.g., `MILVUS_` vars si son accedidas solo por el adaptador).

**6. Gestión de Dependencias**

*   **FastAPI Endpoints:**
    *   Usarán la inyección de dependencias de FastAPI para obtener instancias de los Casos de Uso.
    *   Ejemplo:
        ```python
        # app/api/v1/endpoints/ingest_endpoint.py
        from app.application.use_cases.upload_document_use_case import UploadDocumentUseCase
        # ...

        def get_upload_document_use_case(
            # FastAPI inyectará automáticamente los adaptadores de los puertos
            doc_repo: DocumentRepositoryPort = Depends(get_document_repository_adapter), # Placeholder
            file_storage: FileStoragePort = Depends(get_file_storage_adapter),      # Placeholder
            task_queue: TaskQueuePort = Depends(get_task_queue_adapter)          # Placeholder
        ) -> UploadDocumentUseCase:
            return UploadDocumentUseCase(doc_repo, file_storage, task_queue)

        @router.post("/upload")
        async def upload_document_endpoint(
            # ... request params ...
            use_case: UploadDocumentUseCase = Depends(get_upload_document_use_case)
        ):
            # ...
            result = await use_case.execute(...)
            # ...
        ```
    *   Se necesitarán factorías (funciones `Depends`) para los adaptadores que a su vez obtienen sus propias dependencias (como pools de conexión).
*   **Celery Worker (`celery_worker.py`):**
    *   El worker no usa la inyección de FastAPI. Aquí, el `sync_engine` y `gcs_client_global` se inicializan en `worker_process_init`.
    *   La tarea Celery instanciará el `ProcessDocumentUseCase` y le pasará las instancias de los adaptadores síncronos, que a su vez usarán estos recursos globales.
        ```python
        # app/infrastructure/tasks/celery_worker.py
        # ... importaciones ...
        # sync_engine, gcs_client_global son globales inicializados en worker_process_init

        @celery_app.task(...)
        def process_document_task(self: Task, document_id_str: str, ...):
            # ...
            doc_repo_adapter = PostgresDocumentRepositoryAdapter(engine=sync_engine)
            chunk_repo_adapter = PostgresChunkRepositoryAdapter(engine=sync_engine)
            file_storage_adapter = GCSAdapter(sync_mode=True, client_instance=gcs_client_global) # Adapt GCSAdapter
            vector_store_adapter = MilvusAdapter(sync_mode=True) # Adapt MilvusAdapter
            doc_proc_adapter = RemoteDocProcAdapter()
            embedding_adapter = RemoteEmbeddingAdapter()

            use_case = ProcessDocumentUseCase(
                document_repository=doc_repo_adapter,
                chunk_repository=chunk_repo_adapter,
                file_storage=file_storage_adapter,
                doc_proc_service=doc_proc_adapter,
                embedding_service=embedding_adapter,
                vector_store=vector_store_adapter
            )
            return use_case.execute(document_id_str, ...)
        ```

**7. Reducción de Líneas en `ingest.py` (ahora `ingest_endpoint.py`)**

*   La principal reducción vendrá de mover la lógica de orquestación a los Casos de Uso.
*   La eliminación de los helpers de Milvus (`_get_milvus_collection_sync`, etc.) también contribuirá.
*   Los endpoints se volverán delegadores muy simples.

**Consideraciones Adicionales:**

*   **Modos Async/Sync en Adaptadores:** Los adaptadores como `PostgresDocumentRepositoryAdapter`, `GCSAdapter`, `MilvusAdapter` necesitarán manejar tanto operaciones asíncronas (para la API) como síncronas (para el worker Celery). Esto se puede lograr:
    *   Teniendo métodos `*_async` y `*_sync` separados.
    *   Pasando un flag `sync_mode` al constructor y usando `asyncio.run_coroutine_threadsafe` o `loop.run_in_executor` internamente si el adaptador async es el primario.
    *   Para DB, es más limpio tener implementaciones separadas que usen `asyncpg` vs `SQLAlchemy/psycopg2` directamente, aunque compartan queries.
*   **Transacciones:** Los casos de uso que involucren múltiples operaciones de base de datos deberán manejar las transacciones (e.g., el `UploadDocumentUseCase` para la creación del registro y la subida a GCS). Los repositorios no deberían manejar transacciones que abarquen múltiples llamadas a repositorios.
*   **Pruebas:** La arquitectura hexagonal facilita las pruebas unitarias del núcleo (casos de uso y dominio) al poder mockear los puertos. También permite pruebas de integración para los adaptadores.

---

### Checklist del Plan de Refactorización:

1.  **[x] Estructura de Directorios:**
    *   [x] Crear la nueva estructura de directorios (`application`, `domain`, `infrastructure` y subdirectorios).
2.  **[x] Capa de Dominio (`app/domain/`)**:
    *   [x] Mover Enums (`DocumentStatus`, `ChunkVectorStatus`) a `enums.py`.
    *   [x] Definir entidad `Document` en `entities.py`.
    *   [x] Definir entidad `Chunk` (y `ChunkMetadata`) en `entities.py`.
    *   [x] Eliminar/reemplazar `app/models/domain.py`.
3.  **[x] Puertos de Aplicación (`app/application/ports/`)**:
    *   [x] Definir `DocumentRepositoryPort`.
    *   [x] Definir `ChunkRepositoryPort`.
    *   [x] Definir `FileStoragePort`.
    *   [x] Definir `VectorStorePort`.
    *   [x] Definir `DocProcServicePort`.
    *   [x] Definir `EmbeddingServicePort`.
    *   [x] Definir `TaskQueuePort`.
4.  **[x] Casos de Uso (`app/application/use_cases/`)**:
    *   [x] Implementar `UploadDocumentUseCase`.
    *   [x] Implementar `ProcessDocumentUseCase`.
    *   [x] Implementar `GetDocumentStatusUseCase`.
    *   [x] Implementar `ListDocumentsUseCase`.
    *   [x] Implementar `RetryDocumentUseCase`.
    *   [x] Implementar `DeleteDocumentUseCase`.
    *   [x] Implementar `GetDocumentStatsUseCase`.
5.  **[x] Capa de Infraestructura - Persistencia (`app/infrastructure/persistence/`)**:
    *   [x] Crear `postgres_connector.py` con lógica de conexión async y sync.
    *   [x] Implementar `PostgresDocumentRepositoryAdapter` (implementa `DocumentRepositoryPort`).
    *   [x] Implementar `PostgresChunkRepositoryAdapter` (implementa `ChunkRepositoryPort`).
    *   [x] Migrar toda la lógica de `app/db/postgres_client.py`.
6.  **[x] Capa de Infraestructura - Almacenamiento (`app/infrastructure/storage/`)**:
    *   [x] Implementar `GCSAdapter` (implementa `FileStoragePort`), adaptando `GCSClient`.
7.  **[x] Capa de Infraestructura - VectorStore (`app/infrastructure/vectorstore/`)**:
    *   [x] Implementar `MilvusAdapter` (implementa `VectorStorePort`), absorbiendo lógica de `ingest_pipeline.py` y helpers de Milvus.
8.  **[x] Capa de Infraestructura - Clientes Externos (`app/infrastructure/clients/`)**:
    *   [x] Crear `RemoteDocProcAdapter` (implementa `DocProcServicePort`), adaptando `docproc_service_client.py`.
    *   [x] Crear `RemoteEmbeddingAdapter` (implementa `EmbeddingServicePort`), adaptando `embedding_service_client.py`.
9.  **[x] Capa de Infraestructura - Tareas Asíncronas (`app/infrastructure/tasks/`)**:
    *   [x] Mover configuración de Celery a `celery_app_config.py`.
    *   [x] Implementar `CeleryTaskAdapter` (implementa `TaskQueuePort`).
    *   [x] Crear `celery_worker.py` que defina la tarea Celery y llame a `ProcessDocumentUseCase`.
10. **[x] Capa de API (`app/api/v1/`)**:
    *   [x] Renombrar `ingest.py` a `ingest_endpoint.py`.
    *   [x] Refactorizar endpoints en `ingest_endpoint.py` para que sean delgados y usen los Casos de Uso.
    *   [x] Eliminar helpers de Milvus y referencias directas a GCS/DB de los endpoints.
11. **[x] Inyección de Dependencias**:
    *   [x] Configurar inyección de dependencias de FastAPI para los Casos de Uso en los endpoints (centralizado en `infrastructure/dependency_injection.py`).
    *   [x] Asegurar que el worker Celery pueda instanciar y usar los Casos de Uso con adaptadores síncronos (usando la misma centralización).
12. **[x] Archivos Core y `main.py`**:
    *   [x] Adaptar `main.py` (lifespan) para los nuevos conectores/adaptadores si es necesario. Ya no hay referencias a infraestructura fuera de los puntos de entrada y el lifespan es limpio.
    *   [x] Revisar `config.py` y `logging_config.py` para asegurar compatibilidad. Ambos archivos son compatibles y desacoplados de la infraestructura.
13. **[ ] Pruebas**:
    *   [ ] Planificar y añadir pruebas unitarias para Casos de Uso y Dominio.
    *   [ ] Planificar y añadir pruebas de integración para Adaptadores.
14. **[x] Limpieza y Revisión Final**:
    *   [x] Archivos antiguos (`app/db/postgres_client.py`, `app/services/ingest_pipeline.py`, `app/tasks/process_document.py`) marcados como DEPRECATED y sin uso en el flujo principal.
    *   [x] Revisión de tamaño y claridad de los archivos principales, cumpliendo con el límite de líneas y buenas prácticas.
    *   [x] Se siguen buenas prácticas de POO y arquitectura hexagonal en todo el servicio.
    *   [x] Flujos de ingesta, estado, reintento y eliminación correctamente desacoplados y validados en la arquitectura.
