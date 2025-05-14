# Atenex Query Service (Microservicio de Consulta) v0.3.3

## 1. Visión General

El **Query Service** es el microservicio responsable de manejar las consultas en lenguaje natural de los usuarios y gestionar el historial de conversaciones dentro de la plataforma Atenex. Esta versión ha sido refactorizada para adoptar **Clean Architecture** y un pipeline **Retrieval-Augmented Generation (RAG)** avanzado, configurable y distribuido.

Sus funciones principales son:

1.  Recibir una consulta (`query`) y opcionalmente un `chat_id` vía API (`POST /api/v1/query/ask`), requiriendo headers `X-User-ID` y `X-Company-ID`.
2.  Gestionar el estado de la conversación (crear/continuar chat) usando `ChatRepositoryPort` (implementado por `PostgresChatRepository`).
3.  Detectar saludos simples y responder directamente, omitiendo el pipeline RAG.
4.  Guardar el mensaje del usuario en la tabla `messages` de PostgreSQL.
5.  Si no es un saludo, ejecutar el pipeline RAG orquestado por `AskQueryUseCase`:
    *   **Embedding de Consulta (Remoto):** Genera un vector para la consulta llamando al **Atenex Embedding Service** a través de `EmbeddingPort` (implementado por `RemoteEmbeddingAdapter`).
    *   **Recuperación Híbrida (Configurable):**
        *   **Búsqueda Densa:** Recupera chunks desde Milvus usando `MilvusAdapter` (`pymilvus`), filtrando por `company_id`.
        *   **Búsqueda Dispersa (Remota):** *Opcional* (habilitado por `QUERY_BM25_ENABLED`). Recupera chunks realizando una llamada HTTP al **Atenex Sparse Search Service** (implementado por `RemoteSparseRetrieverAdapter`). Este servicio externo se encarga de la lógica BM25.
        *   **Fusión:** Combina resultados densos y dispersos usando Reciprocal Rank Fusion (RRF). El contenido de los chunks recuperados por búsqueda dispersa (que solo devuelven ID y score) se obtiene de PostgreSQL.
    *   **Reranking (Remoto Opcional):** Habilitado por `QUERY_RERANKER_ENABLED`. Reordena los chunks fusionados llamando al **Atenex Reranker Service**.
    *   **Filtrado de Diversidad (Opcional):** Habilitado por `QUERY_DIVERSITY_FILTER_ENABLED`. Aplica un filtro (MMR o Stub) a los chunks reordenados.
    *   **MapReduce (Opcional):** Si el número de chunks después del filtrado supera `QUERY_MAPREDUCE_ACTIVATION_THRESHOLD` y `QUERY_MAPREDUCE_ENABLED` es true, se activa un flujo MapReduce:
        *   **Map:** Los chunks se dividen en lotes. Para cada lote, se genera un prompt (usando `map_prompt_template.txt`) que instruye al LLM para extraer información relevante.
        *   **Reduce:** Las respuestas de la fase Map se concatenan. Se genera un prompt final (usando `reduce_prompt_template_v2.txt`) que instruye al LLM para sintetizar una respuesta final basada en estos extractos y el historial del chat.
    *   **Construcción del Prompt (Direct RAG):** Si MapReduce no se activa, se crea el prompt para el LLM usando `PromptBuilder` y la plantilla `rag_template_gemini_v2.txt` (o `general_template_gemini_v2.txt` si no hay chunks).
    *   **Generación de Respuesta:** Llama al LLM (Google Gemini) a través de `GeminiAdapter`. Se espera una respuesta JSON estructurada (`RespuestaEstructurada`).
6.  Manejar la respuesta JSON del LLM, guardando el mensaje del asistente (campo `respuesta_detallada` y `fuentes_citadas`) en la tabla `messages` de PostgreSQL.
7.  Registrar la interacción completa (pregunta, respuesta, metadatos del pipeline, `chat_id`) en la tabla `query_logs` usando `LogRepositoryPort`.
8.  Proporcionar endpoints API (`GET /chats`, `GET /chats/{id}/messages`, `DELETE /chats/{id}`) para gestionar el historial, usando `ChatRepositoryPort`.

La autenticación sigue siendo manejada por el API Gateway.

## 2. Arquitectura General (Clean Architecture & Microservicios)

```mermaid
graph TD
    A[API Layer (FastAPI Endpoints)] --> UC[Application Layer (Use Cases)]
    UC -- Uses Ports --> I[Infrastructure Layer (Adapters & Clients)]

    subgraph I [Infrastructure Layer]
        direction LR
        Persistence[(Persistence Adapters<br/>- PostgresChatRepository<br/>- PostgresLogRepository<br/>- PostgresChunkContentRepository)]
        VectorStore[(Vector Store Adapter<br/>- MilvusAdapter)]
        SparseSearchClient[(Sparse Search Client<br/>- SparseSearchServiceClient)]
        EmbeddingClient[(Embedding Client<br/>- EmbeddingServiceClient)]
        RerankerClient[(Reranker Client<br/>- HTTPX calls in UseCase)]
        LLMAdapter[(LLM Adapter<br/>- GeminiAdapter)]
        Filters[(Diversity Filter<br/>- MMRDiversityFilter)]
    end
    
    subgraph UC [Application Layer]
        direction TB
        Ports[Ports (Interfaces)<br/>- ChatRepositoryPort<br/>- VectorStorePort<br/>- LLMPort<br/>- SparseRetrieverPort<br/>- EmbeddingPort<br/>- RerankerPort<br/>- DiversityFilterPort<br/>- ChunkContentRepositoryPort]
        UseCases[Use Cases<br/>- AskQueryUseCase]
    end

    subgraph D [Domain Layer]
         Models[Domain Models<br/>- RetrievedChunk<br/>- Chat<br/>- ChatMessage<br/>- RespuestaEstructurada]
    end
    
    A -- Calls --> UseCases
    UseCases -- Depends on --> Ports
    I -- Implements / Uses --> Ports 
    UseCases -- Uses --> Models
    I -- Uses --> Models

    %% External Dependencies linked to Infrastructure/Adapters %%
    Persistence --> DB[(PostgreSQL 'atenex' DB)]
    VectorStore --> MilvusDB[(Milvus / Zilliz Cloud)]
    LLMAdapter --> GeminiAPI[("Google Gemini API")]
    EmbeddingClient --> EmbeddingSvc["Atenex Embedding Service"]
    SparseSearchClient --> SparseSvc["Atenex Sparse Search Service"]
    RerankerClient --> RerankerSvc["Atenex Reranker Service"]
    

    style UC fill:#D1C4E9,stroke:#333,stroke-width:1px
    style A fill:#C8E6C9,stroke:#333,stroke-width:1px
    style I fill:#BBDEFB,stroke:#333,stroke-width:1px
    style D fill:#FFECB3,stroke:#333,stroke-width:1px
    style EmbeddingSvc fill:#D1E8FF,stroke:#4A90E2,color:#333
    style SparseSvc fill:#E0F2F7,stroke:#00ACC1,color:#333
    style RerankerSvc fill:#FFF9C4,stroke:#FBC02D,color:#333
```

## 3. Características Clave (v0.3.3)

*   **Arquitectura Limpia (Hexagonal):** Separación clara de responsabilidades.
*   **API RESTful:** Endpoints para consultas y gestión de chats.
*   **Pipeline RAG Avanzado y Configurable:**
    *   **Embedding de consulta remoto** vía `Atenex Embedding Service`.
    *   **Recuperación Híbrida:** Dense (`MilvusAdapter`) + Sparse (llamada remota al `Atenex Sparse Search Service`).
    *   Fusión Reciprocal Rank Fusion (RRF).
    *   **Reranking remoto opcional** vía `Atenex Reranker Service`.
    *   Filtrado de Diversidad opcional (MMR o Stub).
    *   **MapReduce opcional** para manejar grandes cantidades de chunks recuperados.
    *   Generación con Google Gemini (`GeminiAdapter`), esperando respuesta JSON estructurada.
    *   Control de etapas del pipeline mediante variables de entorno.
*   **Manejo de Saludos:** Optimización para evitar RAG.
*   **Gestión de Historial de Chat:** Persistencia en PostgreSQL.
*   **Multi-tenancy Estricto.**
*   **Logging Estructurado y Detallado.**
*   **Configuración Centralizada.**
*   **Health Check Robusto** (incluye verificación de salud de servicios dependientes).

## 4. Pila Tecnológica Principal (v0.3.3)

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Arquitectura:** Clean Architecture / Hexagonal
*   **Cliente HTTP:** `httpx` (para servicios externos: Embedding, Sparse Search, Reranker)
*   **Base de Datos Relacional (Cliente):** PostgreSQL (via `asyncpg`)
*   **Base de Datos Vectorial (Cliente):** Milvus (via `pymilvus`)
*   **Modelo LLM (Generación):** Google Gemini (via `google-generativeai`)
*   **Componentes Haystack:** `haystack-ai` (para `Document`, `PromptBuilder`)
*   **Despliegue:** Docker, Kubernetes

## 5. Estructura de la Codebase (v0.3.3)

```
app/
├── api                   # Capa API (FastAPI)
│   └── v1
│       ├── endpoints       # Controladores HTTP (query.py, chat.py)
│       ├── mappers.py      # (Opcional, para mapeo DTO <-> Dominio)
│       └── schemas.py      # DTOs (Pydantic)
├── application           # Capa Aplicación
│   ├── ports             # Interfaces (Puertos)
│   │   ├── embedding_port.py
│   │   ├── llm_port.py
│   │   ├── repository_ports.py
│   │   ├── retrieval_ports.py  # Incluye SparseRetrieverPort, RerankerPort, DiversityFilterPort
│   │   └── vector_store_port.py
│   └── use_cases         # Lógica de orquestación
│       └── ask_query_use_case.py
├── core                  # Configuración central, logging
│   ├── config.py
│   └── logging_config.py
├── domain                # Capa Dominio
│   └── models.py         # Entidades y Value Objects (Chat, Message, RetrievedChunk, RespuestaEstructurada)
├── infrastructure        # Capa Infraestructura
│   ├── clients           # Clientes para servicios externos
│   │   ├── embedding_service_client.py
│   │   └── sparse_search_service_client.py # NUEVO
│   ├── embedding         # Adaptador para EmbeddingPort
│   │   └── remote_embedding_adapter.py
│   ├── filters           # Adaptador para DiversityFilterPort
│   │   └── diversity_filter.py
│   ├── llms              # Adaptador para LLMPort
│   │   └── gemini_adapter.py
│   ├── persistence       # Adaptadores para RepositoryPorts
│   │   ├── postgres_connector.py
│   │   └── postgres_repositories.py
│   ├── retrievers        # Adaptador para SparseRetrieverPort
│   │   └── remote_sparse_retriever_adapter.py # NUEVO (reemplaza bm25_retriever.py)
│   └── vectorstores      # Adaptador para VectorStorePort
│       └── milvus_adapter.py
├── main.py               # Entrypoint FastAPI, Lifespan, Middleware
├── dependencies.py       # Gestión de dependencias (singletons)
├── prompts/              # Plantillas de prompts para LLM
│   ├── general_template_gemini_v2.txt
│   ├── map_prompt_template.txt
│   ├── rag_template_gemini_v2.txt
│   └── reduce_prompt_template_v2.txt
└── utils
    └── helpers.py        # Funciones de utilidad
```

## 6. Configuración (Variables de Entorno y Kubernetes - v0.3.3)

Gestionada mediante ConfigMap `query-service-config` y Secret `query-service-secrets` en el namespace `nyro-develop`.

### ConfigMap (`query-service-config`) - Claves Relevantes

| Clave                                  | Descripción                                                                    | Ejemplo (Valor Esperado)                                                  |
| :------------------------------------- | :----------------------------------------------------------------------------- | :------------------------------------------------------------------------ |
| `QUERY_LOG_LEVEL`                      | Nivel de logging.                                                              | `"INFO"`                                                                  |
| `QUERY_EMBEDDING_SERVICE_URL`          | URL del Atenex Embedding Service.                                              | `"http://embedding-service.nyro-develop.svc.cluster.local:80"`        |
| `QUERY_EMBEDDING_CLIENT_TIMEOUT`       | Timeout para llamadas al Embedding Service.                                    | `"30"`                                                                    |
| `QUERY_EMBEDDING_DIMENSION`            | Dimensión de embeddings (para Milvus y validación).                            | `"384"`                                                                   |
| **`QUERY_BM25_ENABLED`**               | **Habilita/deshabilita el paso de búsqueda dispersa (llamada al servicio remoto).** | `"true"` / `"false"`                                                    |
| **`QUERY_SPARSE_SEARCH_SERVICE_URL`**  | **URL del Atenex Sparse Search Service.**                                        | `"http://sparse-search-service.nyro-develop.svc.cluster.local:80"`    |
| **`QUERY_SPARSE_SEARCH_CLIENT_TIMEOUT`** | **Timeout para llamadas al Sparse Search Service.**                            | `"30"`                                                                    |
| `QUERY_RERANKER_ENABLED`               | Habilita/deshabilita reranking remoto.                                         | `"true"` / `"false"`                                                    |
| `QUERY_RERANKER_SERVICE_URL`           | URL del Atenex Reranker Service.                                               | `"http://reranker-service.nyro-develop.svc.cluster.local:80"`         |
| `QUERY_RERANKER_CLIENT_TIMEOUT`        | Timeout para llamadas al Reranker Service.                                     | `"30"`                                                                    |
| `QUERY_DIVERSITY_FILTER_ENABLED`       | Habilita/deshabilita filtro diversidad (MMR/Stub).                             | `"true"` / `"false"`                                                    |
| `QUERY_DIVERSITY_LAMBDA`               | Parámetro lambda para MMR (si está habilitado).                                | `"0.5"`                                                                   |
| `QUERY_RETRIEVER_TOP_K`                | Nº inicial de chunks por retriever (denso/disperso).                           | `"100"`                                                                   |
| `QUERY_MAX_CONTEXT_CHUNKS`             | Nº máximo de chunks para el prompt del LLM (después de RAG).                   | `"75"`                                                                    |
| `QUERY_MAPREDUCE_ENABLED`              | Habilita/deshabilita el flujo MapReduce.                                       | `"true"`                                                                  |
| `QUERY_MAPREDUCE_ACTIVATION_THRESHOLD` | Nº de chunks para activar MapReduce.                                           | `"25"`                                                                    |
| `QUERY_MAPREDUCE_CHUNK_BATCH_SIZE`     | Tamaño de lote para la fase Map.                                               | `"5"`                                                                     |
| ... (otras claves DB, Milvus, Gemini)  | ...                                                                            | ...                                                                       |

### Secret (`query-service-secrets`)

| Clave del Secreto     | Variable de Entorno Correspondiente en la App | Descripción             |
| :-------------------- | :------------------------------------------ | :---------------------- |
| `POSTGRES_PASSWORD`   | `QUERY_POSTGRES_PASSWORD`                   | Contraseña PostgreSQL.  |
| `GEMINI_API_KEY`      | `QUERY_GEMINI_API_KEY`                      | Clave API Google Gemini.|
| `ZILLIZ_API_KEY`      | `QUERY_ZILLIZ_API_KEY`                      | Clave API Zilliz Cloud. |

## 7. API Endpoints

El prefijo base sigue siendo `/api/v1/query`. Los endpoints mantienen su firma externa:
*   `POST /ask`: Procesa una consulta de usuario, gestiona el chat y devuelve una respuesta.
*   `GET /chats`: Lista los chats del usuario.
*   `GET /chats/{chat_id}/messages`: Obtiene los mensajes de un chat específico.
*   `DELETE /chats/{chat_id}`: Elimina un chat.
*   `GET /health`: (Interno del pod, usado por K8s) Endpoint de salud.

## 8. Dependencias Externas Clave (v0.3.3)

*   **PostgreSQL:** Almacena logs, chats, mensajes y contenido de chunks.
*   **Milvus / Zilliz Cloud:** Almacena vectores de chunks y metadatos para búsqueda densa.
*   **Google Gemini API:** Generación de respuestas LLM.
*   **Atenex Embedding Service:** Proporciona embeddings de consulta (servicio remoto).
*   **Atenex Sparse Search Service:** Proporciona resultados de búsqueda dispersa (BM25) (servicio remoto).
*   **Atenex Reranker Service:** Proporciona reranking de chunks (servicio remoto, opcional).
*   **API Gateway:** Autenticación y enrutamiento.

## 9. Pipeline RAG (Ejecutado por `AskQueryUseCase` - v0.3.3)

1.  **Chat Management:** Crear o continuar chat, guardar mensaje de usuario.
2.  **Greeting Check:** Si es un saludo, responder directamente.
3.  **Embed Query (Remoto):** Llama a `EmbeddingPort.embed_query` (que usa `RemoteEmbeddingAdapter` para contactar al `Atenex Embedding Service`).
4.  **Coarse Retrieval:**
    *   **Dense Retrieval:** Llamada a `VectorStorePort.search` (Milvus).
    *   **Sparse Retrieval (Remoto Opcional):** Si `QUERY_BM25_ENABLED` es true, llamada a `SparseRetrieverPort.search` (que usa `RemoteSparseRetrieverAdapter` para contactar al `Atenex Sparse Search Service`).
5.  **Fusion (RRF):** Combina resultados densos y dispersos.
6.  **Content Fetch:** Si la búsqueda dispersa (o densa, si devuelve solo IDs) no proveyó contenido, se obtiene de PostgreSQL usando `ChunkContentRepositoryPort`.
7.  **Reranking (Remoto Opcional):** Si `QUERY_RERANKER_ENABLED` es true, se envían los chunks fusionados (con contenido) al `Atenex Reranker Service` vía HTTP.
8.  **Diversity Filtering (Opcional):** Si `QUERY_DIVERSITY_FILTER_ENABLED` es true, se aplica un filtro MMR (o Stub) a los chunks (reordenados o fusionados).
9.  **MapReduce o Direct RAG Decision:**
    *   Si `QUERY_MAPREDUCE_ENABLED` es true y el número de chunks supera `QUERY_MAPREDUCE_ACTIVATION_THRESHOLD`:
        *   **Map Phase:** Los chunks se procesan en lotes. Para cada lote, se genera un prompt de mapeo y se llama al LLM para extraer información relevante.
        *   **Reduce Phase:** Las extracciones de la fase Map se concatenan. Se genera un prompt de reducción (que incluye historial y pregunta original) y se llama al LLM para sintetizar la respuesta final en formato JSON (`RespuestaEstructurada`).
    *   **Direct RAG (Default):**
        *   **Build Prompt:** Construye el prompt (RAG o general) con `PromptBuilder` usando los chunks finales y el historial de chat.
        *   **Generate Answer:** Llama a `LLMPort.generate` (Gemini) esperando una respuesta JSON (`RespuestaEstructurada`).
10. **Handle LLM Response:** Parsea la respuesta JSON del LLM.
11. **Save Assistant Message:** Guarda la respuesta del asistente y las fuentes en la base de datos (PostgreSQL).
12. **Log Interaction:** Registra la interacción completa en la tabla `query_logs`.
13. **Return Response:** Devuelve la respuesta al usuario.

## 10. Próximos Pasos y Consideraciones

*   **Resiliencia y Fallbacks:** Reforzar la lógica de fallback si alguno de los servicios remotos (Embedding, Sparse Search, Reranker) no está disponible o falla. Actualmente, el pipeline intenta continuar con la información disponible.
*   **Testing de Integración:** Asegurar tests de integración exhaustivos que cubran las interacciones con todos los servicios remotos.
*   **Observabilidad:** Mejorar el tracing distribuido entre todos los microservicios para facilitar el debugging y monitoreo de rendimiento.
*   **Optimización de Llamadas HTTP:** Asegurar que el `httpx.AsyncClient` global se reutilice eficientemente para las llamadas a los servicios de Reranker y otros futuros, mientras que los clientes específicos (Embedding, SparseSearch) manejan sus propias instancias con timeouts específicos.