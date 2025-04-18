# Atenex Ingest Service (Microservicio de Ingesta)

## 1. Visión General

El **Ingest Service** es un microservicio clave dentro de la plataforma Atenex. Su responsabilidad principal es recibir documentos subidos por los usuarios (PDF, DOCX, TXT, HTML, MD), procesarlos de manera asíncrona utilizando un **pipeline integrado de [Haystack AI 2.x](https://haystack.deepset.ai/)**, almacenar los archivos originales en **MinIO** y finalmente indexar el contenido procesado en bases de datos (**PostgreSQL** para metadatos y **Milvus** para vectores) para su uso posterior en búsquedas semánticas y generación de respuestas por LLMs.

**Flujo principal:**

1.  **Recepción:** La API (`POST /api/v1/ingest/upload`) recibe el archivo (`file`) y metadatos opcionales (`metadata_json`). Requiere el header `X-Company-ID` inyectado por el API Gateway.
2.  **Validación:** Verifica el tipo de archivo (`Content-Type`) contra los tipos soportados y valida el formato JSON de los metadatos. Previene subida de duplicados (mismo nombre, misma compañía, estado no-error).
3.  **Persistencia Inicial:**
    *   Crea un registro inicial del documento en **PostgreSQL** (tabla `documents`) con estado `uploaded` y `chunk_count` 0.
    *   Guarda el archivo original en **MinIO** (bucket `atenex`) bajo la ruta `company_id/document_id/filename`.
    *   Actualiza el registro en PostgreSQL con la ruta del archivo en MinIO.
4.  **Encolado:** Dispara una tarea asíncrona usando **Celery** (con **Redis** como broker) para el procesamiento pesado (`process_document_haystack_task`).
5.  **Respuesta API:** La API responde inmediatamente `202 Accepted` con el `document_id`, `task_id` de Celery y el estado `uploaded`.
6.  **Procesamiento Asíncrono (Worker Celery + Haystack):**
    *   La tarea Celery (`process_document.py`) recoge el trabajo.
    *   Actualiza el estado en PostgreSQL a `processing` (limpiando `error_message`).
    *   Descarga el archivo de MinIO.
    *   **Ejecuta el Pipeline Haystack:** (Usando `run_in_executor` para operaciones bloqueantes)
        *   **Conversión:** Selecciona el conversor Haystack adecuado (`PyPDFToDocument`, `DOCXToDocument`, etc.) según el `Content-Type`. Falla si no es soportado.
        *   **Chunking:** Divide el texto extraído en fragmentos (chunks) usando `DocumentSplitter`.
        *   **Embedding:** Genera vectores de embedding para cada chunk usando **OpenAI** (`text-embedding-3-small` por defecto).
        *   **Indexación:** Escribe los chunks (contenido, vector y metadatos como `company_id`, `document_id`, `file_name`, `file_type`) en **Milvus** (colección `atenex_doc_chunks`) usando `MilvusDocumentStore`.
    *   **Actualización Final:** Actualiza el estado del documento en PostgreSQL a `processed` y registra el número real de chunks escritos (`chunk_count`). Si ocurre un error, actualiza a `error` y guarda un mensaje descriptivo en `error_message`.
7.  **Consulta de Estado:** La API expone endpoints para consultar el estado:
    *   `GET /api/v1/ingest/status/{document_id}`: Estado de un documento específico, incluyendo verificación en tiempo real de existencia en MinIO y conteo de chunks en Milvus. Actualiza el estado en DB si detecta inconsistencias (ej. chunks > 0 pero estado=uploaded).
    *   `GET /api/v1/ingest/status`: Lista paginada de estados. **Realiza verificaciones en MinIO/Milvus en paralelo para cada documento listado y actualiza la DB con el estado real encontrado.** Esto asegura que la lista refleje el estado más fiel posible, aunque puede tener un impacto en el rendimiento si hay muchos documentos o Milvus/MinIO responden lento.
8.  **Reintento de Ingesta:** Permite reintentar la ingesta de un documento en estado `error` (`POST /api/v1/ingest/retry/{document_id}`). Actualiza el estado a `processing`.
9.  **Eliminación Completa:** El endpoint `DELETE /api/v1/ingest/{document_id}` ahora elimina los chunks asociados en **Milvus**, el archivo original en **MinIO** y el registro en **PostgreSQL**.

Este enfoque busca equilibrar la respuesta rápida de la API con la consistencia de los datos mostrados al usuario, realizando verificaciones más costosas de forma explícita o en paralelo donde sea necesario.

## 2. Arquitectura General del Proyecto (Actualizado)

```mermaid
%%{init: {'theme': 'base', 'themeVariables': { 'primaryColor': '#ADD8E6', 'edgeLabelBackground':'#fff', 'tertiaryColor': '#FFFACD'}}}%%
graph TD
    A[Usuario/Cliente Externo] -->|HTTPS / REST API<br/>(via API Gateway)| I["<strong>Atenex Ingest Service API</strong><br/>(FastAPI)"]

    subgraph KubernetesCluster ["Kubernetes Cluster"]

        subgraph Namespace_nyro_develop ["Namespace: nyro-develop"]
            direction TB
            I -- GET /status --> DB[(PostgreSQL<br/>'atenex' DB)]
            I -- GET /status --> S3[(MinIO<br/>'atenex' Bucket)]
            I -- GET /status --> MDB[(Milvus<br/>'atenex_doc_chunks' Collection)]
            I -- POST /upload --> DB
            I -- POST /upload --> S3
            I -- POST /upload --> Q([Redis<br/>Celery Broker])
            I -- DELETE /{id} --> DB
            I -- DELETE /{id} --> S3
            I -- DELETE /{id} --> MDB

            W(Celery Worker<br/>Haystack Pipeline) -- Picks Task --> Q
            W -->|Update Status (processing)| DB
            W -->|Download File| S3
            W -->|Write Chunks + Embeddings| MDB
            W -->|Update Status (processed/error)<br/>+ Chunk Count + Error Msg| DB
            W -->|Generate Embeddings| OpenAI[("OpenAI API<br/>(External)")]
        end
        # Milvus lives in 'default' namespace based on previous info
        # subgraph Namespace_default ["Namespace: default"]
        #     MDB[(Milvus<br/>'atenex_doc_chunks' Collection)]
        # end

    end

    %% Estilo
    style I fill:#f9f,stroke:#333,stroke-width:2px
    style W fill:#ccf,stroke:#333,stroke-width:2px
    style DB fill:#DBF1C2,stroke:#333,stroke-width:1px
    style S3 fill:#FFD700,stroke:#333,stroke-width:1px
    style Q fill:#FFB6C1,stroke:#333,stroke-width:1px
    style MDB fill:#B0E0E6,stroke:#333,stroke-width:1px
```
*Diagrama actualizado reflejando las interacciones de la API con MinIO/Milvus para status y delete, y la persistencia de `error_message`.*

## 3. Características Clave (Actualizado)

*   **API RESTful:** Endpoints para ingesta (`/upload`), consulta de estado (`/status`, `/status/{id}`), reintento (`/retry/{document_id}`) y **eliminación completa** (`/{document_id}`).
*   **Procesamiento Asíncrono:** Celery y Redis.
*   **Almacenamiento:** MinIO para archivos, PostgreSQL para metadatos/estado (incluyendo `error_message`), Milvus para vectores.
*   **Pipeline Haystack:** Conversión, chunking, embedding (**OpenAI** para ingesta), indexación en Milvus.
*   **Multi-tenancy:** Aislamiento por `company_id`.
*   **Estado Actualizado:** El endpoint `GET /status` ahora verifica MinIO/Milvus en paralelo y actualiza la DB para reflejar el estado real.
*   **Eliminación Completa:** `DELETE /{id}` elimina datos de PostgreSQL, MinIO y Milvus.
*   **Configuración Centralizada y Logging Estructurado.**
*   **Manejo de Errores:** Tareas Celery distinguen errores reintentables/no reintentables y almacenan mensajes de error útiles en la DB.

## 3. Requisitos de la base de datos (IMPORTANTE)

> **¡IMPORTANTE!**
> La tabla `documents` en PostgreSQL **debe tener la columna** `error_message TEXT` para que el servicio funcione correctamente.
> Si ves errores como `column "error_message" of relation "documents" does not exist`, ejecuta la siguiente migración SQL:

```sql
ALTER TABLE documents ADD COLUMN error_message TEXT;
```

Esto es necesario para que los endpoints de estado y manejo de errores funcionen correctamente.

## 4. Pila Tecnológica Principal (Sin cambios respecto a versión anterior)

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Procesamiento/Orquestación NLP:** Haystack AI 2.x
*   **Procesamiento Asíncrono:** Celery, Redis
*   **Base de Datos Relacional (Cliente):** PostgreSQL (via asyncpg)
*   **Base de Datos Vectorial (Cliente):** Milvus (via milvus-haystack, pymilvus)
*   **Almacenamiento de Objetos (Cliente):** MinIO (via minio-py)
*   **Modelo de Embeddings (Ingesta):** OpenAI (`text-embedding-3-small` por defecto)
*   **Despliegue:** Docker, Kubernetes (GKE)

## 5. Estructura de la Codebase (Actualizado)

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
│   │       │   └── ingest.py # Endpoints: /upload, /status, /status/{id}, /retry/{id}, /{id} (DELETE)
│   │       └── schemas.py    # Schemas: IngestResponse, StatusResponse
│   ├── core/                 # Configuración, Logging
│   │   ├── __init__.py
│   │   ├── config.py
│   │   └── logging_config.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   └── postgres_client.py # Funciones CRUD para tabla 'documents' (update con error_message)
│   ├── main.py               # Entrypoint FastAPI, lifespan, middleware
│   ├── models/
│   │   ├── __init__.py
│   │   └── domain.py         # Enum DocumentStatus
│   ├── services/
│   │   ├── __init__.py
│   │   ├── base_client.py
│   │   └── minio_client.py   # Cliente MinIO (con método delete_file añadido)
│   ├── tasks/
│   │   ├── __init__.py
│   │   ├── celery_app.py
│   │   └── process_document.py # Tarea Celery (actualiza error_message)
│   └── utils/
│       ├── __init__.py
│       └── helpers.py
├── k8s/
│   ├── ingest-configmap.yaml
│   ├── ingest-deployment.yaml
│   ├── ingest-secret.example.yaml
│   └── ingest-service.yaml
├── Dockerfile
├── pyproject.toml
├── poetry.lock
└── README.md                 # Este archivo
```

## 6. Configuración (Kubernetes - Sin cambios respecto a versión anterior)

Ver sección 6 del README anterior. Las variables de entorno relevantes siguen siendo las mismas (OpenAI Key para ingesta, etc.).

## 7. API Endpoints (Actualizado)

Prefijo base: `/api/v1/ingest`

---

### Health Check

*   **Endpoint:** `GET /` (Raíz del servicio)
*   **Descripción:** Verifica disponibilidad del servicio (DB).
*   **Respuesta OK (`200 OK`):** `OK` (Texto plano)
*   **Respuesta Error (`503 Service Unavailable`):** Si falla conexión DB.

---

### Ingestar Documento

*   **Endpoint:** `POST /upload`
*   **Descripción:** Inicia la ingesta asíncrona. Previene duplicados (mismo nombre, misma Cia, estado no-error).
*   **Headers Requeridos:** `X-Company-ID`
*   **Request Body:** `multipart/form-data` (`file`, `metadata_json` opcional)
*   **Respuesta (`202 Accepted`):** `schemas.IngestResponse`

---

### Consultar Estado de Ingesta (Documento Individual)

*   **Endpoint:** `GET /status/{document_id}`
*   **Descripción:** Obtiene estado detallado, **verificando en tiempo real MinIO/Milvus**. Puede actualizar el estado en DB si detecta inconsistencias.
*   **Headers Requeridos:** `X-Company-ID`
*   **Path Parameters:** `document_id` (UUID)
*   **Respuesta (`200 OK`):** `schemas.StatusResponse` (incluye `minio_exists`, `milvus_chunk_count`, `message`, y `error_message` si aplica).

---

### Listar Estados de Ingesta (Paginado)

*   **Endpoint:** `GET /status`
*   **Descripción:** Obtiene lista paginada. **Realiza verificaciones en MinIO/Milvus en paralelo para cada documento y actualiza el estado/chunks en la DB.** Devuelve los datos actualizados.
*   **Headers Requeridos:** `X-Company-ID`
*   **Query Parameters:** `limit`, `offset`
*   **Respuesta (`200 OK`):** `List[schemas.StatusResponse]` (incluye `minio_exists`, `milvus_chunk_count`, `message`, y `error_message`).

---

### Reintentar Ingesta de Documento con Error

*   **Endpoint:** `POST /retry/{document_id}`
*   **Descripción:** Reintenta la ingesta si el estado es 'error'. Actualiza estado a 'processing' y limpia `error_message`.
*   **Headers Requeridos:** `X-Company-ID`, `X-User-ID`
*   **Path Parameters:** `document_id` (UUID)
*   **Respuesta (`202 Accepted`):** `schemas.IngestResponse`

---

### Eliminar Documento

*   **Endpoint:** `DELETE /{document_id}` (**NOTA:** Ruta base `/` no `/status/`)
*   **Descripción:** Elimina completamente el documento: registro en PostgreSQL, archivo en MinIO y chunks en Milvus. Verifica propiedad.
*   **Headers Requeridos:** `X-Company-ID`
*   **Path Parameters:** `document_id` (UUID)
*   **Respuesta Exitosa (`204 No Content`):** Éxito.
*   **Respuestas Error:** `404 Not Found`, `500 Internal Server Error`.

---

## 8. Dependencias Externas Clave (Sin cambios)

*   PostgreSQL, Milvus, MinIO, Redis, OpenAI API.

## 9. Pipeline Haystack Interno (`process_document_haystack_task` - Sin cambios)

1.  Converter (PDF, DOCX, etc.)
2.  DocumentSplitter
3.  **OpenAIDocumentEmbedder**
4.  DocumentWriter (MilvusDocumentStore)

## 10. TODO / Mejoras Futuras

*   **Embeddings Locales/OpenSource:** Migrar la ingesta a usar FastEmbed u otro modelo si se quiere eliminar la dependencia de OpenAI por completo (requiere re-ingesta).
*   **Optimización Status List:** Si las verificaciones paralelas en `GET /status` resultan demasiado lentas, considerar un endpoint de refresco asíncrono o un job de reconciliación.
*   Implementar OCR, Tests, Observabilidad, etc. (Ver TODOs anteriores).

## 11. Licencia

(Especificar)