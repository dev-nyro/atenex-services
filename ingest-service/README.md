# Atenex Ingest Service (Microservicio de Ingesta) v0.3.1 - (Remote Embedding/Pymilvus)

## 1. Visión General

El **Ingest Service** es un microservicio clave dentro de la plataforma Atenex. Su responsabilidad principal es recibir documentos subidos por los usuarios (PDF, DOCX, TXT, HTML, MD), procesarlos de manera asíncrona, almacenar los archivos originales en **Google Cloud Storage (GCS)** (bucket `atenex`) y finalmente indexar el contenido procesado en bases de datos (**PostgreSQL** para metadatos y **Milvus** para vectores) para su uso posterior en búsquedas semánticas y generación de respuestas por LLMs.

Este servicio ha sido **refactorizado** para:
*   Delegar la generación de embeddings a un microservicio dedicado: **`embedding-service`**. Ya no genera embeddings localmente.
*   Utilizar librerías independientes como **Pymilvus** para la interacción directa con Milvus, y convertidores de documentos standalone (`pypdf`, `python-docx`, etc.).
*   El worker de Celery opera de forma **síncrona** para las operaciones de base de datos (usando SQLAlchemy) y GCS, y realiza llamadas HTTP al `embedding-service`.

**Flujo principal:**

1.  **Recepción:** La API (`POST /api/v1/ingest/upload`) recibe el archivo (`file`) y metadatos opcionales (`metadata_json`). Requiere los headers `X-Company-ID` y `X-User-ID`.
2.  **Validación:** Verifica el tipo de archivo (`Content-Type`) y metadatos. Previene duplicados.
3.  **Persistencia Inicial (API - Async):**
    *   Crea un registro inicial del documento en **PostgreSQL** (tabla `documents`) con estado `pending`.
    *   Guarda el archivo original en **GCS**.
    *   Actualiza el registro en PostgreSQL a estado `uploaded`.
4.  **Encolado:** Dispara una tarea asíncrona usando **Celery** (con **Redis** como broker) para el procesamiento (`process_document_standalone`).
5.  **Respuesta API:** La API responde `202 Accepted` con `document_id`, `task_id` y estado `uploaded`.
6.  **Procesamiento Asíncrono (Worker Celery):**
    *   La tarea Celery (`process_document_standalone`) recoge el trabajo.
    *   Actualiza el estado en PostgreSQL a `processing`.
    *   Descarga el archivo de GCS.
    *   **Ejecuta el Pipeline Personalizado (`ingest_document_pipeline`):**
        *   **Conversión:** Extrae texto usando librerías adecuadas.
        *   **Chunking:** Divide el texto en fragmentos.
        *   **Embedding (Remoto):** Llama al **`embedding-service`** vía HTTP para obtener los vectores de embedding para cada chunk.
        *   **Indexación (Milvus):** Escribe los chunks (contenido, vector y metadatos) en **Milvus** usando **Pymilvus**.
        *   **Indexación (PostgreSQL):** Guarda información detallada de los chunks (contenido, metadatos, ID de embedding de Milvus) en la tabla `document_chunks` de PostgreSQL.
    *   **Actualización Final (Worker - Sync):** Actualiza el estado del documento en PostgreSQL a `processed` y registra el número de chunks. Si hay errores, actualiza a `error` con un mensaje.
7.  **Consulta de Estado y Operaciones Adicionales:** Endpoints para consultar estado, reintentar y eliminar documentos, con verificaciones en GCS y Milvus.

## 2. Arquitectura General del Proyecto (Actualizado)

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
            I -- GET /status, DELETE /{id} -->|Pymilvus Sync Helper<br/>(via Executor)| MDB[(Milvus<br/>Collection Configurada<br/><b>Pymilvus</b>)]

            %% Worker Interactions (Sync + Async HTTP Call) %%
            W(Celery Worker<br/><b>Prefork - Sync Ops</b><br/><i>process_document_standalone</i>) -- Picks Task --> Q
            W -- Update Status, Save Chunks --> DBSync[(PostgreSQL<br/>'atenex' DB<br/><b>SQLAlchemy/psycopg2</b>)]
            W -- Download File --> GCSSync[(Google Cloud Storage<br/>'atenex' Bucket<br/><b>google-cloud-storage (sync)</b>)]
            W -- Execute Pipeline --> Pipe["<strong>Custom Pipeline</strong><br/>(Extract, Chunk, Call Embed Svc, Index)"]
            Pipe -- Convert --> Libs[("Standalone Libs<br/>(PyMuPDF, python-docx...)")]
            Pipe -- Call for Embeddings --> EmbeddingSvc["<strong>Embedding Service</strong><br/>(HTTP API)"]
            Pipe -- Index/Delete Existing --> MDB

        end
        EmbeddingSvc --> ExternalEmbeddingModel[("Modelo Embedding<br/>(ej. all-MiniLM-L6-v2)")]
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
    style EmbeddingSvc fill:#D1E8FF,stroke:#4A90E2,color:#333
    style ExternalEmbeddingModel fill:#FFEBEE,stroke:#F44336,color:#333
    style Libs fill:#CFD8DC,stroke:#333,stroke-width:1px
```
*Diagrama actualizado reflejando la llamada al `embedding-service`.*

## 3. Características Clave (Actualizado)

*   **API RESTful:** Endpoints para ingesta, estado, reintento y eliminación.
*   **Procesamiento Asíncrono:** Celery y Redis.
*   **Worker Síncrono con Llamada HTTP Asíncrona:** El worker Celery realiza operaciones de I/O (DB, GCS) de forma síncrona y llama al `embedding-service` (HTTP).
*   **Almacenamiento:** GCS para archivos, PostgreSQL para metadatos/estado y chunks, Milvus para vectores.
*   **Pipeline Personalizado:**
    *   Conversión de documentos (PyMuPDF, python-docx, etc.).
    *   Chunking de texto.
    *   **Embedding Remoto:** Obtención de embeddings llamando al `embedding-service`.
    *   Indexación en Milvus (Pymilvus) y PostgreSQL.
*   **Multi-tenancy:** Aislamiento por `company_id`.
*   **Estado Actualizado:** Verificaciones en GCS/Milvus en tiempo real.
*   **Eliminación Completa:** `DELETE /{id}` elimina datos de PostgreSQL, GCS y Milvus.

## 4. Requisitos de la base de datos (IMPORTANTE)

La tabla `documents` en PostgreSQL **debe tener la columna** `error_message TEXT`.
La tabla `document_chunks` es nueva y almacena detalles de cada chunk. Su esquema se define y crea (si no existe) a través de SQLAlchemy en el worker. Campos clave: `id`, `document_id`, `company_id`, `chunk_index`, `content`, `metadata (JSONB)`, `embedding_id (Milvus PK)`, `vector_status`, `created_at`.

## 5. Pila Tecnológica Principal (Actualizado)

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Procesamiento Asíncrono:** Celery, Redis
*   **Base de Datos Relacional:** PostgreSQL (asyncpg para API, SQLAlchemy/psycopg2 para Worker)
*   **Base de Datos Vectorial:** Milvus (Pymilvus)
*   **Almacenamiento de Objetos:** Google Cloud Storage (google-cloud-storage)
*   **Cliente HTTP:** HTTPretty (para llamadas a `embedding-service`)
*   **Convertidores de Documentos:** PyMuPDF, python-docx, BeautifulSoup4, Markdown, html2text
*   **Despliegue:** Docker, Kubernetes (GKE)

## 6. Estructura de la Codebase (Relevante)

```
ingest-service/
├── app/
│   ├── api/v1/endpoints/ingest.py
│   ├── core/
│   │   ├── config.py             # Configuración, URLs de servicios externos
│   │   └── logging_config.py
│   ├── db/postgres_client.py     # Clientes DB (async y sync)
│   ├── services/
│   │   ├── clients/
│   │   │   └── embedding_service_client.py # Cliente para embedding-service
│   │   ├── extractors/           # Extractores de texto
│   │   ├── ingest_pipeline.py    # Lógica del pipeline (llama a embedding-service)
│   │   └── text_splitter.py
│   └── tasks/
│       ├── celery_app.py
│       └── process_document.py   # Tarea Celery (usa ingest_pipeline)
├── pyproject.toml                # Dependencias (Poetry)
└── README.md                     # Este archivo
```
*Nota: `app/services/embedder.py` ha sido eliminado.*

## 7. Configuración (Kubernetes)

Variables clave (prefijo `INGEST_`):
*   `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_SERVER`, `POSTGRES_PORT`, `POSTGRES_DB`
*   `GCS_BUCKET_NAME` (y `GOOGLE_APPLICATION_CREDENTIALS` en el entorno)
*   `MILVUS_URI`, `MILVUS_COLLECTION_NAME`
*   `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`
*   **`INGEST_EMBEDDING_SERVICE_URL`**: URL del `embedding-service` (ej. `http://embedding-service.nyro-develop.svc.cluster.local:8003/api/v1/embed`).
*   `EMBEDDING_DIMENSION`: Dimensión de los vectores esperados del `embedding-service`, usada para el esquema de Milvus (ej. 384 para `all-MiniLM-L6-v2`).
*   `LOG_LEVEL`, `SUPPORTED_CONTENT_TYPES`, `SPLITTER_CHUNK_SIZE`, `SPLITTER_CHUNK_OVERLAP`.

## 8. API Endpoints
(Sin cambios en la firma de los endpoints, pero el comportamiento interno se actualiza)

... (Se mantiene la misma lista de endpoints que en el README anterior) ...

## 9. Dependencias Externas Clave (Actualizado)

*   **PostgreSQL**
*   **Milvus**
*   **Google Cloud Storage**
*   **Redis**
*   **Atenex Embedding Service:** El nuevo servicio externo para generar embeddings.

## 10. Pipeline de Ingesta (`ingest_document_pipeline`)

1.  **Extracción de Texto.**
2.  **Chunking.**
3.  **Generación de Metadatos para Chunks.**
4.  **Obtención de Embeddings:** Llama al `embedding-service` con los textos de los chunks.
5.  **Escritura en Milvus:** Los chunks (contenido, vector, metadatos) se insertan en Milvus.
6.  **Escritura en PostgreSQL:** Se guardan los detalles de los chunks (incluyendo el ID de Milvus) en la tabla `document_chunks`.

## 11. TODO / Mejoras Futuras
(Sin cambios respecto al README anterior)

## 12. Licencia
(Especificar Licencia del Proyecto)