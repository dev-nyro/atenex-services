# Plan de Refactorización del `query-service` de Atenex

## 1. Introducción y Objetivos

Este documento describe el plan de refactorización para el microservicio `query-service` de la plataforma Atenex. El objetivo principal es abordar los problemas identificados durante las pruebas de usuario y el análisis de la codebase, mejorando la calidad de las respuestas, la precisión de la citación de fuentes, la experiencia de usuario general (especialmente en lo referente al feedback de carga y visualización de fuentes) y la robustez del servicio.

Los objetivos clave de esta refactorización son:

1.  **Corregir Errores Críticos:** Solucionar el truncamiento de las respuestas JSON del LLM (Gemini) que causa errores de formato y citaciones incorrectas.
2.  **Mejorar la Citación de Fuentes:** Asegurar que las fuentes (`[Doc N]`) en la respuesta del LLM sean precisas, consistentes y se correspondan con la información presentada en el panel de "Fuentes Relevantes".
3.  **Optimizar la Experiencia del Usuario (Backend Support):**
    *   Habilitar el streaming de respuestas del LLM para proporcionar feedback visual de "escritura en tiempo real" al usuario, mejorando la percepción de velocidad y reduciendo la sensación de espera.
    *   Asegurar que el backend sirva toda la información necesaria (contenido completo de chunks, metadatos relevantes) para que el frontend pueda mostrar las fuentes de manera clara y útil.
4.  **Aumentar la Calidad y Fiabilidad de las Respuestas:**
    *   Mejorar la capacidad del LLM para entender y generar formatos de respuesta específicos (ej. cuadros comparativos).
    *   Mejorar la capacidad del LLM para generar respuestas más extensas y detalladas cuando se solicita.
5.  **Incrementar la Robustez del Servicio:** Mejorar el manejo de errores, especialmente aquellos provenientes de servicios externos o del LLM, y proveer fallbacks más inteligentes.

## 2. Alcance de la Refactorización (Backend - `query-service`)

Esta refactorización se centrará exclusivamente en el backend, específicamente en el `query-service`. Las modificaciones en el frontend serán necesarias para consumir las mejoras del backend (ej. streaming, nuevo formato de fuentes) pero no se detallan aquí.

## 3. Áreas de Refactorización y Acciones Detalladas

### 3.1. Gestión de Respuestas del LLM y Citación de Fuentes

Esta es el área más crítica debido al impacto directo en la fiabilidad y usabilidad.

**Problema Principal:** Truncamiento/malformación de la respuesta JSON del LLM (`RespuestaEstructurada`), llevando a errores de validación Pydantic y citaciones incorrectas.

**Acciones Propuestas:**

1.  **Investigación de Causa Raíz del Truncamiento del JSON del LLM:**
    *   **Análisis de Límites de Gemini:** Investigar a fondo los límites de tokens de *salida* para el modelo `gemini-1.5-flash-latest`. Documentar y, si es posible, configurar un límite máximo de tokens de salida en el `GeminiAdapter` que sea seguro para la validación Pydantic.
    *   **Logging Detallado en `GeminiAdapter`:** Añadir logging específico antes y después de la llamada a `self._model.generate_content_async()`, incluyendo el tamaño del prompt enviado y, si la API de Gemini lo provee, información sobre el uso de tokens de la respuesta o razones de finalización (ej. `finish_reason`).
    *   **Manejo de Buffers/Timeouts en `httpx`:** Aunque `httpx` es robusto, verificar si hay alguna configuración de buffer o timeout en el cliente global `http_client_instance` o en cómo `GeminiAdapter` usa la respuesta que podría estar causando cierres prematuros de la conexión o lecturas incompletas para respuestas muy largas.
    *   **Validación de `response_schema` en `GeminiAdapter`:** La función `_clean_pydantic_schema_for_gemini_response` debe ser robusta. Asegurar que el esquema limpio no cause problemas con Gemini que puedan resultar en respuestas malformadas o truncadas. Simplificar al máximo el `RespuestaEstructurada` si es necesario.

2.  **Implementación de Streaming de Respuestas del LLM:**
    *   **Modificar `GeminiAdapter`:**
        *   Investigar la capacidad de streaming del SDK de `google-generativeai` para `generate_content_async` o una función similar (ej. `stream=True`).
        *   Modificar el método `generate` para que, si se le indica (ej. a través de un nuevo parámetro o si el `response_pydantic_schema` es `None`), devuelva un generador asíncrono que produzca los tokens/partes de la respuesta a medida que llegan.
        *   Para el caso de JSON estructurado: Si Gemini no soporta streaming directo a JSON, el streaming se aplicará al campo `respuesta_detallada`. El objeto JSON completo se construiría al final. Si es posible hacer streaming del JSON por partes, sería ideal, pero más complejo. Inicialmente, enfocar en streaming del texto de `respuesta_detallada`.
    *   **Modificar `AskQueryUseCase`:**
        *   Adaptar el método `execute` para manejar la respuesta en streaming del `GeminiAdapter`.
        *   Si se hace streaming de la `respuesta_detallada`, el `use_case` necesitará ensamblar el objeto `RespuestaEstructurada` (especialmente `fuentes_citadas`) una vez que el stream de texto haya concluido, o si el LLM puede generar primero la parte de `fuentes_citadas` y luego streamear `respuesta_detallada`.
    *   **Modificar API Endpoint (`query.py`):**
        *   Crear un nuevo endpoint (ej. `/ask_streaming`) o añadir un parámetro al endpoint `/ask` para solicitar una respuesta en streaming.
        *   Utilizar `StreamingResponse` de FastAPI para enviar los fragmentos de texto al cliente.
        *   Definir un formato para los eventos de streaming (ej. Server-Sent Events - SSE), que puede incluir diferentes tipos de eventos: `text_chunk`, `source_info_chunk` (si se decide streamear también las fuentes), `error`, `end_of_stream`.
        *   Los `retrieved_documents` completos se enviarían al inicio o al final del stream como un payload separado, no en cada chunk de texto.

3.  **Refactorización de `AskQueryUseCase._handle_llm_response` para Citaciones Precisas:**
    *   **Priorizar `fuentes_citadas` del JSON:** Una vez que el JSON de Gemini sea estable, esta lógica debe ser la fuente primaria para mapear las citas `[Doc N]` a los `RetrievedChunk`s. La clave es el `id_documento` en `FuenteCitada` que corresponde al `RetrievedChunk.id` (que es el `pk_id` de Milvus).
    *   **Fallback Mejorado para Citaciones:** Si el JSON de `fuentes_citadas` sigue siendo problemático o se trunca, la lógica de fallback actual (tomar los N primeros chunks) es inadecuada. Un fallback mejorado podría:
        *   Intentar extraer las etiquetas `[Doc N]` directamente del `respuesta_detallada` (usando regex).
        *   Si el LLM consistentemente numera los `[Doc N]` según el orden de los chunks en el prompt, usar ese orden. Esto requiere validación.
        *   Si todo falla, indicar explícitamente al usuario que las fuentes no pudieron ser mapeadas con precisión para esa respuesta.
    *   **Envío de Información Completa de Fuentes al Frontend:**
        *   Asegurar que cada `RetrievedDocument` en `QueryResponse.retrieved_documents` contenga:
            *   `id`: El ID único del chunk (PK de Milvus).
            *   `document_id`: El ID del documento original.
            *   `file_name`: El nombre del archivo original.
            *   `content`: El contenido completo del chunk (no solo `content_preview`). El frontend puede decidir truncar para la vista previa.
            *   `metadata`: Incluyendo `page_number`, `title` si están disponibles.
            *   `score`: El score de relevancia final (después de fusión/reranking).
            *   `cita_tag`: (NUEVO CAMPO OPCIONAL) El tag `[Doc N]` que el LLM usó para este chunk en la respuesta actual. Esto ayudaría al frontend a hacer el mapeo visual.

### 3.2. Calidad de la Información Recuperada y Presentada

**Problema:** "Vista previa no disponible" y contenido de chunks no accesible en el modal de fuentes.

**Acciones Propuestas:**

1.  **Asegurar Contenido Completo del Chunk en `RetrievedChunk`:**
    *   En `AskQueryUseCase._fetch_content_for_fused_results`:
        *   Garantizar que, después de la fusión, si un chunk proviene de la búsqueda dispersa (y por lo tanto solo tiene ID y score inicialmente), su contenido y metadatos (`document_id`, `file_name`) se recuperen SIEMPRE de PostgreSQL usando `ChunkContentRepositoryPort`.
        *   Verificar que `PostgresChunkContentRepository.get_chunk_contents_by_ids` esté devolviendo correctamente `content`, `document_id`, y `file_name`, y que estos se asignen al objeto `RetrievedChunk`.
    *   En `MilvusAdapter.search`:
        *   Asegurar que los `output_fields` solicitados a Milvus incluyan siempre los campos necesarios para popular `RetrievedChunk.content`, `RetrievedChunk.document_id`, y `RetrievedChunk.file_name`. La configuración actual de `INGEST_SCHEMA_FIELDS` y `MILVUS_METADATA_FIELDS` parece correcta, pero verificar el mapeo.
2.  **Consistencia de `RetrievedChunk`:**
    *   A lo largo de todo el pipeline RAG en `AskQueryUseCase` (después de recuperación, fusión, reranking, filtrado), asegurar que los objetos `RetrievedChunk` mantengan consistentemente su `id`, `content`, `metadata` (incluyendo `document_id`, `file_name`, `page_number`, `title`), y `score`.
    *   Esto es crucial porque la lista final de `RetrievedChunk` que se usa para `original_chunks_for_citation` y luego se mapea a `QueryResponse.retrieved_documents` debe ser completa.

### 3.3. Prompt Engineering para Mejorar Capacidades del LLM

**Problemas:** Dificultad para generar formatos específicos (cuadros comparativos), respuestas a veces demasiado concisas para resúmenes de múltiples puntos.

**Acciones Propuestas:**

1.  **Adaptación de Prompts para Formatos Específicos:**
    *   En `rag_template_gemini_v2.txt` y `reduce_prompt_template_v2.txt`:
        *   Modificar la sección "FORMATO MARKDOWN" para incluir ejemplos explícitos de cómo generar tablas Markdown. Ejemplo: `Si la pregunta solicita una comparación o una tabla, puedes usar el siguiente formato Markdown para tablas:\n| Encabezado 1 | Encabezado 2 |\n|---|---|\n| Celda 1.1 | Celda 1.2 |\n| Celda 2.1 | Celda 2.2 |`.
        *   Se podría incluso añadir una instrucción de que si se detecta una solicitud de "cuadro comparativo" o similar, intente usar dicho formato.
2.  **Mejora de Detalle en Resúmenes de Múltiples Puntos:**
    *   En `rag_template_gemini_v2.txt` (y potencialmente `reduce_prompt_template_v2.txt`):
        *   Añadir a la "TAREA PRINCIPAL" o "PRINCIPIOS CLAVE" una instrucción como: "Si la pregunta del usuario lista múltiples puntos o ítems a resumir o explicar, asegúrate de abordar cada uno de ellos con suficiente detalle en tu respuesta. Evita la concisión excesiva para cada punto individual."
3.  **Manejo de Ambigüedad en Prompts Cortos (Ej: "A.3"):**
    *   Esto es más complejo. El LLM por sí solo podría tener dificultades.
    *   **Consideración (Futuro):** Si el `chat_id` está presente, y la conversación anterior está relacionada con un documento específico, se podría añadir al prompt una nota como: "El usuario está probablemente refiriéndose a secciones del documento discutido anteriormente: «Nombre del Archivo del Chat Actual»." Esto requeriría que `AskQueryUseCase` tenga acceso al contexto del documento del chat, lo cual es un cambio mayor.
    *   **Acción Inmediata (Prompt):** En `general_template_gemini_v2.txt` (para cuando no hay RAG), si la pregunta es muy corta y potencialmente una referencia, instruir al LLM para que pregunte al usuario por más contexto si la referencia es ambigua.
4.  **Instrucciones de Citación para el LLM:**
    *   Revisar las instrucciones de citación en los prompts RAG. El LLM debe ser instruido para que el `id_documento` que ponga en el campo `fuentes_citadas` sea el `ID_Fragmento` del chunk que se le proporcionó en el contexto. Actualmente, el prompt dice `[Doc {{ loop.index }}] ID_Fragmento: {{ doc_item.id }}`, y el LLM parece estar generando las citas `[Doc N]` en `respuesta_detallada` bien. El problema principal es el truncamiento del JSON que contiene la lista `fuentes_citadas`. Si el JSON se estabiliza, la correlación debería mejorar significativamente.

### 3.4. Configuración y Manejo de Errores

**Problemas:** Errores genéricos, fallos en cadena por dependencias.

**Acciones Propuestas:**

1.  **Configuración de Límites de Gemini:**
    *   En `app/core/config.py`, añadir nuevas variables de configuración si el SDK de Gemini permite controlar `max_output_tokens` o configuraciones de seguridad que puedan estar causando el truncamiento. Por ejemplo: `QUERY_GEMINI_MAX_OUTPUT_TOKENS: Optional[int] = Field(None)`.
    *   Utilizar esta configuración en `GeminiAdapter`.
2.  **Manejo de Errores Mejorado en `GeminiAdapter`:**
    *   Capturar excepciones más específicas de la librería `google-generativeai` (ej. `BlockedPromptException`, `StopCandidateException`, errores de cuota, errores de API key) y convertirlas en excepciones personalizadas (`LLMOperationalError`, `LLMContentBlockedError`) o propagarlas con más contexto.
    *   Si se detecta que la respuesta fue truncada (algunas APIs lo indican en los `finish_reason`), loguearlo claramente y quizás intentar una estrategia de recuperación o devolver un error específico.
3.  **Manejo de Errores en `AskQueryUseCase`:**
    *   Ser más granular al capturar excepciones de los diferentes componentes del pipeline (embedding, retrieval, reranking, LLM).
    *   Si un componente opcional (como Reranker o Sparse Search si `BM25_ENABLED=True` pero el servicio falla) falla, el pipeline debería poder continuar sin él, logueando una advertencia. Actualmente, parece que se hace, pero se puede reforzar.
    *   Proveer mensajes de error más específicos al endpoint API en caso de fallos para que el frontend pueda mostrarlos (ej. "El servicio de Reranking no está disponible, se omitió este paso.").
4.  **Health Checks:**
    *   En `main.py`, el health check raíz (`/`) ya verifica el Embedding Service. Asegurar que el health check del Sparse Search Service (si `BM25_ENABLED`) también se considere, pero su fallo no debería marcar todo el `query-service` como no saludable, sino emitir una advertencia (ya que es un componente que mejora, pero no es esencial para una respuesta básica). El Reranker, al ser llamado vía HTTP directo, no tiene un "adapter" con health check en `main.py`; su fallo se manejará en tiempo de ejecución.

### 3.5. Consideraciones Adicionales

1.  **Interpretación de "A.3":**
    *   Si la identificación de secciones como "A.3" es crucial, y los metadatos actuales de los chunks (ej. `page_number`, `title`) no son suficientes, se podría necesitar una mejora en `docproc-service` para extraer una estructura de documento más detallada (índices, encabezados con sus niveles) y almacenarla como metadatos de los chunks. El `query-service` podría entonces usar esta información en la fase de recuperación o en el prompt. Esto es un cambio mayor y probablemente fuera del alcance inmediato de esta refactorización, pero a tener en cuenta para el futuro.

## 4. Impacto Esperado

*   **Reducción significativa de errores** relacionados con el formato de respuesta y las citaciones.
*   **Mejora drástica en la experiencia de usuario** debido al streaming de respuestas y a un panel de fuentes claro y útil.
*   **Mayor confianza del usuario** en la información proporcionada por Atenex.
*   **Mayor capacidad de Atenex** para manejar consultas que requieren formatos específicos o respuestas más detalladas.
*   **Mayor robustez y resiliencia** del `query-service` ante fallos en dependencias.

## 5. Checklist de Refactorización

### 5.1. Estabilización de Respuesta del LLM y Citaciones
-   [ ] **GeminiAdapter:** Investigar causa de truncamiento JSON (límites de salida, timeouts, buffers).
-   [ ] **GeminiAdapter:** Mejorar logging de API de Gemini (tamaño de prompt/respuesta, finish_reason).
-   [ ] **GeminiAdapter:** Manejar errores específicos de Gemini y truncamiento de forma robusta.
-   [ ] **AskQueryUseCase:** Implementar streaming de respuesta de texto (`respuesta_detallada`).
-   [ ] **API (`query.py`):** Modificar/crear endpoint para soportar `StreamingResponse` para el texto del LLM.
-   [ ] **API (`query.py`):** Definir formato de eventos SSE para streaming (incluir tipo de evento: `text_chunk`, `source_chunk`, `error`, `end_stream`).
-   [ ] **AskQueryUseCase (`_handle_llm_response`):** Priorizar `fuentes_citadas` del JSON estable para mapeo.
-   [ ] **AskQueryUseCase (`_handle_llm_response`):** Desarrollar fallback mejorado para citaciones si JSON falla (extracción de `[Doc N]` del texto, etc.).
-   [ ] **AskQueryUseCase:** Asegurar que `QueryResponse.retrieved_documents` se popule con contenido completo y todos los metadatos necesarios (incluyendo `file_name`, `document_id`, `page_number`, `title`, `score` final, y `cita_tag` mapeada).
-   [ ] **Domain (`models.py`):** Añadir campo opcional `cita_tag: Optional[str]` a `RetrievedChunk` si se decide pasar esta información de forma estructurada.
-   [ ] **API (`schemas.py`):** Añadir campo opcional `cita_tag: Optional[str]` a `RetrievedDocument` para el frontend.

### 5.2. Calidad de Información de Fuentes
-   [ ] **AskQueryUseCase (`_fetch_content_for_fused_results`):** Garantizar recuperación completa de `content`, `document_id`, `file_name` desde `ChunkContentRepositoryPort` para chunks de búsqueda dispersa.
-   [ ] **PostgresChunkContentRepository:** Verificar que `get_chunk_contents_by_ids` devuelve todos los campos necesarios.
-   [ ] **MilvusAdapter (`search`):** Confirmar que `output_fields` incluye todos los metadatos para `RetrievedChunk`.
-   [ ] **AskQueryUseCase:** Revisar todo el pipeline RAG para asegurar consistencia de datos en objetos `RetrievedChunk`.

### 5.3. Prompt Engineering
-   [ ] **Prompts (RAG y Reduce):** Añadir ejemplos e instrucciones para generar tablas Markdown.
-   [ ] **Prompts (RAG y Reduce):** Añadir instrucciones para dar más detalle por ítem en resúmenes de múltiples puntos.
-   [ ] **Prompt (General):** Instruir al LLM para pedir clarificación en prompts cortos y ambiguos.
-   [ ] **Prompts (RAG y Reduce):** Asegurar que las instrucciones para `fuentes_citadas` pidan el `ID_Fragmento` como `id_documento`.

### 5.4. Configuración y Manejo de Errores
-   [ ] **Config (`config.py`):** Añadir `QUERY_GEMINI_MAX_OUTPUT_TOKENS` y usarlo en `GeminiAdapter`.
-   [ ] **AskQueryUseCase:** Mejorar manejo de fallos en componentes opcionales del pipeline (continuar si es posible).
-   [ ] **Health Check (`main.py`):** Revisar cómo se considera la salud de Sparse Search Service (no debe ser bloqueante para `SERVICE_READY` si `BM25_ENABLED` es true pero el servicio falla).

### 5.5. Pruebas y Validación
-   [ ] **Pruebas Unitarias:** Para nuevas lógicas en `GeminiAdapter` (streaming, manejo de errores), `AskQueryUseCase` (manejo de fuentes, fallback).
-   [ ] **Pruebas de Integración:**
    *   Probar endpoint de streaming.
    *   Validar la consistencia de las fuentes con múltiples documentos y tipos de consulta.
    *   Probar generación de cuadros comparativos.
    *   Probar resúmenes extensos para verificar detalle y manejo de límites.
    *   Probar el sistema con servicios dependientes (Embedding, Sparse, Reranker) simulando fallos.
-   [ ] **Revisión de Logs:** Asegurar que el logging sea claro y útil después de la refactorización.