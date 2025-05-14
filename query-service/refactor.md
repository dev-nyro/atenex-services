# Plan de Refactorización: query-service y Creación de sparse-search-service

## Introducción

Este documento detalla el plan de refactorización para el microservicio `query-service` de Atenex y la creación de un nuevo microservicio `sparse-search-service`. El objetivo principal es mejorar la calidad de las respuestas del Large Language Model (LLM), optimizar el rendimiento y la escalabilidad de la búsqueda dispersa, y adoptar las últimas capacidades de los modelos Gemini, específicamente **Gemini 2.5 Flash** (usando el identificador `gemini-2.5-flash-preview-04-17` basado en la documentación reciente) con su ventana de contexto extendida.

La refactorización se dividirá en dos partes principales:
1.  La creación del `sparse-search-service` y la integración correspondiente en el `query-service`.
2.  La refactorización profunda del `query-service` para incorporar técnicas avanzadas de prompting, manejo de contexto y generación de respuestas estructuradas.

Este plan no incluye la definición de pruebas, pero se asume que se implementarán siguiendo las mejores prácticas en un ciclo de desarrollo posterior.

## Parte 1: Creación del Nuevo Microservicio `sparse-search-service`

Actualmente, la búsqueda dispersa (BM25) en el `query-service` se realiza en tiempo real, cargando todos los chunks de una compañía en memoria para construir el índice en cada consulta. Esto presenta un cuello de botella significativo en términos de rendimiento y consumo de memoria. Para solucionar esto, se creará un microservicio dedicado.

### 1.1. Visión General y Responsabilidades del `sparse-search-service`

El `sparse-search-service` tendrá las siguientes responsabilidades:

*   **Indexación Offline/Periódica:** Construir y mantener índices de búsqueda dispersa (BM25 u otro, inicialmente BM25 con `bm2s`) para cada compañía.
*   **Persistencia de Índices:** Almacenar los índices de forma eficiente.
*   **API de Búsqueda:** Exponer un endpoint para que el `query-service` pueda realizar búsquedas dispersas sobre los índices precalculados.
*   **Multi-tenancy:** Manejar los índices y las búsquedas de forma aislada por `company_id`.
*   **Actualización de Índices:** Implementar un mecanismo para actualizar los índices cuando se ingieran nuevos documentos o se modifiquen los existentes.

### 1.2. Arquitectura Propuesta para `sparse-search-service` (Hexagonal)

Se seguirá una arquitectura hexagonal similar a los otros microservicios de Atenex:

*   **Capa API (FastAPI):**
    *   Endpoints para búsqueda y estado.
    *   (Opcional) Endpoint para trigger manual de reindexación por compañía.
*   **Capa de Aplicación:**
    *   Casos de Uso: `SearchUseCase`, `IndexCompanyDocumentsUseCase`.
    *   Puertos: `SparseIndexPort`, `DocumentSourcePort` (para obtener el contenido de los chunks para indexar).
*   **Capa de Dominio:**
    *   Modelos: `IndexedDocument`, `SparseSearchResult`.
*   **Capa de Infraestructura:**
    *   Adaptadores:
        *   `BM2SIndexAdapter` (implementa `SparseIndexPort`): Maneja la creación, carga, guardado y búsqueda en índices `bm2s`.
        *   `GCSDocumentSourceAdapter` (implementa `DocumentSourcePort`): Lee los archivos o manifiestos de chunks desde Google Cloud Storage (GCS) para la indexación. Alternativamente, si `ingest-service` o `docproc-service` exponen los chunks de forma accesible, se podría conectar a ellos o a la base de datos de chunks (`document_chunks` en PostgreSQL) si el contenido completo está ahí y es eficiente. Dado que `ingest-service` ya interactúa con GCS y PostgreSQL para los chunks, una opción sería leer desde `document_chunks` en PostgreSQL para obtener el contenido.
    *   Mecanismo de Trigger: Podría ser un listener de eventos de GCS (Cloud Functions/PubSub si se suben nuevos archivos) o un job periódico (Kubernetes CronJob).

```mermaid
graph TD
    QS[Query Service] -->|HTTP Request /api/v1/sparse-search| SSS_API[Sparse Search Service API (FastAPI)]
    
    subgraph SSS[sparse-search-service]
        direction TB
        SSS_API --> SSS_UC{Application Layer (Use Cases)}
        SSS_UC -- Uses Ports --> SSS_I{Infrastructure Layer (Adapters)}

        subgraph SSS_UC [Application Layer]
            direction TB
            SSS_Ports[Ports<br/>- SparseIndexPort<br/>- DocumentSourcePort]
            SSS_UseCases[Use Cases<br/>- SearchUseCase<br/>- IndexCompanyDocumentsUseCase]
        end

        subgraph SSS_I [Infrastructure Layer]
            direction LR
            SSS_BM2S[BM2SIndexAdapter]
            SSS_DocSource[DocumentSourceAdapter<br/>(Lee de GCS/PostgreSQL Chunks)]
            SSS_IndexStore[(Index Storage<br/>GCS / Persistent Disk)]
        end
        
        SSS_UseCases -- Depends on --> SSS_Ports
        SSS_I -- Implements --> SSS_Ports
    end

    SSS_DocSource --> DB_PG_Chunks[(PostgreSQL - atenex.document_chunks)]
    SSS_DocSource --> GCS_Files[(GCS - Bucket 'atenex')]
    SSS_BM2S --> SSS_IndexStore

    K8sCron[Kubernetes CronJob / GCS Trigger] -->|Dispara Indexación| SSS_UC
```

### 1.3. Pila Tecnológica Sugerida

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Motor de Búsqueda Dispersa:** `bm2s` (por su capacidad de guardar/cargar índices)
*   **Almacenamiento de Índices:**
    *   **Opción 1 (Preferida):** Google Cloud Storage (GCS). Los índices se guardan como archivos en GCS y se descargan/cargan en el pod del `sparse-search-service` según sea necesario.
    
*   **Cliente PostgreSQL (si se lee de `document_chunks`):** `asyncpg`.
*   **Orquestación de Indexación:** Kubernetes CronJob o un listener de eventos de GCS (Cloud Functions + Pub/Sub).
*   **Contenerización:** Docker.
*   **Logging:** Structlog.
*   **Configuración:** Pydantic-settings.

### 1.4. Flujo de Indexación Offline y Periódica

1.  **Disparador:**
    *   **Periódico (CronJob):** Un Kubernetes CronJob se ejecuta (e.g., cada N horas o una vez al día) e invoca un endpoint en `sparse-search-service` o un script interno para reindexar todas las compañías o aquellas con cambios recientes.
    *   **Evento (GCS):** Cuando un nuevo archivo es procesado por `ingest-service` y sus chunks son almacenados, `ingest-service` que llame a `sparse-search-service`**IMPORTANTE: todo esta en un cluster llamalos directamente en su servicio como en el query-service**  para disparar la reindexación de la compañía afectada. Esta es una opción más reactiva.
        *   Considerar que `ingest-service` ya guarda metadatos en PostgreSQL (`documents` y `document_chunks`). El trigger también podría basarse en cambios en estas tablas.

2.  **Proceso de Construcción del Índice (por `IndexCompanyDocumentsUseCase`):**
    *   Para una `company_id` dada:
        *   El `DocumentSourceAdapter` obtiene todos los contenidos textuales de los chunks asociados a esa compañía. Esto implicaría:
            *   Leer de la tabla `document_chunks` de PostgreSQL, filtrando por `company_id`, para obtener `embedding_id` (o el PK del chunk si es diferente) y `content`.
        *   Se prepara un corpus (lista de textos de chunks) y una lista correspondiente de IDs de chunks.
        *   El `BM2SIndexAdapter` utiliza `bm2s` para:
            *   Tokenizar el corpus.
            *   Construir el índice BM25.
            *   Guardar el índice tokenizado y el objeto `BM25` (usando `bm2s.BM25.dump()` o serialización similar) junto con el mapeo de `chunk_ids` a un archivo en el almacenamiento persistente (GCS o PVC), nombrado por `company_id` (e.g., `indices_bm25/{company_id}/index.bm2s` y `indices_bm25/{company_id}/chunk_ids.json`).

3.  **Carga del Índice para Búsqueda:**
    *   Cuando el `SearchUseCase` recibe una solicitud, identifica la `company_id`.
    *   El `BM2SIndexAdapter` verifica si el índice para esa compañía está cargado en memoria (podría usar un caché LRU simple).
    *   Si no está cargado, lo descarga de GCS/PVC y lo carga usando `bm2s.BM25.load()`.

### 1.5. API del `sparse-search-service`

*   **`POST /api/v1/sparse-search`**:
    *   **Request Body:**
        ```json
        {
          "query": "texto de la consulta",
          "company_id": "uuid-de-la-compania",
          "top_k": 50 
        }
        ```
    *   **Response Body (200 OK):**
        ```json
        {
          "results": [
            {"chunk_id": "id_chunk_1", "score": 0.89},
            {"chunk_id": "id_chunk_2", "score": 0.75}
          ],
          "model_info": {"name": "BM25s", "params": "..."} 
        }
        ```
    *   **Autenticación:** Podría ser interna del clúster o requerir un token de servicio si se expone externamente (no recomendado inicialmente). Asumimos que es llamado por `query-service` dentro del clúster.

*   **`GET /health`**:
    *   Verifica la salud general del servicio.
    *   Podría incluir el estado de los índices (e.g., número de compañías indexadas).

*   **(Opcional) `POST /api/v1/reindex`**:
    *   **Request Body:** `{ "company_id": "uuid-de-la-compania" }` o `{ "all_companies": true }`
    *   Permite forzar una reindexación. Requiere protección adecuada.

### 1.6. Consideraciones de Multi-tenancy (`company_id`)

*   Los índices se almacenan y gestionan por separado para cada `company_id`.
*   Las búsquedas siempre se filtran/realizan contra el índice de la `company_id` especificada.

### 1.7. Configuración del `sparse-search-service`

Variables de entorno clave:
*   `SPARSE_LOG_LEVEL`
*   `SPARSE_INDEX_STORAGE_PATH` (e.g., `gs://<bucket-name>/bm25_indices/` o `/mnt/indices/`)
*   `SPARSE_POSTGRES_USER`, `SPARSE_POSTGRES_PASSWORD`, etc. (si lee de `document_chunks`)
*   Variables para la estrategia de trigger de indexación (e.g., `INDEXING_CRON_SCHEDULE`).

### 1.8. Impacto y Refactorización en `query-service`

1.  **Eliminación de Código Relacionado con BM25 Actual:**
    *   Eliminar `app/infrastructure/retrievers/bm25_retriever.py` (`BM25sRetriever`).
    *   Eliminar la dependencia `bm2s` del `pyproject.toml` del `query-service`.
    *   Eliminar `ChunkContentRepositoryPort` y su implementación `PostgresChunkContentRepository` de `app/application/ports/repository_ports.py` y `app/infrastructure/persistence/postgres_repositories.py` si solo eran usados por BM25.
        *   **Nota:** `PostgresChunkContentRepository.get_chunk_contents_by_ids` todavía se usa en `AskQueryUseCase._fetch_content_for_fused_results` para obtener contenido de chunks que no estaban en los resultados densos. Se debe evaluar si esta lógica se mantiene o si el nuevo `sparse-search-service` podría devolver también el contenido (aumentaría el payload pero simplificaría `query-service`).
        *   **Decisión para este plan:** Asumamos que `sparse-search-service` solo devuelve IDs y scores. Por lo tanto, `ChunkContentRepositoryPort` y su implementación que busca por IDs (`get_chunk_contents_by_ids`) **se mantiene** en `query-service` para la fase de fusión. Lo que se elimina es `get_chunk_contents_by_company`.

2.  **Nuevo Cliente para `sparse-search-service` en `query-service`:**
    *   **Puerto:** Crear `app/application/ports/sparse_search_port.py` con `SparseSearchPort`.
        ```python
        # app/application/ports/sparse_search_port.py
        import abc
        from typing import List, Tuple
        
        class SparseSearchPort(abc.ABC):
            @abc.abstractmethod
            async def search(self, query: str, company_id: str, top_k: int) -> List[Tuple[str, float]]:
                raise NotImplementedError

            @abc.abstractmethod
            async def health_check(self) -> bool:
                raise NotImplementedError
        ```
    *   **Cliente HTTP:** Crear `app/infrastructure/clients/sparse_search_service_client.py`. Similar al `EmbeddingServiceClient`.
    *   **Adaptador:** Crear `app/infrastructure/sparse_search/remote_sparse_search_adapter.py` que implemente `SparseSearchPort` y use el nuevo cliente.

3.  **Modificación de `AskQueryUseCase` en `query-service`:**
    *   Inyectar el nuevo `SparseSearchPort`.
    *   En lugar de llamar a `self.sparse_retriever.search(...)`, llamará a `self.sparse_search_adapter.search(...)`.
    *   El parámetro `top_k` que se pasa a `search` del `sparse_search_adapter` será el `retriever_k` actual.
    *   La lógica de fusión (`_reciprocal_rank_fusion` y `_fetch_content_for_fused_results`) permanece mayormente igual, pero `_fetch_content_for_fused_results` seguirá necesitando `ChunkContentRepositoryPort.get_chunk_contents_by_ids` para los chunks provenientes de la búsqueda dispersa remota (que solo devuelve IDs y scores).

4.  **Configuración del `query-service`:**
    *   Añadir `QUERY_SPARSE_SEARCH_SERVICE_URL` a `config.py` y al ConfigMap.
    *   Añadir `QUERY_SPARSE_SEARCH_CLIENT_TIMEOUT`.
    *   La variable `QUERY_BM25_ENABLED` ahora controlará si se llama o no al `sparse-search-service`.

5.  **`main.py` y `dependencies.py` en `query-service`:**
    *   Inicializar el nuevo `SparseSearchServiceClient` y `RemoteSparseSearchAdapter`.
    *   Actualizar la instanciación del `AskQueryUseCase` para inyectar el nuevo adaptador.
    *   Incluir la salud del `sparse-search-service` en el health check del `query-service`.

## Parte 2: Refactorización del `query-service` para Mejora de Calidad de Respuesta LLM

Esta parte se enfoca en mejorar cómo `query-service` interactúa con el LLM y procesa la información.

### 2.1. Cambio a Gemini 2.5 Flash y Gestión de Contexto Extendido

1.  **Actualizar `GeminiAdapter`:**
    *   Modificar `self._model_name` en `app/infrastructure/llms/gemini_adapter.py` para usar `settings.GEMINI_MODEL_NAME`.
    *   Asegurar que la librería `google-generativeai` esté actualizada en `pyproject.toml` a una versión que soporte Gemini 2.5 Flash. (La actual `^0.5.4` debería ser suficiente, pero verificar).

2.  **Actualizar `config.py` y ConfigMap:**
    *   Cambiar `QUERY_GEMINI_MODEL_NAME` a `"gemini-2.5-flash-preview-04-17"` (o el identificador "latest" si se confirma).
    *   Ajustar `QUERY_MAX_PROMPT_TOKENS`: El modelo tiene 1M de tokens de entrada. Lo limitaremos a la mitad: `524288` (512 * 1024).
        ```python
        # app/core/config.py
        # ...
        DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-preview-04-17" # Cambio de modelo
        DEFAULT_MAX_PROMPT_TOKENS: int = 524288 # Mitad de 1M (1048576 / 2)
        # ...
        ```
    *   Ajustar `QUERY_MAX_CONTEXT_CHUNKS`: Con 500k tokens de contexto, podemos pasar muchos más chunks. Esto dependerá del tamaño promedio de los chunks y del resto del prompt (historial, instrucciones). Se necesitará experimentación, pero se podría aumentar significativamente desde los `75` actuales.
        *   **Ejemplo:** Si un chunk promedio + metadatos en el prompt ocupa 2k tokens, podríamos teóricamente pasar `500000 / 2000 = 250` chunks. Empecemos con un valor conservador pero más alto, como `200`.
        ```python
        # app/core/config.py
        # ...
        DEFAULT_MAX_CONTEXT_CHUNKS: int = 200 
        # ...
        ```
    *   Revisar `RETRIEVER_TOP_K`: Debe ser lo suficientemente alto para alimentar las etapas de fusión y reranking, que a su vez alimentan al `MAX_CONTEXT_CHUNKS`. Si `MAX_CONTEXT_CHUNKS` es 200, `RETRIEVER_TOP_K` podría ser `250-300` para tener margen. Actualmente es `100`, por lo que `fusion_fetch_k` es `MAX_CONTEXT_CHUNKS + 10`. Si `MAX_CONTEXT_CHUNKS` sube a 200, `RETRIEVER_TOP_K` podría ser `200` o `250`.

### 2.2. Externalización y Mejora del System Prompt

1.  **Cargar Prompt desde Archivo:**
    *   Mover el contenido de `ATENEX_RAG_PROMPT_TEMPLATE` y `ATENEX_GENERAL_PROMPT_TEMPLATE` de `app/core/config.py` a archivos separados, por ejemplo, `prompts/rag_template_v2.txt` y `prompts/general_template_v2.txt`. Podrían ser archivos `.jinja2` si se requiere lógica de templating más avanzada que la que ofrece `PromptBuilder` directamente.
    *   En `app/core/config.py`, reemplazar las constantes de string con paths a estos archivos.
        ```python
        # app/core/config.py
        from pathlib import Path
        # ...
        PROMPT_DIR = Path(__file__).parent.parent / "prompts" # Asume una carpeta 'prompts' a nivel de 'app'
        DEFAULT_RAG_PROMPT_TEMPLATE_PATH: str = str(PROMPT_DIR / "rag_template_gemini_v2.txt")
        DEFAULT_GENERAL_PROMPT_TEMPLATE_PATH: str = str(PROMPT_DIR / "general_template_gemini_v2.txt")
        # ...
        # En la clase Settings:
        RAG_PROMPT_TEMPLATE_PATH: str = Field(default=DEFAULT_RAG_PROMPT_TEMPLATE_PATH)
        GENERAL_PROMPT_TEMPLATE_PATH: str = Field(default=DEFAULT_GENERAL_PROMPT_TEMPLATE_PATH)
        # ...
        # Y eliminar las variables RAG_PROMPT_TEMPLATE y GENERAL_PROMPT_TEMPLATE
        ```
    *   Modificar `AskQueryUseCase._initialize_prompt_builder` para leer el template desde la ruta especificada en `settings`.
        ```python
        # app/application/use_cases/ask_query_use_case.py
        class AskQueryUseCase:
            def __init__(self, ...):
                # ...
                self._prompt_builder_rag = self._initialize_prompt_builder_from_path(settings.RAG_PROMPT_TEMPLATE_PATH)
                self._prompt_builder_general = self._initialize_prompt_builder_from_path(settings.GENERAL_PROMPT_TEMPLATE_PATH)

            def _initialize_prompt_builder_from_path(self, template_path: str) -> PromptBuilder:
                log.debug(f"Initializing PromptBuilder from path: {template_path}")
                try:
                    with open(template_path, "r", encoding="utf-8") as f:
                        template_content = f.read()
                    return PromptBuilder(template=template_content)
                except FileNotFoundError:
                    log.error(f"Prompt template file not found: {template_path}")
                    # Fallback a un template hardcodeado simple o error crítico
                    default_fallback_template = "Query: {{ query }}\n{% if documents %}Context: {{documents}}{% endif %}\nAnswer:"
                    log.warning(f"Falling back to basic template due to missing file: {template_path}")
                    return PromptBuilder(template=default_fallback_template)
                except Exception as e:
                    log.error(f"Failed to load prompt template from {template_path}", error=e)
                    raise  # Re-raise para detener el inicio si el prompt es crítico
        ```

2.  **Revisión y Mejora del Contenido del Prompt:**
    *   Ajustar el `ATENEX_RAG_PROMPT_TEMPLATE` para Gemini 2.5 Flash, considerando su mayor capacidad y las nuevas técnicas.
    *   Incorporar explícitamente instrucciones para **Chain-of-Thought (CoT)** (ver sección 2.4).
    *   Reforzar las instrucciones de **grounding** y manejo de "no sé".
    *   Optimizar la sección de formato de **respuesta estructurada (JSON)** (ver sección 2.6).
    *   Asegurar que el prompt sea claro sobre el uso del historial de chat.
    *   El prompt actual v1.3 es un buen punto de partida, pero se puede refinar más. La sección `5 · FORMATO DE RESPUESTA REQUERIDO` será clave para la salida JSON.

### 2.3. Implementación de Estrategia MapReduce

Para manejar un gran número de chunks (`MAX_CONTEXT_CHUNKS` aumentado a ~200), la estrategia MapReduce puede ser beneficiosa para no sobrecargar al LLM con un solo prompt masivo y mejorar la extracción de información.

1.  **Descripción del Enfoque:**
    *   **Fase Map:** Para cada chunk (o pequeños lotes de chunks), enviar una pregunta específica al LLM para extraer información relevante a la consulta original del usuario.
    *   **Fase Reduce:** Tomar todas las respuestas de la fase Map y sintetizarlas en una respuesta final coherente usando el LLM.

2.  **Modificaciones en `AskQueryUseCase`:**
    *   **Nueva Variable de Configuración:** `QUERY_MAPREDUCE_ENABLED: bool` y `QUERY_MAPREDUCE_CHUNK_BATCH_SIZE: int` (e.g., 3-5 chunks por llamada Map).
    *   **Lógica Condicional:** Si `MAPREDUCE_ENABLED` es true y `len(final_chunks_for_llm)` supera un umbral (e.g., 20-30 chunks), activar el flujo MapReduce.
    *   **Fase Map:**
        *   Iterar sobre `final_chunks_for_llm` en lotes.
        *   Crear un nuevo template de prompt para la fase Map: `map_prompt_template.txt`. Este prompt instruirá al LLM a extraer información del (lote de) chunk(s) que responda directamente a la `query` original del usuario.
            ```
            # prompts/map_prompt_template.txt (Ejemplo conceptual)
            Eres un asistente de extracción de información.
            PREGUNTA ORIGINAL DEL USUARIO:
            {{ original_query }}

            DOCUMENTO(S) ACTUAL(ES):
            {% for doc in documents %}
            [Doc {{ doc.meta.id_original_o_indice }}] «{{ doc.meta.file_name }}» (Pág: {{ doc.meta.page }})
            Extracto: {{ doc.content }}
            --------------------
            {% endfor %}

            TAREA: Basándote *únicamente* en el(los) DOCUMENTO(S) ACTUAL(ES) proporcionado(s) arriba, extrae la información clave que responde a la PREGUNTA ORIGINAL DEL USUARIO. Si no hay información relevante, indica "No hay información relevante en este/estos extracto(s)". Sé conciso. Incluye citas al documento si es relevante ([Doc X]).
            
            INFORMACIÓN EXTRAÍDA:
            ```
        *   Para cada lote, llamar a `self.llm.generate()` con este prompt.
        *   Recolectar todas las respuestas de la fase Map.
    *   **Fase Reduce:**
        *   Crear un nuevo template de prompt para la fase Reduce: `reduce_prompt_template.txt`. Este prompt tomará la `query` original, el `chat_history` y todas las "informaciones extraídas" de la fase Map.
            ```
            # prompts/reduce_prompt_template.txt (Ejemplo conceptual)
            Eres Atenex, un asistente de IA experto. Tu tarea es sintetizar la información proveída para responder la pregunta del usuario. Sigue todas las INSTRUCCIONES DE GENERACIÓN de Atenex (identidad, tono, citación, no especulación, etc.).

            PREGUNTA ORIGINAL DEL USUARIO:
            {{ original_query }}

            {% if chat_history %}
            HISTORIAL RECIENTE:
            {{ chat_history }}
            {% endif %}

            INFORMACIÓN RECOPILADA DE VARIOS DOCUMENTOS (de la fase Map):
            {{ mapped_responses }} {# Concatenación de todas las respuestas de la fase Map #}

            INSTRUCCIONES DE GENERACIÓN (Como el ATENEX_RAG_PROMPT_TEMPLATE original, pero enfocado en sintetizar mapped_responses y la pregunta)
            (...)
            FORMATO DE RESPUESTA REQUERIDO (JSON):
            (...) {# Como se define en la sección 2.6 #}

            RESPUESTA FINAL SINTETIZADA (en formato JSON):
            ```
        *   Llamar a `self.llm.generate()` una vez con este prompt de reducción. Esta será la respuesta final.
    *   Las `sources` para la respuesta final deben consolidarse a partir de los documentos que contribuyeron a las respuestas de la fase Map.

3.  **Impacto en `GeminiAdapter`:**
    *   No se requiere un gran cambio si `generate()` ya es genérico. Podría ser útil añadir un parámetro `prompt_type` ("rag", "map", "reduce") para logging.

4.  **Consideraciones:**
    *   Aumentará la latencia y el costo (múltiples llamadas al LLM).
    *   La gestión de citas a través de las fases Map y Reduce debe ser cuidadosa. Los `map_prompts` deben instruir para mantener la referencia al `Doc N` original. El `reduce_prompt` debe sintetizar esto.
    *   El tamaño del `mapped_responses` para la fase Reduce también podría ser grande. Se puede necesitar truncar o resumir si excede los límites.

### 2.4. Implementación de Chain-of-Thought (CoT)

CoT puede integrarse en el prompt principal (RAG o Reduce) para guiar al LLM a razonar antes de responder.

1.  **Modificar el Prompt:**
    *   Añadir una sección al prompt (e.g., `ATENEX_RAG_PROMPT_TEMPLATE` o el `reduce_prompt_template` si se usa MapReduce) que le pida al LLM seguir un proceso de pensamiento.
    *   Ejemplo de instrucción CoT (inspirado en el análisis inicial):
        ```
        (...)
        5 · PROCESO DE PENSAMIENTO SUGERIDO (INTERNO, NO MOSTRAR AL USUARIO):
        Antes de generar la respuesta final en el formato JSON requerido, mentalmente sigue estos pasos:
        a. Revisa la PREGUNTA ACTUAL DEL USUARIO y el HISTORIAL RECIENTE para entender completamente la intención.
        b. Analiza los DOCUMENTOS RECUPERADOS / INFORMACIÓN RECOPILADA. Identifica los fragmentos más relevantes que responden directamente a cada parte de la pregunta.
        c. Si hay información contradictoria o complementaria, resuélvela o preséntala de forma clara.
        d. Formula una respuesta concisa y directa a la pregunta principal en el campo `respuesta_detallada`.
        e. Asegúrate de que CADA DATO fáctico en `respuesta_detallada` que provenga de los documentos esté apropiadamente citado [Doc N].
        f. Genera un `resumen_ejecutivo` si la respuesta detallada es larga.
        g. Construye la lista `fuentes_citadas` basándote ESTRICTAMENTE en los [Doc N] que usaste y citaste. Verifica que los números de documento coincidan.
        h. Finalmente, ensambla toda la respuesta en el formato JSON especificado.
        (...)
        ```

2.  **No se Requieren Cambios en Código (Excepto el Prompt):** CoT es una técnica de prompting.

### 2.5. Implementación de Reason+Act (ReAct)

ReAct es un patrón más avanzado. Para esta fase inicial de refactorización, nos centraremos en MapReduce y CoT. ReAct podría considerarse una mejora futura si los casos de uso demandan una planificación y ejecución de herramientas más dinámica por parte del LLM. Por ahora, **no se incluirá en este plan de refactorización inmediato** para mantener el alcance manejable.

### 2.6. Generación de Respuestas Estructuradas (JSON)

Utilizar la capacidad de Gemini para generar JSON directamente.

1.  **Definir Esquema de Respuesta Pydantic:**
    *   Crear un modelo Pydantic en `app/domain/models.py` (o un nuevo `app/domain/structured_responses.py`) que represente la estructura JSON deseada.
        ```python
        # app/domain/models.py o app/domain/structured_responses.py
        from pydantic import BaseModel, Field
        from typing import List, Optional, Any
        import uuid

        class FuenteCitada(BaseModel):
            # Usar los nombres que se le dan al LLM en el prompt
            id_documento: Optional[str] = Field(None, description="El ID del chunk o documento original, si está disponible.")
            nombre_archivo: str
            pagina: Optional[str] = None # Mantener como str ya que puede ser 'N/A' o '?'
            score: Optional[float] = None
            cita_tag: str = Field(..., description="La etiqueta de cita usada en el texto, ej: '[Doc 1]'")

        class RespuestaEstructurada(BaseModel):
            resumen_ejecutivo: Optional[str] = Field(None, description="Un breve resumen de 1-2 frases, si la respuesta es larga.")
            respuesta_detallada: str = Field(..., description="La respuesta completa y elaborada, incluyendo citas [Doc N].")
            fuentes_citadas: List[FuenteCitada] = Field(default_factory=list, description="Lista de los documentos efectivamente utilizados y citados.")
            # Otros campos si son necesarios, como:
            # siguiente_pregunta_sugerida: Optional[str] = None
            # error_flag: bool = False
            # error_message: Optional[str] = None
        ```

2.  **Modificar Prompt para Salida JSON:**
    *   Actualizar la sección `FORMATO DE RESPUESTA REQUERIDO` en el prompt principal (RAG o Reduce) para instruir al LLM a generar una salida JSON que coincida con el esquema Pydantic.
    *   Utilizar el ejemplo de la documentación de Gemini (sección "Configurar un esquema (recomendado)") y el notebook.
        ```
        # En el prompt .txt o .jinja2
        (...)
        6 · FORMATO DE RESPUESTA REQUERIDO (SALIDA JSON)
        Tu respuesta DEBE ser un objeto JSON válido con la siguiente estructura. Presta mucha atención a los tipos de datos y a los campos requeridos.
        {
          "resumen_ejecutivo": "string (opcional, solo si la respuesta es larga)",
          "respuesta_detallada": "string (requerido, con citas [Doc N])",
          "fuentes_citadas": [
            {
              "id_documento": "string (opcional, id del chunk/doc)",
              "nombre_archivo": "string (requerido)",
              "pagina": "string (opcional)",
              "score": "number (opcional, float)",
              "cita_tag": "string (requerido, ej: '[Doc 1]')"
            }
            // ... más fuentes si son citadas
          ]
        }
        Asegúrate de que las citas [Doc N] en `respuesta_detallada` coincidan con los `cita_tag` y la información en `fuentes_citadas`.
        La lista `fuentes_citadas` solo debe contener documentos que realmente hayas usado y citado.
        No incluyas comentarios en el JSON.
        (...)
        ```

3.  **Actualizar `GeminiAdapter`:**
    *   En `generate()`, configurar `generation_config` para el modelo Gemini.
        ```python
        # app/infrastructure/llms/gemini_adapter.py
        from app.domain.models import RespuestaEstructurada # O el path correcto

        class GeminiAdapter(LLMPort):
            # ... __init__ ...

            async def generate(self, prompt: str, expecting_json: bool = True) -> str: # Añadir expecting_json
                # ... (manejo de cliente no inicializado) ...
                generate_log = log.bind(adapter="GeminiAdapter", model_name=self._model_name, prompt_length=len(prompt), expecting_json=expecting_json)
                
                gen_config_dict = {
                    "max_output_tokens": 8192 # Ajustar según necesidad, Gemini 2.5 Flash tiene hasta 65k
                }
                
                response_schema_for_gemini = None
                if expecting_json:
                    gen_config_dict["response_mime_type"] = "application/json"
                    # Convertir el modelo Pydantic a un esquema que Gemini entienda,
                    # la librería google-generativeai lo hace automáticamente si pasas el tipo.
                    response_schema_for_gemini = RespuestaEstructurada # Pasar la clase Pydantic
                
                generation_config_obj = genai.types.GenerationConfig(**gen_config_dict) if not expecting_json else genai.types.GenerationConfig(
                    response_mime_type="application/json",
                    response_schema=response_schema_for_gemini, # Pasar la clase Pydantic directamente
                    max_output_tokens=gen_config_dict["max_output_tokens"]
                )

                try:
                    response = await self.model.generate_content_async(
                        prompt,
                        generation_config=generation_config_obj
                    )
                    # ... (resto del manejo de respuesta, candidate, etc.) ...
                    # El .text de la respuesta ya debería ser el string JSON si expecting_json=True
                    generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text'))
                    
                    if expecting_json:
                        generate_log.debug("Received JSON response string from Gemini API", response_length=len(generated_text))
                        # Validar que sea un JSON parseable (aunque no necesariamente que cumpla el schema aquí)
                        try:
                            json.loads(generated_text)
                        except json.JSONDecodeError as json_err:
                            generate_log.error("Gemini returned invalid JSON string", raw_response=generated_text, error=str(json_err))
                            # Podrías intentar una recuperación o devolver un error JSON formateado
                            return '{"error": "LLM returned malformed JSON", "details": "' + str(json_err) + '"}'
                    else:
                         generate_log.debug("Received text response from Gemini API", response_length=len(generated_text))

                    return generated_text.strip()
                # ... (resto de los bloques except) ...
        ```

4.  **Actualizar `AskQueryUseCase`:**
    *   Cuando llame a `self.llm.generate()`, pasar `expecting_json=True`.
    *   Parsear la respuesta string JSON a un objeto `RespuestaEstructurada`.
        ```python
        # app/application/use_cases/ask_query_use_case.py
        # from app.domain.models import RespuestaEstructurada
        
        # ... en el método execute ...
        json_answer_str = await self.llm.generate(final_prompt, expecting_json=True)
        exec_log.info("LLM JSON answer string generated.", length=len(json_answer_str))

        try:
            structured_answer_obj = RespuestaEstructurada.model_validate_json(json_answer_str)
            answer_for_user = structured_answer_obj.respuesta_detallada # El texto principal para el usuario
            
            # Usar structured_answer_obj.fuentes_citadas para guardar en ChatMessage.sources
            # y para log_query_interaction.
            assistant_sources_from_llm = [
                f.model_dump() for f in structured_answer_obj.fuentes_citadas
            ]
             # También mapear estas fuentes para `retrieved_chunks` si es necesario que la respuesta
             # del endpoint `/ask` devuelva las fuentes tal como las entendió el LLM.
             # Los `final_chunks_for_llm` son los que se enviaron al LLM.
             # `structured_answer_obj.fuentes_citadas` son los que el LLM *dice* que usó.
             # Aquí hay que decidir qué `retrieved_chunks` devolver en la API:
             # Opción A: Los `final_chunks_for_llm` originales (hasta NUM_SOURCES_TO_SHOW).
             # Opción B: Mapear `structured_answer_obj.fuentes_citadas` a objetos `RetrievedChunk`.
             # Por ahora, usaremos Opción A para consistencia con el código actual.
            
        except json.JSONDecodeError as json_err:
            exec_log.error("Failed to parse JSON response from LLM", raw_response=json_answer_str, error=str(json_err))
            # Fallback a una respuesta de error o intentar extraer texto si es posible
            answer_for_user = "Hubo un error al procesar la respuesta del asistente. Por favor, intenta de nuevo."
            assistant_sources_from_llm = [] # No hay fuentes fiables
            # Considerar no guardar este mensaje o guardarlo con una nota de error.
        except ValidationError as pydantic_err: # Si el JSON es válido pero no cumple el schema Pydantic
            exec_log.error("LLM JSON response failed Pydantic validation", raw_response=json_answer_str, errors=pydantic_err.errors())
            answer_for_user = "La respuesta del asistente no tuvo el formato esperado. Por favor, intenta de nuevo."
            assistant_sources_from_llm = []


        await self.chat_repo.save_message(
            chat_id=final_chat_id, role='assistant',
            content=answer_for_user, # Guardar la parte textual de la respuesta estructurada
            sources=assistant_sources_from_llm[:self.settings.NUM_SOURCES_TO_SHOW] if assistant_sources_from_llm else None
        )

        # ... logging ...
        
        # Devolver answer_for_user y los chunks originales que se pasaron al LLM (o las fuentes parseadas)
        return answer_for_user, final_chunks_for_llm[:self.settings.NUM_SOURCES_TO_SHOW], log_id, final_chat_id

        ```

5.  **Actualizar `schemas.QueryResponse`:**
    *   La `QueryResponse` actual ya tiene `answer: str` y `retrieved_documents: List[RetrievedDocument]`.
    *   `answer` contendrá `structured_answer_obj.respuesta_detallada`.
    *   `retrieved_documents` puede seguir siendo los chunks que se consideraron para el LLM (de `final_chunks_for_llm`) o podría modificarse para reflejar más de cerca las `fuentes_citadas` por el LLM. Por simplicidad y para mantener la trazabilidad de lo que *se envió* al LLM, mantener `final_chunks_for_llm` es razonable, pero truncado.

### 2.7. Mejoras en el Manejo del Historial de Chat

Con un contexto de 500k tokens, el historial de chat tiene más espacio. Sin embargo, si las conversaciones son extremadamente largas, aún podría consumir una porción significativa.

*   **Revisar `MAX_CHAT_HISTORY_MESSAGES`:** Actualmente es 10. Se puede aumentar, pero con cuidado. Por ejemplo, a `30-50` mensajes.
*   **Consideración Futura (No en este plan inmediato):** Para conversaciones muy largas, implementar un paso de "resumen de conversación" o "filtrado de relevancia del historial" usando un LLM para condensar el historial antes de inyectarlo en el prompt RAG principal.

## Checklist Detallado de Refactorización

## Parte 1: Creación del Nuevo Microservicio `sparse-search-service`

Actualmente, la búsqueda dispersa (BM25) en el `query-service` se realiza en tiempo real, cargando todos los chunks de una compañía en memoria para construir el índice en cada consulta. Esto presenta un cuello de botella significativo en términos de rendimiento y consumo de memoria. Para solucionar esto, se creará un microservicio dedicado.

### 1.1. Visión General y Responsabilidades del `sparse-search-service`
[X] Definir responsabilidades claras: Indexación (actualmente por-solicitud, no offline/periódica), API de búsqueda, multi-tenancy.

### 1.2. Arquitectura Propuesta para `sparse-search-service` (Hexagonal)
[X] Seguir arquitectura hexagonal similar a otros servicios.
    - [X] **Capa API (FastAPI):** Endpoints para búsqueda y estado.
    - [X] **Capa de Aplicación:** `SearchUseCase`. Puertos: `ChunkContentRepositoryPort`, `SparseSearchPort`.
    - [X] **Capa de Dominio:** Modelos: `SparseSearchResultItem`.
    - [X] **Capa de Infraestructura:** Adaptadores: `BM25Adapter`, `PostgresChunkContentRepository`.
[X] Diagrama de arquitectura creado (en `sparse-search-service/README.md`).

### 1.3. Pila Tecnológica Sugerida
[X] **Lenguaje:** Python 3.10+
[X] **Framework API:** FastAPI
[X] **Motor de Búsqueda Dispersa:** `bm2s`
[X] **Almacenamiento de Índices:** (Actualmente en memoria por solicitud, no persistente en GCS/PVC para esta fase inicial).
[X] **Cliente PostgreSQL:** `asyncpg`
[X] **Orquestación de Indexación:** (No aplica para indexación por-solicitud. Si se pasa a offline, se necesitará CronJob).
[X] **Contenerización:** Docker
[X] **Logging:** Structlog
[X] **Configuración:** Pydantic-settings

### 1.4. Flujo de Indexación (Por Solicitud, no Offline)
[X] **Disparador:** La solicitud HTTP a `/api/v1/search` dispara la indexación para esa compañía.
[X] **Proceso de Construcción del Índice (por `SparseSearchUseCase` y `BM25Adapter`):**
    - [X] Para una `company_id` dada:
        - [X] `PostgresChunkContentRepository` obtiene todos los contenidos textuales de los chunks.
        - [X] Se prepara corpus y lista de IDs.
        - [X] `BM25Adapter` usa `bm2s` para construir el índice en memoria.
[X] **Carga del Índice para Búsqueda:** El índice se construye y usa en la misma solicitud. No hay carga/descarga de GCS/PVC en esta fase.

### 1.5. API del `sparse-search-service`
-   [X] **`POST /api/v1/search`**:
    -   [X] Request Body definido.
    -   [X] Response Body definido.
    -   [X] Autenticación: Asumida interna del clúster (manejada por API Gateway si se expone).
-   [X] **`GET /health`**:
    -   [X] Verifica salud general y dependencias (DB, bm2s).
-   [ ] **(Opcional) `POST /api/v1/reindex`**: (No implementado en esta fase, ya que la indexación es por solicitud).

### 1.6. Consideraciones de Multi-tenancy (`company_id`)
[X] Los índices se construyen en memoria por `company_id` en cada solicitud.
[X] Las búsquedas se realizan contra el índice de la `company_id` especificada.

### 1.7. Configuración del `sparse-search-service`
[X] Variables de entorno clave definidas (`SPARSE_LOG_LEVEL`, `SPARSE_PORT`, `SPARSE_POSTGRES_*`).
[X] `config.py` implementado.

### Checklist Detallado de Refactorización para `sparse-search-service` (según lo completado)

-   [X] **Diseño y Estructura del Proyecto:**
    -   [X] Crear estructura de directorios para `sparse-search-service` (FastAPI, Hexagonal).
    -   [X] Definir modelos de dominio (`SparseSearchResultItem`, `CompanyCorpusStats`).
    -   [X] Definir puertos (`ChunkContentRepositoryPort`, `SparseSearchPort`).
-   [X] **Implementación de Indexación (Por Solicitud):**
    -   [X] `PostgresChunkContentRepository`: Implementar lógica para obtener contenido de chunks desde PostgreSQL.
    -   [X] `BM2SAdapter`:
        -   [X] Lógica para construir índice BM2S en memoria con `bm2s`.
        -   [-] Lógica para guardar/cargar índice en GCS/PVC (No aplica en esta fase de indexación por-solicitud).
    -   [-] `IndexCompanyDocumentsUseCase`: (No aplica directamente para indexación por-solicitud, la lógica está en `SparseSearchUseCase`).
-   [X] **Implementación de Búsqueda:**
    -   [X] `BM2SAdapter`: Lógica de búsqueda sobre índice en memoria.
    -   [X] `SparseSearchUseCase`: Orquestar obtención de corpus y búsqueda.
-   [X] **API Endpoints:**
    -   [X] `POST /api/v1/search` implementado.
    -   [X] `GET /health` implementado.
-   [ ] **Mecanismo de Disparo de Indexación:** (No aplica para indexación por-solicitud. Si se pasa a offline, se necesitará CronJob o trigger).
-   [X] **Configuración y Docker:**
    -   [X] Variables de entorno, `config.py` implementados.
    -   [X] `Dockerfile` creado.
    -   [X] Manifiestos K8s (Deployment, Service, ConfigMap) creados como ejemplo. (Secret debe ser manejado por el usuario).
-   [X] **Seguridad y Multi-tenancy:**
    -   [X] Aislamiento por `company_id` en la obtención de datos y la construcción del índice por solicitud.

### Refactorización `query-service` - Integración `sparse-search-service`
-   [ ] **Eliminar BM25 Local:**
    -   [ ] Eliminar `BM25sRetriever`.
    -   [ ] Eliminar `bm2s` de dependencias.
    -   [ ] Modificar/Simplificar `ChunkContentRepositoryPort` (mantener `get_chunk_contents_by_ids`, eliminar `get_chunk_contents_by_company`).
-   [ ] **Nuevo Cliente/Adaptador Remoto:**
    -   [ ] Crear `SparseSearchPort`.
    -   [ ] Crear `SparseSearchServiceClient`.
    -   [ ] Crear `RemoteSparseSearchAdapter`.
-   [ ] **Actualizar `AskQueryUseCase`:**
    -   [ ] Inyectar y usar `RemoteSparseSearchAdapter` en lugar del `BM25sRetriever` local.
-   [ ] **Configuración `query-service`:**
    -   [ ] Añadir `QUERY_SPARSE_SEARCH_SERVICE_URL` y timeout.
    -   [ ] `QUERY_BM25_ENABLED` controla llamada al nuevo servicio.
-   [ ] **Actualizar `main.py` / `dependencies.py`:**
    -   [ ] Inicializar nuevo cliente/adaptador.
    -   [ ] Añadir a health check.

### Refactorización `query-service` - Mejoras Calidad LLM
-   [X] **Cambio a Gemini 2.5 Flash:**
    -   [X] Actualizar `GeminiAdapter` para usar `gemini-2.5-flash-preview-04-17` (o `latest`).
    -   [X] Actualizar `config.py` (modelo, `MAX_PROMPT_TOKENS` a ~500k, `MAX_CONTEXT_CHUNKS` a ~200).
    -   [X] Verificar y actualizar `google-generativeai` en `pyproject.toml`.
-   [X] **Externalizar y Mejorar System Prompt:**
    -   [X] Mover templates de prompt a archivos `.txt` o `.jinja2`.
    -   [X] Actualizar `config.py` con rutas a los archivos de prompt.
    -   [X] Modificar `AskQueryUseCase` para cargar prompts desde archivos.
    -   [X] Revisar y refinar contenido de `ATENEX_RAG_PROMPT_TEMPLATE` para Gemini 2.5 y nuevas técnicas.
-   [X] **Implementar Estrategia MapReduce (Opcional pero Recomendado):**
    -   [X] Añadir variables de config: `QUERY_MAPREDUCE_ENABLED`, `QUERY_MAPREDUCE_CHUNK_BATCH_SIZE`, `QUERY_MAPREDUCE_ACTIVATION_THRESHOLD`.
    -   [X] Crear template `map_prompt_template.txt`.
    -   [X] Crear template `reduce_prompt_template_v2.txt`.
    -   [X] Modificar `AskQueryUseCase` para el flujo MapReduce condicional (incluyendo manejo de errores en fase Map).
    -   [X] Gestionar citas y fuentes a través de las fases Map y Reduce.
-   [X] **Implementar Chain-of-Thought (CoT):**
    -   [X] Integrar instrucciones CoT en el prompt RAG (o Reduce si se usa MapReduce).
-   [X] **Generación de Respuestas Estructuradas (JSON):**
    -   [X] Definir `RespuestaEstructurada` y `FuenteCitada` Pydantic en `app/domain/`.
    -   [X] Modificar prompt para instruir salida JSON según el esquema Pydantic.
    -   [X] Actualizar `GeminiAdapter.generate()` para configurar `response_mime_type="application/json"` y `response_schema` con la clase Pydantic.
    -   [X] Actualizar `AskQueryUseCase` para parsear la respuesta JSON del LLM a `RespuestaEstructurada` (usando `_handle_llm_response`).
    -   [X] Usar campos de `RespuestaEstructurada` para la respuesta al usuario y el guardado de mensajes/logs.
-   [X] **Manejo del Historial de Chat:**
    -   [X] Revisar y posiblemente aumentar `QUERY_MAX_CHAT_HISTORY_MESSAGES` (aumentado a 20).

Este plan proporciona una hoja de ruta completa para las refactorizaciones solicitadas. Cada punto del checklist representa una tarea de desarrollo significativa.