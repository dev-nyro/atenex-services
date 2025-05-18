# Atenex Embedding Service

**Versión:** 1.1.1

## 1. Visión General

El **Atenex Embedding Service** es un microservicio de Atenex dedicado exclusivamente a la generación de embeddings (vectores numéricos) para fragmentos de texto. Utiliza la **API de Embeddings de OpenAI** como proveedor principal, ofreciendo acceso a modelos de vanguardia como `text-embedding-3-small`.

Este servicio es consumido internamente por otros microservicios de Atenex, como `ingest-service` (durante la ingesta de documentos) y `query-service` (para embeber las consultas de los usuarios), centralizando la lógica de generación de embeddings y facilitando la gestión de API Keys y modelos.

La lógica para `FastEmbed` se conserva en la codebase para posible uso futuro o como fallback, pero la configuración por defecto y el funcionamiento primario se basan en OpenAI.

## 2. Funcionalidades Principales

*   **Generación de Embeddings con OpenAI:** Procesa una lista de textos y devuelve sus representaciones vectoriales utilizando los modelos de embedding de OpenAI.
*   **Modelo OpenAI Configurable:** El nombre del modelo de OpenAI (`OPENAI_EMBEDDING_MODEL_NAME`) y opcionalmente sus dimensiones (`OPENAI_EMBEDDING_DIMENSIONS_OVERRIDE`) son configurables.
*   **Dimensiones de Embedding Validadas:** La dimensión global (`EMBEDDING_DIMENSION`) se valida contra el modelo OpenAI seleccionado.
*   **Manejo de API Key Seguro:** Utiliza `SecretStr` de Pydantic para la API Key de OpenAI.
*   **Reintentos y Timeouts:** Implementa reintentos con backoff exponencial para las llamadas a la API de OpenAI.
*   **API Sencilla:** Expone un único endpoint principal (`POST /api/v1/embed`) para la generación de embeddings.
*   **Health Check Detallado:** Proporciona un endpoint `GET /health` para verificar el estado del servicio y la inicialización del cliente de OpenAI.
*   **Arquitectura Limpia:** Estructurado siguiendo principios de arquitectura limpia/hexagonal.
*   **Logging Estructurado:** Utiliza `structlog` para logs en formato JSON.

## 3. Pila Tecnológica

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Proveedor de Embeddings Principal:** OpenAI API
    *   **Cliente Python:** `openai`
    *   **Modelo por Defecto:** `text-embedding-3-small` (1536 dimensiones)
*   **Servidor ASGI/WSGI:** Uvicorn con Gunicorn
*   **Contenerización:** Docker
*   **Gestión de Dependencias:** Poetry
*   **Logging:** Structlog
*   **Reintentos:** Tenacity

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
│   │   ├── config.py                     # Gestión de configuración
│   │   └── logging_config.py             # Configuración de Structlog
│   ├── domain/models.py
│   ├── infrastructure/embedding_models/  # Adaptadores de modelos
│   │   ├── openai_adapter.py             # Adaptador para OpenAI API (Principal)
│   │   └── fastembed_adapter.py          # Adaptador para FastEmbed (Secundario/Opcional)
│   ├── dependencies.py                   # Inyección de dependencias
│   └── main.py                           # Entrypoint FastAPI, lifespan
├── k8s/                                  # Manifiestos Kubernetes (ejemplos)
│   ├── embedding-service-configmap.yaml
│   ├── embedding-service-deployment.yaml
│   ├── embedding-service-secret.example.yaml
│   └── embedding-service-svc.yaml
├── Dockerfile
├── pyproject.toml
├── poetry.lock
├── README.md (Este archivo)
└── .env.example                          # Ejemplo de variables de entorno
```

## 5. API Endpoints

### `POST /api/v1/embed`

*   **Descripción:** Genera embeddings para los textos proporcionados utilizando el cliente OpenAI configurado.
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
        "model_name": "text-embedding-3-small",
        "dimension": 1536
      }
    }
    ```
*   **Errores Comunes:**
    *   `400 Bad Request`: Si `texts` está vacío o tiene un formato inválido.
    *   `422 Unprocessable Entity`: Si el cuerpo de la solicitud es inválido.
    *   `500 Internal Server Error`: Si ocurre un error inesperado durante la generación de embeddings o en la API de OpenAI.
    *   `503 Service Unavailable`: Si el cliente de OpenAI no está inicializado correctamente (e.g., API Key faltante o inválida) o si la API de OpenAI no está accesible después de reintentos.

### `GET /health`

*   **Descripción:** Verifica la salud del servicio y el estado del cliente del modelo de embedding (OpenAI).
*   **Response Body (200 OK - `HealthCheckResponse` - Servicio Saludable):**
    ```json
    {
      "status": "ok",
      "service": "Atenex Embedding Service",
      "model_status": "client_ready",
      "model_name": "text-embedding-3-small",
      "model_dimension": 1536
    }
    ```
*   **Response Body (503 Service Unavailable - Cliente OpenAI no listo o error):**
    Ejemplo:
    ```json
    {
      "status": "error",
      "service": "Atenex Embedding Service",
      "model_status": "client_initialization_pending_or_failed", 
      "model_name": "text-embedding-3-small",
      "model_dimension": 1536
    }
    ```
    Otros `model_status` posibles en caso de error: `client_error`, `client_not_initialized`.

## 6. Configuración

El servicio se configura mediante variables de entorno, con el prefijo `EMBEDDING_`. Ver `app/core/config.py` y `.env.example` para una lista completa y valores por defecto.

**Variables Clave para OpenAI (Proveedor Principal):**

| Variable                                   | Descripción                                                                                                | Por Defecto (`config.py`)             | Requerido |
| :----------------------------------------- | :--------------------------------------------------------------------------------------------------------- | :------------------------------------ | :-------- |
| `EMBEDDING_OPENAI_API_KEY`                 | **Tu API Key de OpenAI.**                                                                                  | `None`                                | **Sí**    |
| `EMBEDDING_OPENAI_EMBEDDING_MODEL_NAME`    | Nombre del modelo de embedding de OpenAI a utilizar.                                                       | `text-embedding-3-small`              | No        |
| `EMBEDDING_EMBEDDING_DIMENSION`            | Dimensión de los embeddings que producirá el modelo OpenAI. Debe coincidir con el modelo o el override.    | `1536` (para `text-embedding-3-small`) | No        |
| `EMBEDDING_OPENAI_EMBEDDING_DIMENSIONS_OVERRIDE` | (Opcional) Permite especificar una dimensión diferente para modelos OpenAI que lo soporten (ej. `text-embedding-3-*`). | `None`                                | No        |
| `EMBEDDING_OPENAI_API_BASE`                | (Opcional) URL base para la API de OpenAI (ej. para Azure OpenAI o proxies).                               | `None`                                | No        |
| `EMBEDDING_OPENAI_TIMEOUT_SECONDS`         | Timeout en segundos para las llamadas a la API de OpenAI.                                                  | `30`                                  | No        |
| `EMBEDDING_OPENAI_MAX_RETRIES`             | Número máximo de reintentos para llamadas fallidas a la API de OpenAI.                                     | `3`                                   | No        |

**Variables Generales:**

| Variable                             | Descripción                                                                 | Por Defecto |
| :----------------------------------- | :-------------------------------------------------------------------------- | :---------- |
| `EMBEDDING_LOG_LEVEL`                | Nivel de logging (DEBUG, INFO, WARNING, ERROR, CRITICAL).                   | `INFO`      |
| `EMBEDDING_PORT`                     | Puerto en el que escuchará el servicio.                                     | `8003`      |

**Variables para FastEmbed (Secundario/Opcional):**
Si se decidiera usar FastEmbed, las siguientes variables serían relevantes:
`EMBEDDING_FASTEMBED_MODEL_NAME`, `EMBEDDING_FASTEMBED_CACHE_DIR`, `EMBEDDING_FASTEMBED_THREADS`, `EMBEDDING_FASTEMBED_MAX_LENGTH`.
Actualmente, `EMBEDDING_DIMENSION` está validado para OpenAI. Para usar FastEmbed, esta validación o la configuración de `EMBEDDING_DIMENSION` necesitaría ajustarse.

## 7. Ejecución Local (Desarrollo)

1.  Asegurar que Poetry esté instalado (`pip install poetry`).
2.  Clonar el repositorio (o crear la estructura de archivos).
3.  Desde el directorio raíz `embedding-service/`, ejecutar `poetry install` para instalar dependencias.
4.  Crear un archivo `.env` en la raíz (`embedding-service/.env`) a partir de `.env.example`.
    *   **Esencial:** Configurar `EMBEDDING_OPENAI_API_KEY` con tu clave de API de OpenAI.
    *   Ajustar otras variables (`EMBEDDING_PORT`, `EMBEDDING_OPENAI_EMBEDDING_MODEL_NAME`, etc.) si es necesario.
5.  Ejecutar el servicio:
    ```bash
    poetry run uvicorn app.main:app --host 0.0.0.0 --port ${EMBEDDING_PORT:-8003} --reload
    ```
    El servicio estará disponible en `http://localhost:8003` (o el puerto configurado). Los logs indicarán si el cliente de OpenAI se inicializó correctamente.

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
      --name embedding-svc-openai \
      -e EMBEDDING_LOG_LEVEL="DEBUG" \
      -e EMBEDDING_OPENAI_API_KEY="tu_api_key_de_openai" \
      ghcr.io/dev-nyro/embedding-service:latest 
    ```
    **Nota:** Es preferible montar el archivo `.env` o usar secrets de Docker para la API key en lugar de pasarla directamente con `-e` para mayor seguridad.

3.  **Push a un Registro de Contenedores (ej. GitHub Container Registry):**
    Asegúrate de estar logueado a tu registro (`docker login ghcr.io`).
    ```bash
    docker push ghcr.io/dev-nyro/embedding-service:latest # o tu tag específico
    ```
4.  **Despliegue en Kubernetes:**
    Los manifiestos de Kubernetes se encuentran en el directorio `k8s/`.
    *   `k8s/embedding-service-configmap.yaml`: Contiene la configuración no sensible.
    *   `k8s/embedding-service-secret.example.yaml`: Ejemplo para el K8s Secret. **Debes crear un Secret real (`embedding-service-secret`) en tu clúster con el `OPENAI_API_KEY` real.**
    *   `k8s/embedding-service-deployment.yaml`: Define el despliegue del servicio.
    *   `k8s/embedding-service-svc.yaml`: Define el servicio de Kubernetes para exponer el deployment.

    Aplica los manifiestos al clúster (asegúrate que el namespace `nyro-develop` exista y el Secret haya sido creado):
    ```bash
    # 1. Crear el Secret (SOLO LA PRIMERA VEZ o si cambia la key)
    # kubectl create secret generic embedding-service-secret \
    #   --namespace nyro-develop \
    #   --from-literal=EMBEDDING_OPENAI_API_KEY='sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'

    # 2. Aplicar el resto de manifiestos
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
*   Actualizar automáticamente el tag de la imagen en el archivo `k8s/embedding-service-deployment.yaml` del repositorio de manifiestos, asumiendo que dicho repositorio está configurado para ArgoCD o un sistema similar de GitOps.