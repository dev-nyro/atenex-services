# Plan de Refactorización: Query Service y API Gateway (Post-Ingest Service)

**Objetivo:** Actualizar los microservicios `query-service` y `api-gateway` para utilizar los servicios gestionados configurados para `ingest-service`: Cloud SQL (PostgreSQL) y Zilliz Cloud (Vector DB), aprovechando Workload Identity para la autenticación con GCP.

**Contexto:** Esta refactorización asume que `ingest-service` ya ha sido modificado y desplegado con éxito utilizando Cloud SQL, GCS y Zilliz Cloud.

**Información Clave de los Servicios Gestionados:**

*   **Cloud SQL Instance Connection Name:** `praxis-study-458413-f4:southamerica-west1:atenex-db`
*   **Cloud SQL DB Name:** `atenex`
*   **Cloud SQL User:** `postgres` (o el usuario configurado)
*   **Zilliz Cloud Endpoint URI:** `https://in03-0afab716eb46d7f.serverless.gcp-us-west1.cloud.zilliz.com`
*   **Zilliz Collection Name:** `atenex-vector-db`
*   **GCP Service Account (para Workload Identity):** `atenex-backend@praxis-study-458413-f4.iam.gserviceaccount.com`

---

## A. Refactorización `query-service`

**Archivos Afectados:**

1.  `query-service/pyproject.toml`
2.  `query-service/app/core/config.py`
3.  `query-service/app/db/postgres_client.py`
4.  `query-service/app/pipelines/rag_pipeline.py` (o donde se conecte a Milvus/Zilliz)
5.  `query-service/k8s/deployment.yaml` (para añadir `serviceAccountName`)
6.  `query-service/k8s/configmap.yaml`
7.  `query-service/k8s/secret.example.yaml` (para añadir `ZILLIZ_API_KEY`)

**Pasos Detallados:**

1.  **Actualizar Dependencias (`pyproject.toml`):**
    *   Añadir `cloud-sql-python-connector[asyncpg]` (ya que `query-service` usa `asyncpg`).
    *   Añadir `google-auth`.
    *   Verificar/actualizar `pymilvus>=2.4.1`.
    *   *(Opcional: Añadir `google-cloud-logging` para integración directa con Cloud Logging)*.
    *   Ejecutar `poetry lock` y `poetry install` (localmente).

2.  **Actualizar Configuración (`app/core/config.py`):**
    *   **Eliminar:** `QUERY_POSTGRES_SERVER`, `QUERY_POSTGRES_PORT`, `QUERY_MILVUS_URI`.
    *   **Añadir:**
        *   `QUERY_POSTGRES_INSTANCE_CONNECTION_NAME: str = "praxis-study-458413-f4:southamerica-west1:atenex-db"`
        *   `QUERY_ZILLIZ_URI: str = "https://in03-0afab716eb46d7f.serverless.gcp-us-west1.cloud.zilliz.com"`
        *   `QUERY_ZILLIZ_API_KEY: SecretStr`
    *   **Modificar:**
        *   `QUERY_MILVUS_COLLECTION_NAME`: Asegurar que el valor sea `"atenex-vector-db"`.
        *   `QUERY_EMBEDDING_DIMENSION`: Asegurar que sea `384` (para coincidir con `ingest-service`).
    *   Actualizar el logging de configuración al final del archivo.

3.  **Refactorizar Cliente DB (`app/db/postgres_client.py`):**
    *   Aplicar la **misma lógica** que en `ingest-service` para `get_db_pool` usando `cloud-sql-python-connector` con el driver `"asyncpg"`.
    *   Asegurar que las funciones CRUD existentes (ej. `log_query_interaction`, `create_chat`, `get_chat_messages`, etc.) sigan funcionando con el nuevo pool.

4.  **Refactorizar Conexión Vector DB (`app/pipelines/rag_pipeline.py` o similar):**
    *   Localizar el punto donde se inicializa `MilvusEmbeddingRetriever` o se establece la conexión `pymilvus`.
    *   Modificar la conexión para usar `settings.ZILLIZ_URI` y `settings.ZILLIZ_API_KEY.get_secret_value()`.
        *   Si usas `pymilvus` directamente: `connections.connect(alias="query_zilliz", uri=settings.ZILLIZ_URI, token=settings.ZILLIZ_API_KEY.get_secret_value())`.
        *   Si usas `MilvusEmbeddingRetriever` de Haystack (si aún queda algo): อาจจะต้องตรวจสอบว่าเวอร์ชันล่าสุดรองรับ `token` หรือไม่ หรืออาจจะต้องใช้ `pymilvus` โดยตรงสำหรับการเชื่อมต่อ (Revisar si la versión más reciente soporta `token` o si se necesita usar `pymilvus` directamente para la conexión).
    *   Asegurar que se use `settings.MILVUS_COLLECTION_NAME` (`"atenex-vector-db"`).

5.  **Actualizar Manifests K8s:**
    *   **`deployment.yaml`:** Añadir `serviceAccountName: atenex-backend-ksa` (o un KSA específico si se prefiere).
    *   **`configmap.yaml`:** Reflejar los cambios de variables de `config.py`.
    *   **`secret.example.yaml` / Comando `kubectl create secret`:** Añadir la clave `QUERY_ZILLIZ_API_KEY`.

---

## B. Refactorización `api-gateway`

**Archivos Afectados:**

1.  `api-gateway/pyproject.toml`
2.  `api-gateway/app/core/config.py`
3.  `api-gateway/app/db/postgres_client.py`
4.  `api-gateway/k8s/deployment.yaml` (para añadir `serviceAccountName`)
5.  `api-gateway/k8s/configmap.yaml`

**Pasos Detallados:**

1.  **Actualizar Dependencias (`pyproject.toml`):**
    *   Añadir `cloud-sql-python-connector[asyncpg]`.
    *   Añadir `google-auth`.
    *   *(Opcional: Añadir `google-cloud-logging`)*.
    *   Ejecutar `poetry lock` y `poetry install` (localmente).

2.  **Actualizar Configuración (`app/core/config.py`):**
    *   **Eliminar:** `GATEWAY_POSTGRES_SERVER`, `GATEWAY_POSTGRES_PORT`.
    *   **Añadir:** `GATEWAY_POSTGRES_INSTANCE_CONNECTION_NAME: str = "praxis-study-458413-f4:southamerica-west1:atenex-db"`
    *   Actualizar el logging de configuración al final del archivo.

3.  **Refactorizar Cliente DB (`app/db/postgres_client.py`):**
    *   Aplicar la **misma lógica** que en `ingest-service` (y `query-service`) para `get_db_pool` usando `cloud-sql-python-connector` con el driver `"asyncpg"`.
    *   Asegurar que las funciones existentes (`get_user_by_email`, `get_user_by_id`, `update_user_company`) funcionen con el nuevo pool.

4.  **Actualizar Manifests K8s:**
    *   **`deployment.yaml`:** Añadir `serviceAccountName: atenex-backend-ksa` (o un KSA específico).
    *   **`configmap.yaml`:** Reflejar los cambios de variables de `config.py` (quitar server/port, añadir instance name).

---

**Checklist General de Refactorización (Query y Gateway):**

*   [ ] **Query Service:** `pyproject.toml` actualizado.
*   [ ] **Query Service:** `config.py` actualizado (Cloud SQL, Zilliz).
*   [ ] **Query Service:** `postgres_client.py` usa Cloud SQL Connector.
*   [ ] **Query Service:** Conexión a Zilliz Cloud actualizada (URI/Token/Collection).
*   [ ] **Query Service:** `deployment.yaml` incluye `serviceAccountName`.
*   [ ] **Query Service:** `configmap.yaml` y `secret.yaml` actualizados.
*   [ ] **API Gateway:** `pyproject.toml` actualizado.
*   [ ] **API Gateway:** `config.py` actualizado (Cloud SQL).
*   [ ] **API Gateway:** `postgres_client.py` usa Cloud SQL Connector.
*   [ ] **API Gateway:** `deployment.yaml` incluye `serviceAccountName`.
*   [ ] **API Gateway:** `configmap.yaml` actualizado.
*   [ ] (Ambos) Pruebas locales/staging realizadas después de la refactorización.