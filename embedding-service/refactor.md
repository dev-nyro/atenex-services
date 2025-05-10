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

*   Quitar `app/services/embedder.py`.
*   Eliminar `sentence-transformers` y `onnxruntime` de `pyproject.toml` (si solo eran para embeddings).
*   Worker Celery no inicializará `worker_embedding_model`.

### 3.2. Modificación del Pipeline de Ingesta

*   `ingest_document_pipeline` no recibirá `embedding_model`.
*   Llamará vía HTTP al `embedding-service` para obtener embeddings.

### 3.3. Cliente HTTP para `embedding-service`

*   Crear `app/services/clients/embedding_service_client.py`.

### 3.4. Configuración

*   Añadir `INGEST_EMBEDDING_SERVICE_URL` en `app/core/config.py`.

## 4. Refactorización del `query-service`

### 4.1. Eliminación de Componentes de Embedding

*   Eliminar `FastembedTextEmbedder` de `AskQueryUseCase` y `main.py`.
*   Eliminar `fastembed-haystack`, `fastembed`, `onnxruntime` de `pyproject.toml` (si solo eran para embeddings).

### 4.2. Modificación de `AskQueryUseCase`

*   Método `_embed_query` llamará vía HTTP al `embedding-service`.

### 4.3. Cliente HTTP para `embedding-service`

*   Crear `app/infrastructure/clients/embedding_service_client.py`.

### 4.4. Configuración

*   Añadir `QUERY_EMBEDDING_SERVICE_URL` en `app/core/config.py`.

## 5. Checklist de Implementación

### 5.1. `embedding-service` (Nuevo)

*   [x] Definir estructura de directorios.
*   [x] Crear `app/core/config.py` con `EMBEDDING_MODEL_NAME`, `EMBEDDING_DIMENSION`, `LOG_LEVEL`, `PORT`, `FASTEMBED_CACHE_DIR`, `FASTEMBED_THREADS`, `FASTEMBED_MAX_LENGTH`.
*   [x] Crear `app/core/logging_config.py`.
*   [x] Definir `app/domain/models.py` (actualmente placeholder).
*   [x] Crear `app/application/ports/embedding_model_port.py`.
*   [x] Implementar `app/infrastructure/embedding_models/fastembed_adapter.py` (usando `FastEmbed` con `all-MiniLM-L6-v2`).
*   [x] Crear `app/application/use_cases/embed_texts_use_case.py`.
*   [x] Definir `app/api/v1/schemas.py` (`EmbedRequest`, `EmbedResponse`, `ModelInfo`, `HealthCheckResponse`).
*   [x] Implementar `app/api/v1/endpoints/embedding_endpoint.py` con ruta `/embed`.
*   [x] Crear `app/dependencies.py` para la inyección de dependencias del caso de uso.
*   [x] Implementar `app/main.py` con FastAPI, lifespan para cargar modelo y endpoint `/health`, e inyección de dependencias.
*   [x] Crear `pyproject.toml` con dependencias: `fastapi`, `uvicorn`, `gunicorn`, `structlog`, `pydantic`, `pydantic-settings`, `fastembed`, `onnxruntime`.
*   [ ] Crear `README.md` para el servicio.
*   [ ] Crear `Dockerfile` para el servicio.
*   [ ] Crear `.env.example`.
*   [ ] Crear manifiestos de Kubernetes (`deployment.yaml`, `service.yaml`, `configmap.yaml`) en `k8s/`.
*   [ ] Actualizar pipeline CI/CD (`cicd.yml`) para construir y desplegar `embedding-service`.

### 5.2. `ingest-service` (Refactorización)

*   [ ] Eliminar `app/services/embedder.py`.
*   [ ] Actualizar `pyproject.toml` eliminando `sentence-transformers` y `onnxruntime` (si no son necesarios para otra cosa).
*   [ ] Modificar `app/tasks/process_document.py` para no inicializar `worker_embedding_model`.
*   [ ] Añadir cliente HTTP para `embedding-service` (ej. `app/services/clients/embedding_service_client.py`).
*   [ ] Modificar `app/services/ingest_pipeline.py` para llamar al `embedding-service` vía HTTP.
*   [ ] Añadir `INGEST_EMBEDDING_SERVICE_URL` a la configuración.
*   [ ] Actualizar `Dockerfile` si es necesario.

### 5.3. `query-service` (Refactorización)

*   [ ] Eliminar la lógica de `FastembedTextEmbedder` de `AskQueryUseCase` y del `lifespan` en `main.py`.
*   [ ] Actualizar `pyproject.toml` eliminando `fastembed-haystack`, `fastembed`, `onnxruntime` (si no son necesarios para otra cosa).
*   [ ] Añadir cliente HTTP para `embedding-service` (ej. `app/infrastructure/clients/embedding_service_client.py`).
*   [ ] Modificar `app/application/use_cases/ask_query_use_case.py` método `_embed_query` para llamar al `embedding-service` vía HTTP.
*   [ ] Añadir `QUERY_EMBEDDING_SERVICE_URL` a la configuración.
*   [ ] Actualizar `Dockerfile` si es necesario.

### 5.4. General

*   [ ] Actualizar diagramas de arquitectura.
*   [ ] Asegurar que las NetworkPolicies permitan la comunicación entre los servicios.
*   [ ] Planificar y ejecutar pruebas de integración y E2E.
*   [ ] Actualizar la documentación general del sistema.