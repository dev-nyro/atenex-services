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

## 4. Reglas de Oro para la Refactorización

*   **NO MODIFICAR RUTAS API:** El enrutamiento (`/api/v1/ingest/...`) y la forma en que se definen las rutas en `app/main.py` (`app.include_router`) son correctos y están integrados con el API Gateway. **NO CAMBIAR EL PREFIJO `/api/v1/ingest` NI LA LÓGICA DE INCLUSIÓN DEL ROUTER.**
*   **MODIFICAR ENDPOINTS EXISTENTES CON CUIDADO:** Si necesitas cambiar la lógica interna de un endpoint (ej., `DELETE`), hazlo sin alterar su firma (path, método, parámetros esperados) a menos que sea absolutamente necesario y coordinado con el API Gateway y los clientes.
*   **NUEVOS ENDPOINTS:** Si se requieren endpoints adicionales, agrégalos al router existente (`app/api/v1/endpoints/ingest.py`) siguiendo el patrón actual.
*   **MANTENER OPERACIONES SÍNCRONAS EN WORKER:** El worker Celery (`process_document.py`) debe seguir realizando operaciones de base de datos (PostgreSQL) y almacenamiento (GCS/Milvus) de forma síncrona usando las librerías correspondientes (`SQLAlchemy`, `GCSClient sync methods`, `pymilvus`).
*   **AISLAMIENTO:** Asegúrate de que todas las operaciones (DB, Milvus, GCS) estén correctamente aisladas por `company_id` y `document_id` donde corresponda.
*   **ENFOQUE EN LO NECESARIO:** Edita solo las partes del código directamente implicadas en la persistencia de chunks en PG, generación de metadatos y sincronización con Milvus. Evita refactorizaciones cosméticas o cambios en lógica no relacionada para minimizar riesgos.

## 5. Plan Detallado de Refactorización

### 5.1. Base de Datos (PostgreSQL)

*   **Acción:** Crear la tabla `document_chunks`.
*   **SQL (Ejecutar como migración):**
    ```sql
    -- Crear la tabla si no existe
    CREATE TABLE IF NOT EXISTS document_chunks (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        document_id UUID NOT NULL,
        company_id UUID NOT NULL, -- Para facilitar queries y aislamiento
        chunk_index INTEGER NOT NULL,
        content TEXT NOT NULL,
        metadata JSONB, -- Almacenará page, title, tokens, content_hash, etc.
        embedding_id VARCHAR(255), -- PK de Milvus (VARCHAR porque auto_id de Milvus suele ser string ahora, ajustar si es BIGINT)
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

    -- Crear Índices Esenciales si no existen
    CREATE INDEX IF NOT EXISTS idx_document_chunks_document_id ON document_chunks(document_id);
    CREATE INDEX IF NOT EXISTS idx_document_chunks_company_id ON document_chunks(company_id);
    CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding_id ON document_chunks(embedding_id); -- Para buscar por ID de Milvus

    -- Actualizar la tabla documents si falta error_message (verificado en README v0.2.0)
    ALTER TABLE documents ADD COLUMN IF NOT EXISTS error_message TEXT;
    ```
*   **Verificar:** Asegurar que las FKs (especialmente a `documents`) y los tipos de datos sean correctos.

### 5.2. Modelos (SQLAlchemy/Pydantic)

*   **Acción:** Definir modelos/schemas para `DocumentChunk`.
*   **SQLAlchemy (`app/db/postgres_client.py`):** Definición de la tabla `document_chunks_table` usando SQLAlchemy Core Metadata.
*   **Pydantic (`app/models/domain.py`):** Crear schemas `DocumentChunkMetadata` y `DocumentChunkData` para representar los datos de un chunk internamente.

### 5.3. Pipeline de Ingesta (`app/services/ingest_pipeline.py`)

*   **Extracción:**
    *   Modificar `extract_text_from_pdf` para devolver `List[Tuple[int, str]]` (page_num, page_text).
    *   Otros extractores devuelven `str`.
*   **Chunking (`app/services/text_splitter.py` / `ingest_pipeline.py`):**
    *   Adaptar `ingest_document_pipeline` para iterar sobre el contenido extraído (texto único o lista de páginas).
    *   Llamar a `split_text` para cada bloque/página.
    *   Asociar `page_num` a los chunks resultantes.
*   **Generación de Metadatos:**
    *   En `ingest_document_pipeline`, *después* de obtener los chunks de texto:
        *   Calcular `tokens` (usando `tiktoken`).
        *   Calcular `content_hash` (SHA256).
        *   Asignar `page`.
        *   Generar `title` (heurística).
        *   Empaquetar en objeto `DocumentChunkMetadata`.
*   **Estructura de Datos:** Crear lista de `DocumentChunkData` (Pydantic).
*   **Milvus Insertion:**
    *   Asegurar que el schema Milvus (`_create_milvus_collection`) incluya `page`, `title`, `tokens`, `content_hash`. **PK cambiado a VARCHAR.**
    *   Modificar `collection.insert()`:
        *   Usar PKs customizados (ej: `f"{document_id}_{chunk_index}"`).
        *   Incluir los nuevos campos escalares en `data_to_insert`.
        *   Validar `mutation_result.insert_count` y `mutation_result.primary_keys`.
    *   Devolver `(inserted_count, primary_keys, processed_chunk_data_list)` desde `ingest_document_pipeline`.

### 5.4. Persistencia de Chunks en PostgreSQL (`app/tasks/process_document.py` / `app/db/postgres_client.py`)

*   **Nueva Función DB (`postgres_client.py`):**
    *   Crear `bulk_insert_chunks_sync(engine: Engine, chunks_data: List[Dict[str, Any]]) -> int`.
    *   Usar SQLAlchemy Core (`document_chunks_table.insert()`) para inserción masiva.
    *   Manejar serialización JSONB.
    *   Devolver número de filas insertadas.
*   **Tarea Celery (`process_document.py`):**
    *   Llamar a `ingest_document_pipeline` para obtener `(inserted_milvus_count, milvus_pks, processed_chunks_data)`.
    *   Construir `chunks_for_pg` (lista de dicts) a partir de `processed_chunks_data`, asignando `embedding_id`.
    *   Llamar a `bulk_insert_chunks_sync` con `chunks_for_pg`.
    *   **Manejo de Errores:** Si `bulk_insert_chunks_sync` falla, loguear error crítico, intentar eliminar de Milvus (`delete_milvus_chunks`), marcar documento como `ERROR`, y lanzar `Reject`.
    *   **Status Final:** Actualizar `documents.status` a `PROCESSED` y `documents.chunk_count` con el resultado de `bulk_insert_chunks_sync`.

### 5.5. Schema Milvus (`app/services/ingest_pipeline.py`)

*   **Acción:** Modificar `_create_milvus_collection`.
*   Añadir `FieldSchema` para `page`, `title`, `tokens`, `content_hash`.
*   Cambiar `MILVUS_PK_FIELD` a `DataType.VARCHAR`.
*   **Opcional:** Añadir índices escalares para los nuevos campos.

### 5.6. Manejo de Errores (General)

*   Reforzar manejo de excepciones en `process_document_standalone`.
*   Asegurar que `documents.error_message` sea descriptivo.
*   Implementar intento de limpieza de Milvus si falla la inserción en PG.

### 5.7. API Endpoints (`app/api/v1/endpoints/ingest.py`)

*   **`GET /status/{id}` y `GET /status`:**
    *   Sin cambios funcionales requeridos inmediatamente. `chunk_count` reflejará el conteo de PG.
*   **`DELETE /{document_id}`:**
    *   Verificar que `ON DELETE CASCADE` en FK de `document_chunks` esté funcionando. (Confirmado: Sí está en el SQL).

### 5.8. Configuración (`app/core/config.py`)

*   Añadir `TIKTOKEN_ENCODING_NAME`.
*   Eliminar `MILVUS_METADATA_FIELDS` (obsoleto).

### 5.9. Dependencias (`pyproject.toml`)

*   Añadir `tiktoken`.
*   Verificar/actualizar versiones.

## 6. Consideraciones Adicionales

*   **Transaccionalidad:** Priorizar marcar como `ERROR` en caso de inconsistencia Milvus/PG. Considerar job de limpieza.
*   **Rendimiento:** Monitorizar `bulk_insert_chunks_sync`.
*   **Retrocompatibilidad:** Documentos antiguos no tendrán chunks en PG. `query-service` debe manejarlo. Considerar re-ingesta.

## 7. Checklist de Refactorización v0.3.0 (Actualizado)

**Base de Datos (PostgreSQL):**

*   [X] Definir y aplicar migración SQL para crear tabla `document_chunks` con todas las columnas, FKs e índices.
*   [X] Verificar/Añadir columna `error_message` a tabla `documents`.
*   [X] Crear función síncrona `bulk_insert_chunks_sync` en `postgres_client.py` usando SQLAlchemy.

**Código:**

*   **Modelos:**
    *   [X] Definir definición de tabla `document_chunks_table` en `postgres_client.py` (SQLAlchemy Core).
    *   [X] Definir schemas Pydantic `DocumentChunkMetadata`, `DocumentChunkData`, `ChunkVectorStatus` en `app/models/domain.py`.
*   **Servicios (`app/services/`):**
    *   [X] Modificar extractor PDF (`pdf_extractor.py`) para devolver `List[Tuple[int, str]]`.
    *   [X] Adaptar lógica de chunking en `ingest_pipeline.py` para preservar/asociar metadatos (`page`).
    *   [X] Añadir lógica en `ingest_pipeline.py` para generar metadatos (`tokens`, `content_hash`, `title`).
    *   [X] Modificar `ingest_document_pipeline` para estructurar datos enriquecidos en `DocumentChunkData`.
    *   [X] Actualizar inserción en Milvus (`collection.insert`) en `ingest_pipeline.py` para incluir nuevos campos escalares y usar PK VARCHAR.
    *   [X] Capturar y devolver los PKs de Milvus (`embedding_id`) desde `ingest_document_pipeline`.
    *   [X] Modificar schema Milvus (`_create_milvus_collection`) para incluir nuevos campos escalares y PK VARCHAR.
*   **Tareas Celery (`app/tasks/process_document.py`):**
    *   [X] Modificar `process_document_standalone` para llamar a `ingest_document_pipeline` y recibir `(count, pks, chunk_data)`.
    *   [X] Preparar `chunks_for_pg` (lista de dicts) con `embedding_id` de Milvus.
    *   [X] Llamar a `bulk_insert_chunks_sync` para guardar chunks en PostgreSQL.
    *   [X] Actualizar `documents.status` y `documents.chunk_count` basado en el resultado de la inserción en PG.
    *   [X] Mejorar manejo de errores (fallo PG post-Milvus, intentar limpieza Milvus).
*   **API (`app/api/v1/endpoints/ingest.py`):**
    *   [X] Revisar/Adaptar `DELETE /{document_id}` para asegurar limpieza de `document_chunks` (Confirmado: `CASCADE` ok).
    *   [ ] (Opcional) Considerar añadir chequeo de conteo en `document_chunks` (PG) en endpoints de status. (Pospuesto)

**Configuración y Dependencias:**

*   [X] Añadir `tiktoken` a `pyproject.toml`.
*   [X] Verificar todas las dependencias (`pyproject.toml`).
*   [X] Actualizar `settings` (`config.py`) - Añadido `TIKTOKEN_ENCODING_NAME`.

**Testing y Documentación:**

*   [ ] Actualizar README.md de `ingest-service` para reflejar la nueva arquitectura (v0.3.0). (Pendiente - Hacer después de merge)

**Progreso General:** ¡La refactorización principal del código está completa según el plan! Quedan pendientes los tests y la actualización de la documentación principal.