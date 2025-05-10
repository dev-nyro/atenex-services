# Atenex Reranker Service

**Versión:** 0.1.0

## 1. Visión General

El **Atenex Reranker Service** es un microservicio especializado dentro de la plataforma Atenex. Su única responsabilidad es recibir una consulta de usuario y una lista de fragmentos de texto (chunks de documentos) y reordenar dichos fragmentos basándose en su relevancia semántica para la consulta. Este proceso mejora la calidad de los resultados que se utilizan en etapas posteriores, como la generación de respuestas por un LLM en el `query-service`.

Utiliza modelos Cross-Encoder de la librería `sentence-transformers` para realizar el reranking, siendo `BAAI/bge-reranker-base` el modelo por defecto. El servicio está diseñado con una arquitectura limpia/hexagonal para facilitar su mantenimiento y escalabilidad.

## 2. Funcionalidades Principales

*   **Reranking de Documentos:** Acepta una consulta y una lista de documentos (con ID, texto y metadatos) y devuelve la misma lista de documentos, pero reordenada según su score de relevancia para la consulta.
*   **Modelo Configurable:** El modelo de reranking (`RERANKER_MODEL_NAME`), el dispositivo de inferencia (`RERANKER_MODEL_DEVICE`), y otros parámetros como el tamaño del lote (`RERANKER_BATCH_SIZE`) y la longitud máxima de secuencia (`RERANKER_MAX_SEQ_LENGTH`) son configurables mediante variables de entorno.
*   **Eficiencia:**
    *   El modelo Cross-Encoder se carga en memoria una vez durante el inicio del servicio (usando el `lifespan` de FastAPI).
    *   Las operaciones de predicción del modelo, que pueden ser intensivas en CPU/GPU, se ejecutan en un `ThreadPoolExecutor` para no bloquear el event loop principal de FastAPI, permitiendo manejar múltiples solicitudes concurrentes.
*   **API Sencilla:** Expone un único endpoint principal (`POST /api/v1/rerank`) para la funcionalidad de reranking.
*   **Health Check:** Proporciona un endpoint `GET /health` que verifica el estado del servicio y si el modelo de reranking se ha cargado correctamente.
*   **Logging Estructurado:** Utiliza `structlog` para generar logs en formato JSON, facilitando la observabilidad y el debugging.
*   **Manejo de Errores:** Implementa manejo de excepciones robusto y devuelve códigos de estado HTTP apropiados.

## 3. Pila Tecnológica

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Motor de Reranking:** `sentence-transformers` (que a su vez utiliza `transformers` y PyTorch)
*   **Modelo por Defecto:** `BAAI/bge-reranker-base`
*   **Servidor ASGI/WSGI:** Uvicorn gestionado por Gunicorn
*   **Contenerización:** Docker
*   **Gestión de Dependencias:** Poetry
*   **Logging:** Structlog

## 4. Estructura del Proyecto

La estructura del proyecto sigue los principios de la Arquitectura Limpia/Hexagonal:

```
reranker-service/
├── app/
│   ├── api/v1/
│   │   ├── endpoints/rerank_endpoint.py
│   │   └── schemas.py
│   ├── application/
│   │   ├── ports/reranker_model_port.py
│   │   └── use_cases/rerank_documents_use_case.py
│   ├── core/
│   │   ├── config.py
│   │   └── logging_config.py
│   ├── domain/
│   │   └── models.py
│   ├── infrastructure/
│   │   └── rerankers/
│   │       └── sentence_transformer_adapter.py
│   ├── dependencies.py
│   └── main.py
├── Dockerfile
├── pyproject.toml
├── poetry.lock
├── README.md
└── .env.example
```

*   **`app/api/v1/`**: Endpoints FastAPI y schemas Pydantic para DTOs.
*   **`app/application/`**: Lógica de orquestación (casos de uso) y puertos (interfaces).
*   **`app/domain/`**: Modelos de datos Pydantic que representan las entidades del dominio.
*   **`app/infrastructure/`**: Implementaciones concretas de los puertos, como el adaptador para `sentence-transformers`.
*   **`app/core/`**: Configuración del servicio y del logging.
*   **`app/dependencies.py`**: Funciones para la inyección de dependencias en FastAPI.
*   **`app/main.py`**: Punto de entrada de la aplicación FastAPI, incluyendo el `lifespan` para la carga del modelo y la configuración de middlewares.

## 5. API Endpoints

### `POST /api/v1/rerank`

*   **Descripción:** Reordena una lista de documentos/chunks basada en su relevancia para una consulta dada.
*   **Request Body (`RerankRequest`):**
    ```json
    {
      "query": "string",
      "documents": [
        {
          "id": "chunk_id_1",
          "text": "Contenido textual del primer chunk.",
          "metadata": {"source_file": "documentA.pdf", "page": 1}
        },
        {
          "id": "chunk_id_2",
          "text": "Otro fragmento de texto relevante.",
          "metadata": {"source_file": "documentB.docx", "page": 10}
        }
      ],
      "top_n": 5
    }
    ```
    *   `query`: La consulta del usuario.
    *   `documents`: Una lista de objetos, cada uno representando un documento/chunk con `id`, `text` y `metadata` opcional.
    *   `top_n` (opcional): Si se especifica, el servicio devolverá como máximo este número de documentos de la lista rerankeada.

*   **Response Body (200 OK - `RerankResponse`):**
    ```json
    {
      "data": {
        "reranked_documents": [
          {
            "id": "chunk_id_2",
            "text": "Otro fragmento de texto relevante.",
            "score": 0.9875,
            "metadata": {"source_file": "documentB.docx", "page": 10}
          },
          {
            "id": "chunk_id_1",
            "text": "Contenido textual del primer chunk.",
            "score": 0.8532,
            "metadata": {"source_file": "documentA.pdf", "page": 1}
          }
        ],
        "model_info": {
          "model_name": "BAAI/bge-reranker-base"
        }
      }
    }
    ```
    Los `reranked_documents` se devuelven ordenados por `score` de forma descendente.

*   **Posibles Códigos de Error:**
    *   `422 Unprocessable Entity`: Error de validación en la solicitud (e.g., `query` vacío, lista `documents` vacía).
    *   `500 Internal Server Error`: Error inesperado durante el procesamiento del reranking.
    *   `503 Service Unavailable`: El modelo de reranking no está cargado o hay un problema crítico con el servicio.

### `GET /health`

*   **Descripción:** Endpoint de verificación de salud del servicio.
*   **Response Body (200 OK - `HealthCheckResponse` - Servicio Saludable):**
    ```json
    {
      "status": "ok",
      "service": "Atenex Reranker Service",
      "model_status": "loaded",
      "model_name": "BAAI/bge-reranker-base"
    }
    ```
*   **Response Body (503 Service Unavailable - Problema con el Modelo/Servicio):**
    ```json
    {
      "status": "error",
      "service": "Atenex Reranker Service",
      "model_status": "error", // o "loading", "unloaded"
      "model_name": "BAAI/bge-reranker-base",
      "message": "Service is not ready or model loading failed."
    }
    ```

## 6. Configuración

El servicio se configura mediante variables de entorno, con el prefijo `RERANKER_`. Ver el archivo `.env.example` y `app/core/config.py` para una lista completa de variables y sus valores por defecto.

**Variables Clave:**

| Variable                             | Descripción                                                               | Por Defecto (`config.py`)    |
| :----------------------------------- | :------------------------------------------------------------------------ | :--------------------------- |
| `RERANKER_LOG_LEVEL`                 | Nivel de logging (DEBUG, INFO, WARNING, ERROR, CRITICAL).                 | `INFO`                       |
| `RERANKER_PORT`                      | Puerto en el que el servicio escuchará.                                   | `8004`                       |
| `RERANKER_MODEL_NAME`                | Nombre o ruta del modelo Cross-Encoder de Hugging Face.                   | `BAAI/bge-reranker-base`     |
| `RERANKER_MODEL_DEVICE`              | Dispositivo para la inferencia (`cpu`, `cuda`, `mps`).                    | `cpu`                        |
| `RERANKER_HF_CACHE_DIR`              | Directorio para cachear modelos de Hugging Face.                          | `/app/.cache/huggingface`    |
| `RERANKER_BATCH_SIZE`                | Tamaño del lote para la predicción del reranker.                          | `32`                         |
| `RERANKER_MAX_SEQ_LENGTH`            | Longitud máxima de secuencia para el modelo.                              | `512`                        |
| `RERANKER_WORKERS`                   | Número de workers Gunicorn para producción.                               | `2`                          |

## 7. Ejecución Local (Desarrollo)

1.  Asegurarse de tener **Python 3.10+** y **Poetry** instalados.
2.  Clonar el repositorio (si aplica) o crear la estructura de archivos.
3.  Navegar al directorio raíz `reranker-service/`.
4.  Ejecutar `poetry install` para instalar todas las dependencias.
5.  (Opcional) Crear un archivo `.env` en la raíz (`reranker-service/.env`) a partir de `.env.example` y modificar las variables según sea necesario.
6.  Ejecutar el servicio con Uvicorn para desarrollo con auto-reload:
    ```bash
    poetry run uvicorn app.main:app --host 0.0.0.0 --port ${RERANKER_PORT:-8004} --reload
    ```
    El servicio estará disponible en `http://localhost:8004` (o el puerto configurado).

## 8. Construcción y Despliegue Docker

1.  **Construir la Imagen Docker:**
    Desde el directorio raíz `reranker-service/`:
    ```bash
    docker build -t atenex/reranker-service:latest .
    # O con un tag específico, ej. el hash corto de git:
    # docker build -t ghcr.io/YOUR_ORG/atenex-reranker-service:$(git rev-parse --short HEAD) .
    ```

2.  **Ejecutar Localmente con Docker (Opcional para probar la imagen):**
    ```bash
    docker run -d -p 8004:8004 \
      --name reranker-svc-local \
      -e RERANKER_LOG_LEVEL="DEBUG" \
      -e RERANKER_PORT="8004" \
      atenex/reranker-service:latest
    ```

3.  **Push a un Registro de Contenedores (ej. GitHub Container Registry, Google Artifact Registry):**
    Asegurarse de estar logueado al registro (`docker login ghcr.io`, `gcloud auth configure-docker`).
    ```bash
    docker push ghcr.io/YOUR_ORG/atenex-reranker-service:latest # o tu tag específico
    ```

4.  **Despliegue en Kubernetes:**
    Se requieren manifiestos de Kubernetes (`Deployment`, `Service`, `ConfigMap`) para desplegar en un clúster (ej. GKE). Estos manifiestos se gestionarán en un repositorio separado o en una sección `k8s/` dentro de este proyecto.
    *   El `Deployment` especificará la imagen, réplicas, variables de entorno (desde ConfigMap/Secrets) y montaje de volúmenes para el caché del modelo si se usa `PersistentVolumeClaim`.
    *   El `Service` expondrá el `Deployment` internamente en el clúster.
    *   El `ConfigMap` contendrá la configuración no sensible.

## 9. CI/CD

Se recomienda integrar este servicio en un pipeline de CI/CD que automatice:
*   Detección de cambios en el directorio del servicio.
*   Ejecución de tests (unitarios, integración).
*   Análisis de código estático y linting.
*   Construcción y etiquetado de la imagen Docker.
*   Push de la imagen a un registro de contenedores.
*   (Opcional) Actualización automática de manifiestos de despliegue en un repositorio GitOps.
