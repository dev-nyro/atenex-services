# Atenex Ingest Service (Microservicio de Ingesta) v0.3.2

## 1. Visión General

El **Ingest Service** es un microservicio clave dentro de la plataforma Atenex. Su responsabilidad principal es recibir documentos subidos por los usuarios (PDF, DOCX, TXT, HTML, MD), orquestar su procesamiento de manera asíncrona, almacenar los archivos originales en **Google Cloud Storage (GCS)** (bucket `atenex`) y finalmente indexar el contenido procesado en bases de datos (**PostgreSQL** para metadatos y **Milvus/Zilliz Cloud** para vectores) para su uso posterior en búsquedas semánticas y generación de respuestas por LLMs.

Este servicio ha sido **refactorizado** para:
*   Delegar la **extracción de texto y el chunking de documentos** a un microservicio dedicado: **`docproc-service`**.
*   Delegar la **generación de embeddings** a otro microservicio dedicado: **`embedding-service`**.
*   Utilizar **Pymilvus** para la interacción directa con Milvus (Zilliz Cloud).
*   El worker de Celery opera de forma **síncrona** para las operaciones de base de datos (usando SQLAlchemy) y GCS, y realiza llamadas HTTP síncronas (con reintentos) a los servicios `docproc-service` y `embedding-service`.

**Flujo principal:**

1.  **Recepción:** La API (`POST /api/v1/ingest/upload`) recibe el archivo (`file`) y metadatos opcionales (`metadata_json`). Requiere los headers `X-Company-ID` y `X-User-ID`.
2.  **Validación:** Verifica el tipo de archivo (`Content-Type`) y metadatos. Previene duplicados basados en nombre de archivo y `company_id` (a menos que el existente esté en estado `error`).
3.  **Persistencia Inicial (API - Async):**
    *   Crea un registro inicial del documento en **PostgreSQL** (tabla `documents`) con estado `pending`.
    *   Guarda el archivo original en **GCS** (ruta: `{company_id}/{document_id}/{normalized_filename}`).
    *   Actualiza el registro en PostgreSQL a estado `uploaded`.
4.  **Encolado:** Dispara una tarea asíncrona usando **Celery** (con **Redis** como broker) para el procesamiento (`process_document_standalone`).
5.  **Respuesta API:** La API responde `202 Accepted` con `document_id`, `task_id` y estado `uploaded`.
6.  **Procesamiento Asíncrono (Worker Celery - Tarea `process_document_standalone`):**
    *   La tarea Celery recoge el trabajo.
    *   Actualiza el estado del documento en PostgreSQL a `processing`.
    *   Descarga el archivo de GCS a una ubicación temporal.
    *   **Orquestación del Pipeline de Procesamiento:**
        *   **Extracción y Chunking (Remoto):** Llama al **`docproc-service`** vía HTTP, enviando el archivo. Este servicio externo se encarga de extraer el texto crudo y dividirlo en fragmentos (chunks). Devuelve los chunks y metadatos asociados.
        *   **Embedding (Remoto):** Para los textos de los chunks obtenidos, llama al **`embedding-service`** vía HTTP para obtener los vectores de embedding para cada chunk.
        *   **Indexación (Milvus/Zilliz):** La función `index_chunks_in_milvus_and_prepare_for_pg` escribe los chunks (contenido, vector y metadatos incluyendo `company_id` y `document_id`) en la colección configurada en **Milvus/Zilliz Cloud** usando **Pymilvus**. Se eliminan los chunks existentes para ese documento antes de la nueva inserción.
        *   **Indexación (PostgreSQL):** Guarda información detallada de los chunks (contenido, metadatos del chunk, `company_id`, `document_id`, y el ID de embedding de Milvus) en la tabla `document_chunks` de PostgreSQL.
    *   **Actualización Final (Worker - Sync):** Actualiza el estado del documento en PostgreSQL a `processed` y registra el número de chunks indexados. Si hay errores durante cualquier paso, actualiza a `error` con un mensaje descriptivo.
7.  **Consulta de Estado y Operaciones Adicionales:** Endpoints para consultar estado (`/status/{document_id}`, `/status`), reintentar (`/retry/{document_id}`), y eliminar documentos (`/{document_id}`, `/bulk`), con verificaciones en GCS y Milvus para mantener la consistencia.

## 2. Arquitectura General del Proyecto

```mermaid
%%{init: {'theme': 'base', 'themeVariables': { 'primaryColor': '#E0F2F7', 'edgeLabelBackground':'#fff', 'tertiaryColor': '#FFFACD', 'lineColor': '#666', 'nodeBorder': '#333'}}}%%
graph TD
    A[Usuario/Cliente Externo] -->|HTTPS / REST API<br/>(via API Gateway)| I["<strong>Atenex Ingest Service API</strong><br/>(FastAPI - Async)"]

    subgraph KubernetesCluster ["Kubernetes Cluster"]

        subgraph Namespace_nyro_develop ["Namespace: nyro-develop"]
            direction TB

            %% API Interactions (Async) %%
            I -- GET /status, DELETE /{id} --> DBAsync[(PostgreSQL<br/>'atenex' DB<br/><b>asyncpg</b>)]
            I -- POST /upload, RETRY --> DBAsync
            I -- GET /status, POST /upload, DELETE /{id} --> GCSAsync[(Google Cloud Storage<br/>'atenex' Bucket<br/><b>google-cloud-storage (async helper)</b>)]
            I -- POST /upload, RETRY --> Q([Redis<br/>Celery Broker])
            I -- GET /status, DELETE /{id} -->|Pymilvus Sync Helper<br/>(via Executor)| MDB[(Milvus / Zilliz Cloud<br/>Collection Configurada<br/><b>Pymilvus</b>)]

            %% Worker Interactions (Sync + HTTP Calls) %%
            W(Celery Worker<br/><b>Prefork - Sync Ops</b><br/><i>process_document_standalone</i>) -- Picks Task --> Q
            W -- Update Status, Save Chunks --> DBSync[(PostgreSQL<br/>'atenex' DB<br/><b>SQLAlchemy/psycopg2</b>)]
            W -- Download File --> GCSSync[(Google Cloud Storage<br/>'atenex' Bucket<br/><b>google-cloud-storage (sync)</b>)]
            
            W -- Orchestrates Pipeline --> Pipe["<strong>Orchestration Pipeline</strong><br/>(Call DocProc, Call Embed Svc, Index)"]
            
            Pipe -- Call for Text/Chunks --> DocProcSvc["<strong>DocProc Service</strong><br/>(HTTP API)<br/><i>Extracts Text, Chunks Document</i>"]
            Pipe -- Call for Embeddings --> EmbeddingSvc["<strong>Embedding Service</strong><br/>(HTTP API)<br/><i>Generates Embeddings</i>"]
            Pipe -- Index/Delete Existing --> MDB

        end
        DocProcSvc --> ExternalParserLibs[("Internal Document<br/>Parsing Libraries<br/>(PyMuPDF, python-docx, etc.)")]
        EmbeddingSvc --> ExternalEmbeddingModel[("Modelo Embedding<br/>(ej. all-MiniLM-L6-v2 via FastEmbed)")]
    end

    %% Estilo %%
    style I fill:#C8E6C9,stroke:#333,stroke-width:2px
    style W fill:#BBDEFB,stroke:#333,stroke-width:2px
    style Pipe fill:#FFECB3,stroke:#666,stroke-width:1px
    style DBAsync fill:#F8BBD0,stroke:#333,stroke-width:1px
    style DBSync fill:#F8BBD0,stroke:#333,stroke-width:1px
    style GCSAsync fill:#FFF9C4,stroke:#333,stroke-width:1px
    style GCSSync fill:#FFF9C4,stroke:#333,stroke-width:1px
    style Q fill:#FFCDD2,stroke:#333,stroke-width:1px
    style MDB fill:#B2EBF2,stroke:#333,stroke-width:1px
    style DocProcSvc fill:#D7CCC8,stroke:#5D4037,color:#333
    style EmbeddingSvc fill:#D1E8FF,stroke:#4A90E2,color:#333
    style ExternalEmbeddingModel fill:#FFEBEE,stroke:#F44336,color:#333
    style ExternalParserLibs fill:#CFD8DC,stroke:#333,stroke-width:1px
```

## 3. Características Clave

*   **API RESTful:** Endpoints para ingesta, consulta de estado, reintento y eliminación de documentos.
*   **Procesamiento Asíncrono:** Utiliza Celery con Redis como broker para manejar tareas de procesamiento de documentos de forma desacoplada.
*   **Worker Síncrono con Llamadas HTTP:** El worker Celery realiza operaciones de I/O de base de datos (PostgreSQL, Milvus) y almacenamiento (GCS) de forma síncrona, y orquesta llamadas HTTP síncronas a servicios externos (`docproc-service`, `embedding-service`).
*   **Almacenamiento Distribuido:**
    *   **Google Cloud Storage (GCS):** Para los archivos originales.
    *   **PostgreSQL:** Para metadatos de documentos, estado de procesamiento y detalles de los chunks.
    *   **Milvus (Zilliz Cloud):** Para los vectores de embedding y metadatos clave para búsqueda.
*   **Pipeline de Orquestación:**
    *   **Extracción de Texto y Chunking (Remoto):** Delegado al `docproc-service`.
    *   **Embedding (Remoto):** Delegado al `embedding-service`.
    *   **Indexación Dual:** Los vectores y metadatos primarios se indexan en Milvus; información más detallada de los chunks se almacena en PostgreSQL.
*   **Multi-tenancy:** Aislamiento de datos por `company_id` en GCS (prefijos), PostgreSQL (columnas dedicadas) y Milvus (campos escalares en la colección y filtros en queries).
*   **Estado Actualizado y Consistencia:** Los endpoints de estado realizan verificaciones en GCS y Milvus para intentar mantener la consistencia con los registros de PostgreSQL.
*   **Eliminación Completa:** El endpoint `DELETE /{document_id}` (y `DELETE /bulk`) se encarga de eliminar los datos del documento de PostgreSQL (incluyendo chunks vía cascada), GCS y Milvus.

## 4. Requisitos de la Base de Datos (PostgreSQL)

*   **Tabla `documents`:** Almacena metadatos generales del documento. Debe tener una columna `error_message TEXT` para registrar fallos. Campos clave: `id`, `company_id`, `file_name`, `file_type`, `file_path`, `metadata (JSONB)`, `status`, `chunk_count`, `error_message`, `uploaded_at`, `updated_at`.
*   **Tabla `document_chunks`:** Almacena detalles de cada chunk procesado. Se crea y gestiona vía SQLAlchemy en el worker. Campos clave: `id`, `document_id` (FK a `documents.id` con `ON DELETE CASCADE`), `company_id`, `chunk_index`, `content`, `metadata (JSONB)` (para metadatos específicos del chunk como página, título, hash), `embedding_id` (PK del chunk en Milvus), `vector_status`, `created_at`.

## 5. Pila Tecnológica Principal

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Procesamiento Asíncrono:** Celery, Redis
*   **Base de Datos Relacional:** PostgreSQL (accedida con `asyncpg` en la API y `SQLAlchemy/psycopg2` en el Worker)
*   **Base de Datos Vectorial:** Milvus (conectado a Zilliz Cloud usando `Pymilvus`)
*   **Almacenamiento de Objetos:** Google Cloud Storage (`google-cloud-storage`)
*   **Cliente HTTP:** `httpx` (para llamadas síncronas desde el worker a servicios externos, y también usado por los clientes de servicio base)
*   **Tokenización (para conteo):** `tiktoken` (opcional, para metadatos de chunks)
*   **Despliegue:** Docker, Kubernetes (GKE)

## 6. Estructura de la Codebase (Relevante)

```
ingest-service/
├── app/
│   ├── api/v1/
│   │   ├── endpoints/ingest.py   # Endpoints FastAPI
│   │   └── schemas.py            # Schemas Pydantic para API
│   ├── core/
│   │   ├── config.py             # Configuración (Pydantic BaseSettings)
│   │   └── logging_config.py     # Configuración de logging (Structlog)
│   ├── db/
│   │   └── postgres_client.py    # Clientes DB (async y sync) y schema de chunks
│   ├── main.py                   # Punto de entrada FastAPI, lifespan
│   ├── models/
│   │   └── domain.py             # Modelos de dominio Pydantic (estados, etc.)
│   ├── services/
│   │   ├── base_client.py        # Cliente HTTP base para servicios externos
│   │   ├── clients/
│   │   │   ├── docproc_service_client.py   # Cliente para docproc-service
│   │   │   └── embedding_service_client.py # Cliente para embedding-service
│   │   ├── gcs_client.py         # Cliente para Google Cloud Storage
│   │   └── ingest_pipeline.py    # Lógica de indexación en Milvus y preparación PG
│   └── tasks/
│       ├── celery_app.py         # Configuración de la app Celery
│       └── process_document.py   # Tarea Celery principal (process_document_standalone)
├── pyproject.toml                # Dependencias (Poetry)
└── README.md                     # Este archivo
```

## 7. Configuración (Variables de Entorno y Kubernetes)

Variables de entorno clave (prefijo `INGEST_`):
*   `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_SERVER`, `POSTGRES_PORT`, `POSTGRES_DB`
*   `GCS_BUCKET_NAME` (y credenciales `GOOGLE_APPLICATION_CREDENTIALS` en el entorno del pod/worker)
*   `MILVUS_URI`: URI del endpoint de Zilliz Cloud (ej. `https://in03-xxxx.serverless.gcp-us-west1.cloud.zilliz.com`)
*   `ZILLIZ_API_KEY`: API Key para autenticarse con Zilliz Cloud.
*   `MILVUS_COLLECTION_NAME`: Nombre de la colección en Milvus (ej. `document_chunks_minilm`).
*   `EMBEDDING_DIMENSION`: Dimensión de los vectores de embedding, usada para el esquema de Milvus (ej. 384).
*   `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` (URLs de Redis).
*   **`INGEST_DOCPROC_SERVICE_URL`**: URL del `docproc-service` (ej. `http://docproc-service.nyro-develop.svc.cluster.local:80/api/v1/process`).
*   **`INGEST_EMBEDDING_SERVICE_URL`**: URL del `embedding-service` (ej. `http://embedding-service.nyro-develop.svc.cluster.local:80/api/v1/embed`).
*   `LOG_LEVEL`, `SUPPORTED_CONTENT_TYPES`.
*   `HTTP_CLIENT_TIMEOUT`, `HTTP_CLIENT_MAX_RETRIES`, `HTTP_CLIENT_BACKOFF_FACTOR`: Para llamadas a servicios externos.
*   `TIKTOKEN_ENCODING_NAME`: Para el conteo de tokens en chunks.

## 8. API Endpoints

Los endpoints principales se encuentran bajo el prefijo `/api/v1/ingest`.

*   **`POST /upload`**: Inicia la ingesta de un nuevo documento.
    *   Headers: `X-Company-ID`, `X-User-ID`.
    *   Body: `file` (UploadFile), `metadata_json` (Form, opcional).
    *   Respuesta (202): `IngestResponse` con `document_id`, `task_id`, `status`.
*   **`GET /status/{document_id}`**: Obtiene el estado detallado de un documento.
    *   Headers: `X-Company-ID`.
    *   Respuesta (200): `StatusResponse`.
*   **`GET /status`**: Lista documentos para la compañía con paginación.
    *   Headers: `X-Company-ID`.
    *   Query Params: `limit`, `offset`.
    *   Respuesta (200): `List[StatusResponse]`. (El schema `PaginatedStatusResponse` es para referencia, la API devuelve la lista directamente).
*   **`POST /retry/{document_id}`**: Reintenta el procesamiento de un documento en estado `error`.
    *   Headers: `X-Company-ID`, `X-User-ID`.
    *   Respuesta (202): `IngestResponse`.
*   **`DELETE /{document_id}`**: Elimina un documento y todos sus datos asociados (GCS, Milvus, PostgreSQL).
    *   Headers: `X-Company-ID`.
    *   Respuesta (204): No Content.
*   **`DELETE /bulk`**: Elimina múltiples documentos y sus datos asociados.
    *   Headers: `X-Company-ID`.
    *   Body: `{"document_ids": ["id1", "id2", ...]}`.
    *   Respuesta (200): `{"deleted": [...], "failed": [...]}`.

Rutas duplicadas sin el prefijo `/ingest` (ej. `/upload` en lugar de `/ingest/upload`) están marcadas con `include_in_schema=False` para evitar confusión en la documentación OpenAPI, pero funcionan.

## 9. Dependencias Externas Clave

*   **PostgreSQL:** Almacén de metadatos y estado.
*   **Milvus / Zilliz Cloud:** Base de datos vectorial para embeddings.
*   **Google Cloud Storage (GCS):** Almacenamiento de archivos originales.
*   **Redis:** Broker y backend para Celery.
*   **Atenex Document Processing Service (`docproc-service`):** Servicio externo para extracción de texto y chunking.
*   **Atenex Embedding Service (`embedding-service`):** Servicio externo para generar embeddings.

## 10. Pipeline de Ingesta (Lógica del Worker Celery)

El pipeline es orquestado por la tarea Celery `process_document_standalone`:

1.  **Descarga de Archivo:** El worker obtiene el archivo original desde GCS.
2.  **Extracción de Texto y Chunking (Remoto):**
    *   Se realiza una llamada HTTP al `docproc-service` enviando el contenido del archivo.
    *   `docproc-service` es responsable de convertir el formato del archivo (PDF, DOCX, etc.) a texto plano y dividir este texto en fragmentos (chunks) de tamaño y superposición adecuados.
    *   Devuelve una lista de chunks con su contenido textual y metadatos básicos (ej. número de página si aplica).
3.  **Generación de Embeddings (Remoto):**
    *   Para cada texto de chunk obtenido, se realiza una llamada HTTP al `embedding-service`.
    *   `embedding-service` genera los vectores de embedding para los textos de los chunks.
    *   Devuelve la lista de vectores y la información del modelo de embedding utilizado.
4.  **Generación de Metadatos Adicionales para Chunks:**
    *   El `ingest-service` calcula metadatos como el hash del contenido del chunk y el número de tokens (usando `tiktoken`).
5.  **Indexación en Milvus (Zilliz Cloud):**
    *   Se utiliza `Pymilvus` para interactuar con la colección en Zilliz Cloud.
    *   Se eliminan los chunks existentes en Milvus para el `document_id` y `company_id` actual.
    *   Se insertan los nuevos chunks. Cada chunk en Milvus contiene:
        *   `pk_id`: Clave primaria (formato: `{document_id}_{chunk_index}`).
        *   `embedding`: El vector de embedding.
        *   `content`: El texto del chunk (truncado si excede `MILVUS_CONTENT_FIELD_MAX_LENGTH`).
        *   `company_id`: UUID de la compañía (para multi-tenancy).
        *   `document_id`: UUID del documento padre.
        *   `file_name`: Nombre del archivo original.
        *   `page`: Número de página (si aplica).
        *   `title`: Título generado para el chunk.
        *   `tokens`: Conteo de tokens.
        *   `content_hash`: Hash SHA256 del contenido del chunk.
    *   Se realiza un `flush` de la colección para asegurar la persistencia de los datos.
6.  **Indexación en PostgreSQL (Tabla `document_chunks`):**
    *   Se realiza una inserción masiva (`bulk_insert_chunks_sync`) en la tabla `document_chunks`.
    *   Cada registro contiene: `id` (UUID del chunk en PG), `document_id`, `company_id`, `chunk_index`, `content` (texto completo), `metadata` (JSONB con página, título, tokens, hash), `embedding_id` (el `pk_id` de Milvus), `vector_status` (`created`).

## 11. Multi-Tenancy en la Base de Datos Vectorial (Milvus/Zilliz)

El aislamiento de datos entre diferentes compañías (tenants) en Milvus se logra a nivel de datos dentro de una única colección, en lugar de usar colecciones separadas por tenant:

*   **Esquema de Colección:** La colección Milvus (definida por `MILVUS_COLLECTION_NAME`) incluye campos escalares específicos para la tenencia, principalmente `company_id` (de tipo `DataType.VARCHAR`). También se almacena `document_id`.
*   **Inserción de Datos:** Durante la indexación (`index_chunks_in_milvus_and_prepare_for_pg`), el `company_id` y `document_id` del documento que se está procesando se incluyen en cada entidad (chunk) que se inserta en Milvus.
*   **Filtrado en Operaciones:**
    *   **Consultas/Búsquedas:** Cualquier búsqueda semántica o consulta de metadatos que se realice contra Milvus (por ejemplo, desde un `query-service`) *debe* incluir una cláusula de filtro en la expresión booleana que especifique el `company_id` del tenant actual. Ejemplo: `expr = f'{MILVUS_COMPANY_ID_FIELD} == "uuid_de_la_compañia"'`.
    *   **Eliminaciones:** Al eliminar chunks de un documento (`_delete_milvus_sync` en la API, o `delete_milvus_chunks` en el pipeline del worker), la expresión de eliminación siempre incluye `MILVUS_COMPANY_ID_FIELD == "{company_id}"` y `MILVUS_DOCUMENT_ID_FIELD == "{document_id}"` para asegurar que solo se borren los datos del documento específico dentro de la compañía correcta.
    *   **Conteo:** Al contar chunks para un documento (`_get_milvus_chunk_count_sync`), también se filtra por `company_id` y `document_id`.
*   **Indexación:** Se crean índices escalares en los campos `company_id` y `document_id` en Milvus para optimizar el rendimiento de los filtros basados en estos campos.

Esta estrategia permite gestionar múltiples tenants en una sola infraestructura de Milvus, simplificando la gestión de colecciones, pero requiere una disciplina estricta en la aplicación para aplicar siempre los filtros de `company_id`.

## 12. TODO / Mejoras Futuras

*   Implementar un sistema de caché más sofisticado para los resultados de GCS/Milvus en los endpoints de status si se convierten en un cuello de botella.
*   Considerar estrategias de re-procesamiento más granulares (ej. solo re-embeddear si el modelo cambia).
*   Mejorar la observabilidad con trazas distribuidas entre los microservicios.
*   Añadir tests unitarios y de integración más exhaustivos.
*   Explorar el uso de particiones en Milvus si el número de tenants o la cantidad de datos por tenant crece masivamente, aunque el filtrado por `company_id` es efectivo para muchos casos.

## 13. Licencia

(Especificar Licencia del Proyecto)