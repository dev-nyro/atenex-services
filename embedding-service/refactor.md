# Plan de Refactorización: Creación de `embedding-service` y Adaptación de `ingest-service` y `query-service`

## 1. Introducción y Objetivos

Este plan detalla la refactorización de los microservicios `ingest-service` y `query-service` de Atenex mediante la extracción de la funcionalidad de generación de embeddings a un nuevo microservicio dedicado: `embedding-service`.

**Objetivos Principales:**

*   **Centralizar la Generación de Embeddings:** Crear un único servicio responsable de generar embeddings, utilizando `FastEmbed` con el modelo `sentence-transformers/all-MiniLM-L6-v2`.
*   **Reducir el Tamaño de las Imágenes Docker:** Aliviar a `ingest-service` y `query-service` de la carga de los modelos de embedding, disminuyendo significativamente el tamaño de sus imágenes.
*   **Mejorar la Cohesión:** `ingest-service` y `query-service` se enfocarán más en sus responsabilidades principales (orquestación de ingesta y procesamiento de consultas RAG, respectivamente).
*   **Consistencia de Embeddings:** Asegurar que todos los embeddings en el sistema se generan de la misma manera y con el mismo modelo.
*   **Escalabilidad Independiente:** Permitir que el `embedding-service` escale de forma independiente según la carga de solicitudes de embedding.

## 2. Creación del `embedding-service`

Este nuevo microservicio será el responsable exclusivo de generar embeddings.

### 2.1. Pila Tecnológica

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Modelo de Embedding:** `FastEmbed` con `sentence-transformers/all-MiniLM-L6-v2` (dimensión 384).
*   **Servidor:** Uvicorn + Gunicorn
*   **Contenerización:** Docker

### 2.2. Estructura del Servicio

```
embedding-service/
├── app/
│   ├── api/v1/
│   │   ├── endpoints/embedding_endpoint.py
│   │   └── schemas.py
│   ├── application/
│   │   ├── ports/embedding_model_port.py
│   │   └── use_cases/embed_texts_use_case.py
│   ├── core/
│   │   ├── config.py
│   │   └── logging_config.py
│   ├── domain/models.py
│   ├── infrastructure/embedding_models/fastembed_adapter.py
│   ├── dependencies.py
│   └── main.py
├── k8s/ # Directorio para manifiestos de Kubernetes
│   ├── embedding-service-configmap.yaml
│   ├── embedding-service-deployment.yaml
│   └── embedding-service-svc.yaml
├── Dockerfile
├── pyproject.toml
├── poetry.lock
├── README.md
└── .env.example
```

### 2.3. API Endpoint

*   **`POST /api/v1/embed`**
    *   **Request Body (`EmbedRequest`):** `{"texts": ["texto 1", "texto 2"]}`
    *   **Response Body (`EmbedResponse`):** `{"embeddings": [[...],[...]], "model_info": {"model_name": "...", "dimension": ...}}`
*   **`GET /health`**

### 2.4. Lógica Interna

*   Modelo `FastEmbed` cargado en `startup` (lifespan).
*   `FastEmbedAdapter` implementa `EmbeddingModelPort`.
*   `EmbedTextsUseCase` orquesta la lógica.

## 3. Refactorización del `ingest-service`

### 3.1. Eliminación de Componentes de Embedding

*   Se eliminó `app/services/embedder.py`.
*   Se eliminaron `sentence-transformers` y `onnxruntime` de `pyproject.toml` del `ingest-service`.
*   El Worker Celery (`app/tasks/process_document.py`) ya no inicializa `worker_embedding_model` localmente.

### 3.2. Modificación del Pipeline de Ingesta

*   `app/services/ingest_pipeline.py` ya no recibe una instancia de `SentenceTransformer` local.
*   Ahora es una función `async` y llama vía HTTP al `embedding-service` para obtener embeddings utilizando un `EmbeddingServiceClient`.

### 3.3. Cliente HTTP para `embedding-service`

*   Se creó `app/services/clients/embedding_service_client.py` para encapsular la comunicación con el `embedding-service`.

### 3.4. Configuración

*   Se añadió `INGEST_EMBEDDING_SERVICE_URL` en `app/core/config.py` del `ingest-service`.
*   Se eliminó `EMBEDDING_MODEL_ID` de la configuración del `ingest-service`.
*   Se mantuvo `EMBEDDING_DIMENSION` en la configuración del `ingest-service` para definir el esquema de Milvus y como referencia para la dimensión esperada del `embedding-service`.

## 4. Refactorización del `query-service` (Futuro)

*Se mantiene como referencia, pero esta fase se centra en `embedding-service` e `ingest-service`.*
### 4.1. Eliminación de Componentes de Embedding
### 4.2. Modificación de `AskQueryUseCase`
### 4.3. Cliente HTTP para `embedding-service`
### 4.4. Configuración

## 5. Checklist de Implementación

### 5.1. `embedding-service` (Nuevo)

*   [x] Definir estructura de directorios.
*   [x] Crear `app/core/config.py` con `EMBEDDING_MODEL_NAME`, `EMBEDDING_DIMENSION`, `LOG_LEVEL`, `PORT`, `FASTEMBED_CACHE_DIR`, `FASTEMBED_THREADS`, `FASTEMBED_MAX_LENGTH`.
*   [x] Crear `app/core/logging_config.py`.
*   [x] Definir `app/domain/models.py` (placeholder actual).
*   [x] Crear `app/application/ports/embedding_model_port.py`.
*   [x] Implementar `app/infrastructure/embedding_models/fastembed_adapter.py`.
*   [x] Crear `app/application/use_cases/embed_texts_use_case.py`.
*   [x] Definir `app/api/v1/schemas.py` (`EmbedRequest`, `EmbedResponse`, `ModelInfo`, `HealthCheckResponse`).
*   [x] Implementar `app/api/v1/endpoints/embedding_endpoint.py` con ruta `/embed`.
*   [x] Crear `app/dependencies.py` para la inyección de dependencias del caso de uso.
*   [x] Implementar `app/main.py` con FastAPI, lifespan para cargar modelo y endpoint `/health`, e inyección de dependencias.
*   [x] Crear `pyproject.toml` con dependencias: `fastapi`, `uvicorn`, `gunicorn`, `structlog`, `pydantic`, `pydantic-settings`, `fastembed`, `onnxruntime`.
*   [x] Crear `README.md` para el servicio.
*   [x] Crear `Dockerfile` para el servicio.
*   [x] Crear `.env.example`.
*   [x] Crear manifiestos de Kubernetes (`deployment.yaml`, `service.yaml`, `configmap.yaml`) en `k8s/`.
*   [x] Actualizar pipeline CI/CD (`cicd.yml`) para construir y desplegar `embedding-service`.

### 5.2. `ingest-service` (Refactorización - **Completado**)

*   [x] Eliminar `app/services/embedder.py`.
*   [x] Actualizar `pyproject.toml` eliminando `sentence-transformers` y `onnxruntime`.
*   [x] Modificar `app/tasks/process_document.py` para no inicializar `worker_embedding_model` y para llamar al nuevo cliente HTTP (`EmbeddingServiceClient`), utilizando un helper `run_async_from_sync` para la llamada al pipeline asíncrono.
*   [x] Añadir cliente HTTP para `embedding-service` en `app/services/clients/embedding_service_client.py`.
*   [x] Modificar `app/services/ingest_pipeline.py` para que sea `async` y use el `EmbeddingServiceClient` en lugar de `embed_chunks` local.
*   [x] Añadir `INGEST_EMBEDDING_SERVICE_URL` a `app/core/config.py`.
*   [x] Actualizar `Dockerfile` del `ingest-service` para reflejar cambios de dependencias (implícito al cambiar `pyproject.toml`).
*   [x] Actualizar `README.md` del `ingest-service` para reflejar la nueva arquitectura y la dependencia del `embedding-service`.

### 5.3. `query-service` (Refactorización - Pendiente)

*   [ ] Eliminar la lógica de `FastembedTextEmbedder` de `AskQueryUseCase` y del `lifespan` en `main.py`.
*   [ ] Actualizar `pyproject.toml` eliminando `fastembed-haystack`, `fastembed` y `onnxruntime` (si no son necesarios para otros componentes).
*   [ ] Añadir cliente HTTP para `embedding-service` (ej. `app/infrastructure/clients/embedding_service_client.py`).
*   [ ] Modificar `app/application/use_cases/ask_query_use_case.py` método `_embed_query` para llamar al `embedding-service` vía HTTP.
*   [ ] Añadir `QUERY_EMBEDDING_SERVICE_URL` a la configuración.
*   [ ] Actualizar `Dockerfile` del `query-service`.
*   [ ] Actualizar `README.md` del `query-service`.

### 5.4. General

*   [x] Actualizar diagramas de arquitectura.
*   [ ] Asegurar que las NetworkPolicies permitan la comunicación entre los servicios.
*   [ ] Planificar y ejecutar pruebas de integración y E2E después de cada fase de refactorización.
*   [ ] Actualizar la documentación general del sistema.