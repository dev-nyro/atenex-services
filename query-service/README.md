# Atenex Query Service (Microservicio de Consulta) v1.3.5

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
        *   **Fusión:** Combina resultados densos y dispersos usando Reciprocal Rank Fusion (RRF).
        *   **Obtención de Contenido:** Si los chunks fusionados (especialmente los de búsqueda dispersa) no tienen contenido, se obtiene de PostgreSQL utilizando `ChunkContentRepositoryPort`.
    *   **Reranking (Remoto Opcional):** Habilitado por `QUERY_RERANKER_ENABLED`. Reordena los chunks fusionados (que ya tienen contenido) llamando al **Atenex Reranker Service** vía HTTP.
    *   **Filtrado de Diversidad (Opcional):** Habilitado por `QUERY_DIVERSITY_FILTER_ENABLED`. Aplica un filtro (MMR o Stub) a los chunks reordenados, usando embeddings recuperados de Milvus o generados ad-hoc si es necesario.
    *   **MapReduce (Opcional):** Si `QUERY_MAPREDUCE_ENABLED` es true, el total de tokens de los chunks después del filtrado supera `QUERY_MAX_PROMPT_TOKENS` y hay más de 1 chunk, se activa un flujo MapReduce:
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
        Ports[Ports (Interfaces)<br/>- ChatRepositoryPort<br/>- VectorStorePort<br/>- LLMPort<br/>- SparseRetrieverPort<br/>- RerankerPort<br/>- DiversityFilterPort<br/>- ChunkContentRepositoryPort]
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

## 3. Características Clave (v1.3.5)

*   **Arquitectura Limpia (Hexagonal):** Separación clara de responsabilidades.
*   **API RESTful:** Endpoints para consultas y gestión de chats.
*   **Pipeline RAG Avanzado y Configurable:**
    *   **Embedding de consulta remoto** vía `Atenex Embedding Service`.
    *   **Recuperación Híbrida:** Dense (`MilvusAdapter`) + Sparse (llamada remota al `Atenex Sparse Search Service`).
    *   Fusión Reciprocal Rank Fusion (RRF).
    *   **Obtención de Contenido** para chunks de la DB si es necesario.
    *   **Reranking remoto opcional** vía `Atenex Reranker Service` usando un cliente HTTP global.
    *   Filtrado de Diversidad opcional (MMR o Stub).
    *   **MapReduce opcional** para manejar grandes cantidades de tokens de chunks recuperados.
    *   Generación con Google Gemini (`GeminiAdapter`), esperando respuesta JSON estructurada.
    *   Control de etapas del pipeline mediante variables de entorno.
*   **Manejo de Saludos:** Optimización para evitar RAG.
*   **Gestión de Historial de Chat:** Persistencia en PostgreSQL.
*   **Multi-tenancy Estricto.**
*   **Logging Estructurado y Detallado.**
*   **Configuración Centralizada.**
*   **Health Check Robusto** (incluye verificación de salud de servicios dependientes).

## 4. Pila Tecnológica Principal (v1.3.5)

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Arquitectura:** Clean Architecture / Hexagonal
*   **Cliente HTTP:** `httpx` (global, usado para servicios externos como Reranker Service; los clientes específicos como EmbeddingServiceClient y SparseSearchServiceClient usan sus propias instancias de httpx)
*   **Base de Datos Relacional (Cliente):** PostgreSQL (via `asyncpg`)
*   **Base de Datos Vectorial (Cliente):** Milvus (via `pymilvus`)
*   **Modelo LLM (Generación):** Google Gemini (via `google-generativeai`)
*   **Componentes Haystack:** `haystack-ai` (para `Document`, `PromptBuilder`)
*   **Tokenización (Conteo):** `tiktoken`
*   **Despliegue:** Docker, Kubernetes

## 5. Estructura de la Codebase (v1.3.5)

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
├── dependencies.py       # Gestión de dependencias (singletons para use case)
├── domain                # Capa Dominio
│   └── models.py         # Entidades y Value Objects (Chat, Message, RetrievedChunk, RespuestaEstructurada)
├── infrastructure        # Capa Infraestructura
│   ├── clients           # Clientes para servicios externos
│   │   ├── embedding_service_client.py
│   │   └── sparse_search_service_client.py
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
│   │   └── remote_sparse_retriever_adapter.py
│   └── vectorstores      # Adaptador para VectorStorePort
│       └── milvus_adapter.py
├── main.py               # Entrypoint FastAPI, Lifespan, Middleware
├── prompts/              # Plantillas de prompts para LLM
│   ├── general_template_gemini_v2.txt
│   ├── map_prompt_template.txt
│   ├── rag_template_gemini_v2.txt
│   └── reduce_prompt_template_v2.txt
└── utils
    └── helpers.py        # Funciones de utilidad
```

## 6. Configuración (Variables de Entorno y Kubernetes - v1.3.5)

Gestionada mediante ConfigMap `query-service-config` y Secret `query-service-secrets` en el namespace `nyro-develop` (o equivalentes en el entorno de ejecución). El prefijo de las variables de entorno es `QUERY_`.

### ConfigMap (`query-service-config`) - Claves Relevantes

| Clave                                  | Descripción                                                                         | Ejemplo/Valor por Defecto (de `config.py`)                         |
| :------------------------------------- | :---------------------------------------------------------------------------------- | :------------------------------------------------------------------------ |
| `LOG_LEVEL`                            | Nivel de logging (DEBUG, INFO, WARNING, ERROR, CRITICAL).                         | `"INFO"`                                                                  |
| `EMBEDDING_SERVICE_URL`                | URL del Atenex Embedding Service.                                                   | `"http://embedding-service.nyro-develop.svc.cluster.local:80"`        |
| `EMBEDDING_CLIENT_TIMEOUT`             | Timeout para llamadas al Embedding Service.                                         | `"30"` (segundos)                                                       |
| `EMBEDDING_DIMENSION`                  | Dimensión de embeddings (para Milvus y validación).                                 | `"1536"` (coincide con OpenAI `text-embedding-3-small`)                 |
| **`BM25_ENABLED`**                     | **Habilita/deshabilita el paso de búsqueda dispersa (llamada al servicio remoto).**  | `true` / `false` (Default: `true`)                                      |
| **`SPARSE_SEARCH_SERVICE_URL`**        | **URL del Atenex Sparse Search Service.**                                             | `"http://sparse-search-service.nyro-develop.svc.cluster.local:80"`    |
| **`SPARSE_SEARCH_CLIENT_TIMEOUT`**     | **Timeout para llamadas al Sparse Search Service.**                                   | `"30"` (segundos)                                                       |
| `RERANKER_ENABLED`                     | Habilita/deshabilita reranking remoto.                                              | `true` / `false` (Default: `true`)                                      |
| `RERANKER_SERVICE_URL`                 | URL del Atenex Reranker Service.                                                    | `"http://reranker-service.nyro-develop.svc.cluster.local:80"`         |
| `RERANKER_CLIENT_TIMEOUT`              | Timeout para llamadas al Reranker Service.                                          | `"30"` (segundos)                                                       |
| `DIVERSITY_FILTER_ENABLED`             | Habilita/deshabilita filtro diversidad (MMR/Stub).                                  | `true` / `false` (Default: `false`)                                     |
| `QUERY_DIVERSITY_LAMBDA`               | Parámetro lambda para MMR (si está habilitado).                                     | `"0.5"` (Default)                                                         |
| `RETRIEVER_TOP_K`                      | Nº inicial de chunks por retriever (denso/disperso).                                | `"200"` (Default)                                                         |
| `MAX_CONTEXT_CHUNKS`                   | Nº máximo de chunks para el prompt del LLM (después de RAG y filtrado de diversidad) o para el filtro de diversidad. | `"200"` (Default, no 75)                                                  |
| `MAX_PROMPT_TOKENS`                    | Umbral de tokens para activar MapReduce (si está habilitado).                       | `"500000"` (Default)                                                    |
| `MAPREDUCE_ENABLED`                    | Habilita/deshabilita el flujo MapReduce.                                            | `true` / `false` (Default: `true`)                                      |
| `MAPREDUCE_ACTIVATION_THRESHOLD_CHUNKS`| Umbral de chunks (solo como guía para logging, no es trigger principal).            | `"25"` (Default)                                                          |
| `MAPREDUCE_CHUNK_BATCH_SIZE`           | Tamaño de lote para la fase Map de MapReduce.                                       | `"5"` (Default)                                                           |
| `GEMINI_MODEL_NAME`                    | Nombre del modelo Google Gemini a utilizar.                                         | `"gemini-1.5-flash-latest"` (Default)                                   |
| `TIKTOKEN_ENCODING_NAME`               | Nombre del encoding TikToken para contar tokens.                                    | `"cl100k_base"` (Default)                                               |
| ... (otras claves de DB, Milvus, HTTP) | ...                                                                                 | ...                                                                       |

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
*   `GET /`: Endpoint raíz para liveness/readiness del servicio.

El antiguo `/health` ahora está integrado en `/`.

## 8. Dependencias Externas Clave (v1.3.5)

*   **PostgreSQL:** Almacena logs de consultas, chats, mensajes y contenido de chunks para búsquedas.
*   **Milvus / Zilliz Cloud:** Almacena vectores de chunks y metadatos clave para búsqueda densa.
*   **Google Gemini API:** Generación de respuestas LLM.
*   **Atenex Embedding Service:** Proporciona embeddings de consulta (servicio remoto).
*   **Atenex Sparse Search Service:** Proporciona resultados de búsqueda dispersa (BM25) (servicio remoto, opcional).
*   **Atenex Reranker Service:** Proporciona reranking de chunks (servicio remoto, opcional).
*   **API Gateway:** Autenticación y enrutamiento de las solicitudes de usuario.

## 9. Pipeline RAG (Ejecutado por `AskQueryUseCase` - v1.3.5)

1.  **Gestión de Chat:** Crear o continuar chat. Guardar mensaje del usuario en PostgreSQL.
2.  **Verificación de Saludo:** Si la consulta es un saludo simple (ej. "hola"), responder directamente sin RAG.
3.  **Embedding de Consulta (Remoto):** Llama a `EmbeddingPort.embed_query` (implementado por `RemoteEmbeddingAdapter`) para obtener el vector de la consulta desde `Atenex Embedding Service`.
4.  **Recuperación Inicial (Coarse Retrieval):**
    *   **Búsqueda Densa (Vectorial):** Llama a `VectorStorePort.search` (implementado por `MilvusAdapter`) para obtener chunks relevantes de Milvus.
    *   **Búsqueda Dispersa (Keyword - Remoto Opcional):** Si `QUERY_BM25_ENABLED` es true, llama a `SparseRetrieverPort.search` (implementado por `RemoteSparseRetrieverAdapter`) para obtener chunks desde `Atenex Sparse Search Service`.
5.  **Fusión (RRF):** Combina los resultados de la búsqueda densa y dispersa usando Reciprocal Rank Fusion (RRF) para obtener una lista fusionada de IDs de chunks y sus scores.
6.  **Obtención de Contenido (Content Fetch):** Para los chunks de la lista fusionada (especialmente aquellos que solo tienen ID y score, como los de la búsqueda dispersa, o si Milvus no devolvió contenido completo), se recupera su contenido textual y metadatos adicionales desde PostgreSQL usando `ChunkContentRepositoryPort.get_chunk_contents_by_ids`. Los chunks sin contenido se descartan.
7.  **Reranking (Remoto Opcional):** Si `QUERY_RERANKER_ENABLED` es true, los chunks fusionados (con contenido) se envían al `Atenex Reranker Service` vía una llamada HTTP. El servicio de reranking reordena los chunks y devuelve la lista reordenada.
8.  **Filtrado de Diversidad (Opcional):** Si `QUERY_DIVERSITY_FILTER_ENABLED` es true, se aplica un filtro MMR (Maximal Marginal Relevance) o un filtro Stub (simple truncamiento) a los chunks (reordenados o de la etapa anterior). El filtro MMR utiliza los embeddings de los chunks (obtenidos de Milvus o, como fallback, generados por `Atenex Embedding Service`) para asegurar diversidad además de relevancia. Los chunks se limitan a `QUERY_MAX_CONTEXT_CHUNKS`.
9.  **Decisión: MapReduce o Direct RAG:**
    *   Se calcula el número total de tokens de los chunks resultantes.
    *   Si `QUERY_MAPREDUCE_ENABLED` es true, Y el total de tokens excede `QUERY_MAX_PROMPT_TOKENS`, Y hay más de un chunk, se activa el flujo **MapReduce**:
        *   **Fase Map:** Los chunks se dividen en lotes. Para cada lote, se usa `map_prompt_template.txt` para instruir al LLM (Gemini) que extraiga información relevante.
        *   **Fase Reduce:** Las extracciones de la fase Map se concatenan. Se usa `reduce_prompt_template_v2.txt` para instruir al LLM que sintetice la respuesta final en formato JSON (`RespuestaEstructurada`), basándose en estas extracciones y el historial del chat.
    *   **Flujo Direct RAG (Predeterminado):**
        *   **Construcción de Prompt:** Se seleccionan los N chunks más relevantes (hasta `QUERY_MAX_CONTEXT_CHUNKS`). Se usa `PromptBuilder` con `rag_template_gemini_v2.txt` (o `general_template_gemini_v2.txt` si no hay chunks) para crear el prompt, incorporando los chunks seleccionados y el historial de chat.
        *   **Generación de Respuesta:** Se llama a `LLMPort.generate` (implementado por `GeminiAdapter`) con el prompt, esperando una respuesta JSON estructurada según el schema `RespuestaEstructurada`.
10. **Manejo de Respuesta del LLM:** Se parsea la respuesta JSON del LLM.
11. **Guardar Mensaje del Asistente:** Se guarda la respuesta del asistente (`respuesta_detallada`) y las fuentes citadas (`fuentes_citadas`) en la tabla `messages` de PostgreSQL.
12. **Loguear Interacción:** Se registra la interacción completa (pregunta, respuesta, metadatos del pipeline, `chat_id`, etc.) en la tabla `query_logs` usando `LogRepositoryPort`.
13. **Devolver Respuesta:** Se devuelve la respuesta final al usuario.

## 10. Próximos Pasos y Consideraciones

*   **Resiliencia y Fallbacks:** Reforzar la lógica de fallback si alguno de los servicios remotos (Embedding, Sparse Search, Reranker) no está disponible o falla. Actualmente, el pipeline intenta continuar con la información disponible, pero se podría refinar.
*   **Testing de Integración:** Asegurar tests de integración exhaustivos que cubran las interacciones con todos los servicios remotos y los diferentes flujos del pipeline RAG.
*   **Observabilidad:** Mejorar el tracing distribuido (e.g., OpenTelemetry) entre todos los microservicios para facilitar el debugging y monitoreo de rendimiento en flujos complejos.
*   **Optimización de Llamadas HTTP:** Asegurar que el `httpx.AsyncClient` global se reutilice eficientemente.