# Plan de Creación de `reranker-service` y Refactorización Asociada

**Versión:** 1.2
**Fecha:** 2023-10-29
**Autor:** Software Engineering Senior (IA Asistente)

## 1. Visión General y Objetivos

Este documento detalla el plan para la creación de un nuevo microservicio, **`reranker-service`**, y la refactorización necesaria del **`query-service`** para integrarse con él. Los objetivos principales son:

1.  **Centralizar la Lógica de Reranking:** Crear un servicio dedicado responsable exclusivamente de reordenar documentos/chunks basados en su relevancia para una consulta dada.
2.  **Reducir la Huella del `query-service`:** Extraer el modelo de reranking y sus dependencias (ej. `sentence-transformers`, PyTorch) del `query-service`, disminuyendo significativamente el tamaño de su imagen Docker y su consumo de memoria.
3.  **Mejorar la Cohesión y Escalabilidad:** Permitir que el `reranker-service` se escale independientemente del `query-service`.
4.  **Mantener/Mejorar la Calidad del RAG:** Asegurar que la funcionalidad de reranking siga siendo efectiva y configurable.
5.  **Adherencia a Buenas Prácticas:** Implementar el nuevo servicio siguiendo principios de Arquitectura Limpia/Hexagonal y las mejores prácticas de desarrollo observadas en el ecosistema Atenex.

## 2. Creación del `reranker-service`

### 2.1. Responsabilidades y Funcionalidades

*   **Entrada:** Recibe una consulta (string) y una lista de documentos/chunks (cada uno con un ID, texto y metadatos opcionales).
*   **Procesamiento:** Utiliza un modelo Cross-Encoder (configurable, por defecto `BAAI/bge-reranker-base`) para calcular el score de relevancia de cada documento/chunk con respecto a la consulta.
*   **Salida:** Devuelve la lista de documentos/chunks originales, reordenados por su score de relevancia (de mayor a menor), junto con su score. Los metadatos originales de cada documento se preservan.
*   **Configurabilidad:** Permite configurar el modelo de reranking, el dispositivo de inferencia (CPU/GPU) y otros parámetros relevantes a través de variables de entorno.
*   **Eficiencia:** Carga el modelo en memoria al inicio (lifespan de FastAPI) para evitar cold starts en las peticiones. Las predicciones se ejecutan en un thread pool para no bloquear el event loop.
*   **Salud:** Proporciona un endpoint de health check que verifica el estado del modelo cargado.

### 2.2. Pila Tecnológica

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Motor de Reranking:** `sentence-transformers` (utilizando `transformers` y PyTorch como backend)
*   **Modelo por Defecto:** `BAAI/bge-reranker-base`
*   **Servidor ASGI/WSGI:** Uvicorn con Gunicorn
*   **Contenerización:** Docker
*   **Gestión de Dependencias:** Poetry
*   **Logging:** Structlog (JSON-formatted)

### 2.3. Arquitectura Hexagonal Propuesta

El `reranker-service` seguirá una arquitectura hexagonal para una clara separación de concerns, similar a otros servicios de Atenex:

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
         Models[Domain Models<br/>- DocumentToRerank<br/>- RerankedDocument<br/>- ModelInfo]
    end

    A -- Calls --> RerankDocsUseCase
    RerankDocsUseCase -- Depends on --> RMP
    ST_Adapter -- Uses --> ExternalModel[("HuggingFace Model\n(BAAI/bge-reranker-base via sentence-transformers)")]

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
│   │   └── models.py                       # Modelos de dominio (Pydantic)
│   ├── infrastructure/
│   │   └── rerankers/                      # Implementaciones del RerankerModelPort
│   │       └── sentence_transformer_adapter.py # Adaptador para sentence-transformers
│   ├── dependencies.py                     # Gestión de dependencias para FastAPI
│   └── main.py                             # Entrypoint FastAPI, lifespan (carga de modelo), middleware
├── tests/                                  # (Placeholder para tests futuros)
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
      "top_n": 10
    }
    ```
    *   `top_n` (Opcional): Si se proporciona, el servicio devolverá como máximo este número de documentos rerankeados.
*   **Response Body (200 OK - `RerankResponse`):**
    ```json
    {
      "data": {
        "reranked_documents": [
          {"id": "doc2", "text": "Otro texto relevante.", "score": 0.987, "metadata": {"source": "fileB.docx", "page": 5}},
          {"id": "doc1", "text": "Contenido del documento 1.", "score": 0.853, "metadata": {"source": "fileA.pdf", "page": 1}}
        ],
        "model_info": {
          "model_name": "BAAI/bge-reranker-base"
        }
      }
    }
    ```
    *Nota: Los documentos se devuelven ordenados por `score` descendente.*
*   **Errores Comunes:**
    *   `422 Unprocessable Entity`: Cuerpo de la solicitud inválido (ej. `query` vacío, `documents` lista vacía).
    *   `500 Internal Server Error`: Error inesperado durante el proceso de reranking.
    *   `503 Service Unavailable`: El modelo de reranking no está cargado o el servicio no está listo.

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
*   **Response Body (503 Service Unavailable - Modelo no cargado o error):**
    ```json
    {
      "status": "error",
      "service": "Atenex Reranker Service",
      "model_status": "error", // o "loading"
      "model_name": "BAAI/bge-reranker-base", // o el configurado
      "message": "Service is not ready or model loading failed."
    }
    ```

### 2.6. Configuración (Variables de Entorno)

Prefijo: `RERANKER_`

| Variable                             | Descripción                                                               | Por Defecto (`app/core/config.py`) | Gestionado por |
| :----------------------------------- | :------------------------------------------------------------------------ | :--------------------------------- | :------------- |
| `RERANKER_LOG_LEVEL`                 | Nivel de logging (DEBUG, INFO, WARNING, ERROR, CRITICAL).                 | `INFO`                             | ConfigMap      |
| `RERANKER_PORT`                      | Puerto en el que escuchará el servicio.                                   | `8004`                             | Deployment     |
| `RERANKER_MODEL_NAME`                | Nombre o ruta del modelo Cross-Encoder de Hugging Face.                   | `BAAI/bge-reranker-base`           | ConfigMap      |
| `RERANKER_MODEL_DEVICE`              | Dispositivo para la inferencia (`cpu`, `cuda`, `mps`, etc.). Auto-detectado por `sentence-transformers` si es `None`. | `cpu`                              | ConfigMap      |
| `RERANKER_HF_CACHE_DIR`              | (Opcional) Directorio para cachear los modelos de Hugging Face.           | `/app/.cache/huggingface`          | Deployment (Volumen) |
| `RERANKER_BATCH_SIZE`                | Tamaño del lote para la predicción del reranker.                          | `32`                               | ConfigMap      |
| `RERANKER_MAX_SEQ_LENGTH`            | Longitud máxima de secuencia para el modelo.                              | `512`                              | ConfigMap      |
| `RERANKER_WORKERS`                   | Número de workers Gunicorn.                                               | `2`                                | Deployment     |

## 3. Refactorización del `query-service`

### 3.1. Objetivos de la Refactorización

*   Eliminar la lógica y dependencias de reranking local del `query-service`.
*   Integrar el `query-service` para que llame al nuevo `reranker-service` vía HTTP cuando `QUERY_RERANKER_ENABLED` sea `true`.

### 3.2. Cambios Clave en `query-service`

1.  **Eliminación de Código y Dependencias:**
    *   Quitar el directorio `app/infrastructure/rerankers/` y su contenido (`bge_reranker.py`).
    *   Quitar la importación y uso de `RerankerPort` y `BGEReranker` del `AskQueryUseCase` y del `lifespan` en `main.py`.
    *   Remover `sentence-transformers` de `pyproject.toml` (asegurándose que no sea una dependencia transitiva de otro componente esencial).
    *   Eliminar las variables de entorno `QUERY_RERANKER_MODEL_NAME` de la configuración (`app/core/config.py`). La variable `QUERY_RERANKER_ENABLED` se mantiene.

2.  **Modificación del Flujo en `AskQueryUseCase` (`app/application/use_cases/ask_query_use_case.py`):**
    *   **Cliente HTTP:** Asegurar que un cliente HTTP asíncrono (`httpx.AsyncClient`) esté disponible. Este ya se inicializa en el `lifespan` del `query-service` para el `EmbeddingServiceClient` y puede ser reutilizado o se puede crear uno específico si se prefiere.
    *   **Llamada al `reranker-service` (si `self.settings.RERANKER_ENABLED` es `true`):**
        *   Después de la etapa de fusión (`_fetch_content_for_fused_results`) y antes del filtrado de diversidad (si está habilitado).
        *   **Preparar Payload:** Crear el cuerpo de la solicitud para el `reranker-service`:
            ```python
            # En AskQueryUseCase, dentro del bloque if self.settings.RERANKER_ENABLED:
            documents_to_rerank_payload = [
                {"id": doc.id, "text": doc.content, "metadata": doc.metadata}
                for doc in combined_chunks_with_content # Chunks después de fusión y fetch de contenido
            ]
            
            reranker_request_payload = {
                "query": query, # La consulta original del usuario
                "documents": documents_to_rerank_payload,
                # top_n puede ser self.settings.MAX_CONTEXT_CHUNKS o un nuevo setting
                # si se quiere que el reranker-service ya devuelva una cantidad limitada.
                # Por ahora, el diversity_filter/limitador final está en query-service.
                # "top_n": self.settings.MAX_CONTEXT_CHUNKS
            }
            ```
        *   **Realizar Llamada HTTP:**
            ```python
            # http_client es la instancia de httpx.AsyncClient
            # reranker_service_url = self.settings.RERANKER_SERVICE_URL
            try:
                response = await self.http_client.post(
                    f"{reranker_service_url}/api/v1/rerank",
                    json=reranker_request_payload,
                    # timeout=self.settings.QUERY_RERANKER_CLIENT_TIMEOUT # Si se define un timeout específico
                )
                response.raise_for_status() # Lanza excepción para errores HTTP 4xx/5xx
                reranker_response_json = response.json()
                
                # Mapear la respuesta
                # Es crucial tener un mapa id -> chunk original para reconstruir con metadatos y embeddings
                original_chunks_map = {chunk.id: chunk for chunk in combined_chunks_with_content}
                reranked_domain_chunks = []
                
                # La respuesta del reranker-service está anidada en "data"
                api_response_data = reranker_response_json.get("data", {})
                
                for reranked_doc_data in api_response_data.get("reranked_documents", []):
                    original_chunk = original_chunks_map.get(reranked_doc_data["id"])
                    if original_chunk:
                        # Crear un nuevo RetrievedChunk o actualizar el existente
                        # Es importante mantener el embedding original si MMRDiversityFilter lo usa después
                        updated_chunk = RetrievedChunk(
                            id=original_chunk.id,
                            content=original_chunk.content, # Usar el contenido original
                            score=reranked_doc_data["score"], # Score del reranker
                            metadata=original_chunk.metadata, # Metadatos originales
                            embedding=original_chunk.embedding # Mantener embedding original
                            # ... y otros campos relevantes del original_chunk
                        )
                        reranked_domain_chunks.append(updated_chunk)
                
                chunks_to_process_further = reranked_domain_chunks
                pipeline_stages_used.append(f"reranking (remote:{api_response_data.get('model_info',{}).get('model_name','unknown')})")

            except httpx.HTTPStatusError as e:
                exec_log.error(f"Reranker service request failed", status_code=e.response.status_code, response_text=e.response.text, exc_info=True)
                # Decidir si continuar sin reranking o fallar. Por ahora, continuar con chunks pre-reranking.
                pipeline_stages_used.append("reranking (remote_http_error)")
                # chunks_to_process_further se mantiene como combined_chunks_with_content
            except Exception as e:
                exec_log.error(f"Error calling reranker service", error_message=str(e), exc_info=True)
                pipeline_stages_used.append("reranking (remote_client_error)")
                # chunks_to_process_further se mantiene como combined_chunks_with_content
            ```
        *   Los `chunks_to_process_further` resultantes se pasarán al filtro de diversidad o al limitador final.

3.  **Nueva Configuración para `query-service` (`app/core/config.py`):**
    *   Añadir `QUERY_RERANKER_SERVICE_URL: AnyHttpUrl` (ej. `"http://reranker-service.nyro-develop.svc.cluster.local:80"`). El puerto `80` es el del Service K8s, no el `targetPort` del pod.
    *   (Opcional) `QUERY_RERANKER_CLIENT_TIMEOUT: int = 30` (timeout específico para esta llamada).

4.  **Actualizar `Dockerfile` y `pyproject.toml` de `query-service`:**
    *   Remover `sentence-transformers` y sus dependencias transitivas si ya no son necesarias por otros componentes.
    *   Reconstruir la imagen Docker.

## 4. Comunicación Inter-Servicio

*   **Protocolo:** HTTP/1.1 síncrono (request/response).
*   **URLs:** Comunicación interna del clúster (ej. `reranker-service.nyro-develop.svc.cluster.local`).
*   **Seguridad:** Se asume un entorno de clúster seguro. No se implementará autenticación adicional entre `query-service` y `reranker-service` en esta fase.

## 5. Checklist de Implementación y Refactorización

### Fase 1: Creación del `reranker-service`

*   [X] 1.1. Estructurar proyecto `reranker-service`.
*   [X] 1.2. Implementar `app/core/config.py` y `app/core/logging_config.py`.
*   [X] 1.3. Definir modelos Pydantic en `app/domain/models.py`.
*   [X] 1.4. Definir schemas Pydantic para API en `app/api/v1/schemas.py`.
*   [X] 1.5. Implementar `RerankerModelPort` en `app/application/ports/`.
*   [X] 1.6. Implementar `SentenceTransformerRerankerAdapter` en `app/infrastructure/rerankers/`.
*   [X] 1.7. Implementar `RerankDocumentsUseCase` en `app/application/use_cases/`.
*   [X] 1.8. Implementar `app/dependencies.py` para inyección de dependencias.
*   [X] 1.9. Implementar `app/main.py` (App FastAPI, lifespan, health check, middleware).
*   [X] 1.10. Implementar `app/api/v1/endpoints/rerank_endpoint.py`.
*   [X] 1.11. Escribir `Dockerfile` funcional.
*   [X] 1.12. Completar `pyproject.toml` con todas las dependencias y generar `poetry.lock`.
*   [X] 1.14. **Tarea Separada:** Crear manifiestos Kubernetes (`ConfigMap`, `Deployment`, `Service`).
*   [ ] 1.15. Configurar pipeline CI/CD para `reranker-service`.
*   [ ] 1.16. Desplegar y probar `reranker-service` en entorno de desarrollo/staging.

### Fase 2: Refactorización del `query-service`

*   [X] 2.1. Eliminar código y dependencias de reranker local de `query-service`.
*   [X] 2.2. Actualizar `pyproject.toml` de `query-service` y regenerar `poetry.lock`.
*   [X] 2.3. Añadir `QUERY_RERANKER_SERVICE_URL` a la configuración de `query-service`.
*   [X] 2.4. Modificar `AskQueryUseCase` en `query-service` para llamar al `reranker-service` vía HTTP.