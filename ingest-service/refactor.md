# Plan de Refactorización: Ingest Service v0.3.0

## 1. Introducción y Objetivos

**Versión:** 0.3.0
**Fecha:** 2024-07-30

Este documento detalla el plan de refactorización para el `ingest-service` de Atenex, llevándolo a la versión v0.3.0. El objetivo principal es alinear este microservicio con los requisitos introducidos por la refactorización del `query-service` v0.3.0, asegurando la correcta funcionalidad del pipeline RAG avanzado y la disponibilidad de datos enriquecidos para la búsqueda y presentación de resultados.

**Objetivos Clave:**

1.  **Implementar Persistencia de Chunks en PostgreSQL:** Crear y poblar una nueva tabla `document_chunks` en PostgreSQL para almacenar el contenido textual y metadatos detallados de cada chunk generado.
2.  **Enriquecer Metadatos de Chunks:** Modificar el pipeline de ingesta para extraer o generar metadatos adicionales por chunk (ej., número de página, título heurístico, número de tokens, hash de contenido).
3.  **Sincronizar Metadatos con Milvus:** Asegurar que los metadatos enriquecidos se almacenen como campos escalares en la colección de Milvus junto con el vector y el contenido.
4.  **Mantener Consistencia:** Asegurar la coherencia entre los datos almacenados en PostgreSQL y Milvus en la medida de lo posible.
5.  **Adaptar Lógica Existente:** Modificar el worker Celery, las funciones de base de datos y el pipeline para incorporar los nuevos requisitos.

## 2. Estado Actual (v0.2.0 - Análisis)

*   **Pipeline:** Utiliza un pipeline personalizado con librerías standalone (PyMuPDF, python-docx, SentenceTransformers, etc.) y Pymilvus.
*   **Almacenamiento:**
    *   Archivos originales en GCS.
    *   Metadatos de documentos a nivel general en PostgreSQL (tabla `documents`).
    *   **Chunks (Vector, Contenido, Metadatos Mínimos):** *Solo* en Milvus.
*   **Problema Principal:** Ausencia total de persistencia detallada de chunks en PostgreSQL, lo cual es un requisito fundamental para `query-service` v0.3.0 (especialmente BM25).
*   **Metadatos en Milvus:** Actualmente limitados a `company_id`, `document_id`, `file_name`, `content`.
*   **Worker:** Celery con operaciones síncronas para DB (SQLAlchemy) y GCS.

## 3. Requisitos Impulsados por Query Service v0.3.0

1.  **Tabla `document_chunks` en PostgreSQL:** Necesidad de una tabla que contenga como mínimo:
    *   `id` (UUID PK)
    *   `document_id` (UUID FK -> documents.id)
    *   `company_id` (UUID FK -> companies.id - *Recomendado para queries*)
    *   `chunk_index` (INT - Orden del chunk dentro del documento)
    *   `content` (TEXT - Contenido textual del chunk)
    *   `metadata` (JSONB - Metadatos flexibles, incluyendo al menos `page`, `title`, `tokens`, `content_hash`)
    *   `embedding_id` (VARCHAR/TEXT - ID del vector correspondiente en Milvus, usualmente el PK de Milvus)
    *   `created_at` (TIMESTAMPTZ)
    *   `vector_status` (VARCHAR - Estado del vector, ej. 'created', 'error') - *Heredado del esquema original*
2.  **Extracción/Generación de Metadatos:** El pipeline de ingesta debe generar:
    *   `page`: Número de página de donde proviene el chunk (si aplica).
    *   `title`: Título heurístico del chunk o sección (opcional/best-effort).
    *   `tokens`: Número de tokens del chunk (usando un tokenizador como `tiktoken`).
    *   `content_hash`: Hash (ej. SHA256) del contenido del chunk para detectar duplicados o cambios.
3.  **Sincronización con Milvus:** Los campos escalares en Milvus deben incluir los metadatos enriquecidos (además de `company_id`, `document_id`, `file_name`, `content`). El schema de Milvus debe ser actualizado.
4.  **Consistencia de `chunk_count`:** El campo `chunk_count` en la tabla `documents` debe reflejar la cantidad de chunks *exitosamente* escritos tanto en PostgreSQL (`document_chunks`) como en Milvus.

## 4. Plan Detallado de Refactorización

### 4.1. Base de Datos (PostgreSQL)

*   **Acción:** Crear la tabla `document_chunks`.
*   **SQL (Ejecutar como migración):**
    ```sql
    CREATE TABLE IF NOT EXISTS document_chunks (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        document_id UUID NOT NULL,
        company_id UUID NOT NULL, -- Para facilitar queries y aislamiento
        chunk_index INTEGER NOT NULL,
        content TEXT NOT NULL,
        metadata JSONB, -- Almacenará page, title, tokens, content_hash, etc.
        embedding_id VARCHAR(255), -- PK de Milvus (VARCHAR si son strings, o BIGINT si son numéricos auto_id)
        created_at TIMESTAMPTZ DEFAULT timezone('utc', now()),
        vector_status VARCHAR(50) DEFAULT 'pending', -- 'created', 'error'

        CONSTRAINT fk_document
            FOREIGN KEY(document_id)
            REFERENCES documents(id)
            ON DELETE CASCADE, -- Eliminar chunks si se elimina el documento

        -- Opcional pero recomendado FK a companies
        -- CONSTRAINT fk_company
        --     FOREIGN KEY(company_id)
        --     REFERENCES companies(id)
        --     ON DELETE CASCADE,

        UNIQUE (document_id, chunk_index) -- Un chunk específico por documento
    );

    -- Índices Esenciales
    CREATE INDEX IF NOT EXISTS idx_document_chunks_document_id ON document_chunks(document_id);
    CREATE INDEX IF NOT EXISTS idx_document_chunks_company_id ON document_chunks(company_id);
    CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding_id ON document_chunks(embedding_id); -- Para buscar por ID de Milvus si es necesario

    -- Índice para posible búsqueda de contenido (usado por BM25 de query-service)
    -- Considerar tipo de índice según uso: GIN para búsquedas de texto completo si se usa tsvector
    -- CREATE INDEX IF NOT EXISTS idx_document_chunks_content_gin ON document_chunks USING gin(to_tsvector('simple', content));
    -- O un índice B-tree normal si solo se recupera por ID y BM25 se hace en memoria
    -- No crear índice de texto completo aquí si BM25 construye índice en memoria en query-service

    -- Actualizar la tabla documents si falta error_message (verificado en README v0.2.0)
    ALTER TABLE documents ADD COLUMN IF NOT EXISTS error_message TEXT;
    ```
*   **Verificar:** Asegurar que las FKs (especialmente a `documents`) y los tipos de datos sean correctos.

### 4.2. Modelos (SQLAlchemy/Pydantic)

*   **Acción:** Definir modelos/schemas para `DocumentChunk`.
*   **SQLAlchemy (`app/db/models.py` - Nuevo archivo o existente):** Crear una clase `DocumentChunkOrm` que mapee a la tabla `document_chunks` para ser usada por el worker síncrono (SQLAlchemy).
*   **Pydantic (`app/models/domain.py` o `schemas.py`):** Crear un schema `DocumentChunkData` para representar los datos de un chunk internamente antes de la inserción, incluyendo todos los metadatos.

### 4.3. Pipeline de Ingesta (`app/services/ingest_pipeline.py`)

*   **Extracción:**
    *   Modificar `extract_text_from_pdf` (usando PyMuPDF) para devolver una estructura que incluya el texto *y* el número de página de origen. Podría devolver una lista de tuplas `(page_num, page_text)`.
    *   Otros extractores (DOCX, TXT, etc.) podrían no tener noción de página; asignar un número de página genérico (e.g., 0 o 1) o manejarlo como Nulo.
*   **Chunking (`app/services/text_splitter.py` / `ingest_pipeline.py`):**
    *   La lógica de chunking *debe* preservar la asociación con el número de página original.
    *   En lugar de juntar todo el texto y luego dividirlo, iterar sobre las páginas (si aplica), dividir el texto de cada página y mantener el `page_num` asociado a cada chunk resultante.
    *   Modificar `split_text` para que opcionalmente acepte metadatos iniciales (como `page_num`) o manejar la asociación en el bucle que llama al splitter en `ingest_document_pipeline`.
*   **Generación de Metadatos:**
    *   *Después* de obtener los chunks de texto:
        *   Calcular `tokens` para cada chunk (ej., `import tiktoken; enc = tiktoken.get_encoding("cl100k_base"); len(enc.encode(chunk_text))`).
        *   Calcular `content_hash` (ej., `import hashlib; hashlib.sha256(chunk_text.encode()).hexdigest()`).
        *   Extraer `page` (del paso de chunking).
        *   Generar `title` (heurística simple: primeras N palabras, o `Document X, Page Y, Chunk Z`).
        *   Empaquetar estos metadatos en un diccionario para el campo `metadata` JSONB.
*   **Estructura de Datos:** Crear una lista de objetos `DocumentChunkData` (o diccionarios) que contengan `content` y el `metadata` enriquecido.
*   **Milvus Insertion:**
    *   Asegurarse de que el schema de Milvus (`_create_milvus_collection`) incluya los nuevos campos escalares (`page INT64`, `title VARCHAR`, `tokens INT64`, `content_hash VARCHAR`).
    *   Modificar `collection.insert(data_to_insert)`:
        *   `data_to_insert` debe ser una lista de listas/arrays, donde cada sublista corresponde a un campo del schema (vector, content, company_id, document_id, file_name, **page**, **title**, **tokens**, **content_hash**).
        *   **IMPORTANTE:** Capturar los `ids` (Primary Keys) devueltos por `mutation_result = collection.insert(...)`. Estos son los `embedding_id` que se necesitan para PostgreSQL. `mutation_result.primary_keys` contendrá la lista de PKs generados por Milvus (si `auto_id=True`).
    *   Devolver la lista de `embedding_id`s generados junto con el `inserted_count` desde `ingest_document_pipeline`.

### 4.4. Persistencia de Chunks en PostgreSQL (`app/tasks/process_document.py` / `app/db/postgres_client.py`)

*   **Nueva Función DB (`postgres_client.py`):**
    *   Crear `bulk_insert_chunks_sync(engine: Engine, chunks_data: List[Dict[str, Any]]) -> int`.
    *   `chunks_data` será una lista de diccionarios, cada uno representando un chunk e incluyendo `document_id`, `company_id`, `chunk_index`, `content`, `metadata` (JSONB), `embedding_id` (el PK de Milvus), `vector_status`.
    *   Utilizar SQLAlchemy Core (`engine.connect()`, `connection.execute(table.insert().values(chunks_data))`) para realizar una inserción masiva eficiente. Manejar la serialización JSONB para `metadata`.
    *   Devolver el número de filas insertadas.
*   **Tarea Celery (`process_document.py`):**
    *   Llamar a `ingest_document_pipeline`. Si tiene éxito, devolverá `(inserted_milvus_count, milvus_pks)`.
    *   Construir la lista `chunks_data` para la inserción en PostgreSQL, asegurándose de incluir los `milvus_pks` como `embedding_id`.
    *   Llamar a `bulk_insert_chunks_sync` con `chunks_data`.
    *   **Manejo de Errores:** Si la inserción en Milvus tiene éxito pero la inserción en PostgreSQL falla, el estado del documento debe ser `ERROR`. Idealmente, se deberían eliminar los chunks de Milvus para mantener la consistencia, o marcar los chunks en PG con un estado de error vectorial. Por simplicidad inicial, marcar el documento como `ERROR` si la inserción en PG falla.
    *   **Status Final:** Actualizar `documents.status` a `PROCESSED` y `documents.chunk_count` con el número de chunks *exitosamente insertados en PostgreSQL* (devuelto por `bulk_insert_chunks_sync`).

### 4.5. Schema Milvus (`app/services/ingest_pipeline.py`)

*   **Acción:** Modificar `_create_milvus_collection`.
*   Añadir los `FieldSchema` para `page`, `title`, `tokens`, `content_hash` con los `DataType` apropiados (e.g., `INT64`, `VARCHAR`, `VARCHAR`).
*   **Opcional:** Considerar añadir índices (`collection.create_index`) a estos campos escalares si se prevé filtrar por ellos en el futuro, aunque no es un requisito inmediato del `query-service` v0.3.0.

### 4.6. Manejo de Errores (General)

*   Reforzar el manejo de excepciones en `process_document_standalone` para diferenciar entre fallos de descarga (GCS), extracción, chunking, embedding, inserción en Milvus e inserción en PostgreSQL.
*   Asegurar que el estado final del documento (`documents.status`, `documents.error_message`) refleje correctamente dónde ocurrió el fallo.
*   Si falla la inserción en PostgreSQL después de Milvus, intentar una operación de compensación (eliminar de Milvus) o, como mínimo, loguear un error crítico y marcar el documento como erróneo.

### 4.7. API Endpoints (`app/api/v1/endpoints/ingest.py`)

*   **`GET /status/{id}` y `GET /status`:**
    *   No requieren cambios funcionales *inmediatos*, ya que verifican GCS y Milvus.
    *   **Mejora Opcional Futura:** Podrían añadir una verificación de consistencia comparando `documents.chunk_count` con el conteo real en `document_chunks` (PostgreSQL), además de Milvus.
    *   El valor `chunk_count` devuelto ahora debe ser el validado tras la escritura en PostgreSQL.
*   **`DELETE /{document_id}`:**
    *   Actualmente elimina de Milvus, GCS y `documents`.
    *   **Modificación:** Debe asegurarse de que la eliminación en cascada (`ON DELETE CASCADE`) en la FK de `document_chunks` funcione correctamente al eliminar el registro de `documents`. Si no se usa `CASCADE`, se debe añadir un paso explícito para eliminar los registros de `document_chunks` *antes* de eliminar el registro de `documents`.

### 4.8. Configuración (`app/core/config.py`)

*   Añadir `TIKTOKEN_ENCODING_NAME` (ej. `"cl100k_base"`) si se usa `tiktoken`.
*   Verificar/actualizar `MILVUS_METADATA_FIELDS` si es necesario (aunque la inserción ahora es más explícita).

### 4.9. Dependencias (`pyproject.toml`)

*   Añadir `tiktoken` si se decide usarlo para el conteo de tokens.
*   Verificar que `sqlalchemy`, `psycopg2-binary`, `pymilvus`, `google-cloud-storage`, `sentence-transformers`, `pymupdf`, etc., estén presentes y en versiones compatibles.

## 5. Consideraciones Adicionales

*   **Transaccionalidad:** No hay transacciones distribuidas fáciles entre Milvus y PostgreSQL. Se priorizará marcar el documento como `ERROR` si hay inconsistencias (ej., éxito en Milvus, fallo en PG). Podría implementarse un job de limpieza periódico para documentos en error.
*   **Rendimiento:** La inserción masiva (`bulk_insert_chunks_sync`) es crucial para el rendimiento del worker. Monitorizar tiempos de procesamiento.
*   **Retrocompatibilidad:** Los documentos ingeridos con v0.2.0 no tendrán datos en la tabla `document_chunks`. El `query-service` debe ser consciente de esto (BM25 solo funcionará para documentos nuevos/re-ingeridos) o se debe planificar una re-ingesta.

## 6. Checklist de Refactorización v0.3.0

**Base de Datos (PostgreSQL):**

*   [ ] Definir y aplicar migración SQL para crear tabla `document_chunks` con todas las columnas, FKs e índices.
*   [ ] Verificar/Añadir columna `error_message` a tabla `documents`.
*   [ ] Crear función síncrona `bulk_insert_chunks_sync` en `postgres_client.py` usando SQLAlchemy.

**Código:**

*   **Modelos:**
    *   [ ] Definir modelo SQLAlchemy `DocumentChunkOrm`.
    *   [ ] Definir schema Pydantic `DocumentChunkData`.
*   **Servicios (`app/services/`):**
    *   [ ] Modificar extractores (especialmente PDF) para devolver metadatos (ej. `page`).
    *   [ ] Adaptar lógica de chunking para preservar/asociar metadatos (`page`).
    *   [ ] Añadir lógica en `ingest_pipeline.py` para generar metadatos (`tokens`, `content_hash`, `title`).
    *   [ ] Modificar `ingest_document_pipeline` para estructurar datos enriquecidos.
    *   [ ] Actualizar inserción en Milvus (`collection.insert`) en `ingest_pipeline.py` para incluir nuevos campos escalares.
    *   [ ] Capturar y devolver los PKs de Milvus (`embedding_id`) desde `ingest_document_pipeline`.
    *   [ ] Modificar schema Milvus (`_create_milvus_collection`) para incluir nuevos campos escalares.
*   **Tareas Celery (`app/tasks/process_document.py`):**
    *   [ ] Modificar `process_document_standalone` para llamar a `ingest_document_pipeline` y recibir `(count, pks)`.
    *   [ ] Preparar `chunks_data` con `embedding_id` de Milvus.
    *   [ ] Llamar a `bulk_insert_chunks_sync` para guardar chunks en PostgreSQL.
    *   [ ] Actualizar `documents.status` y `documents.chunk_count` basado en el resultado de la inserción en PG.
    *   [ ] Mejorar manejo de errores (fallo PG post-Milvus).
*   **API (`app/api/v1/endpoints/ingest.py`):**
    *   [ ] Revisar/Adaptar `DELETE /{document_id}` para asegurar limpieza de `document_chunks` (vía CASCADE o explícita).
    *   [ ] (Opcional) Considerar añadir chequeo de conteo en `document_chunks` (PG) en endpoints de status.

**Configuración y Dependencias:**

*   [ ] Añadir `tiktoken` a `pyproject.toml` si se usa.
*   [ ] Verificar todas las dependencias (`pyproject.toml`).
*   [ ] Actualizar `settings` (`config.py`) si es necesario.

**Testing y Documentación:**

*   [ ] Añadir/Actualizar tests unitarios para las nuevas funciones (DB, pipeline).
*   [ ] Añadir/Actualizar tests de integración para el flujo completo de ingesta.
*   [ ] Actualizar README.md de `ingest-service` para reflejar la nueva arquitectura (v0.3.0).