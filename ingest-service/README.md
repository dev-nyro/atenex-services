# Atenex Ingest Service (Microservicio de Ingesta) v0.2.0 - (FastEmbed/Pymilvus)

## 1. Visión General

El **Ingest Service** es un microservicio clave dentro de la plataforma Atenex. Su responsabilidad principal es recibir documentos subidos por los usuarios (PDF, DOCX, TXT, HTML, MD), procesarlos de manera asíncrona utilizando un **pipeline personalizado** (que ya **NO** utiliza Haystack), almacenar los archivos originales en **MinIO** y finalmente indexar el contenido procesado en bases de datos (**PostgreSQL** para metadatos y **Milvus** para vectores) para su uso posterior en búsquedas semánticas y generación de respuestas por LLMs.

Este servicio ha sido **refactorizado** para eliminar la dependencia de Haystack AI, utilizando en su lugar librerías independientes como **FastEmbed** para la generación de embeddings (ejecutados localmente en CPU por defecto), **Pymilvus** para la interacción directa con Milvus, y convertidores de documentos standalone (`pypdf`, `python-docx`, etc.). El worker de Celery ahora opera de forma **síncrona** para las operaciones de base de datos (usando SQLAlchemy) y MinIO.

**Flujo principal:**

1.  **Recepción:** La API (`POST /api/v1/ingest/upload`) recibe el archivo (`file`) y metadatos opcionales (`metadata_json`). Requiere los headers `X-Company-ID` y `X-User-ID` inyectados por el API Gateway.
2.  **Validación:** Verifica el tipo de archivo (`Content-Type`) contra los tipos soportados y valida el formato JSON de los metadatos. Previene subida de duplicados (mismo nombre, misma compañía, estado no-error).
3.  **Persistencia Inicial (API - Async):**
    *   Crea un registro inicial del documento en **PostgreSQL** (tabla `documents`) con estado `pending` usando `asyncpg`.
    *   Guarda el archivo original en **MinIO** (bucket configurado, ej. `ingested-documents`) bajo la ruta `company_id/document_id/filename` usando el cliente `minio-py` (asíncrono vía `run_in_executor`).
    *   Actualiza el registro en PostgreSQL a estado `uploaded`.
4.  **Encolado:** Dispara una tarea asíncrona usando **Celery** (con **Redis** como broker) para el procesamiento pesado (`process_document_standalone`).
5.  **Respuesta API:** La API responde inmediatamente `202 Accepted` con el `document_id`, `task_id` de Celery y el estado `uploaded`.
6.  **Procesamiento Asíncrono (Worker Celery - Sync Operations):**
    *   La tarea Celery (`process_document.py` -> `process_document_standalone`) recoge el trabajo.
    *   **Pre-checks:** Verifica que los recursos del worker (DB Engine síncrono, modelo FastEmbed, cliente MinIO) estén inicializados. Falla si no lo están.
    *   Actualiza el estado en PostgreSQL a `processing` (limpiando `error_message`) usando el cliente **síncrono** de DB (`SQLAlchemy` + `psycopg2`).
    *   Descarga el archivo de MinIO usando el cliente **síncrono** de MinIO.
    *   **Ejecuta el Pipeline Personalizado (`ingest_document_pipeline`):**
        *   **Conversión:** Selecciona el extractor de texto adecuado (`pypdf`, `python-docx`, `beautifulsoup4`, etc.) según el `Content-Type`. Falla si no es soportado.
        *   **Chunking:** Divide el texto extraído en fragmentos (chunks) usando una función personalizada (`chunk_text`).
        *   **Embedding:** Genera vectores de embedding para cada chunk usando el modelo **FastEmbed** configurado (ej. `BAAI/bge-large-en-v1.5`) que se ejecuta **localmente en CPU** (por defecto).
        *   **Indexación:** Escribe los chunks (contenido, vector y metadatos como `company_id`, `document_id`, `file_name`) en **Milvus** (colección configurada, ej. `document_chunks_haystack`) usando el cliente **Pymilvus**. Elimina chunks existentes para el documento si `delete_existing=True`.
    *   **Actualización Final (Worker - Sync):** Actualiza el estado del documento en PostgreSQL a `processed` y registra el número real de chunks escritos (`chunk_count`) usando el cliente DB **síncrono**. Si ocurre un error durante el pipeline, actualiza a `error` y guarda un mensaje descriptivo en `error_message`.
7.  **Consulta de Estado (API - Async):** La API expone endpoints para consultar el estado:
    *   `GET /api/v1/ingest/status/{document_id}`: Estado de un documento específico, incluyendo verificación en tiempo real de existencia en MinIO (async) y conteo de chunks en Milvus (usando helpers **síncronos** de **Pymilvus** ejecutados en `run_in_executor`). Puede actualizar el estado en DB si detecta inconsistencias.
    *   `GET /api/v1/ingest/status`: Lista paginada de estados. Realiza verificaciones en MinIO/Milvus en paralelo para cada documento listado y actualiza la DB con el estado real encontrado antes de devolver la respuesta.
8.  **Reintento de Ingesta:** Permite reintentar la ingesta de un documento en estado `error` (`POST /api/v1/ingest/retry/{document_id}`). Actualiza el estado a `processing` y limpia `error_message` (async), y encola de nuevo la tarea `process_document_standalone`.
9.  **Eliminación Completa:** El endpoint `DELETE /api/v1/ingest/{document_id}` elimina los chunks asociados en **Milvus** (usando helpers síncronos de Pymilvus en executor), el archivo original en **MinIO** (async) y el registro en **PostgreSQL** (async).

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
            I -- GET /status, POST /upload, DELETE /{id} --> S3[(MinIO<br/>'ingested-documents' Bucket<br/><b>minio-py (async helper)</b>)]
            I -- POST /upload, RETRY --> Q([Redis<br/>Celery Broker])
            I -- GET /status, DELETE /{id} -->|Pymilvus Sync Helper<br/>(via Executor)| MDB[(Milvus<br/>'document_chunks_haystack' Collection<br/><b>Pymilvus</b>)]

            %% Worker Interactions (Sync) %%
            W(Celery Worker<br/><b>Prefork - Sync Ops</b><br/><i>process_document_standalone</i>) -- Picks Task --> Q
            W -- Initialize --> FE((FastEmbed Model<br/><b>Local CPU</b>))
            W -- Update Status --> DBSync[(PostgreSQL<br/>'atenex' DB<br/><b>SQLAlchemy/psycopg2</b>)]
            W -- Download File --> S3Sync[(MinIO<br/>'ingested-documents' Bucket<br/><b>minio-py (sync)</b>)]
            W -- Execute Pipeline --> Pipe["<strong>Custom Pipeline</strong><br/>(Extract, Chunk, Embed, Index)"]
            Pipe -- Convert --> Libs[("Standalone Libs<br/>(PyPDF, python-docx...)")]
            Pipe -- Embed --> FE
            Pipe -- Index/Delete Existing --> MDB

        end

    end

    %% Estilo %%
    style I fill:#C8E6C9,stroke:#333,stroke-width:2px
    style W fill:#BBDEFB,stroke:#333,stroke-width:2px
    style Pipe fill:#FFECB3,stroke:#666,stroke-width:1px
    style DBAsync fill:#F8BBD0,stroke:#333,stroke-width:1px
    style DBSync fill:#F8BBD0,stroke:#333,stroke-width:1px
    style S3 fill:#FFF9C4,stroke:#333,stroke-width:1px
    style S3Sync fill:#FFF9C4,stroke:#333,stroke-width:1px
    style Q fill:#FFCDD2,stroke:#333,stroke-width:1px
    style MDB fill:#B2EBF2,stroke:#333,stroke-width:1px
    style FE fill:#D1C4E9,stroke:#333,stroke-width:1px
    style Libs fill:#CFD8DC,stroke:#333,stroke-width:1px
```
*Diagrama actualizado reflejando la eliminación de Haystack, el uso de FastEmbed local, Pymilvus, operaciones síncronas en el worker (DB, MinIO), y helpers Pymilvus en la API.*

## 3. Características Clave (Actualizado)

*   **API RESTful:** Endpoints para ingesta (`/upload`), consulta de estado (`/status`, `/status/{id}`), reintento (`/retry/{document_id}`) y eliminación completa (`/{document_id}`).
*   **Procesamiento Asíncrono:** Celery y Redis para orquestación de tareas.
*   **Worker Síncrono:** El worker Celery realiza operaciones de I/O (DB, MinIO) y procesamiento de forma síncrona.
*   **Almacenamiento:** MinIO para archivos, PostgreSQL para metadatos/estado (incluyendo `error_message`), Milvus para vectores.
*   **Pipeline Personalizado (Sin Haystack):**
    *   Conversión de documentos usando librerías standalone (`pypdf`, `python-docx`, `beautifulsoup4`, etc.).
    *   Chunking de texto mediante función personalizada.
    *   Embedding usando **FastEmbed** (ejecución local, CPU por defecto).
    *   Indexación y borrado en Milvus usando **Pymilvus**.
*   **Multi-tenancy:** Aislamiento por `company_id` (en DB, MinIO path, y metadatos Milvus).
*   **Estado Actualizado:** `GET /status` y `GET /status/{id}` verifican MinIO/Milvus en tiempo real (usando helpers Pymilvus) y actualizan la DB si es necesario.
*   **Eliminación Completa:** `DELETE /{id}` elimina datos de PostgreSQL, MinIO y Milvus.
*   **Configuración Centralizada y Logging Estructurado (JSON).**
*   **Manejo de Errores:** Tareas Celery con reintentos para errores transitorios y registro de errores persistentes en la DB.

## 4. Requisitos de la base de datos (IMPORTANTE)

> **¡IMPORTANTE!**
> La tabla `documents` en PostgreSQL **debe tener la columna** `error_message TEXT` para que el servicio funcione correctamente.
> Si ves errores como `column "error_message" of relation "documents" does not exist`, ejecuta la siguiente migración SQL:

```sql
-- Asegúrate de que la columna exista
ALTER TABLE documents ADD COLUMN IF NOT EXISTS error_message TEXT;

-- Otras columnas esperadas (verifica tu esquema actual):
-- id UUID PRIMARY KEY
-- company_id UUID NOT NULL
-- file_name VARCHAR(512) NOT NULL
-- file_type VARCHAR(100) NOT NULL
-- file_path VARCHAR(1024) -- Almacena la ruta en MinIO
-- metadata JSONB
-- status VARCHAR(50) NOT NULL
-- chunk_count INTEGER DEFAULT 0
-- uploaded_at TIMESTAMPTZ DEFAULT timezone('utc', now())
-- updated_at TIMESTAMPTZ DEFAULT timezone('utc', now())

-- Índices recomendados:
-- CREATE INDEX IF NOT EXISTS idx_documents_company_id ON documents(company_id);
-- CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
-- CREATE INDEX IF NOT EXISTS idx_documents_company_filename ON documents(company_id, file_name); -- Para check de duplicados
```

Esto es necesario para que los endpoints de estado y manejo de errores funcionen correctamente.

## 5. Pila Tecnológica Principal (Actualizado)

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Procesamiento Asíncrono (Orquestación):** Celery, Redis
*   **Base de Datos Relacional (Cliente API):** PostgreSQL (via **asyncpg**)
*   **Base de Datos Relacional (Cliente Worker):** PostgreSQL (via **SQLAlchemy** + **psycopg2-binary**)
*   **Base de Datos Vectorial (Cliente API/Worker):** Milvus (via **Pymilvus**)
*   **Almacenamiento de Objetos (Cliente API/Worker):** MinIO (via **minio-py**)
*   **Modelo de Embeddings (Ingesta):** **FastEmbed** (ej. `BAAI/bge-large-en-v1.5`, ejecución local en CPU por defecto)
*   **Convertidores de Documentos:** PyPDF, python-docx, BeautifulSoup4, Markdown, html2text
*   **Despliegue:** Docker, Kubernetes (GKE)

## 6. Estructura de la Codebase (Actualizado)

```
ingest-service/
├── app/
│   ├── __init__.py
│   ├── api/
│   │   ├── __init__.py
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── endpoints/
│   │       │   ├── __init__.py
│   │       │   └── ingest.py       # Endpoints API (FastAPI, async)
│   │       └── schemas.py        # Schemas Pydantic (Request/Response)
│   ├── core/                     # Configuración, Logging
│   │   ├── __init__.py
│   │   ├── config.py
│   │   └── logging_config.py
│   ├── db/
│   │   ├── __init__.py
│   │   └── postgres_client.py    # Cliente DB: Async (asyncpg) para API, Sync (SQLAlchemy) para Worker
│   ├── main.py                   # Entrypoint FastAPI, lifespan, middleware, health check
│   ├── models/
│   │   ├── __init__.py
│   │   └── domain.py             # Enum DocumentStatus
│   ├── services/
│   │   ├── __init__.py
│   │   ├── base_client.py        # (Opcional) Base para otros clientes HTTP
│   │   ├── ingest_pipeline.py    # Lógica del pipeline SIN Haystack (extract, chunk, embed, index)
│   │   └── minio_client.py       # Cliente MinIO (métodos sync y async helper)
│   └── tasks/
│       ├── __init__.py
│       ├── celery_app.py         # Configuración de la app Celery
│       └── process_document.py   # Tarea Celery `process_document_standalone` (usa pipeline, sync DB/MinIO)
├── k8s/                          # Configuración Kubernetes (No incluida en análisis)
│   ├── ...
├── Dockerfile                    # Define imagen Docker
├── pyproject.toml                # Dependencias (Poetry) - Refleja FastEmbed, Pymilvus, SQLAlchemy, etc.
├── poetry.lock
└── README.md                     # Este archivo
```

## 7. Configuración (Kubernetes)

El servicio se configura principalmente mediante variables de entorno (o un ConfigMap/Secret en Kubernetes). Claves importantes (prefijo `INGEST_`):

*   **`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_SERVER`, `POSTGRES_PORT`, `POSTGRES_DB`**: Conexión a PostgreSQL. Usada por API (asyncpg) y Worker (psycopg2).
*   **`MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_BUCKET_NAME`**: Conexión a MinIO.
*   **`MILVUS_URI`**: URI de conexión a Milvus (ej. `http://milvus-standalone.nyro-develop.svc.cluster.local:19530`).
*   **`MILVUS_COLLECTION_NAME`**: Nombre de la colección en Milvus (ej. `document_chunks_haystack`).
*   **`CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`**: URLs de Redis para Celery.
*   **`FASTEMBED_MODEL`**: Nombre del modelo FastEmbed a usar (ej. `BAAI/bge-large-en-v1.5`).
*   **`EMBEDDING_DIMENSION`**: Dimensión de los vectores generados por el modelo FastEmbed (ej. 1024 para `bge-large`). Debe coincidir con el modelo.
*   **`LOG_LEVEL`**: Nivel de logging (ej. `INFO`, `DEBUG`).
*   `SUPPORTED_CONTENT_TYPES`, `SPLITTER_CHUNK_SIZE`, `SPLITTER_CHUNK_OVERLAP`: Parámetros del pipeline.
*   `OPENAI_API_KEY`: (Opcional) Puede estar presente pero **no** se usa para la ingesta por defecto.

## 8. API Endpoints (Actualizado)

Prefijo base: `/api/v1/ingest`

---

### Health Check

*   **Endpoint:** `GET /` (Raíz del servicio)
*   **Descripción:** Verifica disponibilidad básica del servicio y conexión a DB durante startup.
*   **Respuesta OK (`200 OK`):** `OK` (Texto plano)
*   **Respuesta Error (`503 Service Unavailable`):** Si la conexión a DB falló en startup.

---

### Ingestar Documento

*   **Endpoint:** `POST /upload`
*   **Descripción:** Inicia la ingesta asíncrona. Previene duplicados.
*   **Headers Requeridos:** `X-Company-ID`, `X-User-ID`
*   **Request Body:** `multipart/form-data` (`file`, `metadata_json` opcional)
*   **Respuesta (`202 Accepted`):** `schemas.IngestResponse`

---

### Consultar Estado de Ingesta (Documento Individual)

*   **Endpoint:** `GET /status/{document_id}`
*   **Descripción:** Obtiene estado detallado, **verificando en tiempo real MinIO y Milvus (usando Pymilvus helpers)**. Puede actualizar el estado en DB si detecta inconsistencias.
*   **Headers Requeridos:** `X-Company-ID`
*   **Path Parameters:** `document_id` (UUID)
*   **Respuesta (`200 OK`):** `schemas.StatusResponse` (incluye `minio_exists`, `milvus_chunk_count`, `error_message`, etc.).

---

### Listar Estados de Ingesta (Paginado)

*   **Endpoint:** `GET /status`
*   **Descripción:** Obtiene lista paginada. **Realiza verificaciones en MinIO/Milvus en paralelo (usando Pymilvus helpers) para cada documento y actualiza el estado/chunks en la DB.** Devuelve los datos actualizados.
*   **Headers Requeridos:** `X-Company-ID`
*   **Query Parameters:** `limit`, `offset`
*   **Respuesta (`200 OK`):** `List[schemas.StatusResponse]` (incluye `minio_exists`, `milvus_chunk_count`, etc.).

---

### Reintentar Ingesta de Documento con Error

*   **Endpoint:** `POST /retry/{document_id}`
*   **Descripción:** Reintenta la ingesta si el estado es 'error'. Actualiza estado a 'processing', limpia `error_message` y reenvió la tarea `process_document_standalone`.
*   **Headers Requeridos:** `X-Company-ID`, `X-User-ID`
*   **Path Parameters:** `document_id` (UUID)
*   **Respuesta (`202 Accepted`):** `schemas.IngestResponse`

---

### Eliminar Documento

*   **Endpoint:** `DELETE /{document_id}`
*   **Descripción:** Elimina completamente el documento: registro en PostgreSQL (async), archivo en MinIO (async), y chunks en Milvus (usando Pymilvus helpers sync). Verifica propiedad.
*   **Headers Requeridos:** `X-Company-ID`
*   **Path Parameters:** `document_id` (UUID)
*   **Respuesta Exitosa (`204 No Content`):** Éxito.
*   **Respuestas Error:** `404 Not Found`, `500 Internal Server Error`, `503 Service Unavailable`.

---

## 9. Dependencias Externas Clave (Actualizado)

*   **PostgreSQL:** Almacenamiento de metadatos y estado.
*   **Milvus:** Base de datos vectorial para chunks y embeddings.
*   **MinIO:** Almacenamiento de objetos para los archivos originales.
*   **Redis:** Broker y backend de resultados para Celery.
*   **(Implícita) Modelo FastEmbed:** El modelo de embedding (ej. `BAAI/bge-large-en-v1.5`) debe ser descargable por el worker (generalmente la primera vez que se instancia `TextEmbedding`).

## 10. Pipeline de Ingesta Personalizado (`ingest_document_pipeline` en `app/services/ingest_pipeline.py`)

El worker Celery ejecuta esta función síncrona:

1.  **Extracción de Texto:** Se selecciona un extractor basado en `content_type` (ej. `PyPDF` para PDF, `python-docx` para DOCX).
2.  **Chunking:** El texto extraído se divide en chunks usando la función `chunk_text`.
3.  **Embedding:** Se generan los vectores para cada chunk usando la instancia de **FastEmbed** (`TextEmbedding`) inicializada en el worker.
4.  **Escritura en Milvus:** Los chunks (contenido, vector, metadatos) se insertan en la colección Milvus usando **Pymilvus**. Se pueden borrar los chunks existentes previamente.

## 11. TODO / Mejoras Futuras

*   **Optimización `GET /status` (Lista):** Si las verificaciones paralelas en `GET /status` resultan demasiado lentas bajo carga, considerar estrategias alternativas (ej. endpoint de refresco asíncrono, job de reconciliación periódico, caché).
*   **Soporte GPU para FastEmbed:** Investigar y habilitar opcionalmente el uso de GPU en el worker para acelerar la generación de embeddings si hay hardware disponible y el volumen lo justifica.
*   **Worker Asíncrono:** Evaluar la migración del worker a un modelo asíncrono (ej. `aio-celery`, `arq`) para potencialmente mejorar el rendimiento y la utilización de recursos, aunque aumenta la complejidad.
*   **Tests Unitarios y de Integración:** Expandir la cobertura de tests.
*   **Observabilidad:** Mejorar métricas (Prometheus), tracing distribuido (OpenTelemetry) y logging detallado.
*   **Manejo Avanzado de Errores:** Implementar patrones más sofisticados para errores específicos (ej. OCR para PDFs basados en imágenes).

## 12. Licencia

(Especificar Licencia del Proyecto)