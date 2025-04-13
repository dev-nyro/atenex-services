# Atenex Query Service (Microservicio de Consulta)

## 1. Visión General

El **Query Service** es el microservicio responsable de manejar las consultas en lenguaje natural de los usuarios y gestionar el historial de conversaciones dentro de la plataforma Atenex. Su función principal es:

1.  Recibir una consulta del usuario (`query`) y opcionalmente un `chat_id` a través de la API (`POST /api/v1/query/query`).
2.  Gestionar el estado de la conversación: crear un nuevo chat si no se proporciona `chat_id`, o continuar uno existente (verificando la propiedad del usuario).
3.  Guardar el mensaje del usuario en la tabla `messages` de **PostgreSQL**.
4.  Ejecutar un **pipeline de Retrieval-Augmented Generation (RAG) construido con [Haystack AI 2.x](https://haystack.deepset.ai/)**:
    *   Generar un embedding para la consulta usando **OpenAI**.
    *   Recuperar chunks de documentos relevantes desde **Milvus** (namespace `default`), **filtrando estrictamente por el `company_id`** del usuario.
    *   Construir un prompt para el LLM usando los documentos recuperados y la consulta.
5.  Generar una respuesta utilizando un Large Language Model (LLM) externo, actualmente **Google Gemini**.
6.  Guardar el mensaje del asistente (respuesta y fuentes) en la tabla `messages` de **PostgreSQL**.
7.  Registrar la interacción completa (pregunta, respuesta, metadatos, `chat_id`) en la tabla `query_logs` de **PostgreSQL**.
8.  Proporcionar endpoints adicionales (`GET /chats`, `GET /chats/{id}/messages`, `DELETE /chats/{id}`) para que el frontend gestione el historial de chats del usuario.

Este servicio requiere autenticación (gestionada por el API Gateway) para identificar al usuario (`X-User-ID` extraído del JWT) y la empresa (`X-Company-ID` header), asegurando el aislamiento de datos y la correcta asociación del historial de chat.

## 2. Arquitectura General del Proyecto

```mermaid
%%{init: {'theme': 'base', 'themeVariables': { 'primaryColor': '#d3ffd4', 'edgeLabelBackground':'#fff', 'tertiaryColor': '#dcd0ff'}}}%%
graph TD
    A[Usuario/Cliente Externo] -->|HTTPS / REST API<br/>(via API Gateway)| Q["<strong>Atenex Query Service API</strong><br/>(FastAPI)"]

    subgraph KubernetesCluster ["Kubernetes Cluster"]

        subgraph Namespace_nyro_develop ["Namespace: nyro-develop"]
            direction TB
            Q -->|Get/Create Chat, Save Msg (User),<br/>Save Msg (Assistant), Save Query Log,<br/>List/Delete Chats| DB[(PostgreSQL<br/>'atenex' DB<br/>Tables: chats, messages, query_logs)]

            %% Conexión a Ingest (no directa, pero usa sus datos)
            Ingest[Ingest Service] -.-> DB
            Ingest -.-> MDB_default[(Milvus)]
            Ingest -.-> S3[(MinIO)]

        end

        subgraph Namespace_default ["Namespace: default"]
            direction LR
            %% RAG Pipeline Steps happening logically within Query Service / Haystack call
            RAG_Pipeline["RAG Pipeline (Haystack)"] -->|Retrieve Chunks (Filtered)| MDB_default
        end

        Q --> RAG_Pipeline # Query Service ejecuta el pipeline
        RAG_Pipeline -->|Generate Query Embedding| OpenAI[("OpenAI API<br/>(External)")]
        RAG_Pipeline -->|Generate Answer| Gemini[("Google Gemini API<br/>(External)")]


    end

    %% Estilo
    style Q fill:#f9f,stroke:#333,stroke-width:2px
    style DB fill:#DBF1C2,stroke:#333,stroke-width:1px
    style MDB_default fill:#B0E0E6,stroke:#333,stroke-width:1px
    style RAG_Pipeline fill:#ccf,stroke:#333,stroke-width:1px,stroke-dasharray: 5 5

```
*Diagrama simplificado enfocándose en las interacciones del Query Service.*

## 3. Características Clave

*   **API RESTful:** Endpoints para consultas RAG (`/query`), listar chats (`/chats`), obtener mensajes (`/chats/{id}/messages`) y borrar chats (`/chats/{id}`).
*   **Pipeline RAG con Haystack:** Orquesta embedding de consulta (OpenAI), retrieval filtrado por tenant (Milvus) y construcción de prompt.
*   **Generación con LLM:** Integración con Google Gemini para generar respuestas contextualizadas.
*   **Gestión de Historial de Chat Persistente:** Almacena y recupera conversaciones (chats y mensajes) en PostgreSQL, asociadas a usuarios y empresas.
*   **Multi-tenancy Estricto:** Filtra documentos por `company_id` en Milvus y asegura acceso a chats solo para el usuario propietario.
*   **Logging Detallado:** Persistencia de interacciones (`query_logs`) y mensajes (`messages`) en PostgreSQL.
*   **Configuración Centralizada:** Uso de ConfigMaps y Secrets en Kubernetes (namespace `nyro-develop`).
*   **Logging Estructurado (JSON):** Facilita el análisis y monitorización.
*   **Health Check Robusto:** Endpoint `/` verifica disponibilidad y conexión a BD.
*   **Manejo de Errores y Reintentos:** Captura errores y usa `tenacity` para reintentos en llamadas a APIs externas.

## 4. Pila Tecnológica Principal

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Orquestación RAG:** Haystack AI 2.x (`haystack-ai`)
*   **Base de Datos Relacional (Log/Chat):** PostgreSQL (via `asyncpg`)
*   **Base de Datos Vectorial (Retrieval):** Milvus (via `milvus-haystack`, `pymilvus`)
*   **Modelo de Embeddings (Query):** OpenAI (`text-embedding-3-small` por defecto) via `openai`.
*   **Modelo LLM (Generación):** Google Gemini (via `google-generativeai`)
*   **Despliegue:** Docker, Kubernetes

## 5. Estructura de la Codebase

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
│   │       │   └── query.py  # Endpoint: /query (con lógica chat)
│   │       └── schemas.py    # Schemas Pydantic
│   ├── core/                 # Configuración, Logging
│   │   ├── __init__.py
│   │   ├── config.py
│   │   └── logging_config.py
│   ├── db/                   # Acceso a Base de Datos
│   │   ├── __init__.py
│   │   └── postgres_client.py # Funciones asyncpg (logs, chats, messages)
│   ├── main.py               # Entrypoint FastAPI
│   ├── models/               # (Vacío)
│   │   └── __init__.py
│   ├── pipelines/            # Lógica Pipeline Haystack
│   │   ├── __init__.py
│   │   └── rag_pipeline.py   # Construcción y ejecución RAG
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

## 6. Configuración (Kubernetes)

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
| `QUERY_OPENAI_EMBEDDING_MODEL`  | Modelo embedding OpenAI (consulta).                | `text-embedding-3-small`                              |
| `QUERY_EMBEDDING_DIMENSION`     | Dimensión embedding (auto-calculado).            | `1536`                                                |
| `QUERY_RETRIEVER_TOP_K`         | Nº docs. por defecto a recuperar.                  | `5`                                                   |
| `QUERY_GEMINI_MODEL_NAME`       | Modelo Gemini para generación.                     | `gemini-1.5-flash-latest`                             |
| `QUERY_RAG_PROMPT_TEMPLATE`     | (Opcional) Plantilla prompt RAG.                   | *(Ver default en `config.py`)*                        |
| `QUERY_HTTP_CLIENT_TIMEOUT`     | Timeout clientes HTTP (s).                         | `60`                                                  |
| `QUERY_HTTP_CLIENT_MAX_RETRIES` | Reintentos clientes HTTP.                          | `2`                                                   |
| `QUERY_HTTP_CLIENT_BACKOFF_FACTOR`| Factor backoff reintentos.                     | `1.0`                                                 |

### Secret (`query-service-secrets` en `nyro-develop`)

| Clave del Secreto     | Variable de Entorno Correspondiente | Descripción             |
| :-------------------- | :---------------------------------- | :---------------------- |
| `POSTGRES_PASSWORD`   | `QUERY_POSTGRES_PASSWORD`           | Contraseña PostgreSQL.  |
| `OPENAI_API_KEY`      | `QUERY_OPENAI_API_KEY`              | Clave API OpenAI.       |
| `GEMINI_API_KEY`      | `QUERY_GEMINI_API_KEY`              | Clave API Google Gemini.|

## 7. API Endpoints

Prefijo base: `/api/v1/query` (definido en `main.py` para ambos routers)

---

### Health Check

*   **Endpoint:** `GET /` (Raíz del servicio)
*   **Descripción:** Chequeo Liveness/Readiness. Verifica inicio y conexión DB.
*   **Respuesta OK (`200 OK`):** `OK` (Texto plano)
*   **Respuesta Error (`503 Service Unavailable`):** Si no está listo o falla conexión DB.

---

### Realizar Consulta / Continuar Chat

*   **Endpoint:** `POST /query`
*   **Descripción:** Procesa consulta, gestiona chat, ejecuta RAG, loguea y devuelve respuesta.
*   **Headers:** `X-Company-ID` (UUID), `Authorization` (Bearer Token) **requeridos**.
*   **Body:** `schemas.QueryRequest` (`{"query": "...", "chat_id": "uuid|null", "retriever_top_k": int|null}`)
*   **Respuesta OK (`200 OK`):** `schemas.QueryResponse` (`{"answer": "...", "retrieved_documents": [...], "query_log_id": "uuid|null", "chat_id": "uuid"}`)
*   **Errores:** 400, 401, 403, 422, 500, 503.

---

### Listar Chats

*   **Endpoint:** `GET /chats`
*   **Descripción:** Lista chats del usuario/compañía (paginado).
*   **Headers:** `X-Company-ID` (UUID), `Authorization` (Bearer Token) **requeridos**.
*   **Query Params:** `limit` (int, default 100), `offset` (int, default 0).
*   **Respuesta OK (`200 OK`):** `List[schemas.ChatSummary]`
*   **Errores:** 401, 500.

---

### Obtener Mensajes de Chat

*   **Endpoint:** `GET /chats/{chat_id}/messages`
*   **Descripción:** Obtiene mensajes de un chat específico (paginado).
*   **Headers:** `X-Company-ID` (UUID), `Authorization` (Bearer Token) **requeridos**.
*   **Path Params:** `chat_id` (UUID).
*   **Query Params:** `limit` (int, default 100), `offset` (int, default 0).
*   **Respuesta OK (`200 OK`):** `List[schemas.ChatMessage]` (Puede ser vacía si no hay mensajes o no se tiene acceso).
*   **Errores:** 401, 404 (si el chat no existe o no pertenece al usuario), 422, 500.

---

### Borrar Chat

*   **Endpoint:** `DELETE /chats/{chat_id}`
*   **Descripción:** Elimina un chat y sus mensajes.
*   **Headers:** `X-Company-ID` (UUID), `Authorization` (Bearer Token) **requeridos**.
*   **Path Params:** `chat_id` (UUID).
*   **Respuesta OK (`204 No Content`):** Éxito (sin cuerpo).
*   **Errores:** 401, 404, 422, 500.

---

## 8. Dependencias Externas Clave

*   **PostgreSQL:** (Namespace `nyro-develop`) Logs, Chats, Mensajes.
*   **Milvus:** (Namespace `default`) Vectores para retrieval.
*   **OpenAI API:** Embedding de consultas.
*   **Google Gemini API:** Generación de respuestas LLM.
*   **API Gateway:** Orquestación, autenticación inicial.

## 9. Pipeline Haystack (`app/pipelines/rag_pipeline.py`)

1.  **`OpenAITextEmbedder`:** Embedding de `query`.
2.  **`MilvusEmbeddingRetriever`:** Búsqueda en Milvus filtrando por `company_id`.
3.  **`PromptBuilder`:** Construcción del prompt final.
4.  **(Externo)** Llamada a `GeminiClient` con el prompt.
5.  **(Externo)** Logging de la interacción en `postgres_client`.

## 10. Consideraciones Adicionales

*   **Extracción User ID:** `get_current_user_id` asume que el token ya fue validado por el Gateway. Si este servicio pudiera ser llamado directamente, necesitaría una validación completa.
*   **Contexto de Chat en Prompt:** La implementación actual **no** incluye el historial de chat en el prompt para Gemini. El RAG se basa solo en la última consulta. Se necesitaría lógica adicional para incluir mensajes anteriores y gestionar límites de tokens para conversaciones más largas y coherentes.
*   **Gestión de Contexto Largo (RAG):** No hay manejo explícito si los documentos recuperados exceden el límite de tokens del prompt.
*   **Generación Títulos Chat:** Los títulos son básicos.

## 11. TODO / Mejoras Futuras

*   **Contexto de Chat en Prompt:** Implementar inclusión de historial relevante en el prompt de Gemini.
*   **Tests:** Unitarios y de integración (API, pipeline, DB, chat).
*   **Observabilidad:** Tracing (OpenTelemetry), Métricas (API, latencias, uso de API externas).
*   **Optimización:** Caching, tuning de parámetros Haystack/Milvus/Gemini.
*   **Gestión Contexto Largo RAG.**
*   **Generación/Edición Títulos Chat.**
*   **Evaluación RAG / Feedback.**
*   **Refinar Manejo Errores API.**