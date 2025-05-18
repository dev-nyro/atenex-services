# Atenex Reranker Service

**Versión:** 0.1.1 (refleja las últimas optimizaciones para GPU)

## 1. Visión General

El **Atenex Reranker Service** es un microservicio especializado dentro de la plataforma Atenex. Su única responsabilidad es recibir una consulta de usuario y una lista de fragmentos de texto (chunks de documentos) y reordenar dichos fragmentos basándose en su relevancia semántica para la consulta. Este proceso mejora la calidad de los resultados que se utilizan en etapas posteriores, como la generación de respuestas por un LLM en el `query-service`.

Utiliza modelos Cross-Encoder de la librería `sentence-transformers` para realizar el reranking, siendo `BAAI/bge-reranker-base` el modelo por defecto. El servicio está diseñado con una arquitectura limpia/hexagonal para facilitar su mantenimiento y escalabilidad, y ha sido optimizado para un uso estable y eficiente con GPUs NVIDIA.

## 2. Funcionalidades Principales

*   **Reranking de Documentos:** Acepta una consulta y una lista de documentos (con ID, texto y metadatos) y devuelve la misma lista de documentos, pero reordenada según su score de relevancia para la consulta.
*   **Modelo Configurable:** El modelo de reranking (`RERANKER_MODEL_NAME`), el dispositivo de inferencia (`RERANKER_MODEL_DEVICE`), y otros parámetros como el tamaño del lote (`RERANKER_BATCH_SIZE`) y la longitud máxima de secuencia (`RERANKER_MAX_SEQ_LENGTH`) son configurables mediante variables de entorno.
*   **Optimización para GPU:**
    *   **Conversión a FP16:** Si se utiliza un dispositivo `cuda`, el modelo se convierte automáticamente a precisión media (FP16) para reducir el uso de VRAM y potencialmente acelerar la inferencia.
    *   **Tokenización Secuencial en GPU:** Para garantizar la estabilidad con CUDA y evitar errores de `multiprocessing` (como "invalid resource handle"), la tokenización de los pares consulta-documento se realiza de forma secuencial (un solo worker para el `DataLoader` interno de `sentence-transformers`) cuando el servicio opera en GPU.
    *   **Workers Gunicorn Limitados en GPU:** El número de workers Gunicorn se limita automáticamente a `1` cuando se opera en GPU para evitar la contención de recursos y asegurar un uso estable de la VRAM.
*   **Eficiencia:**
    *   El modelo Cross-Encoder se carga en memoria una vez durante el inicio del servicio (usando el `lifespan` de FastAPI).
    *   Las operaciones de predicción del modelo se ejecutan en un `ThreadPoolExecutor` para no bloquear el event loop principal de FastAPI.
*   **API Sencilla:** Expone un único endpoint principal (`POST /api/v1/rerank`) para la funcionalidad de reranking.
*   **Health Check:** Proporciona un endpoint `GET /health` que verifica el estado del servicio y si el modelo de reranking se ha cargado correctamente.
*   **Logging Estructurado:** Utiliza `structlog` para generar logs en formato JSON, facilitando la observabilidad y el debugging.
*   **Manejo de Errores:** Implementa manejo de excepciones robusto y devuelve códigos de estado HTTP apropiados.
*   **Validación de Configuración:** Se realizan validaciones al inicio para asegurar que la configuración de workers (Gunicorn y tokenización) sea segura para el dispositivo seleccionado (CPU/GPU).

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
│   │   └── use_cases/rerank_documents_use_case.py  # (rerank_texts_use_case.py es una copia)
│   ├── core/
│   │   ├── config.py                     # Gestión de configuración y validaciones
│   │   └── logging_config.py
│   ├── domain/
│   │   └── models.py
│   ├── infrastructure/
│   │   └── rerankers/
│   │       └── sentence_transformer_adapter.py # Adaptador para el modelo
│   ├── dependencies.py                   # Inyección de dependencias
│   └── main.py                           # Entrypoint FastAPI, lifespan, middlewares
├── Dockerfile
├── pyproject.toml
├── poetry.lock
├── README.md (Este archivo)
└── .env.example
```
*(Nota: `app/application/use_cases/rerank_texts_use_case.py` parece ser una copia de `rerank_documents_use_case.py` en la codebase actual. El endpoint principal utiliza `RerankDocumentsUseCase`.)*

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
    *   `documents`: Una lista de objetos, cada uno representando un documento/chunk con `id`, `text` y `metadata` opcional. Debe contener al menos un documento.
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
      "model_status": "error", 
      "model_name": "BAAI/bge-reranker-base",
      "message": "Service is not ready. Model status is 'error' (expected 'loaded')." 
    }
    ```

## 6. Configuración

El servicio se configura mediante variables de entorno, con el prefijo `RERANKER_`. La configuración incluye validadores que ajustan automáticamente los workers si se usa CUDA para garantizar la estabilidad.

**Variables Clave (ver `app/core/config.py` para todos los defaults):**

| Variable                             | Descripción                                                                                                | Por Defecto (puede cambiar si `MODEL_DEVICE=cuda`) |
| :----------------------------------- | :--------------------------------------------------------------------------------------------------------- | :------------------------------------------------- |
| `RERANKER_LOG_LEVEL`                 | Nivel de logging (DEBUG, INFO, WARNING, ERROR, CRITICAL).                                                  | `INFO`                                             |
| `RERANKER_PORT`                      | Puerto en el que el servicio escuchará.                                                                    | `8004`                                             |
| `RERANKER_MODEL_NAME`                | Nombre o ruta del modelo Cross-Encoder de Hugging Face.                                                    | `BAAI/bge-reranker-base`                           |
| `RERANKER_MODEL_DEVICE`              | Dispositivo para la inferencia (`cpu`, `cuda`, `mps`). Si `cuda` no está disponible, revierte a `cpu`.        | `cpu`                                              |
| `RERANKER_HF_CACHE_DIR`              | Directorio para cachear modelos de Hugging Face.                                                           | `/app/.cache/huggingface`                          |
| `RERANKER_BATCH_SIZE`                | Tamaño del lote para la predicción del reranker.                                                           | `64` (si CUDA), `128` (si CPU)                     |
| `RERANKER_MAX_SEQ_LENGTH`            | Longitud máxima de secuencia para el modelo.                                                               | `512`                                              |
| `RERANKER_WORKERS`                   | Número de workers Gunicorn. **Se fuerza a `1` si `MODEL_DEVICE=cuda`**.                                      | `1` (si CUDA), `2` (si CPU)                        |
| `RERANKER_TOKENIZER_WORKERS`         | Workers para el `DataLoader` de tokenización. **Se fuerza a `0` si `MODEL_DEVICE=cuda` para estabilidad.** | `0` (si CUDA), `2` (si CPU)                        |

**Importante sobre Configuración con CUDA:**
*   Si `RERANKER_MODEL_DEVICE` se establece a `cuda`:
    *   `RERANKER_WORKERS` (Gunicorn workers) se forzará a `1`, incluso si se pasa un valor mayor por variable de entorno, para prevenir contención en la GPU.
    *   `RERANKER_TOKENIZER_WORKERS` se forzará a `0`, incluso si se pasa un valor mayor, para asegurar que la tokenización se realice secuencialmente en el worker principal de Gunicorn, evitando así errores de `multiprocessing` con CUDA.
*   Estas coacciones se loguearán con nivel `WARNING` si los valores originales de las variables de entorno son modificados.

## 7. Ejecución Local (Desarrollo)

1.  Asegurarse de tener **Python 3.10+** y **Poetry** instalados.
2.  Clonar el repositorio y navegar al directorio raíz `reranker-service/`.
3.  Ejecutar `poetry install` para instalar todas las dependencias.
4.  (Opcional) Crear un archivo `.env` en la raíz (`reranker-service/.env`) a partir de `.env.example` y modificar las variables.
    *   Para usar GPU localmente: `RERANKER_MODEL_DEVICE=cuda`
5.  Ejecutar el servicio con Uvicorn para desarrollo con auto-reload:
    ```bash
    poetry run uvicorn app.main:app --host 0.0.0.0 --port ${RERANKER_PORT:-8004} --reload
    ```
    El servicio estará disponible en `http://localhost:8004` (o el puerto configurado). Los logs indicarán la configuración efectiva.

## 8. Construcción y Despliegue Docker

1.  **Construir la Imagen Docker:**
    Desde el directorio raíz `reranker-service/`:
    ```bash
    docker build -t atenex/reranker-service:latest .
    # O con un tag específico para tu registro:
    # docker build -t ghcr.io/YOUR_ORG/atenex-reranker-service:$(git rev-parse --short HEAD) .
    ```

2.  **Ejecutar Localmente con Docker (Ejemplo con GPU en WSL2):**
    Asegúrate de que los drivers NVIDIA y el NVIDIA Container Toolkit estén configurados en WSL2.
    ```bash
    docker run -d --name reranker-gpu \
      --restart unless-stopped \
      --gpus all \
      -p 127.0.0.1:8004:8004 \ # Exponer solo en localhost de WSL2
      -v $HOME/hf_cache:/hf_cache \ # Montar caché de Hugging Face
      -e RERANKER_MODEL_DEVICE=cuda \
      -e RERANKER_HF_CACHE_DIR=/hf_cache \
      -e RERANKER_PORT=8004 \
      -e RERANKER_LOG_LEVEL=INFO \
      # -e RERANKER_WORKERS=1 # Será forzado a 1 por config si MODEL_DEVICE=cuda
      # -e RERANKER_TOKENIZER_WORKERS=0 # Será forzado a 0 por config si MODEL_DEVICE=cuda
      ghcr.io/dev-nyro/reranker-service:develop-2af5635 # Reemplazar con tu imagen y tag
    ```
    Los logs del contenedor mostrarán los valores efectivos para `WORKERS` y `TOKENIZER_WORKERS` después de la validación.

3.  **Push a un Registro de Contenedores:**
    ```bash
    docker push ghcr.io/YOUR_ORG/atenex-reranker-service:latest 
    ```

4.  **Despliegue en Kubernetes (Versión CPU):**
    El `reranker-service/deployment.yaml` en el repositorio de manifiestos está configurado para una versión CPU. Si se requiere una instancia en Kubernetes, se usaría este manifiesto.
    
    **Despliegue Externo (Versión GPU en WSL2 u otra VM):**
    Si el servicio se ejecuta fuera de Kubernetes (como en WSL2 con GPU):
    *   El clúster Kubernetes necesita poder acceder a la IP y puerto del host donde corre el contenedor Docker.
    *   Se usan manifiestos de tipo `Service` (sin selector) y `Endpoints` en Kubernetes para apuntar al servicio externo.
    *   Ejemplo (`manifests-nyro/reranker_gpu-service/`):
        *   `endpoints.yaml`: Define la IP y puerto del servicio en WSL2.
        *   `service.yaml`: Crea un servicio K8s que usa esos endpoints.
    *   El `query-service` (o cualquier otro consumidor) se configurará para usar el nombre de este servicio K8s (e.g., `http://reranker-gpu.nyro-develop.svc.cluster.local`).

## 9. CI/CD

*   El pipeline de CI/CD (`.github/workflows/cicd.yml`) está configurado para:
    *   Detectar cambios en el directorio `reranker-service/`.
    *   Construir y etiquetar la imagen Docker.
    *   Pushear la imagen al registro de contenedores (`ghcr.io`).
*   **Para la versión GPU externa (WSL2):**
    *   El pipeline de CI **no** actualiza automáticamente manifiestos de Kubernetes para esta versión.
    *   En su lugar, el pipeline **imprime instrucciones en los logs de la acción de GitHub** cuando se construye una nueva imagen para `reranker-service`. Estas instrucciones incluyen el comando `docker run` completo y actualizado que el desarrollador debe ejecutar manualmente en la máquina WSL2 para detener el contenedor antiguo y lanzar el nuevo con la imagen recién construida.
*   **Para la versión CPU en Kubernetes:**
    *   Si se mantiene un `deployment.yaml` para una versión CPU del reranker en el repositorio de manifiestos, el pipeline de CI puede ser configurado para actualizar el tag de imagen en ese archivo específico. Actualmente, el pipeline omite la actualización de manifiestos K8s si el servicio está marcado como `is_wsl_gpu_service: true`.

## 10. Notas de Rendimiento y Estabilidad en GPU

*   **FP16:** Habilitado por defecto en GPU para mejorar rendimiento y reducir uso de VRAM.
*   **Workers Gunicorn:** Limitado a 1 en GPU para serializar las solicitudes a la única instancia del modelo y evitar problemas de contención de recursos CUDA.
*   **Tokenización:** Forzada a ser secuencial (0 workers para el DataLoader) en GPU para máxima estabilidad, eliminando conflictos de `multiprocessing` con CUDA. Esto podría ser un cuello de botella si los textos son extremadamente largos o numerosos y la CPU es muy lenta, pero para la mayoría de los casos de reranking, la inferencia en GPU domina.
