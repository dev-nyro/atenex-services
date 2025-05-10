# Atenex Query Service (Microservicio de Consulta) v0.3.1

## 1. Visión General

El **Query Service** es el microservicio responsable de manejar las consultas en lenguaje natural de los usuarios y gestionar el historial de conversaciones dentro de la plataforma Atenex. Esta versión ha sido refactorizada para adoptar **Clean Architecture** y un pipeline **Retrieval-Augmented Generation (RAG)** avanzado y configurable.

Sus funciones principales son:

1.  Recibir una consulta (`query`) y opcionalmente un `chat_id` vía API (`POST /api/v1/query/ask`), requiriendo headers `X-User-ID` y `X-Company-ID`.
2.  Gestionar el estado de la conversación (crear/continuar chat) usando `ChatRepositoryPort` (implementado por `PostgresChatRepository`).
3.  Detectar saludos simples y responder directamente, omitiendo el pipeline RAG.
4.  Guardar el mensaje del usuario en la tabla `messages` de PostgreSQL.
5.  Si no es un saludo, ejecutar el pipeline RAG orquestado por `AskQueryUseCase`:
    *   **Embedding de Consulta (Remoto):** Genera un vector para la consulta llamando al **Atenex Embedding Service** a través de `EmbeddingPort` (implementado por `RemoteEmbeddingAdapter`). La dimensión del embedding es configurada (`QUERY_EMBEDDING_DIMENSION`).
    *   **Recuperación Híbrida (Configurable):**
        *   **Búsqueda Densa:** Recupera chunks desde Milvus usando `MilvusAdapter` (`pymilvus`), filtrando por `company_id`.
        *   **Búsqueda Dispersa (BM25):** *Opcional* (habilitado por `QUERY_BM25_ENABLED`). Recupera chunks desde PostgreSQL usando `BM25sRetriever`, que construye un índice en memoria del contenido textual de los chunks de la compañía.
        *   **Fusión:** Combina resultados densos y dispersos usando Reciprocal Rank Fusion (RRF).
    *   **Reranking (Opcional):** Habilitado por `QUERY_RERANKER_ENABLED`. Reordena los chunks fusionados usando un modelo Cross-Encoder (`BGEReranker` con `sentence-transformers`).
    *   **Filtrado de Diversidad (Opcional):** Habilitado por `QUERY_DIVERSITY_FILTER_ENABLED`. Aplica un filtro (MMR o Stub) a los chunks reordenados.
    *   **Construcción del Prompt:** Crea el prompt para el LLM usando `PromptBuilder`, seleccionando una plantilla RAG (si hay chunks finales) o una general.
    *   **Generación de Respuesta:** Llama al LLM (Google Gemini) a través de `GeminiAdapter`.
6.  Guardar el mensaje del asistente (respuesta y fuentes de los chunks finales) en la tabla `messages`.
7.  Registrar la interacción completa (pregunta, respuesta, metadatos del pipeline, `chat_id`) en la tabla `query_logs` usando `LogRepositoryPort`.
8.  Proporcionar endpoints API (`GET /chats`, `GET /chats/{id}/messages`, `DELETE /chats/{id}`) para gestionar el historial, usando `ChatRepositoryPort`.

La autenticación sigue siendo manejada por el API Gateway.

## 2. Arquitectura General (Clean Architecture)

```mermaid
graph TD
    A[API Layer (FastAPI Endpoints)] --> UC{Application Layer (Use Cases)}
    UC -- Uses Ports --> I{Infrastructure Layer (Adapters)}

    subgraph I [Infrastructure Layer]
        direction LR
        P[(Persistence Adapters<br/>Postgres Repositories<br/>- ChatRepository<br/>- LogRepository<br/>- ChunkContentRepository)]
        V[(Vector Store Adapter<br/>MilvusAdapter)]
        S[(Sparse Retriever<br/>BM25sRetriever)]
        R[(Reranker Adapter<br/>BGEReranker)]
        F[(Diversity Filter<br/>MMR/StubFilter)]
        L[(LLM Adapter<br/>GeminiAdapter)]
        EmbSvcClient[(Embedding Client<br/>RemoteEmbeddingAdapter)]
    end

    subgraph UC [Application Layer]
        direction TB
        Ports[Ports (Interfaces)<br/>- ChatRepositoryPort<br/>- VectorStorePort<br/>- LLMPort<br/>- SparseRetrieverPort<br/>- EmbeddingPort<br/>...]
        UseCases[Use Cases<br/>- AskQueryUseCase]
    end

    subgraph D [Domain Layer]
         Models[Domain Models<br/>- RetrievedChunk<br/>- Chat<br/>- ChatMessage<br/>...]
    end


    A -- Calls --> UseCases
    UseCases -- Depends on --> Ports
    I -- Implements --> Ports
    UseCases -- Uses --> Models
    I -- Uses --> Models

    %% External Dependencies linked to Infrastructure %%
    P --> DB[(PostgreSQL<br/>'atenex' DB)]
    V --> Milvus[(Milvus<br/>Collection)]
    L --> LLM_API[("Google Gemini API")]
    S -- Reads content --> DB
    EmbSvcClient --> ExtEmbSvc["Atenex Embedding Service<br/>(HTTP API)"]


    style UC fill:#D1C4E9,stroke:#333,stroke-width:1px
    style A fill:#C8E6C9,stroke:#333,stroke-width:1px
    style I fill:#BBDEFB,stroke:#333,stroke-width:1px
    style D fill:#FFECB3,stroke:#333,stroke-width:1px
    style ExtEmbSvc fill:#D1E8FF,stroke:#4A90E2,color:#333
```
*Diagrama actualizado reflejando la llamada al `embedding-service`.*

## 3. Características Clave (v0.3.1)

*   **Arquitectura Limpia (Hexagonal):** Separación clara de responsabilidades.
*   **API RESTful:** Endpoints para consultas y gestión de chats.
*   **Pipeline RAG Avanzado y Configurable:**
    *   **Embedding de consulta remoto** vía `Atenex Embedding Service`.
    *   Recuperación Híbrida opcional (Dense: `MilvusAdapter` + Sparse: `BM25sRetriever`).
    *   Fusión Reciprocal Rank Fusion (RRF).
    *   Reranking opcional (`BGEReranker`).
    *   Filtrado de Diversidad opcional (MMR o Stub).
    *   Generación con Google Gemini (`GeminiAdapter`).
    *   Control de etapas del pipeline mediante variables de entorno.
*   **Manejo de Saludos:** Optimización para evitar RAG.
*   **Gestión de Historial de Chat:** Persistencia en PostgreSQL.
*   **Multi-tenancy Estricto.**
*   **Logging Estructurado y Detallado.**
*   **Configuración Centralizada.**
*   **Health Check** (incluye verificación de salud del `embedding-service`).

## 4. Pila Tecnológica Principal (v0.3.1)

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Arquitectura:** Clean Architecture / Hexagonal
*   **Cliente HTTP:** `httpx` (para `embedding-service`)
*   **Base de Datos Relacional (Cliente):** PostgreSQL (via `asyncpg`)
*   **Base de Datos Vectorial (Cliente):** Milvus (via `pymilvus`)
*   **Recuperación Dispersa:** `bm2s`
*   **Modelo Reranker:** `sentence-transformers` (`BAAI/bge-reranker-base`)
*   **Modelo LLM (Generación):** Google Gemini (via `google-generativeai`)
*   **Componentes Haystack:** `haystack-ai` (para `Document`, `PromptBuilder`)
*   **Despliegue:** Docker, Kubernetes

## 5. Estructura de la Codebase (v0.3.1)

```
app/
├── api                   # Capa API (FastAPI)
│   └── v1
│       ├── endpoints       # Controladores HTTP
│       ├── mappers.py
│       └── schemas.py      # DTOs (Pydantic)
├── application           # Capa Aplicación
│   ├── ports             # Interfaces (Puertos)
│   │   ├── embedding_port.py # NUEVO
│   │   └── ... (otros puertos)
│   └── use_cases         # Lógica de orquestación (Casos de Uso)
│       └── ask_query_use_case.py
├── core                  # Configuración central, logging
│   ├── config.py
│   └── logging_config.py
├── domain                # Capa Dominio
│   └── models.py         # Entidades y Value Objects
├── infrastructure        # Capa Infraestructura
│   ├── clients           # Clientes para servicios externos (NUEVO dir)
│   │   └── embedding_service_client.py # NUEVO
│   ├── embedding         # Adaptadores para EmbeddingPort (NUEVO dir)
│   │   └── remote_embedding_adapter.py # NUEVO
│   ├── filters
│   ├── llms
│   ├── persistence
│   ├── rerankers
│   ├── retrievers
│   └── vectorstores
├── main.py               # Entrypoint FastAPI, Lifespan, Middleware
├── dependencies.py
└── utils
    └── helpers.py
```

## 6. Configuración (Kubernetes - v0.3.1)

Gestionada mediante ConfigMap `query-service-config` y Secret `query-service-secrets` en `nyro-develop`.

### ConfigMap (`query-service-config`) - Claves Importantes

| Clave                            | Descripción                                                | Ejemplo (Valor Esperado)                                              |
| :------------------------------- | :--------------------------------------------------------- | :------------------------------------------------------------------ |
| `QUERY_LOG_LEVEL`                | Nivel de logging.                                          | `"INFO"`                                                              |
| **`QUERY_EMBEDDING_SERVICE_URL`**| **URL del Atenex Embedding Service.**                      | `"http://embedding-service.nyro-develop.svc.cluster.local:8003"`    |
| `QUERY_EMBEDDING_DIMENSION`      | Dimensión de embeddings (para Milvus y validación).        | `"384"`                                                               |
| `QUERY_BM25_ENABLED`             | Habilita/deshabilita retrieval BM25.                       | `"true"` / `"false"`                                                |
| `QUERY_RERANKER_ENABLED`         | Habilita/deshabilita reranking.                            | `"true"` / `"false"`                                                |
| `QUERY_RERANKER_MODEL_NAME`      | Modelo sentence-transformer para reranker.                 | `"BAAI/bge-reranker-base"`                                          |
| `QUERY_DIVERSITY_FILTER_ENABLED` | Habilita/deshabilita filtro diversidad (MMR/Stub).         | `"true"` / `"false"`                                                |
| `QUERY_DIVERSITY_LAMBDA`         | Parámetro lambda para MMR (si está habilitado).            | `"0.5"`                                                               |
| `QUERY_RETRIEVER_TOP_K`          | Nº inicial de chunks por retriever (denso/sparse).         | `"100"`                                                               |
| `QUERY_MAX_CONTEXT_CHUNKS`       | Nº máximo de chunks para el prompt del LLM.                | `"75"`                                                                |
| ... (otras claves)             | ...                                                        | ...                                                                 |

### Secret (`query-service-secrets`)

| Clave del Secreto     | Variable de Entorno Correspondiente | Descripción             |
| :-------------------- | :---------------------------------- | :---------------------- |
| `POSTGRES_PASSWORD`   | `QUERY_POSTGRES_PASSWORD`           | Contraseña PostgreSQL.  |
| `GEMINI_API_KEY`      | `QUERY_GEMINI_API_KEY`              | Clave API Google Gemini.|

## 7. API Endpoints (Sin cambios funcionales externos)

El prefijo base sigue siendo `/api/v1/query`. Los endpoints `/ask`, `/chats`, `/chats/{id}/messages`, `/chats/{id}` mantienen su firma externa.

## 8. Dependencias Externas Clave (v0.3.1)

*   **PostgreSQL:** Almacena logs, chats, mensajes y contenido de chunks.
*   **Milvus:** Almacena vectores de chunks y metadatos.
*   **Google Gemini API:** Generación de respuestas LLM.
*   **Atenex Embedding Service:** **NUEVO** - Proporciona embeddings de consulta.
*   **API Gateway:** Autenticación y enrutamiento.
*   **(Implícito) Modelos ML:** `sentence-transformers` (BGE Reranker) se descargan/cachean en el pod.

## 9. Pipeline RAG (Ejecutado por `AskQueryUseCase` - v0.3.1)

1.  **Embed Query:** Llama a `EmbeddingPort.embed_query` (que usa el `RemoteEmbeddingAdapter` para contactar al `Atenex Embedding Service`).
2.  **Coarse Retrieval:** (Sin cambios) Llamada concurrente a `VectorStorePort.search` y `SparseRetrieverPort.search`.
3.  **Fusion:** (Sin cambios) Combina resultados con RRF.
4.  **Content Fetch:** (Sin cambios) Obtiene contenido de chunks.
5.  **Reranking (Opcional):** (Sin cambios) Llama a `RerankerPort.rerank`.
6.  **Diversity Filtering (Opcional):** (Sin cambios) Llama a `DiversityFilterPort.filter`.
7.  **Build Prompt:** (Sin cambios) Construye el prompt con `PromptBuilder`.
8.  **Generate Answer:** (Sin cambios) Llama a `LLMPort.generate`.
9.  **Log Interaction:** (Sin cambios) Guarda detalles en `query_logs`.

## 10. Próximos Pasos y Consideraciones

*   **Rendimiento y Robustez del `embedding-service`:** Es crucial asegurar que el `embedding-service` sea performante y robusto, ya que ahora es una dependencia crítica para cada consulta.
*   **Testing:** Añadir tests de integración que cubran la interacción con el `embedding-service`.