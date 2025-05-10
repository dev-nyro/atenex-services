# Plan de Creación de `reranker-service` y Refactorización Asociada

**Versión:** 1.1
**Fecha:** 2023-10-28
**Autor:** Software Engineering Senior (IA Asistente)

## 1. Visión General y Objetivos

Este documento detalla el plan para la creación de un nuevo microservicio, **`reranker-service`**, y la refactorización necesaria del **`query-service`** para integrarse con él. Los objetivos principales son:

1.  **Centralizar la Lógica de Reranking:** Crear un servicio dedicado responsable exclusivamente de reordenar documentos/chunks basados en su relevancia para una consulta dada.
2.  **Reducir la Huella del `query-service`:** Extraer el modelo de reranking y sus dependencias (ej. `sentence-transformers`, PyTorch/TensorFlow) del `query-service`, disminuyendo significativamente el tamaño de su imagen Docker y su consumo de memoria.
3.  **Mejorar la Cohesión y Escalabilidad:** Permitir que el `reranker-service` se escale independientemente del `query-service`.
4.  **Mantener/Mejorar la Calidad del RAG:** Asegurar que la funcionalidad de reranking siga siendo efectiva y configurable.
5.  **Adherencia a Buenas Prácticas:** Implementar el nuevo servicio siguiendo principios de Arquitectura Limpia/Hexagonal y las mejores prácticas de desarrollo.

## 2. Creación del `reranker-service`

### 2.1. Responsabilidades y Funcionalidades

*   **Entrada:** Recibe una consulta (string) y una lista de documentos/chunks (cada uno con un ID, texto y metadatos opcionales).
*   **Procesamiento:** Utiliza un modelo Cross-Encoder (configurable, por defecto `BAAI/bge-reranker-base`) para calcular el score de relevancia de cada documento/chunk con respecto a la consulta.
*   **Salida:** Devuelve la lista de documentos/chunks originales, reordenados por su score de relevancia (de mayor a menor), junto con su score. Los metadatos originales de cada documento se preservan.
*   **Configurabilidad:** Permite configurar el modelo de reranking, el dispositivo de inferencia (CPU/GPU) y otros parámetros relevantes a través de variables de entorno.
*   **Eficiencia:** Carga el modelo en memoria al inicio (lifespan) para evitar cold starts.
*   **Salud:** Proporciona un endpoint de health check.

### 2.2. Pila Tecnológica

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Motor de Reranking:** `sentence-transformers` (con `transformers` y PyTorch como backend)
*   **Modelo por Defecto:** `BAAI/bge-reranker-base`
*   **Servidor ASGI/WSGI:** Uvicorn con Gunicorn
*   **Contenerización:** Docker
*   **Gestión de Dependencias:** Poetry
*   **Logging:** Structlog

### 2.3. Arquitectura Hexagonal Propuesta

El `reranker-service` seguirá una arquitectura hexagonal para una clara separación de concerns:

```mermaid
graph TD
    A[API Layer (FastAPI Endpoints & Schemas)] -->|Invoca| UC{Application Layer (Use Cases)}
    UC -- Uses Port --> RMP[RerankerModelPort (Interface)]
    RMP <-- Implemented By -- IAL[Infrastructure Layer (Adapters)]

    subgraph IAL [Infrastructure Layer]
        direction LR
        ST_Adapter[SentenceTransformerRerankerAdapter<br/>(Implements RerankerModelPort)]
    end

    subgraph UC [Application Layer]
        direction TB
        RerankDocsUseCase[RerankDocumentsUseCase]
        RMP
    end

    subgraph D [Domain Layer]
         Models[Domain Models<br/>- DocumentToRerank<br/>- RerankedDocument]
    end

    A -- Calls --> RerankDocsUseCase
    RerankDocsUseCase -- Depends on --> RMP
    ST_Adapter -- Uses --> ExternalModel[("HuggingFace Model\n(BAAI/bge-reranker-base)")]

    style A fill:#C8E6C9,stroke:#333,stroke-width:1px
    style UC fill:#D1C4E9,stroke:#333,stroke-width:1px
    style IAL fill:#BBDEFB,stroke:#333,stroke-width:1px
    style D fill:#FFECB3,stroke:#333,stroke-width:1px
    style ExternalModel fill:#FFEBEE,stroke:#F44336,color:#333
```

### 2.4. Estructura del Proyecto (`reranker-service/`)

```
reranker-service/
├── app/
│   ├── api/v1/
│   │   ├── endpoints/rerank_endpoint.py    # Define el endpoint /rerank
│   │   └── schemas.py                      # Pydantic schemas para request/response
│   ├── application/
│   │   ├── ports/reranker_model_port.py    # Interfaz para el modelo de reranking
│   │   └── use_cases/rerank_documents_use_case.py # Lógica de orquestación
│   ├── core/
│   │   ├── config.py                       # Configuración (Pydantic Settings)
│   │   └── logging_config.py               # Configuración de Structlog
│   ├── domain/
│   │   └── models.py                       # Modelos de dominio (ej. Document, RerankedResult)
│   ├── infrastructure/
│   │   └── rerankers/                      # Implementaciones del RerankerModelPort
│   │       └── sentence_transformer_adapter.py # Adaptador para sentence-transformers
│   ├── dependencies.py                     # Dependencias compartidas (ej. obtener modelo)
│   └── main.py                             # Entrypoint FastAPI, lifespan (carga de modelo)
├── tests/                                  # (Placeholder para tests)
│   ├── integration/
│   └── unit/
├── Dockerfile
├── pyproject.toml
├── poetry.lock                             # Generado por Poetry
├── README.md                               # README específico del servicio
└── .env.example                            # Ejemplo de variables de entorno
```

### 2.5. API Endpoints

#### `POST /api/v1/rerank`

*   **Descripción:** Reordena una lista de documentos basada en su relevancia para una consulta.
*   **Request Body (`RerankRequest`):**
    ```json
    {
      "query": "string",
      "documents": [
        {"id": "doc1", "text": "Contenido del documento 1.", "metadata": {"source": "fileA.pdf", "page": 1}},
        {"id": "doc2", "text": "Otro texto relevante.", "metadata": {"source": "fileB.docx", "page": 5}}
      ],
      "top_n": 10 // Opcional, si se quiere que el servicio trunque los resultados
    }
    ```
*   **Response Body (200 OK - `RerankResponse`):**
    ```json
    {
      "reranked_documents": [
        {"id": "doc2", "text": "Otro texto relevante.", "score": 0.987, "metadata": {"source": "fileB.docx", "page": 5}},
        {"id": "doc1", "text": "Contenido del documento 1.", "score": 0.853, "metadata": {"source": "fileA.pdf", "page": 1}}
      ],
      "model_info": {
        "model_name": "BAAI/bge-reranker-base"
      }
    }
    ```
    *Nota: Los documentos se devuelven ordenados por `score` descendente.*
*   **Errores Comunes:**
    *   `422 Unprocessable Entity`: Cuerpo de la solicitud inválido.
    *   `500 Internal Server Error`: Error durante el reranking.
    *   `503 Service Unavailable`: Modelo no cargado o servicio no listo.

#### `GET /health`

*   **Descripción:** Verifica la salud del servicio y el estado del modelo de reranking.
*   **Response Body (200 OK - `HealthCheckResponse` - Saludable):**
    ```json
    {
      "status": "ok",
      "service": "Atenex Reranker Service",
      "model_status": "loaded",
      "model_name": "BAAI/bge-reranker-base"
    }
    ```
*   **Response Body (503 Service Unavailable - Modelo no cargado):**
    ```json
    {
      "status": "error",
      "service": "Atenex Reranker Service",
      "model_status": "error | loading",
      "model_name": "BAAI/bge-reranker-base" // o el configurado
    }
    ```

### 2.6. Configuración (Variables de Entorno)

Prefijo: `RERANKER_`

| Variable                             | Descripción                                                               | Por Defecto                | Gestionado por |
| :----------------------------------- | :------------------------------------------------------------------------ | :------------------------- | :------------- |
| `RERANKER_LOG_LEVEL`                 | Nivel de logging (DEBUG, INFO, WARNING, ERROR).                           | `INFO`                     | ConfigMap      |
| `RERANKER_PORT`                      | Puerto en el que escuchará el servicio.                                   | `8004` (sugerido)          | Deployment     |
| `RERANKER_MODEL_NAME`                | Nombre o ruta del modelo Cross-Encoder de Hugging Face.                   | `BAAI/bge-reranker-base`   | ConfigMap      |
| `RERANKER_MODEL_DEVICE`              | Dispositivo para la inferencia (`cpu`, `cuda`, `mps`, etc.).              | `cpu`                      | ConfigMap      |
| `RERANKER_HF_CACHE_DIR`              | (Opcional) Directorio para cachear los modelos de Hugging Face.           | `/app/.cache/huggingface`  | Deployment (via VolumeMount si es persistente) |
| `RERANKER_BATCH_SIZE`                | (Opcional) Tamaño del lote para la predicción del reranker.             | `32`                       | ConfigMap      |
| `RERANKER_MAX_SEQ_LENGTH`            | (Opcional) Longitud máxima de secuencia para el modelo.                   | `512`                      | ConfigMap      |
| `RERANKER_WORKERS`                   | Número de workers Gunicorn.                                               | `2`                        | Deployment     |

### 2.7. Esquema General de Implementación de Componentes Clave

*   **`app/main.py` (Lifespan):**
    *   Al inicio:
        *   Configurar logging.
        *   Obtener settings de configuración.
        *   Intentar cargar el modelo `CrossEncoder` (usando `settings.RERANKER_MODEL_NAME`, `_MAX_SEQ_LENGTH`, `_MODEL_DEVICE`).
        *   Almacenar la instancia del modelo o su estado (ej. en un singleton o `app.state`).
        *   Loggear éxito o fracaso de la carga.
    *   Al final: Loggear apagado.
    *   Definir endpoint `/health` que verifique el estado del modelo cargado.
    *   Incluir el router de la API.
*   **`app/dependencies.py`:**
    *   Proveer una forma de acceder a la instancia del modelo cargado (ej. `RerankerModelSingleton` con `get_model()`, `set_model()`, `get_status()`, `set_status()`).
    *   Proveer una función `Depends` para inyectar el `RerankDocumentsUseCase` en los endpoints.
*   **`app/application/ports/reranker_model_port.py`:**
    *   Definir la interfaz `RerankerModelPort` con métodos `rerank(query, documents)` y `get_model_name()`.
*   **`app/infrastructure/rerankers/sentence_transformer_adapter.py`:**
    *   Clase `SentenceTransformerRerankerAdapter` que implementa `RerankerModelPort`.
    *   Recibe una función proveedora del modelo (`model_provider`) en su constructor.
    *   El método `rerank` toma la query y la lista de `DocumentToRerank`.
    *   Prepara pares `(query, doc.text)`.
    *   Llama a `model.predict(pares)` (ejecutado en un thread pool: `asyncio.to_thread`).
    *   Mapea los scores a los documentos originales, preservando metadatos.
    *   Devuelve una lista de `RerankedDocument` ordenada por score.
*   **`app/application/use_cases/rerank_documents_use_case.py`:**
    *   Clase `RerankDocumentsUseCase` que recibe `RerankerModelPort` en su constructor.
    *   Método `execute(query, documents, top_n)`:
        *   Llama a `reranker_model.rerank(query, documents)`.
        *   Si `top_n` es provisto, trunca la lista de resultados.
        *   Construye y devuelve `RerankResponseData` (incluyendo `model_info`).
*   **`app/api/v1/endpoints/rerank_endpoint.py`:**
    *   Define el endpoint POST `/rerank`.
    *   Usa `Depends` para obtener la instancia de `RerankDocumentsUseCase`.
    *   Recibe `RerankRequest` en el body.
    *   Llama a `use_case.execute(...)`.
    *   Devuelve `RerankResponse`.
    *   Maneja excepciones y loggea.
*   **`app/domain/models.py` y `app/api/v1/schemas.py`:**
    *   Definir modelos Pydantic para los datos de entrada y salida, como `DocumentToRerank`, `RerankedDocument`, `ModelInfo`, `RerankRequest`, `RerankResponseData`, `RerankResponse`.

### 2.8. Dockerfile

*   Basado en `python:3.10-slim`.
*   Instalar Poetry.
*   Copiar `pyproject.toml` y `poetry.lock`.
*   Ejecutar `poetry install --no-dev --no-root`.
*   Copiar el código de la aplicación (`app/`).
*   Exponer el puerto (`RERANKER_PORT`).
*   Comando `CMD` para ejecutar `gunicorn` con `uvicorn.workers.UvicornWorker`.

## 3. Refactorización del `query-service`

### 3.1. Objetivos de la Refactorización

*   Eliminar la lógica y dependencias de reranking del `query-service`.
*   Integrar el `query-service` para que llame al nuevo `reranker-service` vía HTTP.

### 3.2. Cambios Clave en `query-service`

1.  **Eliminación de Código y Dependencias:**
    *   Quitar `app/infrastructure/rerankers/bge_reranker.py`.
    *   Quitar `app/application/ports/reranker_port.py` (si existe una interfaz específica para el reranker local).
    *   Remover `sentence-transformers` de `pyproject.toml`.
    *   Eliminar la variable de entorno `QUERY_RERANKER_MODEL_NAME` y su uso.
    *   Eliminar la lógica de carga del modelo `BGEReranker` en `app/main.py` (lifespan) o `AskQueryUseCase`.

2.  **Modificación del Flujo en `AskQueryUseCase` (`app/application/use_cases/ask_query_use_case.py`):**
    *   **Cliente HTTP:** Asegurar que un cliente HTTP asíncrono (ej. `httpx`) esté disponible (inicializado en `main.py` del `query-service` y accesible, por ejemplo, vía `app.state` o inyectado).
    *   **Llamada al `reranker-service`:**
        *   Si `self.settings.RERANKER_ENABLED` es verdadero y hay `fused_chunks` para rerankear:
            *   Preparar el payload para `reranker-service`: `{"query": query_text, "documents": [...], "top_n": ...}`. Los `documents` serán una lista de diccionarios con `id`, `text`, `metadata` de los `RetrievedChunk`s.
            *   Realizar una llamada `POST` asíncrona a `self.settings.RERANKER_SERVICE_URL + "/api/v1/rerank"` con el payload.
            *   Manejar la respuesta:
                *   Si es exitosa, parsear el JSON.
                *   Mapear los `reranked_documents` de la respuesta de vuelta a objetos `RetrievedChunk` del `query-service`, actualizando sus `score` y manteniendo su `id`, `content`, `metadata` originales. Es crucial tener un mapa `id -> original_chunk` para reconstruir correctamente.
                *   Actualizar `pipeline_metadata` con información del reranker (ej. nombre del modelo, IDs rerankeados).
            *   Manejar errores HTTP (ej. `httpx.HTTPStatusError`) o de conexión, loggeando el error y decidiendo si continuar el pipeline RAG con los chunks sin rerankear o fallar.
        *   El resto del pipeline (diversity filter, prompt building) usará los chunks resultantes de este paso.

3.  **Nueva Configuración para `query-service`:**
    *   Añadir `QUERY_RERANKER_SERVICE_URL: str` (ej. `"http://reranker-service.nyro-develop.svc.cluster.local:80"`) a `app/core/config.py` y al ConfigMap.
    *   Opcionalmente, `QUERY_RERANKER_CLIENT_TIMEOUT: int`.

4.  **Actualizar `Dockerfile` y `pyproject.toml`:**
    *   Reflejar la eliminación de dependencias en `pyproject.toml`.
    *   Reconstruir el `Dockerfile` que será más ligero.

5.  **Consideraciones sobre `RetrievedChunk`:**
    *   El objeto `RetrievedChunk` en `query-service` (`app/domain/models.py`) ya contiene `id`, `content`, `score`, `metadata`. Estos campos (o sus equivalentes) serán enviados al `reranker-service`.
    *   Al recibir la respuesta, `query-service` debe actualizar sus `RetrievedChunk`s con los nuevos `score`s y mantener la consistencia de los demás campos.

## 4. Comunicación Inter-Servicio

*   La comunicación entre `query-service` y `reranker-service` será síncrona (HTTP request/response).
*   Se utilizarán las URLs internas del clúster de Kubernetes (ej. `reranker-service.nyro-develop.svc.cluster.local`).
*   No se requiere autenticación adicional para esta comunicación interna si se asume un entorno de clúster seguro.

## 5. Testing

*   **`reranker-service`:**
    *   Tests unitarios para el adaptador y caso de uso (mockeando el modelo).
    *   Tests de integración para el endpoint API (usando `TestClient`).
*   **`query-service`:**
    *   Tests unitarios para `AskQueryUseCase` mockeando la llamada HTTP al `reranker-service` (ej. con `respx`).
    *   Tests de integración del flujo RAG (mockeando la respuesta del `reranker-service`).
*   **End-to-End (E2E):** Pruebas completas a través del API Gateway.

## 6. Despliegue y Migración

1.  Desarrollar y probar `reranker-service` localmente y en staging.
2.  Refactorizar y probar `query-service` localmente y en staging.
3.  Desplegar `reranker-service` en producción.
4.  Desplegar `query-service` actualizado en producción.
5.  Monitorizar ambos servicios y la interacción.

---

## Checklist de Implementación y Refactorización

### Fase 1: Creación del `reranker-service`

*   [ ] 1.1. Definir estructura del proyecto `reranker-service` (directorios y archivos iniciales).
*   [ ] 1.2. Implementar `app/core/config.py` y `app/core/logging_config.py`.
*   [ ] 1.3. Definir modelos Pydantic en `app/domain/models.py`.
*   [ ] 1.4. Definir schemas Pydantic para API en `app/api/v1/schemas.py`.
*   [ ] 1.5. Implementar `RerankerModelPort` en `app/application/ports/`.
*   [ ] 1.6. Implementar `SentenceTransformerRerankerAdapter` en `app/infrastructure/rerankers/`.
*   [ ] 1.7. Implementar `RerankDocumentsUseCase` en `app/application/use_cases/`.
*   [ ] 1.8. Implementar `app/dependencies.py` para gestión de modelo y caso de uso.
*   [ ] 1.9. Implementar `app/main.py` (FastAPI app, lifespan, health check).
*   [ ] 1.10. Implementar `app/api/v1/endpoints/rerank_endpoint.py`.
*   [ ] 1.11. Escribir `Dockerfile`.
*   [ ] 1.12. Añadir dependencias a `pyproject.toml` y generar `poetry.lock`.
*   [ ] 1.13. Escribir tests unitarios y de integración (como mínimo para las partes críticas).
*   [ ] 1.14. **Tarea Separada:** Crear manifiestos Kubernetes (`configmap.yaml`, `deployment.yaml`, `service.yaml`).
*   [ ] 1.15. Configurar CI/CD para `reranker-service`.
*   [ ] 1.16. Desplegar y probar `reranker-service` en entorno de desarrollo/staging.

### Fase 2: Refactorización del `query-service`

*   [ ] 2.1. Eliminar código y dependencias de reranker local de `query-service`.
*   [ ] 2.2. Actualizar `pyproject.toml` de `query-service`.
*   [ ] 2.3. Añadir nueva configuración (`QUERY_RERANKER_SERVICE_URL`) a `query-service`.
*   [ ] 2.4. Modificar `AskQueryUseCase` para llamar al `reranker-service` vía HTTP.
*   [ ] 2.5. Actualizar tests unitarios y de integración de `query-service`.
*   [ ] 2.6. Actualizar `Dockerfile` de `query-service`.
*   [ ] 2.7. Actualizar ConfigMap K8s de `query-service`.
*   [ ] 2.8. Desplegar y probar `query-service` refactorizado en entorno de desarrollo/staging.

### Fase 3: Verificación y Despliegue Final

*   [ ] 3.1. Realizar pruebas E2E del flujo de consulta con reranking.
*   [ ] 3.2. Monitorizar rendimiento y logs en staging.
*   [ ] 3.3. Planificar y ejecutar despliegue a producción.
*   [ ] 3.4. Actualizar documentación general de la arquitectura del backend.