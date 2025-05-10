# Atenex Embedding Service

## 1. Visión General

El **Embedding Service** es un microservicio de Atenex dedicado exclusivamente a la generación de embeddings (vectores numéricos) para fragmentos de texto. Utiliza la librería `FastEmbed` y está configurado por defecto con el modelo `sentence-transformers/all-MiniLM-L6-v2`, que es eficiente y produce embeddings de 384 dimensiones.

Este servicio es consumido internamente por otros microservicios de Atenex, como `ingest-service` (durante la ingesta de documentos) y `query-service` (para embeber las consultas de los usuarios), ayudando a reducir la huella de memoria y el tamaño de las imágenes Docker de dichos servicios.

## 2. Funcionalidades Principales

*   **Generación de Embeddings:** Procesa una lista de textos y devuelve sus representaciones vectoriales.
*   **Modelo Configurable:** El nombre del modelo (`FASTEMBED_MODEL_NAME`) y su dimensión (`EMBEDDING_DIMENSION`) son configurables mediante variables de entorno.
*   **Eficiencia:** Aprovecha `FastEmbed` para una generación rápida de embeddings, optimizada para CPU.
*   **API Sencilla:** Expone un único endpoint principal (`/api/v1/embed`) para la generación de embeddings.
*   **Health Check:** Proporciona un endpoint `/health` para verificar el estado del servicio y la carga del modelo.
*   **Arquitectura Limpia:** Estructurado siguiendo principios de arquitectura limpia/hexagonal para facilitar el mantenimiento y la testabilidad.

## 3. Pila Tecnológica

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Motor de Embeddings:** FastEmbed (con ONNX Runtime)
*   **Modelo por Defecto:** `sentence-transformers/all-MiniLM-L6-v2`
*   **Servidor ASGI/WSGI:** Uvicorn con Gunicorn
*   **Contenerización:** Docker
*   **Gestión de Dependencias:** Poetry
*   **Logging:** Structlog

## 4. Estructura del Proyecto

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
├── k8s/
│   ├── embedding-service-configmap.yaml
│   ├── embedding-service-deployment.yaml
│   └── embedding-service-svc.yaml
├── Dockerfile
├── pyproject.toml
├── poetry.lock
├── README.md
└── .env.example
```

## 5. API Endpoints

### `POST /api/v1/embed`

*   **Descripción:** Genera embeddings para los textos proporcionados.
*   **Request Body (`EmbedRequest`):**
    ```json
    {
      "texts": ["texto a embeber 1", "otro texto más"]
    }
    ```
*   **Response Body (200 OK - `EmbedResponse`):**
    ```json
    {
      "embeddings": [
        [0.021, ..., -0.045],
        [0.123, ..., 0.078]
      ],
      "model_info": {
        "model_name": "sentence-transformers/all-MiniLM-L6-v2",
        "dimension": 384
      }
    }
    ```
*   **Errores Comunes:**
    *   `422 Unprocessable Entity`: Si el cuerpo de la solicitud es inválido (ej. `texts` no es una lista de strings o está vacía).
    *   `500 Internal Server Error`: Si ocurre un error inesperado durante la generación de embeddings.
    *   `503 Service Unavailable`: Si el modelo de embedding no está cargado o el servicio no está listo.

### `GET /health`

*   **Descripción:** Verifica la salud del servicio y el estado del modelo de embedding.
*   **Response Body (200 OK - `HealthCheckResponse` - Servicio Saludable):**
    ```json
    {
      "status": "ok",
      "service": "Atenex Embedding Service",
      "model_status": "loaded",
      "model_name": "sentence-transformers/all-MiniLM-L6-v2",
      "model_dimension": 384
    }
    ```
*   **Response Body (503 Service Unavailable - Modelo no cargado o error):**
    ```json
    {
      "status": "error",
      "service": "Atenex Embedding Service",
      "model_status": "error",
      "model_name": "sentence-transformers/all-MiniLM-L6-v2",
      "model_dimension": 384
    }
    ```

## 6. Configuración

El servicio se configura mediante variables de entorno, con el prefijo `EMBEDDING_`. Ver `.env.example` para una lista completa.

**Variables Clave:**

| Variable                             | Descripción                                                                 | Por Defecto                                |
| :----------------------------------- | :-------------------------------------------------------------------------- | :----------------------------------------- |
| `EMBEDDING_LOG_LEVEL`                | Nivel de logging (DEBUG, INFO, WARNING, ERROR).                             | `INFO`                                     |
| `EMBEDDING_PORT`                     | Puerto en el que escuchará el servicio.                                     | `8003`                                     |
| `EMBEDDING_FASTEMBED_MODEL_NAME`     | Nombre o ruta del modelo FastEmbed a utilizar.                              | `sentence-transformers/all-MiniLM-L6-v2` |
| `EMBEDDING_EMBEDDING_DIMENSION`    | Dimensión esperada de los embeddings generados por el modelo.               | `384`                                      |
| `EMBEDDING_FASTEMBED_CACHE_DIR`      | (Opcional) Directorio para cachear los modelos descargados por FastEmbed.    | `None`                                     |
| `EMBEDDING_FASTEMBED_THREADS`        | (Opcional) Número de hilos para la tokenización en FastEmbed.               | `None`                                     |
| `EMBEDDING_FASTEMBED_MAX_LENGTH`     | Longitud máxima de secuencia para el modelo.                                  | `512`                                      |

**Nota sobre `EMBEDDING_FASTEMBED_CACHE_DIR`:**
Si se despliega en Kubernetes, es recomendable establecer esta variable a una ruta dentro del contenedor (ej. `/app/.cache/fastembed`) para que los modelos se descarguen y cacheen allí. Si se requiere persistencia entre reinicios de pods (aunque los modelos son pequeños y se descargan rápido), se podría montar un `PersistentVolumeClaim`. Por defecto, el caché será efímero.

## 7. Ejecución Local (Desarrollo)

1.  Asegurar que Poetry esté instalado (`pip install poetry`).
2.  Clonar el repositorio (o crear la estructura de archivos).
3.  Desde el directorio raíz `embedding-service/`, ejecutar `poetry install` para instalar dependencias.
4.  (Opcional) Crear un archivo `.env` en la raíz (`embedding-service/.env`) a partir de `.env.example` y modificar las variables según sea necesario.
5.  Ejecutar el servicio:
    ```bash
    poetry run uvicorn app.main:app --host 0.0.0.0 --port 8003 --reload
    ```
    El servicio estará disponible en `http://localhost:8003`. El puerto puede variar según la variable `EMBEDDING_PORT`.

## 8. Construcción y Despliegue Docker

1.  **Construir la Imagen:**
    Desde el directorio raíz `embedding-service/`:
    ```bash
    docker build -t ghcr.io/dev-nyro/embedding-service:latest .
    # O con un tag específico, ej. el hash corto de git:
    # docker build -t ghcr.io/dev-nyro/embedding-service:$(git rev-parse --short HEAD) .
    ```
2.  **Ejecutar Localmente con Docker (Opcional para probar la imagen):**
    ```bash
    docker run -d -p 8003:8003 \
      --name embedding-svc \
      -e EMBEDDING_LOG_LEVEL="DEBUG" \
      ghcr.io/dev-nyro/embedding-service:latest
    ```
3.  **Push a un Registro de Contenedores (ej. GitHub Container Registry):**
    Asegúrate de estar logueado a tu registro (`docker login ghcr.io`).
    ```bash
    docker push ghcr.io/dev-nyro/embedding-service:latest # o tu tag específico
    ```
4.  **Despliegue en Kubernetes:**
    Los manifiestos de Kubernetes se encuentran en el directorio `k8s/`.
    *   `k8s/embedding-service-configmap.yaml`: Contiene la configuración no sensible.
    *   `k8s/embedding-service-deployment.yaml`: Define el despliegue del servicio.
    *   `k8s/embedding-service-svc.yaml`: Define el servicio de Kubernetes para exponer el deployment.

    Aplica los manifiestos al clúster (asegúrate que el namespace `nyro-develop` exista):
    ```bash
    kubectl apply -f k8s/embedding-service-configmap.yaml -n nyro-develop
    kubectl apply -f k8s/embedding-service-deployment.yaml -n nyro-develop
    kubectl apply -f k8s/embedding-service-svc.yaml -n nyro-develop
    ```
    El servicio será accesible internamente en el clúster en `http://embedding-service.nyro-develop.svc.cluster.local:80` (o el puerto que defina el Service K8s).

## 9. CI/CD

Este servicio se incluye en el pipeline de CI/CD definido en `.github/workflows/cicd.yml`. El pipeline se encarga de:
*   Detectar cambios en el directorio `embedding-service/`.
*   Construir y etiquetar la imagen Docker.
*   Empujar la imagen al registro de contenedores (`ghcr.io`).
*   Actualizar automáticamente el tag de la imagen en el archivo `k8s/embedding-service-deployment.yaml` del repositorio de manifiestos.
