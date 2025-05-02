**Objetivo:** Modificar el `ingest-service` para utilizar Cloud SQL (PostgreSQL), Google Cloud Storage (GCS) y Zilliz Cloud como backends, aprovechando Workload Identity para la autenticación con los servicios de GCP. Se mantendrá el Redis interno para Celery.

**Información Clave Confirmada:**

*   **Cloud SQL Instance Connection Name:** `praxis-study-458413-f4:southamerica-west1:atenex-db`
*   **Cloud SQL DB Name:** `atenex` (Usaremos este, ya que coincide con la configuración actual, no `atenex-db`)
*   **Cloud SQL User:** `postgres` (Asumido, verifica si es otro)
*   **GCS Bucket Name:** `atenex`
*   **Zilliz Cloud Endpoint URI:** `https://in03-0afab716eb46d7f.serverless.gcp-us-west1.cloud.zilliz.com`
*   **Zilliz Collection Name:** `atenex-vector-db`
*   **GCP Service Account:** `atenex-backend@praxis-study-458413-f4.iam.gserviceaccount.com`
*   **Redis:** Se mantiene la configuración actual (interno en K8s).

---

**Plan Detallado de Refactorización (Ingest Service)**

**1. Actualización de Dependencias (`pyproject.toml`)**

*   **Objetivo:** Añadir las librerías cliente para los servicios GCP y Zilliz, y eliminar las innecesarias (MinIO).
*   **Acciones:**
    *   **Eliminar:** La línea correspondiente a `minio`.
    *   **Añadir:**
        *   `google-cloud-storage`: Para interactuar con GCS.
        *   `cloud-sql-python-connector[asyncpg,psycopg2]`: Para conectar a Cloud SQL usando ambos drivers necesarios (API y Worker). Incluir ambos extras `[asyncpg,psycopg2]`.
        *   `google-auth`: Necesaria para la autenticación automática (Workload Identity).
        *   *(Verificar)*: `pymilvus>=2.4.1` ya debería estar, confirmar versión. `redis` ya está como extra de `celery`.
*   **Instrucción:** Modifica tu `pyproject.toml` para reflejar estos cambios. Luego ejecuta `poetry lock` para actualizar el lockfile y, opcionalmente, `poetry install` para probar localmente.

**2. Actualización de la Configuración (`app/core/config.py`)**

*   **Objetivo:** Reflejar las nuevas dependencias gestionadas y eliminar las configuraciones obsoletas.
*   **Acciones:**
    *   **Eliminar Variables:** `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_BUCKET_NAME`, `MINIO_USE_SECURE`.
    *   **Añadir Nuevas Variables:**
        *   `GCS_BUCKET_NAME: str`: Para el nombre del bucket GCS (valor: `"atenex"`).
        *   `POSTGRES_INSTANCE_CONNECTION_NAME: str`: Para el nombre de conexión de la instancia Cloud SQL (valor: `"praxis-study-458413-f4:southamerica-west1:atenex-db"`).
        *   `ZILLIZ_URI: str`: Para el endpoint de Zilliz Cloud (valor: `"https://in03-0afab716eb46d7f.serverless.gcp-us-west1.cloud.zilliz.com"`).
        *   `ZILLIZ_API_KEY: SecretStr`: Para la clave API de Zilliz Cloud (el valor vendrá del K8s Secret).
    *   **Modificar Variables Existentes:**
        *   `MILVUS_URI`: **Eliminar** esta variable (se usará `ZILLIZ_URI`).
        *   `MILVUS_COLLECTION_NAME`: Cambiar el valor por defecto o el valor en el ConfigMap a `"atenex-vector-db"`.
        *   `POSTGRES_SERVER`, `POSTGRES_PORT`: **Eliminar** estas variables (el conector de Cloud SQL las maneja internamente usando `POSTGRES_INSTANCE_CONNECTION_NAME`).
    *   **(Opcional) Añadir:** `POSTGRES_IP_TYPE: str = "PRIVATE"` para configurar el tipo de IP a usar por el conector Cloud SQL (por defecto es `PRIVATE`).
*   **Instrucción:** Realiza estos cambios en tu clase `Settings` dentro de `app/core/config.py`. Asegúrate que los valores por defecto o los cargados desde el entorno/`.env` sean correctos para pruebas locales. Recuerda que los secretos (`POSTGRES_PASSWORD`, `ZILLIZ_API_KEY`) serán inyectados en producción. Actualiza también la sección de logging al final del archivo para mostrar las nuevas variables y quitar las obsoletas.

**3. Refactorizar Cliente de Base de Datos (`app/db/postgres_client.py`)**

*   **Objetivo:** Utilizar `cloud-sql-python-connector` para establecer conexiones a Cloud SQL tanto para `asyncpg` (API) como para `SQLAlchemy` (Worker), aprovechando Workload Identity.
*   **Acciones:**
    *   **Importar:** Añadir imports para `google.cloud.sql.connector.Connector`, `google.auth`, `IPTypes`, `NullPool` de SQLAlchemy.
    *   **Inicializar Connector:** Crear una instancia global del `Connector` de Google fuera de las funciones `get_db_pool` y `get_sync_engine`. Intentar obtener credenciales por defecto (`google.auth.default()`) para usar Workload Identity.
    *   **Modificar `get_db_pool` (asyncpg):**
        *   Reemplazar la lógica de `asyncpg.create_pool` que usa `host`, `port`, `user`, `password`.
        *   En su lugar, definir una función interna `getconn` que use `connector.connect_async(...)` pasando el `INSTANCE_CONNECTION_NAME`, el driver `"asyncpg"`, `user`, `password`, `db`, y `ip_type`.
        *   Llamar a `asyncpg.create_pool(connector=getconn, ...)` pasando la *función* `getconn` al argumento `connector` del pool.
        *   Mantener la lógica de `init_connection` para setear los codecs JSON si es necesario (se puede hacer después de obtener la conexión en `getconn`).
    *   **Modificar `get_sync_engine` (SQLAlchemy):**
        *   Reemplazar la lógica de `create_engine` que construye un DSN con host/port.
        *   Usar `create_engine("postgresql+psycopg2://", creator=lambda: connector.connect(...), poolclass=NullPool)`.
        *   El `creator` lambda llamará a `connector.connect(...)` pasando el `INSTANCE_CONNECTION_NAME`, el driver `"pg8000"` o `"psycopg2"`, `user`, `password`, `db`, y `ip_type`.
        *   **Importante:** Usar `poolclass=NullPool`, ya que el conector de Google gestiona su propio pool subyacente.
    *   **Modificar `dispose_sync_engine`:** Añadir opcionalmente `connector.close()` si se quiere cerrar explícitamente el conector global al apagar (aunque generalmente no es estrictamente necesario).
    *   **Funciones CRUD:** La lógica interna de `set_status_sync`, `create_document_record`, etc., **no necesita cambios**, ya que operan sobre el objeto `Engine` o `Pool/Connection` que ahora se obtiene a través del conector.
*   **Instrucción:** Aplica los cambios estructurales descritos a las funciones de obtención de conexión/engine. Referencia las nuevas variables de configuración (`settings.POSTGRES_INSTANCE_CONNECTION_NAME`, etc.).

**4. Refactorizar Cliente de Almacenamiento (`app/services/minio_client.py` -> `gcs_client.py`)**

*   **Objetivo:** Reemplazar la interacción con MinIO por GCS usando `google-cloud-storage`.
*   **Acciones:**
    *   **Renombrar Archivo:** Cambia `minio_client.py` a `gcs_client.py`.
    *   **Reescribir Clase:**
        *   Define una nueva clase `GCSClient`.
        *   Elimina la dependencia de `minio`. Importa `google.cloud.storage` y `google.auth`.
        *   En `__init__`:
            *   Usa `settings.GCS_BUCKET_NAME`.
            *   Inicializa `storage.Client()` (las credenciales se obtendrán automáticamente vía Workload Identity/ADC).
            *   Obtén el objeto `bucket` usando `client.get_bucket(...)`. Maneja `NotFound` y errores de credenciales.
        *   **Reimplementar Métodos:** Adapta la lógica de los métodos existentes (`upload_file_async`, `check_file_exists_async`, `delete_file_async`, `download_file_sync`, `check_file_exists_sync`, `delete_file_sync`) para usar los métodos equivalentes del cliente/bucket/blob de GCS (ej. `blob.upload_from_string`, `blob.exists`, `blob.delete`, `blob.download_to_filename`, `blob.download_as_bytes`).
        *   **Manejo Asíncrono:** Las operaciones del SDK de GCS son síncronas. Usa `loop.run_in_executor(None, ...)` dentro de los métodos `_async` para ejecutarlas sin bloquear el loop de FastAPI.
        *   **Manejo de Errores:** Captura excepciones específicas de GCP (`google.api_core.exceptions`) como `NotFound`, `Forbidden`, etc., y relánzalas como `GCSError` (una nueva clase de excepción personalizada similar a `MinioError`).
    *   **Crear Método `read_file_sync`:** Añade un método síncrono que use `blob.download_as_bytes()` para que el worker pueda obtener el contenido directamente sin escribir a disco.
*   **Instrucción:** Reemplaza el contenido de `app/services/minio_client.py` con la nueva implementación para `GCSClient` en `app/services/gcs_client.py`.

**5. Actualizar Usos del Cliente de Almacenamiento**

*   **Objetivo:** Reemplazar todas las referencias a `MinioClient` por `GCSClient`.
*   **Archivos Afectados:**
    *   `app/api/v1/endpoints/ingest.py`:
        *   Importar `GCSClient`, `GCSError` desde `.services.gcs_client`.
        *   Crear una función `get_gcs_client` similar a `get_minio_client`.
        *   Actualizar la dependencia `Depends(get_minio_client)` a `Depends(get_gcs_client)`.
        *   Cambiar `MinioError` por `GCSError` en los `except`.
    *   `app/tasks/process_document.py`:
        *   Importar `GCSClient`, `GCSError` desde `.services.gcs_client`.
        *   Actualizar la variable global `minio_client` a `gcs_client`.
        *   Actualizar la lógica en `init_worker_resources` para inicializar `GCSClient`.
        *   Cambiar `MinioError` por `GCSError` en los `except`.
        *   **Modificar Lógica de Descarga:**
            *   Eliminar el `with tempfile.TemporaryDirectory()`.
            *   Reemplazar la llamada a `minio_client.download_file_sync(object_name, str(temp_file_path_obj))` por `file_bytes = gcs_client.read_file_sync(object_name)`.
            *   Asegurarse de que `file_bytes` no sea `None`.
            *   Pasar `file_bytes=file_bytes` a `ingest_document_pipeline` en lugar de `file_path`.
*   **Instrucción:** Revisa estos archivos y reemplaza sistemáticamente las importaciones, nombres de clase, dependencias, capturas de excepciones y la lógica de descarga/lectura del worker.

**6. Actualizar Conexión a Vector DB (`app/services/ingest_pipeline.py` y `app/api/v1/endpoints/ingest.py`)**

*   **Objetivo:** Conectar a Zilliz Cloud usando URI y Token/API Key.
*   **Archivos Afectados:**
    *   `app/services/ingest_pipeline.py`:
        *   En `_ensure_milvus_connection_and_collection`:
            *   Usa `settings.ZILLIZ_URI` y `settings.ZILLIZ_API_KEY.get_secret_value()` en la llamada `connections.connect(...)`. Asegúrate de pasar el `token=...`.
            *   Usa un `alias` específico (ej. `"pipeline_worker_zilliz"`).
        *   En `_create_milvus_collection`: Asegúrate que usa `settings.MILVUS_COLLECTION_NAME` (que ahora debería ser `"atenex-vector-db"`).
        *   En `delete_milvus_chunks`: Asegúrate que usa la colección obtenida con el alias correcto.
    *   `app/api/v1/endpoints/ingest.py`:
        *   En `_get_milvus_collection_sync`:
            *   Usa `settings.ZILLIZ_URI` y `settings.ZILLIZ_API_KEY.get_secret_value()` en `connections.connect(...)`.
            *   Usa un `alias` diferente (ej. `"api_sync_helper_zilliz"`).
        *   En `_get_milvus_chunk_count_sync` y `_delete_milvus_sync`: Asegúrate que usan la colección obtenida con el alias correcto de la API.
*   **Instrucción:** Modifica las funciones de conexión en ambos archivos para usar los nuevos detalles de configuración de Zilliz (URI y Token). Cambia el `MILVUS_COLLECTION_NAME` usado para crear/acceder a la colección a `"atenex-vector-db"`.

**7. Actualizar Llamada al Pipeline (`app/tasks/process_document.py`)**

*   **Objetivo:** Pasar `file_bytes` en lugar de `file_path` al pipeline.
*   **Acción:** En la llamada a `ingest_document_pipeline` dentro de `process_document_standalone`, asegúrate de pasar el argumento `file_bytes=file_bytes` (que obtuviste del GCS client) y elimina el argumento `file_path`.
*   **Instrucción:** Modifica la línea donde se llama a `ingest_document_pipeline`.

**8. Verificar Código Restante**

*   Revisa si hay alguna otra referencia directa a MinIO o a la configuración antigua de Milvus/PostgreSQL en otros archivos (aunque parece poco probable según la estructura).

---

**Checklist de Refactorización (Ingest Service):**

*   [x] `pyproject.toml`: Dependencias actualizadas (GCS, Cloud SQL Connector añadidos; MinIO eliminado).
*   [x] `app/core/config.py`: Variables de configuración actualizadas (GCS, Cloud SQL Instance Name, Zilliz URI/Key añadidas; MinIO, Milvus URI, PG Server/Port eliminadas). Nombre de colección Milvus actualizado.
*   [ ] `app/db/postgres_client.py`: `get_db_pool` modificado para usar Cloud SQL Connector (`asyncpg`).
*   [ ] `app/db/postgres_client.py`: `get_sync_engine` modificado para usar Cloud SQL Connector (`psycopg2`/`pg8000`).
*   [x] `app/services/minio_client.py`: Renombrado a `gcs_client.py`.
*   [x] `app/services/gcs_client.py`: Implementación completa de `GCSClient` usando `google-cloud-storage`.
*   [x] `app/services/gcs_client.py`: Incluye método `read_file_sync`.
*   [x] `app/api/v1/endpoints/ingest.py`: Importaciones y dependencias actualizadas para usar `GCSClient`. Captura de `GCSError`.
*   [x] `app/tasks/process_document.py`: Importaciones y variables globales actualizadas para usar `GCSClient`.
*   [x] `app/tasks/process_document.py`: Lógica de `init_worker_resources` actualizada para `GCSClient`.
*   [x] `app/tasks/process_document.py`: Lógica de descarga modificada para usar `read_file_sync` y obtener `bytes`.
*   [x] `app/tasks/process_document.py`: Llamada a `ingest_document_pipeline` modificada para pasar `file_bytes`.
*   [x] `app/services/ingest_pipeline.py`: `_ensure_milvus_connection_and_collection` modificada para Zilliz Cloud (URI, Token, Alias).
*   [x] `app/services/ingest_pipeline.py`: `_create_milvus_collection` usa el nombre de colección `"atenex-vector-db"`.
*   [x] `app/services/ingest_pipeline.py`: `delete_milvus_chunks` usa la conexión/alias correctos.
*   [x] `app/api/v1/endpoints/ingest.py`: `_get_milvus_collection_sync` modificada para Zilliz Cloud (URI, Token, Alias).
*   [x] `app/api/v1/endpoints/ingest.py`: `_delete_milvus_sync` modificada para Zilliz Cloud y usa colección correcta.
*   [x] `app/api/v1/endpoints/ingest.py`: `_get_milvus_chunk_count_sync` usa la colección correcta.
*   [ ] (Revisión General): No quedan referencias a MinIO o configuraciones obsoletas.

---

**Verificación Detallada vs. Plan (Discrepancias Encontradas):**

Aquí detallo los puntos del plan que **no** se reflejan (o están incompletos) en el código adjunto:

1.  **Actualización de Dependencias (`pyproject.toml`)**
    *   **Estado:** **NO COMPLETADO.**
    *   **Código Actual:** Todavía incluye `minio`. Faltan `google-cloud-storage`, `cloud-sql-python-connector[asyncpg,psycopg2]`, y `google-auth`.
    *   **Acción Necesaria:** Modificar `pyproject.toml` según el Plan (Paso 1).

2.  **Actualización de la Configuración (`app/core/config.py`)**
    *   **Estado:** **NO COMPLETADO.**
    *   **Código Actual:** Todavía contiene variables de MinIO (`MINIO_*`), PostgreSQL (`POSTGRES_SERVER`, `POSTGRES_PORT`), y Milvus (`MILVUS_URI`). Faltan las variables nuevas para GCS (`GCS_BUCKET_NAME`), Cloud SQL (`POSTGRES_INSTANCE_CONNECTION_NAME`), y Zilliz (`ZILLIZ_URI`, `ZILLIZ_API_KEY`). El nombre de la colección Milvus (`MILVUS_COLLECTION_NAME`) sigue como `"document_chunks_minilm"` por defecto.
    *   **Acción Necesaria:** Modificar la clase `Settings` en `app/core/config.py` según el Plan (Paso 2), eliminando las variables obsoletas y añadiendo las nuevas. Actualizar el nombre de la colección por defecto.

3.  **Refactorizar Cliente de Base de Datos (`app/db/postgres_client.py`)**
    *   **Estado:** **NO COMPLETADO.**
    *   **Código Actual:** Mantiene la lógica original usando `asyncpg.create_pool` y `sqlalchemy.create_engine` con host/port/usuario/contraseña directos. No hay importaciones ni uso de `cloud-sql-python-connector`.
    *   **Acción Necesaria:** Reescribir las funciones `get_db_pool` y `get_sync_engine` para utilizar `google.cloud.sql.connector.Connector` como se describe en el Plan (Paso 3).

4.  **Refactorizar Cliente de Almacenamiento (`app/services/gcs_client.py`)**
    *   **Estado:** **PARCIALMENTE COMPLETADO.**
    *   **Código Actual:** El archivo existe y contiene una clase `GCSClient` que usa `google-cloud-storage`. *Sin embargo*, hay un error en el código proporcionado: la definición de `download_file_sync` está fuera de la clase `GCSClient`. Además, falta el método `read_file_sync`. La estructura general de la clase parece correcta pero necesita estas correcciones.
    *   **Acción Necesaria:** Mover la definición de `download_file_sync` dentro de la clase `GCSClient`. Implementar el método `read_file_sync` que use `blob.download_as_bytes()`.

5.  **Actualizar Usos del Cliente de Almacenamiento**
    *   **Estado:** **NO COMPLETADO.**
    *   **Código Actual (`app/api/v1/endpoints/ingest.py`):** Sigue importando y usando `MinioClient` y `MinioError`. La dependencia `Depends` sigue apuntando a `get_minio_client`.
    *   **Código Actual (`app/tasks/process_document.py`):** Sigue importando `MinioClient`, `MinioError`. La variable global y la inicialización en `init_worker_resources` usan `MinioClient`. La lógica de descarga todavía usa `download_file_sync` y `tempfile`, y pasa `file_path` al pipeline.
    *   **Acción Necesaria:** Modificar ambos archivos (`ingest.py` y `process_document.py`) para importar y usar `GCSClient`, `GCSError`, y `get_gcs_client`. Actualizar la lógica de descarga en el worker para usar `read_file_sync` y pasar `file_bytes` al pipeline, como se detalla en el Plan (Paso 5).

6.  **Actualizar Conexión a Vector DB (Zilliz Cloud)**
    *   **Estado:** **NO COMPLETADO.**
    *   **Código Actual (`app/services/ingest_pipeline.py`):** `_ensure_milvus_connection_and_collection` sigue usando `settings.MILVUS_URI` y no utiliza `token`. El nombre de la colección es el de `settings.MILVUS_COLLECTION_NAME`.
    *   **Código Actual (`app/api/v1/endpoints/ingest.py`):** Los helpers `_get_milvus_collection_sync`, `_get_milvus_chunk_count_sync`, `_delete_milvus_sync` usan `settings.MILVUS_URI` sin token.
    *   **Acción Necesaria:** Modificar las funciones de conexión (`_ensure_milvus_connection_and_collection`, `_get_milvus_collection_sync`) en ambos archivos para usar `settings.ZILLIZ_URI` y `settings.ZILLIZ_API_KEY` (con `token=...` en `connections.connect`). Actualizar el nombre de la colección a `"atenex-vector-db"` donde se use (`_create_milvus_collection`, y donde se acceda `Collection(name=...)`). Usar alias distintos para API y Worker (Plan, Paso 6).

7.  **Actualizar Llamada al Pipeline (`app/tasks/process_document.py`)**
    *   **Estado:** **NO COMPLETADO.**
    *   **Código Actual:** La llamada a `ingest_document_pipeline` sigue pasando `file_path=temp_file_path_obj`.
    *   **Acción Necesaria:** Modificar la llamada para pasar `file_bytes=file_bytes` después de leer desde GCS (Plan, Paso 7).

8.  **Verificar Código Restante**
    *   **Estado:** **PARECE COMPLETO.** No se observaron otras referencias obvias a MinIO o configuraciones obsoletas en los archivos restantes.

**En Resumen:**

El codebase actual **ha iniciado** la refactorización creando `gcs_client.py`, pero la mayor parte del trabajo crítico **aún no se ha realizado**. Necesitas aplicar los cambios detallados en el plan anterior para las dependencias, la configuración, las conexiones a la base de datos, el uso del cliente GCS, las conexiones a Zilliz Cloud y la lógica del worker.

---

**Checklist de Refactorización (Estado Actual vs. Plan)**

*   [ ] `pyproject.toml`: Dependencias actualizadas (GCS, Cloud SQL Connector añadidos; MinIO eliminado). **<- PENDIENTE**
*   [ ] `app/core/config.py`: Variables de configuración actualizadas (GCS, Cloud SQL Instance Name, Zilliz URI/Key añadidas; MinIO, Milvus URI, PG Server/Port eliminadas). Nombre de colección Milvus actualizado. **<- PENDIENTE**
*   [ ] `app/db/postgres_client.py`: `get_db_pool` modificado para usar Cloud SQL Connector (`asyncpg`). **<- PENDIENTE**
*   [ ] `app/db/postgres_client.py`: `get_sync_engine` modificado para usar Cloud SQL Connector (`psycopg2`/`pg8000`). **<- PENDIENTE**
*   [ ] `app/services/minio_client.py`: Renombrado a `gcs_client.py`. **<- HECHO**
*   [ ] `app/services/gcs_client.py`: Implementación completa de `GCSClient` usando `google-cloud-storage`. **<- PARCIAL (Falta `read_file_sync`, corregir scope `download_file_sync`)**
*   [ ] `app/services/gcs_client.py`: Incluye método `read_file_sync`. **<- PENDIENTE**
*   [ ] `app/api/v1/endpoints/ingest.py`: Importaciones y dependencias actualizadas para usar `GCSClient`. Captura de `GCSError`. **<- PENDIENTE**
*   [ ] `app/tasks/process_document.py`: Importaciones y variables globales actualizadas para usar `GCSClient`. **<- PENDIENTE**
*   [ ] `app/tasks/process_document.py`: Lógica de `init_worker_resources` actualizada para `GCSClient`. **<- PENDIENTE**
*   [ ] `app/tasks/process_document.py`: Lógica de descarga modificada para usar `read_file_sync` y obtener `bytes`. **<- PENDIENTE**
*   [ ] `app/tasks/process_document.py`: Llamada a `ingest_document_pipeline` modificada para pasar `file_bytes`. **<- PENDIENTE**
*   [ ] `app/services/ingest_pipeline.py`: `_ensure_milvus_connection_and_collection` modificada para Zilliz Cloud (URI, Token, Alias). **<- PENDIENTE**
*   [ ] `app/services/ingest_pipeline.py`: `_create_milvus_collection` usa el nombre de colección `"atenex-vector-db"`. **<- PENDIENTE**
*   [ ] `app/services/ingest_pipeline.py`: `delete_milvus_chunks` usa la conexión/alias correctos. **<- PENDIENTE**
*   [ ] `app/api/v1/endpoints/ingest.py`: `_get_milvus_collection_sync` modificada para Zilliz Cloud (URI, Token, Alias). **<- PENDIENTE**
*   [ ] `app/api/v1/endpoints/ingest.py`: `_delete_milvus_sync` modificada para Zilliz Cloud y usa colección correcta. **<- PENDIENTE**
*   [ ] `app/api/v1/endpoints/ingest.py`: `_get_milvus_chunk_count_sync` usa la colección correcta. **<- PENDIENTE**
*   [ ] (Revisión General): No quedan referencias a MinIO o configuraciones obsoletas. **<- PENDIENTE**