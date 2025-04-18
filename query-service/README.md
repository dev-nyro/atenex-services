# Atenex Query Service (Microservicio de Consulta)

## 1. Visión General

El **Query Service** es el microservicio responsable de manejar las consultas en lenguaje natural de los usuarios y gestionar el historial de conversaciones dentro de la plataforma Atenex. Su función principal es:

1.  Recibir una consulta del usuario (`query`) y opcionalmente un `chat_id` a través de la API (`POST /api/v1/query/ask`). Requiere headers `X-User-ID` y `X-Company-ID` inyectados por el API Gateway.
2.  Gestionar el estado de la conversación: crear un nuevo chat si no se proporciona `chat_id`, o continuar uno existente (verificando la propiedad del usuario).
3.  Detectar si la consulta es un saludo simple. Si lo es, generar una respuesta predefinida y omitir el pipeline RAG.
4.  Guardar el mensaje del usuario en la tabla `messages` de **PostgreSQL**.
5.  Si no es un saludo, ejecutar un pipeline de **Retrieval-Augmented Generation (RAG)**:
    *   Generar un embedding para la consulta usando **FastEmbed** (modelo local/open-source, ej. `BAAI/bge-small-en-v1.5`).
    *   Recuperar chunks de documentos relevantes desde **Milvus** (namespace `default`), **filtrando estrictamente por el `company_id`** del usuario.
    *   Construir un prompt para el LLM:
        *   Si se recuperaron documentos, usar una plantilla RAG que instruya al LLM a basarse en ellos (`RAG_PROMPT_TEMPLATE`).
        *   Si *no* se recuperaron documentos, usar una plantilla de conversación general (`GENERAL_PROMPT_TEMPLATE`).
6.  Generar una respuesta utilizando un Large Language Model (LLM) externo, actualmente **Google Gemini** (`gemini-1.5-flash-latest`).
7.  Guardar el mensaje del asistente (respuesta y fuentes si aplica) en la tabla `messages` de **PostgreSQL**.
8.  Registrar la interacción completa (pregunta, respuesta, metadatos, `chat_id`) en la tabla `query_logs` de **PostgreSQL**.
9.  Proporcionar endpoints adicionales (`GET /chats`, `GET /chats/{id}/messages`, `DELETE /chats/{id}`) para que el frontend gestione el historial de chats del usuario. **La eliminación de chat ahora incluye la eliminación de mensajes y logs asociados.**

Este servicio requiere autenticación (gestionada por el API Gateway) para identificar al usuario y la empresa, asegurando el aislamiento de datos y la correcta asociación del historial de chat.

## 2. Arquitectura General del Proyecto (sin cambios)

```mermaid
flowchart LR
    Frontend["Frontend (Vercel / Cliente)"] -->|HTTPS /api/v1/query/ask<br/>POST & /chats* GET/DELETE| Gateway["API Gateway"]
    Gateway -->|/api/v1/query/*| QueryAPI["Query Service API<br/>(FastAPI)"]

    subgraph QueryService
      direction LR
      QueryAPI -->|Headers: X-Company-ID, X-User-ID| QueryLogic{Query/Chat Logic}
      QueryLogic -->|Manage Chats & Msgs| PSQL[(PostgreSQL<br/>atenex.chats, atenex.messages)]
      QueryLogic -->|Log Interaction| PSQLLogs[(PostgreSQL<br/>atenex.query_logs)]
      QueryLogic -->|Invoke RAG| RAGPipeline[RAG Pipeline Steps]

      subgraph RAGPipeline
        direction TB
        EmbedQuery[1. Embed Query (FastEmbed)] --> RetrieveDocs[2. Retrieve Docs (Milvus)]
        RetrieveDocs --> BuildPrompt[3. Build Prompt (Conditional)]
        BuildPrompt --> GenerateLLM[4. Generate Answer (Gemini)]
      end

      RAGPipeline -->|Vector Store (Filter by Company)| Milvus[(Milvus<br/>default.atenex_doc_chunks)]
      RAGPipeline -->|LLM API Call| GeminiAPI[("Google Gemini API")]
      # LLM_COMMENT: OpenAI no es necesario para embedding de query
    end
```

## 3. Características Clave (Actualizado)

*   **API RESTful:** Endpoints para consultas (`/ask`), listar chats (`/chats`), obtener mensajes (`/chats/{id}/messages`) y **eliminar chats completamente** (`/chats/{id}`).
*   **Pipeline RAG Flexible:**
    *   Embedding de consulta con **FastEmbed** (configurable, modelo local).
    *   Retrieval de **Milvus** filtrado por `company_id`.
    *   **Prompt Condicional:** Usa plantillas diferentes si se recuperan o no documentos relevantes.
    *   Generación con **Google Gemini**.
*   **Manejo de Saludos:** Detecta y responde a saludos simples sin ejecutar el pipeline RAG.
*   **Gestión de Historial de Chat Persistente:** Almacena y recupera conversaciones en PostgreSQL. La eliminación de chat ahora limpia mensajes y logs asociados.
*   **Multi-tenancy Estricto:** Aplicado en retrieval de Milvus y acceso a chats/mensajes en PostgreSQL.
*   **Logging Detallado:** Persistencia de interacciones (`query_logs`) y mensajes (`messages`).
*   **Configuración Centralizada:** Kubernetes ConfigMaps/Secrets.
*   **Logging Estructurado (JSON).**
*   **Health Check Robusto.**
*   **Manejo de Errores y Reintentos (Tenacity).**

## 4. Pila Tecnológica Principal (Actualizado)

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Orquestación RAG:** Lógica manual + Haystack Components (`PromptBuilder`, `MilvusEmbeddingRetriever`)
*   **Base de Datos Relacional (Log/Chat):** PostgreSQL (via `asyncpg`)
*   **Base de Datos Vectorial (Retrieval):** Milvus (via `milvus-haystack`, `pymilvus`)
*   **Modelo de Embeddings (Query):** **FastEmbed** (Modelo configurable, ej. `BAAI/bge-small-en-v1.5`) via `fastembed-haystack`.
*   **Modelo LLM (Generación):** Google Gemini (via `google-generativeai`)
*   **Despliegue:** Docker, Kubernetes

## 5. Estructura de la Codebase (sin cambios estructurales mayores)

```
query-service/
├── app/
│   ├── __init__.py
│   ├── api/                  # Definiciones API
│   │   ├── __init__.py
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── endpoints/
│   │       │   ├── __init__.py
│   │       │   ├── chat.py   # Endpoints: /chats, /chats/{id}/messages, /chats/{id}
│   │       │   └── query.py  # Endpoint: /ask (lógica RAG y saludos)
│   │       └── schemas.py    # Schemas Pydantic
│   ├── core/                 # Configuración, Logging
│   │   ├── __init__.py
│   │   ├── config.py
│   │   └── logging_config.py
│   ├── db/                   # Acceso a Base de Datos
│   │   ├── __init__.py
│   │   └── postgres_client.py # Funciones asyncpg (logs, chats, messages - delete_chat modificado)
│   ├── main.py               # Entrypoint FastAPI (lifespan, middleware, health)
│   ├── models/               # (Vacío)
│   │   └── __init__.py
│   ├── pipelines/            # Lógica Pipeline Haystack/RAG
│   │   ├── __init__.py
│   │   └── rag_pipeline.py   # Lógica RAG refactorizada (FastEmbed, prompt condicional)
│   ├── services/             # Clientes APIs Externas
│   │   ├── __init__.py
│   │   ├── base_client.py    # Cliente HTTP base
│   │   └── gemini_client.py  # Cliente Google Gemini
│   └── utils/
│       ├── __init__.py
│       └── helpers.py        # Funciones utilidad (ej. truncate_text)
├── k8s/                      # Manifests K8s (Ejemplos)
│   ├── query-configmap.yaml
│   ├── query-deployment.yaml
│   ├── query-secret.example.yaml
│   └── query-service.yaml
├── Dockerfile
├── pyproject.toml
├── poetry.lock
└── README.md                 # Este archivo
```

## 6. Configuración (Kubernetes - Actualizado)

Gestionada mediante ConfigMap `query-service-config` y Secret `query-service-secrets` en el namespace `nyro-develop`.

### ConfigMap (`query-service-config` en `nyro-develop`)

| Clave                           | Descripción                                        | Ejemplo (Valor Esperado en K8s)                       |
| :------------------------------ | :------------------------------------------------- | :---------------------------------------------------- |
| `QUERY_LOG_LEVEL`               | Nivel de logging.                                  | `INFO`                                                |
| `QUERY_POSTGRES_SERVER`         | Host/Service PostgreSQL (en `nyro-develop`).       | `postgresql.nyro-develop.svc.cluster.local`         |
| `QUERY_POSTGRES_PORT`           | Puerto PostgreSQL.                                 | `5432`                                                |
| `QUERY_POSTGRES_USER`           | Usuario PostgreSQL.                                | `postgres`                                            |
| `QUERY_POSTGRES_DB`             | Base de datos PostgreSQL (`atenex`).               | `atenex`                                              |
| `QUERY_MILVUS_URI`              | URI Milvus (en `default` namespace).               | `http://milvus-milvus.default.svc.cluster.local:19530`|
| `QUERY_MILVUS_COLLECTION_NAME`  | Nombre colección Milvus (`atenex_doc_chunks`).   | `atenex_doc_chunks`                                   |
| `QUERY_MILVUS_EMBEDDING_FIELD`  | Campo vectorial Milvus.                            | `embedding`                                           |
| `QUERY_MILVUS_CONTENT_FIELD`    | Campo contenido Milvus.                            | `content`                                             |
| `QUERY_MILVUS_COMPANY_ID_FIELD` | Campo metadatos para filtrar Cia.                 | `company_id`                                          |
| **`QUERY_FASTEMBED_MODEL_NAME`** | **Modelo FastEmbed para consulta.**                | `BAAI/bge-small-en-v1.5`                              |
| **`QUERY_FASTEMBED_QUERY_PREFIX`**| **(Opcional) Prefijo para embedding de consulta.** | `query: `                                              |
| `QUERY_EMBEDDING_DIMENSION`     | Dimensión embedding (**DEBE coincidir con modelo FastEmbed**). | `384` (para bge-small)                        |
| `QUERY_RETRIEVER_TOP_K`         | Nº docs. por defecto a recuperar.                  | `5`                                                   |
| `QUERY_GEMINI_MODEL_NAME`       | Modelo Gemini para generación.                     | `gemini-1.5-flash-latest`                             |
| `QUERY_RAG_PROMPT_TEMPLATE`     | (Opcional) Plantilla prompt RAG (con documentos).  | *(Ver default en `config.py`)*                        |
| **`QUERY_GENERAL_PROMPT_TEMPLATE`** | **(Opcional) Plantilla prompt sin documentos.** | *(Ver default en `config.py`)*                        |
| `QUERY_HTTP_CLIENT_TIMEOUT`     | Timeout clientes HTTP (s).                         | `60`                                                  |
| `QUERY_HTTP_CLIENT_MAX_RETRIES` | Reintentos clientes HTTP.                          | `2`                                                   |
| `QUERY_HTTP_CLIENT_BACKOFF_FACTOR`| Factor backoff reintentos.                     | `1.0`                                                 |

### Secret (`query-service-secrets` en `nyro-develop`)

| Clave del Secreto     | Variable de Entorno Correspondiente | Descripción             |
| :-------------------- | :---------------------------------- | :---------------------- |
| `POSTGRES_PASSWORD`   | `QUERY_POSTGRES_PASSWORD`           | Contraseña PostgreSQL.  |
| `GEMINI_API_KEY`      | `QUERY_GEMINI_API_KEY`              | Clave API Google Gemini.|
| **`OPENAI_API_KEY`**  | **`QUERY_OPENAI_API_KEY`**          | **(Ya no es necesaria aquí)** |

## 7. API Endpoints (Actualizado)

Prefijo base: `/api/v1/query`

---

### Health Check

*   **Endpoint:** `GET /` (Raíz del servicio)
*   **Descripción:** Chequeo Liveness/Readiness. Verifica inicio y conexión DB/dependencias.
*   **Respuesta OK (`200 OK`):** `OK` (Texto plano)
*   **Respuesta Error (`503 Service Unavailable`):** Si no está listo o falla conexión/dependencia crítica.

---

### Realizar Consulta / Continuar Chat

*   **Endpoint:** `POST /ask` (**Ruta Cambiada internamente**)
*   **Descripción:** Procesa consulta (RAG o saludo), gestiona chat, loguea y devuelve respuesta.
*   **Headers:** `X-Company-ID` (UUID), `X-User-ID` (UUID) **requeridos** (inyectados por Gateway).
*   **Body:** `schemas.QueryRequest` (`{"query": "...", "chat_id": "uuid|null", "retriever_top_k": int|null}`)
*   **Respuesta OK (`200 OK`):** `schemas.QueryResponse` (`{"answer": "...", "retrieved_documents": [...], "query_log_id": "uuid|null", "chat_id": "uuid"}`)
*   **Errores:** 400, 403 (chat no pertenece), 422, 500, 503.

---

### Listar Chats

*   **Endpoint:** `GET /chats`
*   **Descripción:** Lista chats del usuario/compañía (paginado).
*   **Headers:** `X-Company-ID` (UUID), `X-User-ID` (UUID) **requeridos**.
*   **Query Params:** `limit` (int, default 100), `offset` (int, default 0).
*   **Respuesta OK (`200 OK`):** `List[schemas.ChatSummary]`
*   **Errores:** 422, 500.

---

### Obtener Mensajes de Chat

*   **Endpoint:** `GET /chats/{chat_id}/messages`
*   **Descripción:** Obtiene mensajes de un chat específico (paginado). Verifica propiedad.
*   **Headers:** `X-Company-ID` (UUID), `X-User-ID` (UUID) **requeridos**.
*   **Path Params:** `chat_id` (UUID).
*   **Query Params:** `limit` (int, default 100), `offset` (int, default 0).
*   **Respuesta OK (`200 OK`):** `List[schemas.ChatMessage]` (Puede ser vacía si no hay mensajes o no se tiene acceso).
*   **Errores:** 403 (no propietario), 404 (no encontrado - aunque 403 es más preciso aquí), 422, 500.

---

### Borrar Chat

*   **Endpoint:** `DELETE /chats/{chat_id}`
*   **Descripción:** Elimina un chat, sus mensajes y sus logs de consulta asociados. Verifica propiedad.
*   **Headers:** `X-Company-ID` (UUID), `X-User-ID` (UUID) **requeridos**.
*   **Path Params:** `chat_id` (UUID).
*   **Respuesta OK (`204 No Content`):** Éxito (sin cuerpo).
*   **Errores:** 403 (no propietario), 404 (no encontrado), 422, 500.

---

## 8. Dependencias Externas Clave (Actualizado)

*   **PostgreSQL:** (Namespace `nyro-develop`) Logs, Chats, Mensajes.
*   **Milvus:** (Namespace `default`) Vectores para retrieval.
*   **Google Gemini API:** Generación de respuestas LLM.
*   **API Gateway:** Orquestación, autenticación inicial.
*   **(Implícito) Modelo FastEmbed:** Descargado/cacheado por el worker/pod que corre FastEmbed.

## 9. Pipeline RAG (`app/pipelines/rag_pipeline.py` - Actualizado)

1.  **Embed Query:** `FastembedTextEmbedder` con `settings.FASTEMBED_QUERY_PREFIX`.
2.  **Retrieve Docs:** `MilvusEmbeddingRetriever` filtrando por `company_id`.
3.  **Build Prompt:**
    *   Si `docs` existe: `PromptBuilder` con `settings.RAG_PROMPT_TEMPLATE`.
    *   Si `docs` está vacío: `PromptBuilder` con `settings.GENERAL_PROMPT_TEMPLATE`.
4.  **Generate Answer:** Llamada a `GeminiClient` con el prompt construido.
5.  **Log Interaction:** Llamada a `postgres_client.log_query_interaction`.

## 10. Consideraciones Adicionales (Actualizado)

*   **Manejo de Saludos:** Implementado con regex simple. Podría expandirse.
*   **Contexto de Chat en Prompt:** Sigue sin incluirse historial previo en el prompt para Gemini. RAG sigue basándose solo en la última consulta.
*   **Descarga Modelo FastEmbed:** El pod necesitará acceso a internet (o a un mirror interno) para descargar el modelo FastEmbed la primera vez.

## 11. TODO / Mejoras Futuras

*   **Contexto de Chat en Prompt:** Implementar inclusión de historial relevante en el prompt de Gemini.
*   **Tests:** Unitarios y de integración (API, pipeline, DB, chat).
*   **Observabilidad:** Tracing (OpenTelemetry), Métricas (API, latencias, uso de API externas).
*   **Optimización:** Tuning de parámetros Milvus/Gemini/FastEmbed, caching.
*   **Gestión Contexto Largo RAG.**
*   **Generación/Edición Títulos Chat.**
*   **Evaluación RAG / Feedback.**
*   **Refinar Manejo Errores API.**
*   **Intent Detection más robusto:** Ir más allá de saludos simples.