# Ingest Service (Microservicio de Ingesta)

## 1. Visión General

El **Ingest Service** es un microservicio clave dentro de la plataforma SaaS B2B. Su responsabilidad principal es recibir documentos subidos por los usuarios (PDF, DOCX, TXT, etc.), procesarlos de manera asíncrona utilizando un **pipeline integrado de [Haystack](https://haystack.deepset.ai/)**, almacenar los archivos originales y finalmente indexar el contenido procesado en bases de datos para su uso posterior en búsquedas semánticas y generación de respuestas por LLMs.

**Flujo principal:**

1.  **Recepción:** La API recibe el archivo y sus metadatos (`POST /api/v1/ingest`). Requiere `X-Company-ID`.
2.  **Validación:** Verifica el tipo de archivo y los metadatos.
3.  **Persistencia Inicial:** Guarda el archivo original en **MinIO** (almacenamiento de objetos S3 compatible) y crea un registro inicial del documento en **Supabase (PostgreSQL)** marcándolo como `uploaded`.
4.  **Encolado:** Dispara una tarea asíncrona usando **Celery** (con **Redis** como broker) para el procesamiento pesado. La API responde `202 Accepted` con el `document_id` y `task_id`.
5.  **Procesamiento Asíncrono (Worker Celery + Haystack):**
    *   La tarea Celery recoge el trabajo.
    *   Descarga el archivo de MinIO.
    *   Actualiza el estado en Supabase a `processing`.
    *   **Ejecuta el Pipeline Haystack:**
        *   **Conversión:** Convierte el formato original (PDF, DOCX, etc.) a texto plano usando componentes Haystack.
        *   **Chunking:** Divide el texto extraído en fragmentos (chunks) usando `DocumentSplitter`.
        *   **Embedding:** Genera vectores de embedding para cada chunk usando un modelo de **OpenAI** (`OpenAIDocumentEmbedder`).
        *   **Indexación:** Escribe los chunks (contenido, vector y metadatos) en **Milvus** usando `MilvusDocumentStore`.
    *   **Actualización Final:** Actualiza el estado del documento en Supabase a `processed` (o `error`) y registra el número de chunks.
6.  **Consulta de Estado:** La API expone endpoints para consultar el estado de un documento individual (`GET /api/v1/ingest/status/{document_id}`) o listar los estados de todos los documentos de la compañía (`GET /api/v1/ingest/status`).

Este enfoque asíncrono desacopla la carga y el procesamiento intensivo, mejorando la experiencia del usuario y la escalabilidad.

## 2. Arquitectura General del Proyecto

*(El diagrama Mermaid existente es correcto y no necesita cambios)*

```mermaid
%%{
  init: {
    'theme': 'base',
    'themeVariables': {
      'primaryColor': '#ffdfd3',
      'edgeLabelBackground':'#eee',
      'tertiaryColor': '#fff0ea',
      'primaryTextColor': '#333',
      'secondaryColor': '#f9bfa8',
      'clusterBkg': '#fff7f5'
    }
  }
}%%
flowchart TD
    A[Usuario] -->|Upload Document + JWT| B(Frontend App)
    B -->|HTTPS / REST API| C{API Gateway}

    subgraph external [Servicios Externos]
        style external fill:#e6f7ff,stroke:#b3e0ff
        D2[(Supabase Auth)]
        F3[(PostgreSQL - Supabase Pooler)] # Referenciar Pooler
        I[OpenAI API]
    end

    subgraph GKECluster [Cluster Kubernetes]
        style GKECluster fill:#fff7f5,stroke:#ffb399
        C -->|Validación JWT| D1(Auth Service)
        D1 -->|Token Validation| D2

        subgraph IngestService [Ingest Service]
            style IngestService fill:#fff0ea,stroke:#ffb399
            E1_API["<strong>Ingest Service API Pod</strong><br/>(FastAPI)"]
            E2_Worker["<strong>Ingest Service Worker Pod</strong><br/>(Celery + Haystack Pipeline)"]
        end

        C -->|POST /ingest + File + Meta + X-Company-ID| E1_API
        C -->|GET /ingest/status + X-Company-ID| E1_API
        C -->|GET /ingest/status/{id} + X-Company-ID| E1_API

        subgraph InternalDeps [Dependencias Internas K8s]
          style InternalDeps fill:#e0f2f7,stroke:#a0d4e2
            F1["(MinIO<br/>Object Storage)"]
            F2[(Milvus<br/>Vector DB)]
            F4[(Redis<br/>Celery Broker)]
        end

        subgraph Monitoring [Monitoring]
          style Monitoring fill:#f0f0f0,stroke:#cccccc
            H1[Monitoring Service]
            H2[(Prometheus)]
            H3[(Grafana)]
        end

        %% API Pod Flow - Ingest %%
        E1_API -->|1. Upload File| F1
        E1_API -->|2. Create Doc Record<br/>Status: uploaded| F3
        E1_API -->|3. Update Record with Path| F3
        E1_API -->|4. Enqueue Task| F4

        %% Worker Pod Flow - Processing %%
        F4 -->|5. Dequeue Task| E2_Worker
        E2_Worker -->|6. Download File| F1
        E2_Worker -->|7. Update Status: processing| F3

        subgraph HaystackPipeline [Worker Internals: Haystack Pipeline Execution]
           direction LR
           style HaystackPipeline fill:#fff9e6,stroke:#ffd699
           HP_In[Input: ByteStream + Meta] --> HP_Conv(Converter)
           HP_Conv --> HP_Split(Splitter)
           HP_Split -->|Chunks| HP_Embed(Embedder)
           HP_Embed -->|Call External API| I
           HP_Embed -->|Chunks + Embeddings| HP_Write(Writer)
           HP_Write -->|Write Chunks + Vectors| F2
        end

        E2_Worker -->|8. Execute Pipeline| HP_In
        HP_Write -->|9. Pipeline Result| E2_Worker
        E2_Worker -->|10. Update Status: processed/error<br/>+ chunk_count| F3

        %% API Pod Flow - Status Query %%
        E1_API -->|Query Status| F3

        %% Monitoring Connections %%
        E1_API -->|Logs/Metrics| H1
        E2_Worker -->|Logs/Metrics| H1
        H1 --> H2 & H3

    end

    %% Notas:
    %% - Supabase Pooler (F3) y OpenAI API (I) son externos al cluster.
    %% - MinIO (F1), Milvus (F2), Redis (F4) son internos al cluster.
    %% - El Pipeline Haystack se ejecuta DENTRO del Worker Pod (E2_Worker).
```

## 3. Características Clave

*   **API RESTful:** Endpoints para ingesta y consulta de estado (individual y lista).
*   **Procesamiento Asíncrono:** Desacoplamiento mediante Celery y Redis.
*   **Almacenamiento de Archivos:** Persistencia de originales en MinIO.
*   **Pipeline Haystack Integrado:** Orquesta conversión, chunking, embedding (OpenAI) y escritura en Milvus.
*   **Base de Datos Vectorial:** Indexación en **Milvus**.
*   **Base de Datos Relacional:** Persistencia de metadatos y estado en **Supabase (PostgreSQL)** vía **Session Pooler**.
*   **Multi-tenancy:** Aislamiento de datos por empresa (`X-Company-ID` header).
*   **Configuración Centralizada:** Uso de ConfigMaps y Secrets en Kubernetes.
*   **Logging Estructurado:** Logs en JSON con `structlog`.

## 4. Pila Tecnológica Principal

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Procesamiento/Orquestación NLP:** Haystack AI 2.x
*   **Procesamiento Asíncrono:** Celery, Redis
*   **Base de Datos Relacional:** Supabase (PostgreSQL + Session Pooler)
*   **Base de Datos Vectorial:** Milvus
*   **Almacenamiento de Objetos:** MinIO
*   **Modelo de Embeddings:** OpenAI (`text-embedding-3-small` por defecto)
*   **Despliegue:** Docker, Kubernetes (GKE)

## 5. Estructura de la Codebase

*(La estructura del código no ha cambiado)*

```
app/
├── __init__.py
├── api
│   ├── __init__.py
│   └── v1
│       ├── __init__.py
│       ├── endpoints
│       │   ├── __init__.py
│       │   └── ingest.py # Define los endpoints REST (/ingest, /ingest/status, /ingest/status/{id})
│       └── schemas.py    # Define los modelos Pydantic (IngestResponse, StatusResponse)
├── core
│   ├── __init__.py
│   ├── config.py         # Carga y valida la configuración (variables de entorno)
│   └── logging_config.py # Configura el logging estructurado (structlog)
├── db
│   ├── __init__.py
│   ├── base.py           # Vacío
│   └── postgres_client.py # Gestiona conexión (Pooler) y operaciones con Supabase (CRUD + List)
├── main.py               # Punto de entrada de la aplicación FastAPI (API)
├── models
│   ├── __init__.py
│   └── domain.py         # Define Enums (DocumentStatus)
├── services
│   ├── __init__.py
│   ├── base_client.py    # Cliente HTTP base (no usado actualmente)
│   └── minio_client.py   # Gestiona la interacción con MinIO (upload/download async wrappers)
├── tasks
│   ├── __init__.py
│   ├── celery_app.py     # Configura e instancia la aplicación Celery
│   └── process_document.py # Define la tarea Celery principal y el pipeline Haystack
└── utils
    ├── __init__.py
    └── helpers.py        # Vacío
```

## 6. Configuración (Kubernetes)

La configuración se gestiona a través del ConfigMap `ingest-service-config` y el Secret `ingest-service-secrets` en el namespace `nyro-develop`.

### ConfigMap (`ingest-service-config`)

Define conexiones a servicios internos y parámetros no sensibles. **Asegurar consistencia con otros servicios si usan los mismos recursos externos.**

| Clave                          | Descripción                                                      | Ejemplo (Valor Esperado Confirmado)             |
| :----------------------------- | :--------------------------------------------------------------- | :---------------------------------------------- |
| `INGEST_LOG_LEVEL`             | Nivel de logging.                                                | `INFO`                                          |
| `INGEST_CELERY_BROKER_URL`     | URL del servicio Redis master para Celery.                       | `redis://redis-service-master...:6379/0`        |
| `INGEST_CELERY_RESULT_BACKEND` | URL del servicio Redis master para resultados Celery.            | `redis://redis-service-master...:6379/1`        |
| `INGEST_POSTGRES_SERVER`       | Host del **Supabase Session Pooler**.                            | `aws-0-sa-east-1.pooler.supabase.com`           |
| **`INGEST_POSTGRES_PORT`**     | Puerto del **Supabase Session Pooler**.                          | **`6543`** (*Confirmado y usado por defecto*)   |
| `INGEST_POSTGRES_USER`         | Usuario del **Supabase Session Pooler** (`postgres.<project-ref>`). | `postgres.ymsilkrhstwxikjiqqog`                 |
| `INGEST_POSTGRES_DB`           | Base de datos en Supabase.                                       | `postgres`                                      |
| `INGEST_MILVUS_URI`            | URI del servicio Milvus dentro de K8s.                           | `http://milvus-service...:19530`                |
| `INGEST_MILVUS_COLLECTION_NAME`| Nombre de la colección Milvus.                                   | `document_chunks_haystack`                      |
| `INGEST_MINIO_ENDPOINT`        | Endpoint del servicio MinIO dentro de K8s.                       | `minio-service...:9000`                         |
| `INGEST_MINIO_BUCKET_NAME`     | Bucket MinIO para documentos originales.                         | `ingested-documents`                            |
| `INGEST_MINIO_USE_SECURE`      | Usar HTTPS para MinIO (interno K8s: false).                      | `false`                                         |
| `INGEST_OPENAI_EMBEDDING_MODEL`| Modelo de embedding OpenAI.                                      | `text-embedding-3-small`                        |
| `INGEST_SPLITTER_CHUNK_SIZE`   | Tamaño de chunk (Haystack).                                      | `500`                                           |
| `INGEST_SPLITTER_CHUNK_OVERLAP`| Superposición de chunks (Haystack).                              | `50`                                            |
| `INGEST_SPLITTER_SPLIT_BY`     | Unidad de división de chunks (Haystack).                         | `word`                                          |
| `INGEST_OCR_SERVICE_URL`       | (Opcional, No implementado) URL del servicio OCR.                | `http://ocr-service...`                         |

### Secret (`ingest-service-secrets`)

Contiene las credenciales sensibles.

| Clave del Secreto        | Variable de Entorno Correspondiente | Descripción                |
| :----------------------- | :---------------------------------- | :------------------------- |
| `postgres-password`      | `INGEST_POSTGRES_PASSWORD`          | Contraseña de Supabase Pooler. |
| `minio-access-key`       | `INGEST_MINIO_ACCESS_KEY`           | Access Key de MinIO.       |
| `minio-secret-key`       | `INGEST_MINIO_SECRET_KEY`           | Secret Key de MinIO.       |
| `openai-api-key`         | `INGEST_OPENAI_API_KEY`             | Clave API de OpenAI.       |

## 7. API Endpoints

Prefijo base: `/api/v1`

---

### Health Check

*   **Endpoint:** `GET /`
*   **Descripción:** Verifica disponibilidad del servicio (arranque y conexión DB). Usado por Kubernetes Probes.
*   **Respuesta Exitosa (`200 OK`):**
    ```json
    { "status": "ok", "service": "Ingest Service (...)", "ready": true }
    ```
*   **Respuesta No Listo (`503 Service Unavailable`):** Fallo en arranque o DB.
    ```json
    { "detail": "Service is not ready..." }
    ```

---

### Ingestar Documento

*   **Endpoint:** `POST /ingest`
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
*   **Tipos Soportados:** PDF (texto), DOCX, TXT, MD, HTML. (Imágenes/OCR no implementado). Ver `settings.SUPPORTED_CONTENT_TYPES`.

---

### Consultar Estado de Ingesta (Documento Individual)

*   **Endpoint:** `GET /ingest/status/{document_id}`
*   **Descripción:** Obtiene el estado actual de un documento específico.
*   **Headers Requeridos:**
    *   `X-Company-ID`: (String UUID) Identificador de la empresa propietaria.
*   **Path Parameters:**
    *   `document_id`: (String UUID) ID del documento.
*   **Respuesta (`200 OK`):** Detalles del estado.
    ```json
    {
      "document_id": "uuid-del-documento", // Mapeado desde 'id'
      "status": "processed", // "uploaded", "processing", "processed", "error"
      "file_name": "nombre_archivo.pdf",
      "file_type": "application/pdf",
      "chunk_count": 153,
      "error_message": null, // o mensaje si status="error"
      "last_updated": "2025-03-29T21:30:00.123Z", // Mapeado desde 'updated_at'
      "message": "Documento procesado exitosamente con 153 chunks indexados." // Mensaje generado
    }
    ```
*   **Respuestas Error:**
    *   `404 Not Found`: Si el `document_id` no existe.
    *   `403 Forbidden`: Si el `X-Company-ID` no coincide con el propietario del documento.
    *   `500 Internal Server Error`: Si hay problemas al consultar la BD.

---

### **NUEVO:** Listar Estados de Ingesta (Todos los Documentos de la Compañía)

*   **Endpoint:** `GET /ingest/status`
*   **Descripción:** Obtiene una lista con el estado de todos los documentos pertenecientes a la compañía especificada en el header `X-Company-ID`. Los resultados se ordenan por fecha de última actualización descendente.
*   **Headers Requeridos:**
    *   `X-Company-ID`: (String UUID) Identificador de la empresa.
*   **Respuesta (`200 OK`):** Una lista de objetos de estado.
    ```json
    [
      {
        "document_id": "uuid-doc-reciente",
        "status": "processed",
        "file_name": "informe-q1.docx",
        "file_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "chunk_count": 85,
        "error_message": null,
        "last_updated": "2025-04-01T10:00:00Z",
        "message": null // El campo message no se genera en la lista
      },
      {
        "document_id": "uuid-doc-error",
        "status": "error",
        "file_name": "presentacion.pdf",
        "file_type": "application/pdf",
        "chunk_count": null,
        "error_message": "Task Error (No Retry): ValueError: Unsupported content type for processing: application/zip",
        "last_updated": "2025-03-30T15:30:00Z",
        "message": null
      },
      {
        "document_id": "uuid-doc-procesando",
        "status": "processing",
        "file_name": "manual_usuario.txt",
        "file_type": "text/plain",
        "chunk_count": null,
        "error_message": null,
        "last_updated": "2025-03-29T11:05:00Z",
        "message": null
      }
      // ... más documentos
    ]
    ```
*   **Respuestas Error:**
    *   `401 Unauthorized`: Si falta `X-Company-ID`.
    *   `400 Bad Request`: Si `X-Company-ID` no es un UUID válido.
    *   `500 Internal Server Error`: Si hay problemas al consultar la BD.

---

## 8. Dependencias Externas Clave

*   **Supabase (PostgreSQL + Session Pooler):** Metadatos y estado. Conectado vía Pooler (puerto 6543).
*   **Milvus:** Vectores. Conectado vía URI del servicio K8s.
*   **MinIO:** Archivos originales. Conectado vía endpoint del servicio K8s.
*   **Redis:** Broker Celery. Conectado vía endpoint del servicio K8s.
*   **OpenAI API:** Embeddings. Acceso vía Internet.
*   **(Futuro) OCR Service:** No implementado.

## 9. Pipeline Haystack Interno (Tarea Celery `process_document_haystack_task`)

*(La descripción existente del pipeline es correcta y no necesita cambios)*

1.  **`Converter`:** (`PyPDFToDocument`, `DOCXToDocument`, etc.) Seleccionado según `content_type`.
2.  **`DocumentSplitter`:** Divide en chunks.
3.  **`OpenAIDocumentEmbedder`:** Genera y añade embeddings a los chunks.
4.  **`DocumentWriter` (con `MilvusDocumentStore`):** Escribe chunks+embeddings+metadatos en Milvus.

## 10. TODO / Mejoras Futuras

*   **Implementar OCR:** Integrar servicio o componente Haystack OCR.
*   **Seguridad:** Usuario no-root en contenedores.
*   **Validación:** Límites de tamaño de archivo, validación más estricta de `metadata_json`.
*   **Tests:** Unitarios y de integración.
*   **Manejo de Errores:** Refinar gestión de errores específicos (Haystack, Milvus, OpenAI).
*   **Optimización:** Ajustar parámetros Haystack/Milvus.
*   **Observabilidad:** Tracing distribuido.
*   **Escalabilidad:** HPA.
*   **Paginación:** Añadir paginación al endpoint `GET /ingest/status`.

## 11. Licencia

(Especificar Licencia aquí)
```