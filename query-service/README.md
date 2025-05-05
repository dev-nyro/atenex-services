# Atenex Query Service (Microservicio de Consulta) v0.3.0

## 1. Visión General

El **Query Service** es el microservicio responsable de manejar las consultas en lenguaje natural de los usuarios y gestionar el historial de conversaciones dentro de la plataforma Atenex. Esta versión ha sido refactorizada para adoptar **Clean Architecture** y un pipeline **Retrieval-Augmented Generation (RAG)** más avanzado y configurable.

Sus funciones principales son:

1.  Recibir una consulta (`query`) y opcionalmente un `chat_id` vía API (`POST /api/v1/query/ask`), requiriendo headers `X-User-ID` y `X-Company-ID`.
2.  Gestionar el estado de la conversación (crear/continuar chat) usando `ChatRepositoryPort` (implementado por `PostgresChatRepository`).
3.  Detectar saludos simples y responder directamente, omitiendo el pipeline RAG.
4.  Guardar el mensaje del usuario en la tabla `messages` de PostgreSQL.
5.  Si no es un saludo, ejecutar el pipeline RAG orquestado por `AskQueryUseCase`:
    *   **Embedding de Consulta:** Genera un vector para la consulta usando `FastEmbed` (modelo local, configurable vía `QUERY_FASTEMBED_MODEL_NAME`).
    *   **Recuperación Híbrida (Configurable):**
        *   **Búsqueda Densa:** Recupera chunks desde Milvus usando `MilvusAdapter` (`pymilvus`), filtrando por `company_id`.
        *   **Búsqueda Dispersa (BM25):** *Opcional* (habilitado por `QUERY_BM25_ENABLED`). Recupera chunks desde PostgreSQL usando `BM25sRetriever`, que construye un índice en memoria del contenido textual de los chunks de la compañía.
        *   **Fusión:** Combina resultados densos y dispersos usando Reciprocal Rank Fusion (RRF).
    *   **Reranking (Opcional):** Habilitado por `QUERY_RERANKER_ENABLED`. Reordena los chunks fusionados usando un modelo Cross-Encoder (`BGEReranker` con `sentence-transformers`).
    *   **Filtrado de Diversidad (Opcional/Stub):** Habilitado por `QUERY_DIVERSITY_FILTER_ENABLED`. Aplica un filtro a los chunks reordenados (actualmente un `StubDiversityFilter` que solo trunca la lista).
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
        F[(Diversity Filter<br/>StubDiversityFilter)]
        L[(LLM Adapter<br/>GeminiAdapter)]
    end

    subgraph UC [Application Layer]
        direction TB
        Ports[Ports (Interfaces)<br/>- ChatRepositoryPort<br/>- VectorStorePort<br/>- LLMPort<br/>- SparseRetrieverPort<br/>...]
        UseCases[Use Cases<br/>- AskQueryUseCase]
    end

    subgraph D [Domain Layer]
         Models[Domain Models<br/>- RetrievedChunk<br/>- Chat<br/>- ChatMessage<br/>...]
    end


    A -- Calls --> UseCases
    UseCases -- Depends on --> Ports
    I -- Implements --> Ports
    UseCases -- Uses --> Models
    I -- Uses --> Models # Adapters might return/use Domain Models

    %% External Dependencies linked to Infrastructure %%
    P --> DB[(PostgreSQL<br/>'atenex' DB)]
    V --> Milvus[(Milvus<br/>Collection)]
    L --> LLM_API[("Google Gemini API")]
    S -- Reads content --> DB

    %% Component Interactions within RAG Pipeline (Simplified) %%
    style UC fill:#D1C4E9,stroke:#333,stroke-width:1px
    style A fill:#C8E6C9,stroke:#333,stroke-width:1px
    style I fill:#BBDEFB,stroke:#333,stroke-width:1px
    style D fill:#FFECB3,stroke:#333,stroke-width:1px
```
*Diagrama actualizado reflejando la arquitectura hexagonal/clean.*

## 3. Características Clave (v0.3.0)

*   **Arquitectura Limpia (Hexagonal):** Separación clara de responsabilidades (Dominio, Aplicación, Infraestructura).
*   **API RESTful:** Endpoints para consultas (`/ask`) y gestión de chats (`/chats/...`).
*   **Pipeline RAG Avanzado y Configurable:**
    *   Embedding de consulta con `FastEmbed`.
    *   Recuperación Híbrida opcional (Dense: `MilvusAdapter` + Sparse: `BM25sRetriever`).
    *   Fusión Reciprocal Rank Fusion (RRF).
    *   Reranking opcional (`BGEReranker`).
    *   Filtrado de Diversidad opcional (Stub actual).
    *   Generación con Google Gemini (`GeminiAdapter`).
    *   Control de etapas del pipeline mediante variables de entorno.
*   **Manejo de Saludos:** Optimización para evitar RAG en saludos simples.
*   **Gestión de Historial de Chat:** Persistencia completa en PostgreSQL.
*   **Multi-tenancy Estricto:** Aplicado en todos los accesos a datos.
*   **Logging Estructurado y Detallado:** Incluye metadatos del pipeline en `query_logs`.
*   **Configuración Centralizada:** Kubernetes ConfigMaps/Secrets.
*   **Health Check.**

## 4. Pila Tecnológica Principal (v0.3.0)

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Arquitectura:** Clean Architecture / Hexagonal
*   **Base de Datos Relacional (Cliente):** PostgreSQL (via `asyncpg`)
*   **Base de Datos Vectorial (Cliente):** Milvus (via `pymilvus`)
*   **Modelo Embeddings (Query):** `FastEmbed` (via `fastembed-haystack`)
*   **Recuperación Dispersa:** `bm2s`
*   **Modelo Reranker:** `sentence-transformers` (`BAAI/bge-reranker-base`)
*   **Modelo LLM (Generación):** Google Gemini (via `google-generativeai`)
*   **Componentes Haystack:** `haystack-ai` (para `Document`, `PromptBuilder`)
*   **Despliegue:** Docker, Kubernetes

## 5. Estructura de la Codebase (v0.3.0)

```
app/
├── api                   # Capa API (FastAPI)
│   └── v1
│       ├── endpoints       # Controladores HTTP
│       ├── mappers.py      # Mapeadores DTO <-> Dominio (si es necesario)
│       └── schemas.py      # DTOs (Pydantic)
├── application           # Capa Aplicación
│   ├── ports             # Interfaces (Puertos)
│   └── use_cases         # Lógica de orquestación (Casos de Uso)
├── core                  # Configuración central, logging
│   ├── config.py
│   └── logging_config.py
├── domain                # Capa Dominio
│   └── models.py         # Entidades y Value Objects
├── infrastructure        # Capa Infraestructura
│   ├── filters           # Implementaciones DiversityFilterPort
│   ├── llms              # Implementaciones LLMPort (Gemini)
│   ├── persistence       # Implementaciones RepositoryPorts (Postgres)
│   ├── rerankers         # Implementaciones RerankerPort (BGE)
│   ├── retrievers        # Implementaciones SparseRetrieverPort (BM25)
│   └── vectorstores      # Implementaciones VectorStorePort (Milvus)
├── main.py               # Entrypoint FastAPI, Lifespan, Middleware
├── models                # (Vacío, mantenido por si acaso)
└── utils                 # Funciones de utilidad
    └── helpers.py
```
*(Los directorios `db`, `pipelines`, `services` han sido eliminados).*

## 6. Configuración (Kubernetes - v0.3.0)

Gestionada mediante ConfigMap `query-service-config` y Secret `query-service-secrets` en `nyro-develop`.

### ConfigMap (`query-service-config`) - Claves Añadidas/Importantes

| Clave                            | Descripción                                        | Ejemplo (Valor Esperado)            |
| :------------------------------- | :------------------------------------------------- | :---------------------------------- |
| `QUERY_BM25_ENABLED`             | Habilita/deshabilita retrieval BM25.               | `"true"` / `"false"`                |
| `QUERY_RERANKER_ENABLED`         | Habilita/deshabilita reranking.                    | `"true"` / `"false"`                |
| `QUERY_RERANKER_MODEL_NAME`      | Modelo sentence-transformer para reranker.         | `"BAAI/bge-reranker-base"`          |
| `QUERY_DIVERSITY_FILTER_ENABLED` | Habilita/deshabilita filtro diversidad (stub).   | `"true"` / `"false"`                |
| `QUERY_DIVERSITY_K_FINAL`        | Nº chunks tras filtro diversidad.                | `"10"`                              |
| `QUERY_HYBRID_FUSION_ALPHA`      | Peso para fusión (no usado por RRF actualmente).   | `"0.5"`                             |
| `QUERY_RETRIEVER_TOP_K`          | Nº inicial de chunks por retriever (denso/sparse).| `"5"`                               |
| ... (otras claves existentes)    | ...                                                | ...                                 |

### Secret (`query-service-secrets`)

| Clave del Secreto     | Variable de Entorno Correspondiente | Descripción             |
| :-------------------- | :---------------------------------- | :---------------------- |
| `POSTGRES_PASSWORD`   | `QUERY_POSTGRES_PASSWORD`           | Contraseña PostgreSQL.  |
| `GEMINI_API_KEY`      | `QUERY_GEMINI_API_KEY`              | Clave API Google Gemini.|

## 7. API Endpoints (Sin cambios funcionales externos)

El prefijo base sigue siendo `/api/v1/query`. Los endpoints `/ask`, `/chats`, `/chats/{id}/messages`, `/chats/{id}` mantienen su firma externa, pero ahora su lógica interna es manejada por el `AskQueryUseCase` o los repositorios correspondientes.

## 8. Dependencias Externas Clave (v0.3.0)

*   **PostgreSQL:** Almacena logs, chats, mensajes y **contenido de chunks** (leído por BM25).
*   **Milvus:** Almacena **vectores de chunks** y metadatos asociados (leído por MilvusAdapter).
*   **Google Gemini API:** Generación de respuestas LLM.
*   **API Gateway:** Autenticación y enrutamiento.
*   **(Implícito) Modelos ML:** `FastEmbed`, `sentence-transformers` (BGE Reranker) se descargan/cachean en el pod.

## 9. Pipeline RAG (Ejecutado por `AskQueryUseCase` - v0.3.0)

1.  **Embed Query:** `FastEmbed` (`_embed_query`).
2.  **Coarse Retrieval:**
    *   Llamada concurrente a `VectorStorePort.search` (Dense/Milvus) y (si `BM25_ENABLED`) `SparseRetrieverPort.search` (BM25/Postgres+BM25s).
3.  **Fusion:** Combina resultados densos y dispersos con RRF (`_reciprocal_rank_fusion`).
4.  **Content Fetch:** Obtiene contenido de chunks (si faltan) de `ChunkContentRepositoryPort`.
5.  **Reranking (Opcional):** Si `RERANKER_ENABLED`, llama a `RerankerPort.rerank`.
6.  **Diversity Filtering (Opcional):** Si `DIVERSITY_FILTER_ENABLED`, llama a `DiversityFilterPort.filter` (actualmente Stub).
7.  **Build Prompt:** Construye el prompt con `PromptBuilder` usando los chunks finales.
8.  **Generate Answer:** Llama a `LLMPort.generate`.
9.  **Log Interaction:** Guarda detalles (incluyendo etapas usadas) en `query_logs` vía `LogRepositoryPort`.

## 10. Próximos Pasos y Consideraciones

*   **Acciones Manuales:**
    *   Ejecutar `poetry lock && poetry install` en el entorno de despliegue/construcción.
    *   Actualizar el `Dockerfile` para instalar las nuevas dependencias (`sentence-transformers`, `bm2s`, `numpy`).
    *   Aplicar los manifiestos K8s actualizados (`ConfigMap`, `Deployment`).
*   **Modificaciones `ingest-service`:** Priorizar la modificación de `ingest-service` para enriquecer los metadatos en `document_chunks` (Postgres) y sincronizarlos con Milvus para mejorar la calidad del RAG y la información mostrada.
*   **Rendimiento BM25:** Monitorizar el rendimiento del `BM25sRetriever` en memoria. Si se convierte en un cuello de botella, explorar la indexación offline.
*   **Filtro de Diversidad:** Reemplazar `StubDiversityFilter` por una implementación real (MMR o Dartboard) si se necesita mejorar la diversidad de los resultados.
*   **Testing:** Añadir tests unitarios y de integración para la nueva arquitectura y componentes.