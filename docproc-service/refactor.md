# Plan de Refactorización: Creación de `docproc-service` y Actualización de `ingest-service`

## 1. Introducción

Este documento detalla el plan para refactorizar el backend de Atenex mediante la creación de un nuevo microservicio, `docproc-service`, y la adaptación del `ingest-service` existente. El objetivo principal es extraer la lógica de procesamiento de documentos (extracción de texto y chunking) del `ingest-service` a un servicio dedicado, mejorando la cohesión, reduciendo el tamaño de la imagen Docker del `ingest-service` y aislando dependencias.

## 2. Objetivos de la Refactorización

*   **Aislar Responsabilidades:** Separar la lógica de extracción de texto y división en fragmentos (chunks) en un microservicio dedicado (`docproc-service`).
*   **Optimizar `ingest-service`:** Hacer el worker de Celery del `ingest-service` más ligero y enfocado en la orquestación de la ingesta, la generación de embeddings (a través del `embedding-service`) y la indexación.
*   **Reducir Dependencias:** Eliminar librerías pesadas de conversión de documentos (PyMuPDF, python-docx, etc.) del `ingest-service`.
*   **Mejorar la Mantenibilidad:** Facilitar la actualización y el mantenimiento de la lógica de procesamiento de documentos de forma independiente.
*   **Mantener la Arquitectura Hexagonal:** Diseñar el `docproc-service` siguiendo principios de arquitectura hexagonal.

## 3. Microservicio: `docproc-service`

### 3.1. Descripción General

El `docproc-service` será responsable de recibir un archivo (como bytes), extraer su contenido textual y dividirlo en fragmentos cohesivos. Actuará como un servicio interno consumido principalmente por el `ingest-service`.

### 3.2. Responsabilidades Principales

1.  **Recepción de Archivos:** Aceptar archivos (enviados como `UploadFile` en una solicitud `multipart/form-data`) junto con su `content_type` y `original_filename`.
2.  **Extracción de Texto:** Utilizar las librerías apropiadas (PyMuPDF, python-docx, etc.) para extraer el texto crudo del archivo según su `content_type`.
3.  **División en Chunks (Chunking):** Aplicar la lógica de división de texto (actualmente en `ingest-service`) para fragmentar el texto extraído.
4.  **Devolución de Resultados:** Retornar una lista de chunks (cada uno con su texto y metadatos básicos como número de página, si aplica) y metadatos generales del documento procesado (ej. número total de páginas).

### 3.3. Pila Tecnológica

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Servidor ASGI/WSGI:** Uvicorn con Gunicorn
*   **Contenerización:** Docker
*   **Gestión de Dependencias:** Poetry
*   **Logging:** Structlog
*   **Librerías de Extracción:**
    *   `PyMuPDF` (para PDFs)
    *   `python-docx` (para DOCX)
    *   `BeautifulSoup4` y `html2text` (para HTML)
    *   `markdown` (para MD)
    *   Librería estándar de Python para TXT.
*   **Lógica de Chunking:** Lógica similar a la existente en `ingest-service/app/services/text_splitter.py`.

### 3.4. API Endpoints

#### `POST /api/v1/process`

*   **Descripción:** Procesa un archivo para extraer texto y dividirlo en chunks.
*   **Request:** `multipart/form-data`
    *   `file`: El archivo a procesar (tipo `UploadFile` de FastAPI).
    *   `original_filename`: El nombre original del archivo (tipo `str`, `Form(...)`).
    *   `content_type`: El tipo MIME del archivo (tipo `str`, `Form(...)`).
    *   `document_id` (opcional, para tracing): El ID del documento en `ingest-service` (tipo `str`, `Form(None)`).
    *   `company_id` (opcional, para tracing): El ID de la compañía (tipo `str`, `Form(None)`).
*   **Response Body (200 OK - `ProcessResponse`):**
    ```json
    {
      "document_metadata": {
        "original_filename": "nombre_archivo.pdf",
        "content_type": "application/pdf",
        "total_pages_extracted": 15, // Ejemplo para PDF
        "raw_text_length_chars": 25000,
        "processing_time_ms": 1234.56,
        "num_chunks_generated": 25
      },
      "chunks": [
        {
          "text": "Contenido del primer chunk...",
          "source_metadata": { // Metadatos originados en docproc-service
            "page_number": 1, // Si aplica
            // Otros metadatos del extractor/chunker, ej. tipo de contenido original del chunk
          }
        },
        {
          "text": "Contenido del segundo chunk...",
          "source_metadata": {
            "page_number": 1
          }
        }
        // ... más chunks
      ]
    }
    ```
*   **Errores Comunes:**
    *   `400 Bad Request`: Si faltan campos requeridos en el form-data.
    *   `415 Unsupported Media Type`: Si el `content_type` no es soportado.
    *   `422 Unprocessable Entity`: Si el archivo no puede ser procesado (corrupto, etc.).
    *   `500 Internal Server Error`: Errores inesperados durante el procesamiento.

#### `GET /health`

*   **Descripción:** Verifica la salud del servicio.
*   **Response Body (200 OK):**
    ```json
    {
      "status": "ok",
      "service": "Atenex Document Processing Service"
    }
    ```

### 3.5. Estructura de Directorios (Arquitectura Hexagonal)

```
docproc-service/
├── app/
│   ├── api/v1/
│   │   ├── endpoints/process_endpoint.py  # Router FastAPI para /process
│   │   └── schemas.py                     # Schemas Pydantic (Request/Response)
│   ├── application/
│   │   ├── ports/                         # Interfaces (Puertos)
│   │   │   ├── extraction_port.py
│   │   │   └── chunking_port.py
│   │   └── use_cases/                     # Lógica de orquestación
│   │       └── process_document_use_case.py
│   ├── core/                              # Configuración central, logging
│   │   ├── config.py
│   │   └── logging_config.py
│   ├── domain/                            # Modelos de dominio (opcional, podría estar en schemas)
│   │   └── models.py                      # Ej: Chunk, DocumentMetadata
│   ├── infrastructure/
│   │   ├── extractors/                    # Implementaciones concretas de ExtractionPort
│   │   │   ├── pdf_adapter.py             # Recicla de ingest-service
│   │   │   ├── docx_adapter.py            # Recicla de ingest-service
│   │   │   └── ...                        # etc.
│   │   └── chunkers/                      # Implementaciones concretas de ChunkingPort
│   │       └── default_chunker_adapter.py # Recicla lógica de text_splitter.py
│   ├── dependencies.py                    # Inyección de dependencias FastAPI
│   └── main.py                            # Entrypoint FastAPI, lifespan, health check
├── Dockerfile
├── pyproject.toml
├── poetry.lock
├── README.md
└── .env.example
```

### 3.6. Lógica de Extracción y Chunking

*   **Extractores (`infrastructure/extractors/`):**
    *   Se reutilizarán los extractores existentes en `ingest-service/app/services/extractors/`.
    *   Cada extractor (ej. `PdfAdapter`) implementará la interfaz `ExtractionPort`.
    *   El `ProcessDocumentUseCase` seleccionará el adaptador apropiado basado en el `content_type` (a través del `FlexibleExtractionPort`).
*   **Chunker (`infrastructure/chunkers/`):**
    *   Se reutilizará la lógica de `ingest-service/app/services/text_splitter.py`.
    *   El `DefaultChunkerAdapter` implementará la interfaz `ChunkingPort`.

### 3.7. Consideraciones

*   **Manejo de Archivos Grandes y Timeouts:** El endpoint `/process` podría tardar en responder para archivos grandes. Se deben configurar timeouts apropiados en el cliente (`ingest-service`) y en el servidor ASGI de `docproc-service`. Para archivos muy grandes, se podría considerar un patrón asíncrono (callback) en el futuro, pero para empezar, una llamada síncrona HTTP es aceptable para la comunicación interna service-to-service.
*   **Errores de Extracción:** El servicio debe manejar errores de archivos corruptos o formatos inesperados y devolver un código de error HTTP apropiado.
*   **Configuración:** Variables de entorno para configurar el tamaño de chunk, solapamiento, etc. (ej. `DOCPROC_CHUNK_SIZE`, `DOCPROC_CHUNK_OVERLAP`).

## 4. Modificaciones al `ingest-service`

### 4.1. Resumen de Cambios

El `ingest-service` delegará la extracción de texto y el chunking al nuevo `docproc-service`. Su worker Celery se modificará para llamar a este nuevo servicio y luego procederá con la llamada al `embedding-service` y la indexación.

### 4.2. Lógica y Dependencias a Eliminar

*   **Extractores de Texto:**
    *   Eliminar el directorio `ingest-service/app/services/extractors/`.
    *   Eliminar las dependencias `PyMuPDF`, `python-docx`, `markdown`, `beautifulsoup4`, `html2text` del `pyproject.toml` de `ingest-service`.
*   **Lógica de Chunking:**
    *   Eliminar el archivo `ingest-service/app/services/text_splitter.py`.
*   **Lógica en `ingest_pipeline.py`:**
    *   Remover las constantes `EXTRACTORS` y `EXTRACTION_ERRORS`.
    *   Remover la lógica de selección de extractor.
    *   Remover la llamada a `split_text`.
    *   La función `ingest_document_pipeline` ya no recibirá `file_bytes` ni `content_type` directamente para extracción, sino que recibirá los `chunks` pre-procesados (texto y metadatos básicos) del `docproc-service`.
*   **Configuración:**
    *   Eliminar variables de entorno `SPLITTER_CHUNK_SIZE` y `SPLITTER_CHUNK_OVERLAP` si estaban definidas específicamente para `ingest-service` (serán gestionadas por `docproc-service`).

### 4.3. Flujo del Worker Celery Modificado (`tasks/process_document.py`)

La tarea `process_document_standalone` se modificará de la siguiente manera:

1.  **(Sin Cambios) Recepción de Tarea y Validación:**
    *   Recibe `document_id`, `company_id`, `filename`, `content_type`.
    *   Valida argumentos y recursos del worker (DB engine, GCS client, `embedding_service_client`).
2.  **(Sin Cambios) Actualización de Estado Inicial:**
    *   Actualiza el estado del documento en PostgreSQL a `processing`.
3.  **(Sin Cambios) Descarga de Archivo:**
    *   Descarga el archivo original de GCS a un directorio temporal y obtiene `file_bytes`.
4.  **Paso NUEVO: Llamada al `docproc-service`:**
    *   Construir una solicitud `multipart/form-data` conteniendo:
        *   `file`: Los `file_bytes` descargados (como `UploadFile` o tupla `(filename, file_bytes, content_type)` para `httpx`).
        *   `original_filename`: El `filename` de la tarea.
        *   `content_type`: El `content_type` de la tarea.
        *   `document_id` (opcional, para tracing): `document_id_str`.
        *   `company_id` (opcional, para tracing): `company_id_str`.
    *   Realizar una llamada HTTP `POST` al endpoint `INGEST_DOCPROC_SERVICE_URL + /api/v1/process` (donde `INGEST_DOCPROC_SERVICE_URL` es una nueva variable de entorno).
        *   Se puede usar `httpx` directamente (envuelto en `run_async_from_sync` si se usa un cliente `httpx.AsyncClient`) o crear un `DocProcServiceClient` similar al `EmbeddingServiceClient`.
    *   **Manejo de Errores y Reintentos:**
        *   Si la llamada al `docproc-service` falla (error de red, status 5xx), la tarea Celery debería reintentar (usando la configuración `autoretry_for` de la tarea).
        *   Si `docproc-service` devuelve un error 4xx (ej. 415 Unsupported Media Type, 422 Unprocessable Entity), la tarea Celery debería marcar el documento como `error` en PostgreSQL y no reintentar (`Reject`).
    *   **Recepción de Respuesta:**
        *   Si la llamada es exitosa, parsear la respuesta JSON:
            ```json
            {
              "document_metadata": { /* ... */ },
              "chunks": [ { "text": "...", "source_metadata": { /* ... */ } }, ... ]
            }
            ```
        *   Si la lista `chunks` está vacía o no se devuelve, tratarlo como un caso de "0 chunks procesados".

5.  **Paso MODIFICADO: Preparación para Embedding y Metadatos Adicionales:**
    *   Si `docproc-service` devuelve chunks:
        *   Extraer la lista de textos de los chunks: `chunk_texts = [chunk['text'] for chunk in response_data['chunks']]`.
    *   Si no hay `chunk_texts` (o la llamada falló de forma no recuperable y se decidió proceder con 0 chunks):
        *   Actualizar el estado del documento en PostgreSQL a `processed` con `chunk_count = 0`.
        *   Finalizar la tarea.
6.  **Paso MODIFICADO: Llamada al `embedding-service` (si hay chunks):**
    *   Usar el `embedding_service_client_global` para enviar `chunk_texts` y obtener los `embeddings`.
    *   Manejar errores de este servicio como se hace actualmente (reintentos, etc.).
    *   Si la generación de embeddings falla críticamente:
        *   Actualizar el estado del documento a `error`.
        *   Considerar limpiar chunks en Milvus si algunos fueron insertados parcialmente en un intento anterior (poco probable si `docproc-service` es el primer paso nuevo).
        *   Finalizar la tarea con `Reject`.
7.  **Paso MODIFICADO: Indexación en Milvus (si hay embeddings):**
    *   La función `ingest_document_pipeline` (o su lógica refactorizada) se encargará de esto.
    *   Ahora tomará como entrada:
        *   Los `chunks` recibidos del `docproc-service` (que contienen `text` y `source_metadata`).
        *   Los `embeddings` recibidos del `embedding-service`.
        *   `filename`, `company_id`, `document_id`.
    *   Generará los `pk_id` para Milvus.
    *   Construirá los `data_to_insert` para Milvus combinando la información.
        *   `MILVUS_CONTENT_FIELD`: `chunk['text']`.
        *   `MILVUS_PAGE_FIELD`: `chunk['source_metadata'].get('page_number')`.
        *   Otros metadatos (`title`, `tokens`, `content_hash`) se generarán aquí en `ingest-service` a partir del `chunk['text']` y `filename`, como se hacía antes en `ingest_document_pipeline`.
    *   Realizar la inserción en Milvus.
    *   Obtener los `milvus_pks` y `inserted_milvus_count`.
8.  **Paso MODIFICADO: Indexación en PostgreSQL (tabla `document_chunks`):**
    *   Preparar `chunks_for_pg`:
        *   Para cada chunk original del `docproc-service` y su `embedding_id` correspondiente de Milvus:
            *   `document_id`, `company_id`.
            *   `chunk_index` (puede ser el índice de la lista de chunks del `docproc-service`).
            *   `content`: `chunk['text']`.
            *   `metadata`: combinar `chunk['source_metadata']` con los metadatos adicionales generados en `ingest-service` (ej. `tokens`, `content_hash`).
            *   `embedding_id`: el `pk` de Milvus.
            *   `vector_status`: `CREATED`.
    *   Realizar `bulk_insert_chunks_sync`.
9.  **(Sin Cambios) Actualización de Estado Final:**
    *   Actualizar el estado del documento en PostgreSQL a `processed` con el `chunk_count` final (de `inserted_pg_count`).
    *   Devolver el resultado de la tarea.

### 4.4. Variables de Configuración Nuevas para `ingest-service`

*   `INGEST_DOCPROC_SERVICE_URL`: URL base del `docproc-service` (ej. `http://docproc-service.nyro-develop.svc.cluster.local:PORT`).

### 4.5. Impacto en Dockerfile de `ingest-service`

*   Se eliminarán las capas `RUN` que instalaban dependencias de sistema para PyMuPDF (como `libmupdf-dev`, `swig`, etc.) si las hubiera.
*   La imagen Docker resultante será significativamente más pequeña.

## 5. Checklist de Tareas

### `docproc-service` (Nuevo Microservicio)

*   [X] **Infraestructura Básica:**
    *   [X] Crear repositorio o directorio para `docproc-service`.
    *   [X] Configurar `pyproject.toml` con dependencias (FastAPI, Uvicorn, Structlog, PyMuPDF, python-docx, etc.).
    *   [X] Crear `Dockerfile` básico.
    *   [X] Configurar estructura de directorios (hexagonal).
*   [X] **Core:**
    *   [X] Implementar `app/core/config.py` (`pydantic-settings`).
    *   [X] Implementar `app/core/logging_config.py` (Structlog).
*   [X] **API Layer:**
    *   [X] Definir Schemas Pydantic (`app/api/v1/schemas.py` y `app/domain/models.py`) para `ProcessResponse`.
    *   [X] Implementar endpoint `POST /api/v1/process` (`app/api/v1/endpoints/process_endpoint.py`).
    *   [X] Implementar endpoint `GET /health` en `app/main.py`.
*   [X] **Application Layer:**
    *   [X] Definir puertos (`ExtractionPort`, `ChunkingPort`) en `app/application/ports/`.
    *   [X] Implementar `ProcessDocumentUseCase` en `app/application/use_cases/`.
*   [X] **Infrastructure Layer:**
    *   [X] **Extractores:** Mover/Adaptar extractores de `ingest-service` a `app/infrastructure/extractors/` e implementar `ExtractionPort`.
    *   [X] **Chunker:** Mover/Adaptar lógica de `text_splitter.py` de `ingest-service` a `app/infrastructure/chunkers/` e implementar `ChunkingPort`.
*   [X] **Ensamblaje y Main:**
    *   [X] Configurar `app/main.py` (entrypoint FastAPI, inyección de dependencias para el caso de uso).
    *   [X] Implementar `app/dependencies.py` para la inyección.
*   [ ] **Despliegue:**
    *   [ ] Crear manifiestos de Kubernetes (Deployment, Service, ConfigMap).


### `ingest-service` (Modificaciones)

*   [ ] **Limpieza de Código y Dependencias:**
    *   [ ] Eliminar directorio `app/services/extractors/`.
    *   [ ] Eliminar archivo `app/services/text_splitter.py`.
    *   [ ] Actualizar `pyproject.toml` para eliminar dependencias de extracción.
    *   [ ] Actualizar `Dockerfile` para eliminar capas de dependencias de extracción.
*   [ ] **Configuración:**
    *   [ ] Añadir `INGEST_DOCPROC_SERVICE_URL` a `app/core/config.py`.
    *   [ ] Actualizar ConfigMap de Kubernetes.
*   [ ] **Lógica del Worker (`tasks/process_document.py`):**
    *   [ ] Implementar la llamada HTTP al `docproc-service` (usando `httpx` o un nuevo cliente ligero).
    *   [ ] Adaptar el manejo de la respuesta del `docproc-service`.
    *   [ ] Modificar la lógica de generación de metadatos de chunks para usar `source_metadata` del `docproc-service` y enriquecerla.
    *   [ ] Asegurar que los datos correctos (textos de chunks) se envíen al `embedding-service`.
    *   [ ] Adaptar la preparación de datos para Milvus y PostgreSQL.
    *   [ ] Revisar y ajustar el manejo de errores y reintentos de la tarea Celery.
*   [ ] **Refactorización de `services/ingest_pipeline.py`:**
    *   Eliminar la lógica de extracción y chunking.
    *   Adaptar la función `ingest_document_pipeline` para que acepte `chunks` (con texto y `source_metadata`) y `embeddings` como entrada principal para la indexación en Milvus.
*   [ ] **Testing:**
    *   [ ] Actualizar pruebas unitarias e de integración del `ingest-service` para reflejar los cambios.
    *   [ ] Realizar pruebas E2E del flujo de ingesta completo (API -> GCS -> Celery Worker -> `docproc-service` -> `embedding-service` -> Milvus/PostgreSQL).

### General

*   [X] **Documentación:** Actualizar READMEs de ambos servicios (README de `docproc-service` creado).
*   [ ] **Monitorización:** Asegurar que haya logs y métricas adecuadas para el nuevo servicio y las interacciones.