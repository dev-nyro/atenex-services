# Plan de Refactorización: Migración de Milvus Standalone a Zilliz Cloud

## 1. Introducción

Este documento detalla el plan para refactorizar los microservicios `ingest-service` y `query-service` del backend de Atenex con el objetivo de reemplazar la instancia actual de Milvus Standalone por Zilliz Cloud, un servicio gestionado de base de datos vectorial compatible con Milvus.

**Objetivo:** Migrar la capa de almacenamiento vectorial a Zilliz Cloud para aprovechar sus beneficios de escalabilidad, gestión y posibles mejoras de rendimiento, manteniendo la compatibilidad con la API de Milvus utilizada actualmente (`pymilvus`).

**Servicios Afectados:**

1.  **`ingest-service`**: Responsable de indexar los vectores de embeddings de los chunks de documentos en Milvus.
2.  **`query-service`**: Responsable de realizar búsquedas vectoriales en Milvus para recuperar chunks relevantes durante el proceso RAG.

## 2. Prerrequisitos

1.  **Cuenta de Zilliz Cloud:** Una cuenta activa en Zilliz Cloud.
2.  **Cluster Zilliz:** Un cluster Zilliz Cloud creado y en estado "Running".
    *   **Endpoint Público:** `https://in03-0afab716eb46d7f.serverless.gcp-us-west1.cloud.zilliz.com`
    *   **Compatibilidad:** Milvus 2.5.x
3.  **API Key de Zilliz:** Una API Key generada en Zilliz Cloud para la autenticación. Esta clave **debe** tratarse como un secreto.

## 3. Resumen de Cambios Técnicos Clave

1.  **Actualización de Dependencia `pymilvus`:** Se actualizará la versión de `pymilvus` en ambos microservicios a `2.5.3` para asegurar la compatibilidad explícita con la versión soportada por Zilliz Cloud (Milvus 2.5.x).
2.  **Configuración de Conexión:**
    *   Se actualizará la URI de Milvus (`MILVUS_URI`) en la configuración de ambos servicios para apuntar al **Endpoint Público HTTPS** de Zilliz Cloud.
    *   Se añadirá una nueva variable de configuración (`ZILLIZ_API_KEY`) en ambos servicios para almacenar la API Key de Zilliz (como `SecretStr`).
3.  **Autenticación:** Se modificarán las llamadas a `pymilvus.connections.connect()` en ambos servicios para incluir el parámetro `token` utilizando la nueva API Key, en lugar de cualquier método de autenticación previo (si existiera).
4.  **Configuración Kubernetes:**
    *   Se crearán nuevos **Secrets** en Kubernetes (`ingest-service-secrets`, `query-service-secrets` o uno común) para almacenar de forma segura la Zilliz API Key.
    *   Se actualizarán los **ConfigMaps** (`ingest-service-config`, `query-service-config`) para reflejar la nueva `MILVUS_URI`.
    *   Se actualizarán los **Deployments** de ambos servicios para montar el nuevo Secret y exponer la API Key como variable de entorno.

## 4. Pasos Detallados de Refactorización

### 4.1. `ingest-service`

1.  **Actualizar Dependencias (`pyproject.toml`):**
    *   Cambiar la dependencia de `pymilvus`:
        ```toml
        # Antes: pymilvus = ">=2.4.1,<2.5.0"
        # Después:
        pymilvus = "==2.5.3"
        ```
    *   Ejecutar `poetry lock && poetry install` para actualizar.

2.  **Actualizar Configuración (`app/core/config.py`):**
    *   Añadir la nueva variable `ZILLIZ_API_KEY`:
        ```python
        class Settings(BaseSettings):
            # ... otras variables ...
            ZILLIZ_API_KEY: SecretStr = Field(description="API Key for Zilliz Cloud connection.")
            # ...
        ```
    *   Actualizar el valor por defecto o la variable de entorno para `MILVUS_URI`:
        ```python
        class Settings(BaseSettings):
            # ...
            # Antes: DEFAULT_MILVUS_URI = f"http://{MILVUS_K8S_SVC}:{MILVUS_K8S_PORT_DEFAULT}"
            # Después: ZILLIZ_ENDPOINT_DEFAULT = "https://in03-0afab716eb46d7f.serverless.gcp-us-west1.cloud.zilliz.com"
            MILVUS_URI: str = Field(
                default=ZILLIZ_ENDPOINT_DEFAULT, # Usar el endpoint de Zilliz por defecto
                description="Milvus connection URI (Zilliz Cloud HTTPS endpoint)."
            )
            # ...
             @field_validator('MILVUS_URI', mode='before')
             @classmethod
             def validate_milvus_uri(cls, v: Any) -> str:
                 # Asegurar que el validador maneja correctamente HTTPS y no añade http://
                 if not isinstance(v, str):
                     raise ValueError("MILVUS_URI must be a string.")
                 v_strip = v.strip()
                 if not v_strip.startswith("https://"): # Zilliz usa HTTPS
                     raise ValueError(f"Invalid Zilliz URI: Must start with https://. Received: '{v_strip}'")
                 try:
                     parsed = urlparse(v_strip)
                     if not parsed.hostname:
                         raise ValueError(f"Invalid URI: Missing hostname in '{v_strip}'")
                     return v_strip # Mantener HTTPS URI tal cual
                 except Exception as e:
                     raise ValueError(f"Invalid Milvus URI format '{v_strip}': {e}") from e
            # ...
             @field_validator('ZILLIZ_API_KEY', mode='before')
             @classmethod
             def check_zilliz_key(cls, v: Any, info: ValidationInfo) -> Any:
                  if v is None or v == "":
                     raise ValueError(f"Required secret field 'INGEST_ZILLIZ_API_KEY' cannot be empty.")
                  return v
            # ... (resto de validadores) ...
            # Loguear la nueva variable al inicio
            temp_log.info(f"  ZILLIZ_API_KEY:            {'*** SET ***' if settings.ZILLIZ_API_KEY and settings.ZILLIZ_API_KEY.get_secret_value() else '!!! NOT SET !!!'}")

        ```

3.  **Actualizar Conexión Pymilvus (API Helpers - `app/api/v1/endpoints/ingest.py`):**
    *   Modificar `_get_milvus_collection_sync`:
        ```python
        def _get_milvus_collection_sync() -> Collection:
            # ... (declaraciones previas) ...
            if alias not in connections.list_connections():
                sync_milvus_log.info("Connecting to Milvus (Zilliz) for sync helper...")
                try:
                    # --- MODIFICACIÓN ---
                    connections.connect(
                        alias=alias,
                        uri=settings.MILVUS_URI,
                        token=settings.ZILLIZ_API_KEY.get_secret_value(), # Añadir token
                        timeout=settings.MILVUS_GRPC_TIMEOUT
                    )
                    # --- FIN MODIFICACIÓN ---
                    sync_milvus_log.info("Milvus (Zilliz) connection established for sync helper.")
                # ... (manejo de excepciones) ...
            # ... (resto de la función) ...
        ```

4.  **Actualizar Conexión Pymilvus (Worker Pipeline - `app/services/ingest_pipeline.py`):**
    *   Modificar `_ensure_milvus_connection_and_collection_for_pipeline`:
        ```python
        def _ensure_milvus_connection_and_collection_for_pipeline(alias: str = "pipeline_worker_indexing") -> Collection:
            # ... (declaraciones previas) ...
            if not connection_exists:
                uri = settings.MILVUS_URI
                connect_log.info("Connecting to Milvus (Zilliz) for pipeline worker indexing...", uri=uri)
                try:
                    # --- MODIFICACIÓN ---
                    connections.connect(
                        alias=alias,
                        uri=uri,
                        token=settings.ZILLIZ_API_KEY.get_secret_value(), # Añadir token
                        timeout=settings.MILVUS_GRPC_TIMEOUT
                    )
                    # --- FIN MODIFICACIÓN ---
                    connect_log.info("Connected to Milvus (Zilliz) for pipeline worker indexing.")
                # ... (manejo de excepciones) ...
            # ... (resto de la función) ...
        ```

5.  **Actualizar Configuración Kubernetes:**
    *   **Secret (`ingest-service-secrets`):**
        *   Añadir la Zilliz API Key al secreto existente o crear uno nuevo.
            *   Clave: `ZILLIZ_API_KEY`
            *   Valor: La API Key real de Zilliz.
    *   **ConfigMap (`ingest-service-config`):**
        *   Actualizar `INGEST_MILVUS_URI` al endpoint HTTPS de Zilliz Cloud.
            ```yaml
            data:
              # ...
              INGEST_MILVUS_URI: "https://in03-0afab716eb46d7f.serverless.gcp-us-west1.cloud.zilliz.com"
              # ...
            ```
    *   **Deployments (`deployment-api.yaml`, `deployment-worker.yaml`):**
        *   Asegurarse de que la referencia al Secret (`ingest-service-secrets`) esté presente en `envFrom`.
        *   Añadir la variable de entorno `INGEST_ZILLIZ_API_KEY` que tome su valor del Secret:
            ```yaml
            # Dentro de la sección 'env:' del contenedor
            env:
              - name: INGEST_ZILLIZ_API_KEY # La variable que espera config.py
                valueFrom:
                  secretKeyRef:
                    name: ingest-service-secrets # Nombre del Secret en k8s
                    key: ZILLIZ_API_KEY       # Clave dentro del Secret
              # ... otras variables env ...
            ```

### 4.2. `query-service`

1.  **Actualizar Dependencias (`pyproject.toml`):**
    *   Cambiar la dependencia de `pymilvus`:
        ```toml
        # Antes: pymilvus = "^2.4.1"
        # Después:
        pymilvus = "==2.5.3"
        ```
    *   Ejecutar `poetry lock && poetry install` para actualizar.

2.  **Actualizar Configuración (`app/core/config.py`):**
    *   Añadir la nueva variable `ZILLIZ_API_KEY`:
        ```python
        class Settings(BaseSettings):
            # ... otras variables ...
            ZILLIZ_API_KEY: SecretStr = Field(description="API Key for Zilliz Cloud connection.")
            # ...
        ```
    *   Actualizar el valor por defecto o la variable de entorno para `MILVUS_URI`:
        ```python
        class Settings(BaseSettings):
            # ...
            # Antes: MILVUS_K8S_DEFAULT_URI = "http://milvus-standalone.nyro-develop.svc.cluster.local:19530"
            # Después: ZILLIZ_ENDPOINT_DEFAULT = "https://in03-0afab716eb46d7f.serverless.gcp-us-west1.cloud.zilliz.com"
            MILVUS_URI: AnyHttpUrl = Field(default=AnyHttpUrl(ZILLIZ_ENDPOINT_DEFAULT)) # Usar el endpoint Zilliz
            # ... (Asegurar que el validador de MILVUS_URI maneje HTTPS correctamente, similar a ingest-service)
             @field_validator('MILVUS_URI', mode='before')
             @classmethod
             def validate_milvus_uri(cls, v: Any) -> AnyHttpUrl:
                 # Simplificado: Asume que AnyHttpUrl maneja HTTPS, pero verifica el esquema
                 if not isinstance(v, str):
                     raise ValueError("MILVUS_URI must be a string.")
                 v_strip = v.strip()
                 if not v_strip.startswith("https://"):
                     raise ValueError(f"Invalid Zilliz URI: Must start with https://. Received: '{v_strip}'")
                 try:
                     # Pydantic validará el formato
                     validated_url = AnyHttpUrl(v_strip)
                     return validated_url
                 except Exception as e:
                     raise ValueError(f"Invalid Milvus URI format '{v_strip}': {e}") from e
            # ...
             @field_validator('ZILLIZ_API_KEY', mode='before')
             @classmethod
             def check_zilliz_key(cls, v: Any, info: ValidationInfo) -> Any:
                  if v is None or v == "":
                     raise ValueError(f"Required secret field 'QUERY_ZILLIZ_API_KEY' cannot be empty.")
                  return v
            # ...
            # Loguear la nueva variable al inicio
            temp_log.info(f"  ZILLIZ_API_KEY:            {'*** SET ***' if settings.ZILLIZ_API_KEY and settings.ZILLIZ_API_KEY.get_secret_value() else '!!! NOT SET !!!'}")
        ```

3.  **Actualizar Conexión Pymilvus (`app/infrastructure/vectorstores/milvus_adapter.py`):**
    *   Modificar el método `_ensure_connection` en `MilvusAdapter`:
        ```python
        async def _ensure_connection(self):
            # ... (declaraciones previas) ...
            if not self._connected or self._alias not in connections.list_connections():
                uri = str(settings.MILVUS_URI)
                connect_log = log.bind(adapter="MilvusAdapter", action="connect", uri=uri, alias=self._alias)
                connect_log.debug("Attempting to connect to Milvus (Zilliz)...")
                try:
                    # --- MODIFICACIÓN ---
                    connections.connect(
                        alias=self._alias,
                        uri=uri,
                        token=settings.ZILLIZ_API_KEY.get_secret_value(), # Añadir token
                        timeout=settings.MILVUS_GRPC_TIMEOUT
                    )
                    # --- FIN MODIFICACIÓN ---
                    self._connected = True
                    connect_log.info("Connected to Milvus (Zilliz) successfully.")
                # ... (manejo de excepciones) ...
        ```

4.  **Actualizar Configuración Kubernetes:**
    *   **Secret (`query-service-secrets`):**
        *   Añadir la Zilliz API Key al secreto existente o crear uno nuevo.
            *   Clave: `ZILLIZ_API_KEY`
            *   Valor: La API Key real de Zilliz.
    *   **ConfigMap (`query-service-config`):**
        *   Actualizar `QUERY_MILVUS_URI` al endpoint HTTPS de Zilliz Cloud.
            ```yaml
            data:
              # ...
              QUERY_MILVUS_URI: "https://in03-0afab716eb46d7f.serverless.gcp-us-west1.cloud.zilliz.com"
              # ...
            ```
    *   **Deployment (`deployment.yaml`):**
        *   Asegurarse de que la referencia al Secret (`query-service-secrets`) esté presente en `envFrom`.
        *   Añadir la variable de entorno `QUERY_ZILLIZ_API_KEY` que tome su valor del Secret:
            ```yaml
            # Dentro de la sección 'env:' del contenedor
            env:
              - name: QUERY_ZILLIZ_API_KEY # La variable que espera config.py
                valueFrom:
                  secretKeyRef:
                    name: query-service-secrets # Nombre del Secret en k8s
                    key: ZILLIZ_API_KEY       # Clave dentro del Secret
              # ... otras variables env ...
            ```

## 5. Consideraciones Adicionales

*   **Timeouts:** La latencia de red hacia Zilliz Cloud podría ser diferente a la del Milvus Standalone interno. Podría ser necesario ajustar `MILVUS_GRPC_TIMEOUT` en la configuración si se observan timeouts en las operaciones con Milvus.
*   **Colecciones:** Asegurarse de que la colección (`document_chunks_minilm`) exista en el cluster de Zilliz Cloud. Si no existe, `ingest-service` debería crearla en el primer intento de indexación (según la lógica actual de `_ensure_milvus_connection_and_collection_for_pipeline`). Verificar que el esquema creado sea compatible.
*   **Costos y Límites:** Revisar los límites y costos asociados al plan de Zilliz Cloud (Free Tier mencionado) para evitar sorpresas.
*   **Seguridad:** La API Key de Zilliz es sensible. Asegurar que se maneje exclusivamente a través de Kubernetes Secrets y no se exponga en ConfigMaps, logs o el código fuente.

## 6. Estrategia de Pruebas

1.  **Pruebas Locales (Opcional/Simulado):** Configurar variables de entorno locales (`INGEST_MILVUS_URI`, `INGEST_ZILLIZ_API_KEY`, etc.) apuntando a Zilliz Cloud y ejecutar los servicios localmente para verificar la conexión básica y las operaciones CRUD simples.
2.  **Pruebas de Integración (Entorno Staging/Develop):**
    *   Desplegar las versiones refactorizadas de `ingest-service` y `query-service` en el namespace `nyro-develop`, configuradas para usar Zilliz Cloud.
    *   **Flujo de Ingesta:** Subir varios tipos de documentos (PDF, DOCX, TXT) y verificar:
        *   Que el documento pasa por los estados `uploaded`, `processing`, `processed`.
        *   Que no hay errores relacionados con Milvus/Zilliz en los logs del worker de ingesta.
        *   Que los chunks se indexan correctamente (verificar el conteo de chunks vía API o directamente en Zilliz si es posible).
    *   **Flujo de Consulta:** Realizar consultas (simples y complejas) a través de `query-service` que involucren los documentos recién ingeridos y verificar:
        *   Que no hay errores relacionados con Milvus/Zilliz en los logs de `query-service`.
        *   Que se recuperan chunks relevantes desde Zilliz.
        *   Que la generación de respuestas RAG funciona correctamente.
    *   **Flujo de Eliminación:** Eliminar un documento ingerido y verificar:
        *   Que la API responde correctamente (204).
        *   Que los datos se eliminan de PostgreSQL, GCS y Zilliz Cloud (verificar que los chunks asociados al `document_id` ya no existen en la colección Zilliz).
3.  **Pruebas de Carga (Opcional):** Si es relevante, realizar pruebas de carga para evaluar el rendimiento y la estabilidad de la conexión con Zilliz Cloud bajo carga.

## 7. Estrategia de Despliegue

1.  **Aplicar Cambios Kubernetes:** Aplicar los `ConfigMap` y `Secret` actualizados al namespace `nyro-develop`.
2.  **Construir Imágenes:** Construir las nuevas imágenes Docker para `ingest-service` y `query-service` con el código refactorizado y las dependencias actualizadas.
3.  **Actualizar Deployments:** Actualizar los manifiestos `Deployment` con las nuevas etiquetas de imagen.
4.  **Desplegar:** Aplicar los `Deployment` actualizados en Kubernetes. Utilizar estrategias de despliegue como Rolling Update (por defecto en Kubernetes) para minimizar el tiempo de inactividad.
5.  **Monitorizar:** Observar de cerca los logs de los pods, el uso de recursos y las métricas de rendimiento después del despliegue.

## 8. Plan de Rollback

1.  **Revertir Código:** Mantener la versión anterior del código fuente en el control de versiones (Git).
2.  **Revertir Imágenes:** Mantener las imágenes Docker funcionales anteriores en el registro (GHCR).
3.  **Revertir Configuración K8s:**
    *   Revertir los `ConfigMap` para apuntar a la URI del Milvus Standalone anterior.
    *   Revertir los `Deployment` para usar las imágenes Docker anteriores y eliminar la variable de entorno/montaje del Secret `ZILLIZ_API_KEY`.
4.  **Consideración de Datos:** Si se necesita revertir después de que se hayan indexado datos significativos en Zilliz, se debe decidir si es necesario re-indexar esos datos en el Milvus Standalone anterior o si se acepta una pérdida temporal de esos datos en el sistema revertido.

---

## Checklist de Refactorización

**Fase 1: Preparación y Dependencias**

*   [x] Backup del código fuente actual de `ingest-service` y `query-service`.
*   [x] Confirmar acceso y API Key de Zilliz Cloud.
*   [x] Actualizar `pymilvus` a `==2.5.3` en `ingest-service/pyproject.toml`.
*   [ ] Actualizar `pymilvus` a `==2.5.3` en `query-service/pyproject.toml`.
*   [x] Ejecutar `poetry lock && poetry install` en ambos servicios.

**Fase 2: Actualización de Configuración**

*   [x] (`ingest-service`) Añadir `ZILLIZ_API_KEY: SecretStr` a `app/core/config.py`.
*   [x] (`ingest-service`) Actualizar `MILVUS_URI` (default y validador) en `app/core/config.py` a HTTPS Zilliz.
*   [ ] (`query-service`) Añadir `ZILLIZ_API_KEY: SecretStr` a `app/core/config.py`.
*   [ ] (`query-service`) Actualizar `MILVUS_URI` (default y validador) en `app/core/config.py` a HTTPS Zilliz.
*   [ ] (Kubernetes) Crear/Actualizar `ingest-service-secrets` con la clave `ZILLIZ_API_KEY`.
*   [ ] (Kubernetes) Crear/Actualizar `query-service-secrets` con la clave `ZILLIZ_API_KEY`.
*   [ ] (Kubernetes) Actualizar `ingest-service-config` con la nueva `INGEST_MILVUS_URI`.
*   [ ] (Kubernetes) Actualizar `query-service-config` con la nueva `QUERY_MILVUS_URI`.
*   [ ] (Kubernetes) Actualizar `ingest-service/deployment-api.yaml` para montar el secret y definir `INGEST_ZILLIZ_API_KEY`.
*   [ ] (Kubernetes) Actualizar `ingest-service/deployment-worker.yaml` para montar el secret y definir `INGEST_ZILLIZ_API_KEY`.
*   [ ] (Kubernetes) Actualizar `query-service/deployment.yaml` para montar el secret y definir `QUERY_ZILLIZ_API_KEY`.

**Fase 3: Modificación del Código**

*   [x] (`ingest-service`) Modificar `connections.connect` en `app/api/v1/endpoints/ingest.py` para usar `token`.
*   [x] (`ingest-service`) Modificar `connections.connect` en `app/services/ingest_pipeline.py` para usar `token`.
*   [ ] (`query-service`) Modificar `connections.connect` en `app/infrastructure/vectorstores/milvus_adapter.py` para usar `token`.
*   [ ] Revisar manejo de timeouts (`MILVUS_GRPC_TIMEOUT`) si es necesario.

**Fase 4: Pruebas**

*   [ ] Realizar pruebas locales (simuladas/reales).
*   [ ] Realizar pruebas de integración en `nyro-develop` (ingesta, consulta, eliminación).
*   [ ] Validar que los logs no muestren errores de conexión o autenticación con Zilliz.
*   [ ] Validar que los datos se indexan y recuperan correctamente desde Zilliz.

**Fase 5: Despliegue y Monitoreo**

*   [ ] Aplicar cambios de configuración Kubernetes (Secrets, ConfigMaps).
*   [ ] Construir y pushear nuevas imágenes Docker.
*   [ ] Aplicar cambios de Deployments Kubernetes.
*   [ ] Monitorizar logs y rendimiento post-despliegue.

**Fase 6: Finalización**

*   [ ] Documentar los cambios realizados.
*   [ ] Limpiar recursos obsoletos (si aplica, como el Milvus Standalone anterior después de un período de validación).