# Atenex Ingest Service (Microservicio de Ingesta)

## 1. Visión General

El **Ingest Service** es un microservicio clave dentro de la plataforma Atenex. Su responsabilidad principal es recibir documentos subidos por los usuarios (PDF, DOCX, TXT, HTML, MD), procesarlos de manera asíncrona utilizando un **pipeline integrado de [Haystack AI 2.x](https://haystack.deepset.ai/)**, almacenar los archivos originales en **MinIO** y finalmente indexar el contenido procesado en bases de datos (**PostgreSQL** para metadatos y **Milvus** para vectores) para su uso posterior en búsquedas semánticas y generación de respuestas por LLMs.

**Flujo principal:**

1.  **Recepción:** La API (`POST /api/v1/ingest/upload`) recibe el archivo (`file`) y metadatos opcionales (`metadata_json`). Requiere el header `X-Company-ID`.
2.  **Validación:** Verifica el tipo de archivo (`Content-Type`) contra los tipos soportados y valida el formato JSON de los metadatos.
3.  **Persistencia Inicial:**
    *   Crea un registro inicial del documento en **PostgreSQL** (tabla `documents`) con estado `uploaded`.
    *   Guarda el archivo original en **MinIO** (bucket `atenex`) bajo la ruta `company_id/document_id/filename`.
    *   Actualiza el registro en PostgreSQL con la ruta del archivo en MinIO.
4.  **Encolado:** Dispara una tarea asíncrona usando **Celery** (con **Redis** como broker) para el procesamiento pesado (`process_document_haystack_task`).
5.  **Respuesta API:** La API responde inmediatamente `202 Accepted` con el `document_id`, `task_id` de Celery y el estado `uploaded`.
6.  **Procesamiento Asíncrono (Worker Celery + Haystack):**
    *   La tarea Celery (`process_document.py`) recoge el trabajo.
    *   Actualiza el estado en PostgreSQL a `processing`.
    *   Descarga el archivo de MinIO.
    *   **Ejecuta el Pipeline Haystack:** (Usando `run_in_executor` para operaciones bloqueantes)
        *   **Conversión:** Selecciona el conversor Haystack adecuado (`PyPDFToDocument`, `DOCXToDocument`, etc.) según el `Content-Type`.
        *   **Chunking:** Divide el texto extraído en fragmentos (chunks) usando `DocumentSplitter`.
        *   **Embedding:** Genera vectores de embedding para cada chunk usando un modelo de **OpenAI** (`OpenAIDocumentEmbedder`).
        *   **Indexación:** Escribe los chunks (contenido, vector y metadatos filtrados) en **Milvus** (namespace `default`) usando `MilvusDocumentStore`.
    *   **Actualización Final:** Actualiza el estado del documento en PostgreSQL a `processed` (o `error` si falló) y registra el número de chunks (`chunk_count`) y/o el mensaje de error.
7.  **Consulta de Estado:** La API expone endpoints para consultar el estado:
    *   `GET /api/v1/ingest/status/{document_id}`: Estado de un documento específico (requiere `X-Company-ID` coincidente).
    *   `GET /api/v1/ingest/status`: Lista paginada de estados de todos los documentos de la compañía (requiere `X-Company-ID`).
8.  **Reintento de Ingesta:** Permite reintentar la ingesta de un documento en estado `error` mediante el endpoint `POST /api/v1/ingest/retry/{document_id}`.

Este enfoque asíncrono desacopla la carga y el procesamiento intensivo, mejorando la experiencia del usuario y la escalabilidad.

## 2. Arquitectura General del Proyecto

```mermaid
%%{init: {'theme': 'base', 'themeVariables': { 'primaryColor': '#ADD8E6', 'edgeLabelBackground':'#fff', 'tertiaryColor': '#FFFACD'}}}%%
graph TD
    A[Usuario/Cliente Externo] -->|HTTPS / REST API<br/>(via API Gateway)| I["<strong>Atenex Ingest Service API</strong><br/>(FastAPI)"]

    subgraph KubernetesCluster ["Kubernetes Cluster"]

        subgraph Namespace_nyro_develop ["Namespace: nyro-develop"]
            direction TB
            I -->|1. Create Record (uploaded)| DB[(PostgreSQL<br/>'atenex' DB)]
            I -->|2. Upload File| S3[(MinIO<br/>'atenex' Bucket)]
            I -->|3. Enqueue Task| Q([Redis<br/>Celery Broker])

            W(Celery Worker<br/>Haystack Pipeline) -- Picks Task --> Q
            W -->|4. Update Status (processing)| DB
            W -->|5. Download File| S3
            W -->|8. Update Status (processed/error)<br/>+ Chunk Count| DB

        end

        subgraph Namespace_default ["Namespace: default"]
            direction LR
            W -->|7. Write Chunks + Embeddings| MDB[(Milvus<br/>'atenex_doc_chunks' Collection)]
        end

        W -->|6. Generate Embeddings| OpenAI[("OpenAI API<br/>(External)")]

    end

    %% Estilo
    style I fill:#f9f,stroke:#333,stroke-width:2px
    style W fill:#ccf,stroke:#333,stroke-width:2px
    style DB fill:#DBF1C2,stroke:#333,stroke-width:1px
    style S3 fill:#FFD700,stroke:#333,stroke-width:1px
    style Q fill:#FFB6C1,stroke:#333,stroke-width:1px
    style MDB fill:#B0E0E6,stroke:#333,stroke-width:1px

```
*Diagrama actualizado reflejando PostgreSQL, MinIO y Redis en `nyro-develop`, Milvus en `default`, y el flujo de procesamiento.*

## 3. Características Clave

*   **API RESTful:** Endpoints para ingesta (`/upload`), consulta de estado (`/status`, `/status/{id}`), reintento (`/retry/{document_id}`) y eliminación (`/{document_id}`).
*   **Procesamiento Asíncrono:** Desacoplamiento mediante Celery y Redis (en `nyro-develop`).
*   **Almacenamiento de Archivos:** Persistencia de originales en MinIO (bucket `atenex` en `nyro-develop`).
*   **Pipeline Haystack Integrado:** Orquesta conversión, chunking, embedding (OpenAI) y escritura en Milvus.
*   **Base de Datos Vectorial:** Indexación en **Milvus** (namespace `default`).
*   **Base de Datos Relacional:** Persistencia de metadatos y estado en **PostgreSQL** (DB `atenex` en `nyro-develop`) usando `asyncpg`.
*   **Multi-tenancy:** Aislamiento de datos por empresa (requiere `X-Company-ID` header en API, usado en MinIO path y metadatos).
*   **Configuración Centralizada:** Uso de ConfigMaps y Secrets en Kubernetes (namespace `nyro-develop`).
*   **Logging Estructurado:** Logs en JSON con `structlog`.
*   **Manejo de Errores Robusto:** Distinción entre errores reintentables y no reintentables en tareas Celery.
*   **Reintento de Ingesta:** Endpoint para reencolar documentos fallidos sin necesidad de volver a subir el archivo.

## 4. Pila Tecnológica Principal

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Procesamiento/Orquestación NLP:** Haystack AI 2.x
*   **Procesamiento Asíncrono:** Celery, Redis
*   **Base de Datos Relacional (Cliente):** PostgreSQL (via asyncpg)
*   **Base de Datos Vectorial (Cliente):** Milvus (via milvus-haystack, pymilvus)
*   **Almacenamiento de Objetos (Cliente):** MinIO (via minio-py)
*   **Modelo de Embeddings:** OpenAI (`text-embedding-3-small` por defecto)
*   **Despliegue:** Docker, Kubernetes (GKE)

## 5. Estructura de la Codebase

```
ingest-service/
├── app/
│   ├── __init__.py
│   ├── api/                  # Definiciones API (endpoints, schemas)
│   │   ├── __init__.py
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── endpoints/
│   │       │   ├── __init__.py
│   │       │   └── ingest.py # Endpoints: /upload, /status, /status/{id}, /retry/{document_id}, /{document_id}
│   │       └── schemas.py    # Schemas Pydantic: IngestResponse, StatusResponse
│   ├── core/                 # Configuración, Logging
│   │   ├── __init__.py
│   │   ├── config.py
│   │   └── logging_config.py
│   ├── db/                   # Acceso a Base de Datos
│   │   ├── __init__.py
│   │   ├── base.py           # (Vacío o Base SQLAlchemy si se usara)
│   │   └── postgres_client.py # Funciones asyncpg para tabla 'documents'
│   ├── main.py               # Entrypoint FastAPI, lifespan, middleware
│   ├── models/               # Modelos de Dominio/Enums
│   │   ├── __init__.py
│   │   └── domain.py         # Enum DocumentStatus
│   ├── services/             # Clientes para servicios externos/internos
│   │   ├── __init__.py
│   │   ├── base_client.py    # (Cliente HTTP base genérico)
│   │   └── minio_client.py   # Cliente async para MinIO
│   ├── tasks/                # Tareas Asíncronas (Celery)
│   │   ├── __init__.py
│   │   ├── celery_app.py     # Configuración app Celery
│   │   └── process_document.py # Tarea principal y lógica Haystack
│   └── utils/                # Utilidades generales
│       ├── __init__.py
│       └── helpers.py        # (Vacío por ahora)
├── k8s/                      # Manifests de Kubernetes (Ejemplos)
│   ├── ingest-configmap.yaml
│   ├── ingest-deployment.yaml
│   ├── ingest-secret.example.yaml
│   └── ingest-service.yaml
├── Dockerfile                # Construcción de imagen Docker
├── pyproject.toml            # Dependencias y metadatos del proyecto (Poetry)
├── poetry.lock               # Lockfile de dependencias
└── README.md                 # Este archivo
```

## 6. Configuración (Kubernetes)

La configuración se gestiona a través del ConfigMap `ingest-service-config` y el Secret `ingest-service-secrets` en el namespace `nyro-develop`.

### ConfigMap (`ingest-service-config` en `nyro-develop`)

| Clave                          | Descripción                                      | Ejemplo (Valor Esperado en K8s)                       |
| :----------------------------- | :----------------------------------------------- | :---------------------------------------------------- |
| `INGEST_LOG_LEVEL`             | Nivel de logging (`DEBUG`, `INFO`, `WARNING`).    | `INFO`                                                |
| `INGEST_CELERY_BROKER_URL`     | URL Redis Broker (en `nyro-develop`).            | `redis://redis-service-master.nyro-develop...:6379/0` |
| `INGEST_CELERY_RESULT_BACKEND` | URL Redis Backend (en `nyro-develop`).           | `redis://redis-service-master.nyro-develop...:6379/1` |
| `INGEST_POSTGRES_SERVER`       | Host/Service PostgreSQL (en `nyro-develop`).     | `postgresql.nyro-develop.svc.cluster.local`         |
| `INGEST_POSTGRES_PORT`         | Puerto PostgreSQL.                               | `5432`                                                |
| `INGEST_POSTGRES_USER`         | Usuario PostgreSQL.                              | `postgres`                                            |
| `INGEST_POSTGRES_DB`           | Base de datos PostgreSQL (`atenex`).             | `atenex`                                              |
| `INGEST_MILVUS_URI`            | URI Milvus (en `default` namespace).             | `http://milvus-milvus.default.svc.cluster.local:19530`|
| `INGEST_MILVUS_COLLECTION_NAME`| Nombre colección Milvus (`atenex_doc_chunks`). | `atenex_doc_chunks`                                   |
| `INGEST_MILVUS_INDEX_PARAMS`   | Params índice Milvus (JSON string).              | `{"metric_type": "COSINE", ...}`                      |
| `INGEST_MILVUS_SEARCH_PARAMS`  | Params búsqueda Milvus (JSON string).            | `{"metric_type": "COSINE", ...}`                      |
| `INGEST_MINIO_ENDPOINT`        | Endpoint MinIO (en `nyro-develop`).              | `minio-service.nyro-develop...:9000`                |
| `INGEST_MINIO_BUCKET_NAME`     | Bucket MinIO (`atenex`).                         | `atenex`                                              |
| `INGEST_MINIO_USE_SECURE`      | Usar HTTPS para MinIO (interno: false).          | `false`                                               |
| `INGEST_OPENAI_EMBEDDING_MODEL`| Modelo embedding OpenAI.                         | `text-embedding-3-small`                              |
| `INGEST_SPLITTER_CHUNK_SIZE`   | Tamaño chunk Haystack.                           | `500`                                                 |
| `INGEST_SPLITTER_CHUNK_OVERLAP`| Solapamiento chunks Haystack.                    | `50`                                                  |
| `INGEST_SPLITTER_SPLIT_BY`     | Unidad división chunks Haystack.                 | `word`                                                |

### Secret (`ingest-service-secrets` en `nyro-develop`)

| Clave del Secreto      | Variable de Entorno Correspondiente | Descripción          |
| :--------------------- | :---------------------------------- | :------------------- |
| `POSTGRES_PASSWORD`    | `INGEST_POSTGRES_PASSWORD`          | Contraseña PostgreSQL. |
| `MINIO_ACCESS_KEY`     | `INGEST_MINIO_ACCESS_KEY`           | Access Key MinIO.    |
| `MINIO_SECRET_KEY`     | `INGEST_MINIO_SECRET_KEY`           | Secret Key MinIO.    |
| `OPENAI_API_KEY`       | `INGEST_OPENAI_API_KEY`             | Clave API OpenAI.    |

*(Nota: Las claves dentro del Secret deben coincidir con las usadas en el manifiesto del Deployment)*

## 7. API Endpoints

Prefijo base: `/api/v1/ingest` (definido en `main.py`)

---

### Health Check

*   **Endpoint:** `GET /` (Mapeado a la raíz del servicio, ej. `http://ingest-service.nyro-develop/`)
*   **Descripción:** Verifica disponibilidad del servicio (incluyendo conexión DB activa). Usado por Kubernetes Probes.
*   **Respuesta Exitosa (`200 OK`):** `OK` (Texto plano)
*   **Respuesta No Listo (`503 Service Unavailable`):** Si la conexión a la BD falla.

---

### Ingestar Documento

*   **Endpoint:** `POST /upload` (URL completa: `POST /api/v1/ingest/upload`)
*   **Descripción:** Inicia la ingesta asíncrona de un documento.
*   **Headers Requeridos:**
    *   `X-Company-ID`: (String UUID) Identificador de la empresa.
*   **Request Body:** `multipart/form-data`
    *   `metadata_json`: (String JSON, Opcional) Metadatos. *Ej: `'{"clave": "valor"}'`*
    *   `file`: (File Binario, **Requerido**) El documento.
*   **Respuesta (`202 Accepted`):** Confirmación de recepción y encolado.
    ```json
    {
      "document_id": "uuid-del-documento-creado",
      "task_id": "uuid-de-la-tarea-celery",
      "status": "uploaded",
      "message": "Document upload received and queued for processing."
    }
    ```
*   **Tipos Soportados:** PDF, DOCX, TXT, MD, HTML. Ver `settings.SUPPORTED_CONTENT_TYPES`.

---

### Consultar Estado de Ingesta (Documento Individual)

*   **Endpoint:** `GET /status/{document_id}` (URL completa: `GET /api/v1/ingest/status/{document_id}`)
*   **Descripción:** Obtiene el estado actual de un documento específico.
*   **Headers Requeridos:**
    *   `X-Company-ID`: (String UUID) Identificador de la empresa propietaria.
*   **Path Parameters:**
    *   `document_id`: (String UUID) ID del documento.
*   **Respuesta (`200 OK`):** Detalles del estado, incluyendo verificación en MinIO y conteo real en Milvus (útil para refresh).
    ```json
    {
      "document_id": "uuid-del-documento",
      "status": "processed",            // "uploaded", "processing", "processed", "error"
      "file_name": "nombre_archivo.pdf",
      "file_type": "application/pdf",
      "chunk_count": 153,                 // Contado desde PostgreSQL tras procesamiento
      "minio_exists": true,               // true si el objeto sigue en MinIO
      "milvus_chunk_count": 150,          // Chunks realmente indexados en Milvus
      "error_message": null,              // o mensaje si status="error"
      "last_updated": "2025-03-29T21:30:00.123Z", // Mapeado desde 'updated_at'
      "message": "Documento procesado exitosamente." // Mensaje generado
    }
    ```
    
    Nota: Al hacer refresh en el frontend, este endpoint proporciona el estado real en MinIO y Milvus mediante los campos `minio_exists` y `milvus_chunk_count`.

*   **Respuestas Error:**
    *   `404 Not Found`: Si el `document_id` no existe o pertenece a otra compañía.
    *   `500 Internal Server Error`: Si hay problemas al consultar la BD.

---

### Listar Estados de Ingesta (Paginado)

*   **Endpoint:** `GET /status` (URL completa: `GET /api/v1/ingest/status`)
*   **Descripción:** Obtiene una lista paginada del estado de los documentos de la compañía.
*   **Headers Requeridos:**
    *   `X-Company-ID`: (String UUID) Identificador de la empresa.
*   **Query Parameters (Opcionales):**
    *   `limit` (int, default=100): Número máximo de resultados.
    *   `offset` (int, default=0): Número de resultados a saltar.
*   **Respuesta (`200 OK`):** Una lista de objetos de estado (sin el campo `message`).
    ```json
    [
      {
        "document_id": "uuid-doc-reciente",
        "status": "processed",
        "file_name": "informe-q1.docx",
        "file_type": "...",
        "chunk_count": 85,
        "error_message": null,
        "last_updated": "2025-04-01T10:00:00Z"
      },
      // ... más documentos
    ]
    ```
*   **Respuestas Error:**
    *   `401 Unauthorized`: Si falta `X-Company-ID`.
    *   `400 Bad Request`: Si `X-Company-ID` no es un UUID válido.
    *   `500 Internal Server Error`: Si hay problemas al consultar la BD.

---

### Reintentar Ingesta de Documento con Error

*   **Endpoint:** `POST /retry/{document_id}` (URL completa: `POST /api/v1/ingest/retry/{document_id}`)
*   **Descripción:** Permite reintentar la ingesta de un documento que falló previamente (estado `error`). Solo disponible si el documento pertenece a la compañía y está en estado `error`.
*   **Headers Requeridos:**
    *   `X-Company-ID`: (String UUID) Identificador de la empresa.
    *   `X-User-ID`: (String UUID) Identificador del usuario que solicita el reintento.
*   **Path Parameters:**
    *   `document_id`: (String UUID) ID del documento a reintentar.
*   **Respuesta (`202 Accepted`):** Confirmación de reencolado.
    ```json
    {
      "document_id": "uuid-del-documento",
      "task_id": "uuid-de-la-tarea-celery",
      "status": "processing",
      "message": "Reintento de ingesta encolado correctamente."
    }
    ```
*   **Respuestas Error:**
    *   `404 Not Found`: Si el documento no existe o no pertenece a la compañía.
    *   `409 Conflict`: Si el documento no está en estado `error`.
    *   `500 Internal Server Error`: Si ocurre un error inesperado.

---

### Eliminar Documento

* **Endpoint:** `DELETE /{document_id}` (URL completa: `DELETE /api/v1/ingest/{document_id}`)
* **Descripción:** Elimina un documento junto con sus chunks de Milvus, el archivo en MinIO y su registro en PostgreSQL.
* **Headers Requeridos:**
  * `X-Company-ID`: (String UUID) Identificador de la empresa propietaria.
* **Path Parameters:**
  * `document_id`: (String UUID) ID del documento a eliminar.
* **Respuesta Exitosa (`204 No Content`):** Indica que la eliminación se completó correctamente.
* **Respuestas Error:**
  * `404 Not Found`: Si el documento no existe o no pertenece a la compañía.
  * `500 Internal Server Error`: Si ocurre un error inesperado durante la eliminación.

---

## 8. Dependencias Externas Clave

*   **PostgreSQL:** Almacén de metadatos y estado (en `nyro-develop`).
*   **Milvus:** Almacén de vectores (en `default`).
*   **MinIO:** Almacén de archivos originales (bucket `atenex` en `nyro-develop`).
*   **Redis:** Broker y Backend Celery (en `nyro-develop`).
*   **OpenAI API:** Generación de Embeddings (Acceso externo).

## 9. Pipeline Haystack Interno (`process_document_haystack_task`)

1.  **`Converter`:** (`PyPDFToDocument`, `DOCXToDocument`, etc.) Seleccionado según `content_type`.
2.  **`DocumentSplitter`:** Divide en chunks (`word`, `500` tokens, `50` overlap).
3.  **`OpenAIDocumentEmbedder`:** Genera embeddings (`text-embedding-3-small`).
4.  **`DocumentWriter` (con `MilvusDocumentStore`):** Escribe chunks+embeddings+metadatos en Milvus (`atenex_doc_chunks`).

## 10. TODO / Mejoras Futuras

*   **Implementar OCR:** Para procesar documentos basados en imágenes (requiere servicio/componente OCR).
*   **Seguridad:** Usuario no-root en contenedores, revisión de permisos.
*   **Validación:** Límites de tamaño de archivo, schema de `metadata_json`.
*   **Tests:** Unitarios (clientes DB/MinIO, helpers Haystack) y de integración (API -> Celery -> DB/Milvus).
*   **Manejo de Errores:** Mejorar granularidad de errores devueltos por la API y tareas.
*   **Optimización:** Tuning de parámetros Haystack (splitter), Milvus (index/search), Celery (workers, prefetch).
*   **Observabilidad:** Tracing distribuido (OpenTelemetry), métricas Prometheus (Celery, API).
*   **Escalabilidad:** Horizontal Pod Autoscaler (HPA) para API y Workers Celery.

## 11. Licencia

(Especificar Licencia aquí, ej. MIT, Apache 2.0)