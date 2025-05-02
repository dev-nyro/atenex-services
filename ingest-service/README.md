# Atenex Ingest Service (Microservicio de Ingesta) v0.3.0 - (Cloud SQL/GCS/Zilliz Cloud)

## 1. Visión General

El **Ingest Service** es un microservicio clave dentro de la plataforma Atenex. Su responsabilidad principal es recibir documentos subidos por los usuarios (PDF, DOCX, TXT, HTML, MD), procesarlos de manera asíncrona utilizando un **pipeline personalizado**, almacenar los archivos originales en **Google Cloud Storage (GCS)** y finalmente indexar el contenido procesado en bases de datos (**Cloud SQL para PostgreSQL** para metadatos y **Zilliz Cloud** para vectores) para su uso posterior en búsquedas semánticas y generación de respuestas por LLMs.

Este servicio ha sido **refactorizado** para utilizar servicios gestionados de GCP y Zilliz Cloud, y librerías independientes como **SentenceTransformers** (`sentence-transformers/all-MiniLM-L6-v2` por defecto) para la generación de embeddings (ejecutados localmente en CPU y cargados una vez por proceso worker), **Pymilvus** para la interacción directa con Zilliz Cloud, **Cloud SQL Python Connector** para conectar a PostgreSQL, `google-cloud-storage` para GCS, y convertidores de documentos standalone (`PyMuPDF`, `python-docx`, etc.). El worker de Celery opera de forma **síncrona** para las operaciones de base de datos (usando SQLAlchemy) y GCS.

**Flujo principal:**

1.  **Recepción:** La API (`POST /api/v1/ingest/upload`) recibe el archivo (`file`) y metadatos opcionales (`metadata_json`). Requiere los headers `X-Company-ID` y `X-User-ID` inyectados por el API Gateway. Normaliza el nombre del archivo recibido.
2.  **Validación:** Verifica el tipo de archivo (`Content-Type`) contra los tipos soportados y valida el formato JSON de los metadatos. Previene subida de duplicados (mismo nombre normalizado, misma compañía, estado no-error).
3.  **Persistencia Inicial (API - Async):**
    *   Crea un registro inicial del documento en **Cloud SQL (PostgreSQL)** (tabla `documents`) con estado `pending` usando el conector y `asyncpg`.
    *   Guarda el archivo original en **GCS** (bucket configurado, ej. `atenex`) bajo la ruta `company_id/document_id/normalized_filename` usando el cliente `google-cloud-storage` (asíncrono vía `run_in_executor`). Verifica que el archivo exista en GCS tras la subida.
    *   Actualiza el registro en Cloud SQL a estado `uploaded`.
4.  **Encolado:** Dispara una tarea asíncrona usando **Celery** (con **Redis** como broker) para el procesamiento pesado (`process_document_standalone`).
5.  **Respuesta API:** La API responde inmediatamente `202 Accepted` con el `document_id`, `task_id` de Celery y el estado `uploaded`.
6.  **Procesamiento Asíncrono (Worker Celery - Sync Ops):**
    *   **Inicialización del Worker:** Al iniciar cada proceso worker (`@worker_process_init`), se cargan y cachean los recursos necesarios: conexión síncrona a Cloud SQL (SQLAlchemy Engine vía conector), cliente GCS, y el modelo de **SentenceTransformer** configurado.
    *   La tarea Celery (`process_document.py` -> `process_document_standalone`) recoge el trabajo.
    *   **Pre-checks:** Verifica que los recursos del worker (DB Engine síncrono, modelo SentenceTransformer, cliente GCS) estén inicializados correctamente. Falla si no lo están.
    *   Actualiza el estado en Cloud SQL a `processing` (limpiando `error_message`) usando el cliente **síncrono** de DB (`SQLAlchemy` + `psycopg2` vía conector).
    *   Descarga el archivo como **bytes** desde GCS usando el cliente **síncrono** de GCS (`read_file_sync`).
    *   **Ejecuta el Pipeline Personalizado (`ingest_document_pipeline`):**
        *   **Conversión:** Selecciona el extractor de texto adecuado (`PyMuPDF`, `python-docx`, `BeautifulSoup`, etc.) según el `Content-Type` y procesa los bytes del archivo. Falla si no es soportado o la extracción falla.
        *   **Chunking:** Divide el texto extraído en fragmentos (chunks) usando una función personalizada (`text_splitter.py`).
        *   **Embedding:** Genera vectores de embedding para cada chunk usando la instancia del modelo **SentenceTransformer** cargada por el worker (`embedder.py`).
        *   **Truncado (Contenido):** Trunca el texto de los chunks si exceden el `MILVUS_CONTENT_FIELD_MAX_LENGTH` configurado para evitar errores en Zilliz Cloud.
        *   **Indexación:** Escribe los chunks (contenido truncado, vector y metadatos como `company_id`, `document_id`, `file_name`) en **Zilliz Cloud** (colección configurada, ej. `atenex-vector-db`) usando el cliente **Pymilvus**. Elimina chunks existentes para el documento si `delete_existing=True`.
    *   **Actualización Final (Worker - Sync):** Actualiza el estado del documento en Cloud SQL a `processed` y registra el número real de chunks escritos (`chunk_count`) usando el cliente DB **síncrono**. Si ocurre un error durante el pipeline, actualiza a `error` y guarda un mensaje descriptivo en `error_message`.
7.  **Consulta de Estado (API - Async):** La API expone endpoints para consultar el estado:
    *   `GET /api/v1/ingest/status/{document_id}`: Estado de un documento específico, incluyendo verificación en tiempo real de existencia en GCS (async) y conteo de chunks en Zilliz Cloud (usando helpers **síncronos** de **Pymilvus** ejecutados en `run_in_executor`). Puede actualizar el estado en DB si detecta inconsistencias (con un pequeño periodo de gracia tras `processed`).
    *   `GET /api/v1/ingest/status`: Lista paginada de estados. Realiza verificaciones en GCS/Zilliz en paralelo para cada documento listado y actualiza la DB con el estado real encontrado antes de devolver la respuesta.
8.  **Reintento de Ingesta:** Permite reintentar la ingesta de un documento en estado `error` (`POST /api/v1/ingest/retry/{document_id}`). Actualiza el estado a `processing` y limpia `error_message` (async), y encola de nuevo la tarea `process_document_standalone`.
9.  **Eliminación Completa:** El endpoint `DELETE /api/v1/ingest/{document_id}` elimina los chunks asociados en **Zilliz Cloud** (usando helpers síncronos de Pymilvus en executor), el archivo original en **GCS** (async) y el registro en **Cloud SQL** (async).

## 2. Arquitectura General del Proyecto (Refactorizado GCP/Zilliz)

```mermaid
%%{init: {'theme': 'base', 'themeVariables': { 'primaryColor': '#E0F2F7', 'edgeLabelBackground':'#fff', 'tertiaryColor': '#FFFACD', 'lineColor': '#666', 'nodeBorder': '#333'}}}%%
graph TD
    A[Usuario/Cliente Externo] -->|HTTPS / REST API<br/>(via API Gateway)| I["<strong>Atenex Ingest Service API</strong><br/>(FastAPI - Async)"]

    subgraph KubernetesCluster ["Kubernetes Cluster (GKE)"]

        subgraph Namespace_nyro_develop ["Namespace: nyro-develop"]
            direction TB

            %% API Interactions (Async) %%
            I -- GET /status, DELETE /{id} -->|Cloud SQL Connector| SQL[("Cloud SQL (PostgreSQL)<br/>'atenex' DB")]
            I -- POST /upload, RETRY -->|Cloud SQL Connector| SQL
            I -- GET /status, POST /upload, DELETE /{id} -->|GCS Client (Async)| GCS[("Google Cloud Storage<br/>Bucket 'atenex'")]
            I -- POST /upload, RETRY --> Q([Redis<br/>Celery Broker<br/>(Interno o Memorystore)])
            I -- GET /status, DELETE /{id} -->|Pymilvus Sync Helper<br/>(via Executor)| ZLZ[("Zilliz Cloud<br/>Collection 'atenex-vector-db'")]

            %% Worker Interactions (Sync) %%
            W(Celery Worker<br/><b>Prefork - Sync Ops</b><br/><i>process_document_standalone</i>) -- Picks Task --> Q
            W -- Initialize Process (@worker_process_init) --> ST((SentenceTransformer<br/>Model from Config<br/><b>Local CPU</b>))
            W -- Update Status -->|Cloud SQL Connector<br/>(SQLAlchemy)| SQL
            W -- Read Bytes -->|GCS Client (Sync)| GCS
            W -- Execute Pipeline --> Pipe["<strong>Custom Pipeline</strong><br/>(Extract, Chunk, Embed, Index)"]
            Pipe -- Convert --> Libs[("Standalone Extractors<br/>(PyMuPDF, python-docx...)")]
            Pipe -- Embed --> ST
            Pipe -- Index/Delete Existing --> ZLZ

        end

    end

    %% Estilo %%
    style I fill:#C8E6C9,stroke:#333,stroke-width:2px
    style W fill:#BBDEFB,stroke:#333,stroke-width:2px
    style Pipe fill:#FFECB3,stroke:#666,stroke-width:1px
    style SQL fill:#F8BBD0,stroke:#333,stroke-width:1px
    style GCS fill:#FFF9C4,stroke:#333,stroke-width:1px
    style Q fill:#FFCDD2,stroke:#333,stroke-width:1px
    style ZLZ fill:#B2EBF2,stroke:#333,stroke-width:1px
    style ST fill:#D1C4E9,stroke:#333,stroke-width:1px
    style Libs fill:#CFD8DC,stroke:#333,stroke-width:1px
```
*Diagrama actualizado reflejando el uso de Cloud SQL, GCS, Zilliz Cloud, y la interacción vía conectores/clientes específicos.*

## 3. Características Clave (Refactorizado GCP/Zilliz)

*   **API RESTful:** Endpoints para ingesta (`/upload`), consulta de estado (`/status`, `/status/{id}`), reintento (`/retry/{document_id}`) y eliminación completa (`/{document_id}`). Normaliza nombres de archivo.
*   **Procesamiento Asíncrono:** Celery y Redis para orquestación de tareas.
*   **Worker Síncrono con Inicialización Eficiente:** El worker Celery (`prefork`) realiza operaciones de I/O (DB, GCS) y procesamiento de forma síncrona. El modelo de embedding (`SentenceTransformer`) se carga **una vez por proceso worker** al inicio.
*   **Almacenamiento Gestionado:** **GCS** para archivos, **Cloud SQL (PostgreSQL)** para metadatos/estado (incluyendo `error_message`), **Zilliz Cloud** para vectores.
*   **Pipeline Personalizado:**
    *   Conversión de documentos usando librerías standalone (`PyMuPDF`, `python-docx`, `BeautifulSoup`, etc.).
    *   Chunking de texto mediante función personalizada (`text_splitter.py`).
    *   Embedding usando **SentenceTransformers** (ejecución local en CPU, modelo configurable vía `EMBEDDING_MODEL_ID`).
    *   Truncado de texto de chunks para ajustarse al límite de Zilliz Cloud (`MILVUS_CONTENT_FIELD_MAX_LENGTH`).
    *   Indexación y borrado en Zilliz Cloud usando **Pymilvus**.
*   **Multi-tenancy:** Aislamiento por `company_id` (en DB, GCS path, y metadatos Zilliz).
*   **Estado Actualizado:** `GET /status` y `GET /status/{id}` verifican GCS/Zilliz en tiempo real y actualizan la DB si es necesario (con periodo de gracia).
*   **Eliminación Completa:** `DELETE /{id}` elimina datos de Cloud SQL, GCS y Zilliz Cloud.
*   **Autenticación GCP:** Utiliza **Workload Identity** para conectar a Cloud SQL y GCS de forma segura.
*   **Configuración Centralizada y Logging Estructurado (JSON).**
*   **Manejo de Errores:** Tareas Celery con reintentos para errores transitorios y registro de errores persistentes en la DB. Exclusión de reintentos para errores de extracción no recuperables.

## 4. Requisitos de la base de datos (IMPORTANTE)

> **¡IMPORTANTE!**
> La tabla `documents` en tu instancia de **Cloud SQL (PostgreSQL)** **debe tener la columna** `error_message TEXT` para que el servicio funcione correctamente.
> Si ves errores como `column "error_message" of relation "documents" does not exist`, ejecuta la siguiente migración SQL en tu instancia de Cloud SQL:

```sql
-- Asegúrate de que la columna exista
ALTER TABLE documents ADD COLUMN IF NOT EXISTS error_message TEXT;

-- Asegúrate que la longitud de file_name y file_path sea suficiente
ALTER TABLE documents ALTER COLUMN file_name TYPE VARCHAR(1024); -- Aumentado por si acaso
ALTER TABLE documents ALTER COLUMN file_path TYPE VARCHAR(2048); -- Aumentado por si acaso

-- Otras columnas esperadas (verifica tu esquema actual):
-- id UUID PRIMARY KEY
-- company_id UUID NOT NULL
-- file_name VARCHAR(1024) NOT NULL -- Ver longitud
-- file_type VARCHAR(100) NOT NULL
-- file_path VARCHAR(2048) -- Almacena la ruta en GCS, ver longitud
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

## 5. Pila Tecnológica Principal (Refactorizado GCP/Zilliz)

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Procesamiento Asíncrono (Orquestación):** Celery, Redis (interno K8s o Memorystore)
*   **Base de Datos Relacional:** **Cloud SQL para PostgreSQL** (via `cloud-sql-python-connector` con `asyncpg` y `psycopg2`)
*   **Base de Datos Vectorial:** **Zilliz Cloud** (via **Pymilvus**)
*   **Almacenamiento de Objetos:** **Google Cloud Storage (GCS)** (via `google-cloud-storage`)
*   **Modelo de Embeddings (Ingesta):** **SentenceTransformers** (ej. `sentence-transformers/all-MiniLM-L6-v2`, ejecución local en CPU por defecto, modelo configurable). ONNX Runtime es una dependencia transitiva.
*   **Convertidores de Documentos:** **PyMuPDF (fitz)**, python-docx, BeautifulSoup4, Markdown, html2text
*   **Despliegue:** Docker, Kubernetes (GKE)

## 6. Estructura de la Codebase (Refactorizado GCP/Zilliz)

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
│   │       │   └── ingest.py       # Endpoints API (usa GCSClient, Cloud SQL pool)
│   │       └── schemas.py
│   ├── core/                     # Configuración, Logging (actualizado)
│   │   ├── __init__.py
│   │   ├── config.py             # Contiene GCS_BUCKET_NAME, POSTGRES_INSTANCE_CONNECTION_NAME, ZILLIZ_*, etc.
│   │   └── logging_config.py
│   ├── db/
│   │   ├── __init__.py
│   │   └── postgres_client.py    # Cliente DB (usa Cloud SQL Connector)
│   ├── main.py
│   ├── models/
│   │   ├── __init__.py
│   │   └── domain.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── base_client.py
│   │   ├── embedder.py           # Sin cambios
│   │   ├── extractors/           # Sin cambios
│   │   │   ├── ...
│   │   ├── gcs_client.py         # NUEVO: Cliente GCS
│   │   ├── ingest_pipeline.py    # Actualizado para Zilliz
│   │   └── text_splitter.py      # Sin cambios
│   └── tasks/
│       ├── __init__.py
│       ├── celery_app.py         # Sin cambios (si Redis es interno)
│       └── process_document.py   # Actualizado para usar GCSClient, Cloud SQL Engine, Zilliz, pasar bytes
├── k8s/                          # Configuración Kubernetes
│   ├── configmap.yaml            # ACTUALIZADO con nuevas variables
│   ├── deployment-api.yaml       # ACTUALIZADO (imagen, resources, serviceAccountName)
│   ├── deployment-worker.yaml    # ACTUALIZADO (imagen, resources, concurrency, serviceAccountName)
│   ├── secret.example.yaml     # ESTRUCTURA ACTUALIZADA (quitar MinIO, añadir Zilliz)
│   └── service.yaml              # Sin cambios
├── Dockerfile                    # (Recomendado Multi-etapa)
├── pyproject.toml                # ACTUALIZADO con nuevas dependencias
├── poetry.lock
└── README.md                     # Este archivo (actualizado)
```

## 7. Configuración (Kubernetes - Refactorizado)

Configuración mediante `ingest-service-config` (ConfigMap) y `ingest-service-secrets` (Secret) en namespace `nyro-develop`.

### ConfigMap (`ingest-service-config`) - Resumen Clave

*   `INGEST_POSTGRES_INSTANCE_CONNECTION_NAME`: "praxis-study-458413-f4:southamerica-west1:atenex-db"
*   `INGEST_POSTGRES_USER`: "postgres"
*   `INGEST_POSTGRES_DB`: "atenex"
*   `INGEST_GCS_BUCKET_NAME`: "atenex"
*   `INGEST_ZILLIZ_URI`: "https://in03-0afab716eb46d7f.serverless.gcp-us-west1.cloud.zilliz.com"
*   `INGEST_MILVUS_COLLECTION_NAME`: "atenex-vector-db"
*   `INGEST_EMBEDDING_MODEL_ID`: "sentence-transformers/all-MiniLM-L6-v2"
*   `INGEST_EMBEDDING_DIMENSION`: "384"
*   `INGEST_CELERY_BROKER_URL`, `INGEST_CELERY_RESULT_BACKEND`: (Apuntan a Redis interno)

### Secret (`ingest-service-secrets`) - Resumen Clave

*   `INGEST_POSTGRES_PASSWORD`: (Contraseña de tu Cloud SQL)
*   `INGEST_ZILLIZ_API_KEY`: (Tu clave API de Zilliz Cloud)

## 8. API Endpoints

*Sin cambios en la interfaz pública de los endpoints.*

## 9. Dependencias Externas Clave (Refactorizado)

*   **Cloud SQL para PostgreSQL:** Almacenamiento de metadatos y estado.
*   **Zilliz Cloud:** Base de datos vectorial.
*   **Google Cloud Storage (GCS):** Almacenamiento de objetos.
*   **Redis:** Broker y backend de resultados para Celery (interno K8s o Memorystore).
*   **(Implícito) Modelo SentenceTransformer:** Descargado/cacheado por el worker.

## 10. Pipeline de Ingesta Personalizado (`ingest_document_pipeline`)

El flujo interno ahora usa GCS y Zilliz Cloud:

1.  **Extracción de Texto:** Igual (PyMuPDF, etc.), procesa `bytes`.
2.  **Chunking:** Igual (`text_splitter.py`).
3.  **Embedding:** Igual (SentenceTransformer cargado en worker).
4.  **Truncado de Texto:** Igual.
5.  **Escritura en Zilliz Cloud:** Usa `Pymilvus` configurado con URI/Token de Zilliz.

## 11. TODO / Mejoras Futuras

*   **Migrar Redis a Memorystore:** Altamente recomendado para producción.
*   **Optimización `GET /status` (Lista).**
*   **Soporte GPU para SentenceTransformer.**
*   **Worker Asíncrono.**
*   **Tests Unitarios y de Integración.**
*   **Observabilidad.**
*   **Manejo Avanzado de Errores.**
*   **Gestión de Modelos.**

## 12. Licencia

(Especificar Licencia del Proyecto)