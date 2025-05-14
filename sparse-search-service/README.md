# Atenex Sparse Search Service v1.0.0

## 1. Visión General

El **Atenex Sparse Search Service** (v1.0.0) es un microservicio de Atenex dedicado a realizar búsquedas dispersas (basadas en palabras clave) utilizando el algoritmo **BM25**. Este servicio es consumido internamente por otros microservicios de Atenex, principalmente el `query-service`, para proporcionar una de las fuentes de recuperación de chunks en un pipeline RAG híbrido.

En esta versión, el servicio ha sido **refactorizado significativamente** para mejorar el rendimiento y la escalabilidad:
1.  **Indexación Offline:** Los índices BM25 ya no se construyen bajo demanda por cada solicitud. En su lugar, un **proceso de indexación offline (ejecutado como un Kubernetes CronJob)** precalcula los índices BM25 para cada compañía.
2.  **Persistencia de Índices en Google Cloud Storage (GCS):** Los índices BM25 precalculados (el objeto BM25 serializado y un mapa de IDs de chunks) se almacenan de forma persistente en un bucket de GCS dedicado.
3.  **Caché LRU/TTL en Memoria:** Al recibir una solicitud, el servicio primero intenta cargar el índice BM25 desde un caché en memoria (LRU con TTL). Si no está en caché (cache miss), lo descarga desde GCS, lo carga en memoria, lo sirve y lo almacena en el caché para futuras solicitudes.
4.  **Búsqueda con Índices Precargados:** El servicio utiliza los índices cargados (desde caché o GCS) para realizar la búsqueda dispersa sobre la consulta del usuario.

Este enfoque elimina la latencia y la carga en PostgreSQL asociadas con la construcción de índices en tiempo real, mejorando drásticamente el rendimiento de las búsquedas dispersas.

## 2. Funcionalidades Clave (v1.0.0)

*   **Búsqueda BM25 Eficiente:** Utiliza índices BM25 precalculados para realizar búsquedas dispersas de manera rápida.
*   **Carga de Índices desde GCS:** Los índices serializados se descargan desde un bucket de Google Cloud Storage bajo demanda.
*   **Caché en Memoria (LRU/TTL):** Mantiene las instancias de `bm2s.BM25` y sus mapas de IDs de chunks en un caché LRU (Least Recently Used) con TTL (Time To Live) para un acceso rápido en solicitudes subsecuentes para la misma compañía.
*   **Indexación Offline mediante CronJob:** Un script (`app/jobs/index_builder_cronjob.py`), empaquetado en la misma imagen Docker, se ejecuta periódicamente como un Kubernetes CronJob para:
    *   Extraer el contenido de los chunks procesados desde PostgreSQL para cada compañía.
    *   Construir/reconstruir los índices BM25.
    *   Subir los índices serializados y los mapas de IDs a GCS.
*   **API Sencilla y Enfocada:** Expone un único endpoint principal (`POST /api/v1/search`) para realizar la búsqueda dispersa.
*   **Health Check Robusto:** Proporciona un endpoint `/health` para verificar el estado del servicio y sus dependencias críticas (PostgreSQL, GCS, y la disponibilidad de la librería `bm2s`).
*   **Arquitectura Limpia y Modular:** Estructurado siguiendo principios de Clean Architecture (Puertos y Adaptadores).
*   **Multi-tenancy:** Los índices y las búsquedas están aislados por `company_id`, tanto en GCS (mediante rutas) como en el caché.

## 3. Pila Tecnológica

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Motor de Búsqueda Dispersa:** `bm2s`
*   **Almacenamiento de Índices Persistentes:** Google Cloud Storage (`google-cloud-storage`)
*   **Caché en Memoria:** `cachetools` (para `TTLCache`)
*   **Base de Datos (Cliente para Builder y Repositorio):** PostgreSQL (acceso vía `asyncpg`)
*   **Servidor ASGI/WSGI:** Uvicorn gestionado por Gunicorn
*   **Contenerización:** Docker
*   **Gestión de Dependencias:** Poetry
*   **Logging Estructurado:** Structlog

## 4. Estructura del Proyecto (v1.0.0)

```
sparse-search-service/
├── app/
│   ├── api/v1/
│   │   ├── endpoints/search_endpoint.py
│   │   └── schemas.py
│   ├── application/
│   │   ├── ports/
│   │   │   ├── repository_ports.py
│   │   │   ├── sparse_index_storage_port.py  # NUEVO
│   │   │   └── sparse_search_port.py
│   │   └── use_cases/
│   │       └── load_and_search_index_use_case.py # MODIFICADO (antes sparse_search_use_case.py)
│   ├── core/
│   │   ├── config.py
│   │   └── logging_config.py
│   ├── domain/models.py
│   ├── infrastructure/
│   │   ├── cache/                                # NUEVO
│   │   │   └── index_lru_cache.py                # NUEVO
│   │   ├── persistence/
│   │   │   ├── postgres_connector.py
│   │   │   └── postgres_repositories.py
│   │   ├── sparse_retrieval/bm25_adapter.py      # MODIFICADO
│   │   └── storage/                              # NUEVO
│   │       └── gcs_index_storage_adapter.py      # NUEVO
│   ├── jobs/                                     # NUEVO
│   │   └── index_builder_cronjob.py              # NUEVO
│   ├── dependencies.py
│   ├── gunicorn_conf.py
│   └── main.py
├── k8s/
│   ├── sparse-search-service-configmap.yaml
│   ├── sparse-search-service-cronjob.yaml      # NUEVO
│   ├── sparse-search-service-deployment.yaml
│   ├── sparse-search-service-secret.example.yaml
│   └── sparse-search-service-svc.yaml
├── Dockerfile
├── pyproject.toml
├── poetry.lock
├── README.md (Este archivo)
└── .env.example
```

## 5. Flujo de Búsqueda

1.  **Solicitud API:** El cliente (e.g., `query-service`) envía una petición `POST /api/v1/search` con `query`, `company_id`, y `top_k`.
2.  **Use Case (`LoadAndSearchIndexUseCase`):**
    a.  Intenta obtener el par `(instancia_bm25, mapa_ids)` del **Caché LRU/TTL** usando la `company_id`.
    b.  **Cache Hit:** Si se encuentra, pasa la instancia BM25 y el mapa de IDs al `BM25Adapter` para la búsqueda.
    c.  **Cache Miss:**
        i.  Utiliza `GCSIndexStorageAdapter` para descargar los archivos `bm25_index.bm2s` y `id_map.json` desde `gs://{SPARSE_INDEX_GCS_BUCKET_NAME}/indices/{company_id}/`.
        ii. Si los archivos no existen en GCS o hay un error, se loguea y se devuelven resultados vacíos (no hay fallback a indexación on-demand).
        iii. Si se descargan, `BM25Adapter.load_bm2s_from_file()` carga el índice y se lee el `id_map.json`.
        iv. El par `(instancia_bm25, mapa_ids)` se almacena en el Caché LRU/TTL.
        v. Se procede con la búsqueda.
3.  **Adaptador BM25 (`BM25Adapter`):**
    a.  Recibe la consulta, la instancia BM25 pre-cargada, el mapa de IDs y `top_k`.
    b.  Ejecuta `bm25_instance.retrieve()` para obtener los índices de los documentos y sus scores.
    c.  Mapea los índices de documentos a los `chunk_id` reales usando el `id_map`.
    d.  Devuelve la lista de `SparseSearchResultItem`.
4.  **Respuesta API:** El endpoint devuelve la respuesta al cliente.

## 6. Proceso de Indexación Offline (CronJob)

Un script Python (`app/jobs/index_builder_cronjob.py`) se ejecuta periódicamente (e.g., cada 6 horas) como un Kubernetes CronJob.
*   **Objetivo:** Para cada compañía (o para una específica si se invoca manualmente), construir/actualizar su índice BM25 y almacenarlo en GCS.
*   **Pasos:**
    1.  **Obtener Chunks:** Conecta a PostgreSQL (usando `PostgresChunkContentRepository`) y obtiene todos los `embedding_id` (usado como `chunk_id`) y `content` de los `document_chunks` que pertenecen a documentos con estado `processed` para la compañía.
    2.  **Preparar Corpus:** Crea una lista de textos (`corpus_texts`) y una lista paralela de sus IDs (`id_map`).
    3.  **Construir Índice BM25:** Utiliza `bm2s.BM25().index(corpus_texts)`.
    4.  **Serializar:**
        *   El índice BM25 se dumpea a un archivo local temporal (e.g., `bm25_index.bm2s`) usando `BM25Adapter.dump_bm2s_to_file()`.
        *   El `id_map` se guarda como un archivo JSON local temporal (e.g., `id_map.json`).
    5.  **Subir a GCS:** Los dos archivos generados se suben a `gs://{SPARSE_INDEX_GCS_BUCKET_NAME}/indices/{company_id}/` usando `GCSIndexStorageAdapter`, sobrescribiendo los existentes.

## 7. API Endpoints

### `POST /api/v1/search`

*   **Descripción:** Realiza una búsqueda dispersa (BM25) para la consulta y compañía dadas, utilizando índices precalculados cargados desde GCS o un caché en memoria.
*   **Request Body (`SparseSearchRequest`):**
    ```json
    {
      "query": "texto de la consulta del usuario",
      "company_id": "uuid-de-la-compania",
      "top_k": 10
    }
    ```
*   **Response Body (200 OK - `SparseSearchResponse`):**
    ```json
    {
      "query": "texto de la consulta del usuario",
      "company_id": "uuid-de-la-compania",
      "results": [
        {"chunk_id": "id_del_chunk_1_embedding_id", "score": 23.45},
        {"chunk_id": "id_del_chunk_2_embedding_id", "score": 18.99}
      ]
    }
    ```
*   **Errores Comunes:**
    *   `422 Unprocessable Entity`: Cuerpo de solicitud inválido.
    *   `503 Service Unavailable`: Si PostgreSQL no está disponible durante el inicio, o si GCS es inaccesible y no hay índice en caché, o el motor `bm2s` no está disponible.
    *   `500 Internal Server Error`: Errores inesperados.

### `GET /health`

*   **Descripción:** Verifica la salud del servicio y sus dependencias críticas.
*   **Response Body (200 OK - `HealthCheckResponse` - Servicio Saludable):**
    ```json
    {
      "status": "ok",
      "service": "Atenex Sparse Search Service",
      "service_version": "1.0.0",
      "ready": true,
      "dependencies": {
        "PostgreSQL": "ok",
        "BM2S_Engine": "ok (bm2s library loaded and adapter initialized)",
        "GCS_Index_Storage": "ok (adapter initialized)"
      }
    }
    ```
*   **Response Body (503 Service Unavailable - Alguna dependencia crítica falló):**
    ```json
    {
      "status": "error",
      "service": "Atenex Sparse Search Service",
      "service_version": "1.0.0",
      "ready": false,
      "dependencies": {
        "PostgreSQL": "error", // o el estado de otras dependencias
        "BM2S_Engine": "unavailable (bm2s library potentially missing or adapter init failed)",
        "GCS_Index_Storage": "unavailable"
      }
    }
    ```

## 8. Configuración (Variables de Entorno)

El servicio se configura mediante variables de entorno, con el prefijo `SPARSE_`.

**Variables Críticas:**

| Variable                               | Descripción                                                                   | Ejemplo (Valor Esperado en K8s)                       | Gestionado por |
| :------------------------------------- | :---------------------------------------------------------------------------- | :---------------------------------------------------- | :------------- |
| `SPARSE_LOG_LEVEL`                     | Nivel de logging (DEBUG, INFO, WARNING, ERROR, CRITICAL).                     | `INFO`                                                | ConfigMap      |
| `PORT`                                 | Puerto interno del contenedor para Gunicorn.                                    | `8004`                                                | ConfigMap      |
| `SPARSE_SERVICE_VERSION`               | Versión del servicio (usada en health check).                                 | `1.0.0`                                               | ConfigMap      |
| `SPARSE_POSTGRES_USER`                 | Usuario para conectar a PostgreSQL.                                           | `postgres`                                            | ConfigMap      |
| `SPARSE_POSTGRES_PASSWORD`             | Contraseña para el usuario PostgreSQL.                                        | *Valor desde Kubernetes Secret*                       | **Secret**     |
| `SPARSE_POSTGRES_SERVER`               | Host/Service name del servidor PostgreSQL en K8s.                             | `postgresql-service.nyro-develop.svc.cluster.local` | ConfigMap      |
| `SPARSE_POSTGRES_PORT`                 | Puerto del servidor PostgreSQL.                                               | `5432`                                                | ConfigMap      |
| `SPARSE_POSTGRES_DB`                   | Nombre de la base de datos PostgreSQL.                                        | `atenex`                                              | ConfigMap      |
| `SPARSE_DB_POOL_MIN_SIZE`              | Tamaño mínimo del pool de conexiones DB.                                      | `2`                                                   | ConfigMap      |
| `SPARSE_DB_POOL_MAX_SIZE`              | Tamaño máximo del pool de conexiones DB.                                      | `10`                                                  | ConfigMap      |
| `SPARSE_DB_CONNECT_TIMEOUT`            | Timeout (segundos) para conexión a DB.                                        | `30`                                                  | ConfigMap      |
| `SPARSE_DB_COMMAND_TIMEOUT`            | Timeout (segundos) para comandos DB.                                          | `60`                                                  | ConfigMap      |
| **`SPARSE_INDEX_GCS_BUCKET_NAME`**     | **Bucket GCS para almacenar los índices BM25.**                             | `atenex-sparse-indices`                             | ConfigMap      |
| **`SPARSE_INDEX_CACHE_MAX_ITEMS`**     | **Máximo número de índices de compañía en el caché LRU/TTL.**                 | `50`                                                  | ConfigMap      |
| **`SPARSE_INDEX_CACHE_TTL_SECONDS`**   | **TTL (segundos) para los ítems en el caché de índices.**                     | `3600` (1 hora)                                       | ConfigMap      |

**¡ADVERTENCIA DE SEGURIDAD!**
*   **`SPARSE_POSTGRES_PASSWORD`**: Debe ser gestionada de forma segura a través de Kubernetes Secrets.

## 9. Ejecución Local (Desarrollo)

1.  Asegurar Poetry, Python 3.10+.
2.  `poetry install` (instalará `bm2s`, `google-cloud-storage`, `cachetools`).
3.  Configurar `.env` con:
    *   Variables `SPARSE_POSTGRES_*` para tu PostgreSQL local.
    *   `SPARSE_INDEX_GCS_BUCKET_NAME`: Nombre de un bucket GCS al que tengas acceso de lectura/escritura para pruebas.
    *   (Opcional) Credenciales de GCP: `GOOGLE_APPLICATION_CREDENTIALS` apuntando a tu archivo de clave JSON de SA, o asegúrate de estar autenticado con `gcloud auth application-default login`.
4.  Asegurar que PostgreSQL local esté corriendo y tenga datos en `documents` y `document_chunks`.
5.  **Para construir un índice localmente para pruebas:**
    ```bash
    python -m app.jobs.index_builder_cronjob --company-id TU_COMPANY_ID_DE_PRUEBA
    ```
    Esto generará y subirá el índice a tu bucket GCS configurado.
6.  **Ejecutar el servicio API:**
    ```bash
    poetry run uvicorn app.main:app --host 0.0.0.0 --port ${SPARSE_PORT:-8004} --reload
    ```
    El servicio estará en `http://localhost:8004`.

## 10. Construcción y Despliegue Docker

1.  **Construir Imagen:**
    ```bash
    docker build -t tu-registro.io/tu-org/sparse-search-service:v1.0.0 .
    ```
2.  **Push a Registro.**
3.  **Despliegue en Kubernetes:**
    *   Los manifiestos K8s (`configmap.yaml`, `deployment.yaml`, `service.yaml`, `cronjob.yaml`) se gestionan en un repositorio separado.
    *   **Service Account para el CronJob (`sparse-search-builder-sa`):** Necesita permisos de lectura en PostgreSQL y escritura/lectura/borrado en el bucket GCS de índices.
    *   **Service Account para el Deployment (`sparse-search-runtime-sa`):** Necesita permisos de lectura en PostgreSQL y lectura en el bucket GCS de índices. Configurar Workload Identity o montar claves de SA.
    *   Asegurar que el ConfigMap y Secrets (para `SPARSE_POSTGRES_PASSWORD`) existan en el clúster.

## 11. CI/CD

Integrar en el pipeline CI/CD:
*   Detectar cambios, construir/etiquetar/empujar imagen Docker.
*   Actualizar tag de imagen en `deployment.yaml` y `cronjob.yaml` del repositorio de manifiestos.

## 12. Consideraciones de Rendimiento y Escalabilidad

*   **Latencia de Búsqueda:** Mejorada significativamente al eliminar la indexación on-demand. La latencia ahora depende de:
    *   **Cache Hit:** Muy rápida (solo búsqueda en memoria).
    *   **Cache Miss:** Latencia de descarga de GCS + carga de índice en memoria + búsqueda.
*   **Consumo de Memoria del Pod:** Determinado por `SPARSE_INDEX_CACHE_MAX_ITEMS` y el tamaño de los índices BM25 individuales. Ajustar los `resources.limits.memory` del Deployment.
*   **Rendimiento del CronJob:** La construcción de índices para muchas compañías o compañías con muchos chunks puede ser intensiva. Ajustar recursos del pod del CronJob y su frecuencia.
*   **Actualización de Índices:** La frecuencia del CronJob determina cuán "frescos" están los índices. Para actualizaciones más rápidas, se podría considerar un mecanismo de trigger (e.g., Pub/Sub desde `ingest-service`), pero el CronJob periódico es un buen punto de partida.

## 13. TODO / Mejoras Futuras

*   **Métricas Detalladas:** (Como se mencionó en el plan de refactorización) para tiempos de carga GCS, aciertos/fallos de caché, duración del builder.
*   **Invalidación Selectiva del Caché:** Mecanismo para invalidar el caché de una compañía específica si su índice se reconstruye urgentemente fuera del ciclo del CronJob (e.g., vía un endpoint administrativo interno).
*   **Optimización del `index_builder_cronjob.py`:** Paralelizar la construcción de índices para múltiples compañías si el script procesa "ALL".
*   **Estrategia de Rollback de Índices:** Considerar cómo manejar/revertir a una versión anterior de un índice si una nueva construcción resulta corrupta.
*   **Refinar `get_all_active_company_ids`:** Implementar una forma más robusta de obtener las compañías activas en el `index_builder_cronjob.py`.