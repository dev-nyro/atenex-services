# Atenex Document Processing Service (`docproc-service`)

**Versión:** 0.1.0

## 1. Visión General

El **Atenex Document Processing Service** (`docproc-service`) es un microservicio interno de la plataforma Atenex. Su responsabilidad principal es recibir archivos en diversos formatos, extraer su contenido textual y dividir dicho texto en fragmentos (chunks) cohesivos y de tamaño manejable.

Este servicio es consumido principalmente por el `ingest-service` como parte del pipeline de ingesta de documentos, permitiendo aislar la lógica y las dependencias de procesamiento de documentos.

## 2. Funcionalidades Principales

*   **Recepción de Archivos:** Acepta archivos vía `multipart/form-data` junto con metadatos como `original_filename` y `content_type`.
*   **Extracción de Texto Multi-Formato:**
    *   PDF (`application/pdf`) usando PyMuPDF.
    *   DOCX (`application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `application/msword`) usando python-docx.
    *   TXT (`text/plain`) usando decodificación estándar.
    *   HTML (`text/html`) usando BeautifulSoup.
    *   Markdown (`text/markdown`) usando python-markdown y html2text.
*   **División en Chunks (Chunking):** Fragmenta el texto extraído en chunks más pequeños, utilizando una estrategia configurable de tamaño y solapamiento.
*   **API Sencilla:** Expone un endpoint principal (`POST /api/v1/process`) para procesar documentos.
*   **Health Check:** Proporciona un endpoint `GET /health` para verificar el estado del servicio.
*   **Arquitectura Limpia/Hexagonal:** Estructurado para facilitar el mantenimiento y la testabilidad.
*   **Configurable:** Parámetros como el tamaño de chunk, solapamiento y tipos de contenido soportados son configurables mediante variables de entorno.

## 3. Pila Tecnológica

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Servidor ASGI/WSGI:** Uvicorn con Gunicorn
*   **Contenerización:** Docker
*   **Gestión de Dependencias:** Poetry
*   **Logging:** Structlog
*   **Librerías de Extracción Principales:**
    *   `PyMuPDF` (fitz)
    *   `python-docx`
    *   `BeautifulSoup4`
    *   `html2text`
    *   `markdown`

## 4. Estructura del Proyecto (Arquitectura Hexagonal)

```
docproc-service/
├── app/
│   ├── api/v1/
│   │   ├── endpoints/process_endpoint.py
│   │   └── schemas.py
│   ├── application/
│   │   ├── ports/
│   │   │   ├── extraction_port.py
│   │   │   └── chunking_port.py
│   │   └── use_cases/
│   │       └── process_document_use_case.py
│   ├── core/
│   │   ├── config.py
│   │   └── logging_config.py
│   ├── domain/
│   │   └── models.py
│   ├── infrastructure/
│   │   ├── extractors/
│   │   │   ├── pdf_adapter.py, docx_adapter.py, etc.
│   │   │   └── base_extractor.py
│   │   └── chunkers/
│   │       └── default_chunker_adapter.py
│   ├── dependencies.py
│   └── main.py
├── Dockerfile
├── pyproject.toml
├── poetry.lock
├── README.md
└── .env.example
```

## 5. API Endpoints

### `POST /api/v1/process`

*   **Descripción:** Procesa un archivo para extraer texto y dividirlo en chunks.
*   **Request:** `multipart/form-data`
    *   `file`: (Requerido) El archivo a procesar (`UploadFile`).
    *   `original_filename`: (Requerido) El nombre original del archivo (`str`).
    *   `content_type`: (Requerido) El tipo MIME del archivo (`str`).
    *   `document_id`: (Opcional) ID del documento para tracing (`str`).
    *   `company_id`: (Opcional) ID de la compañía para tracing (`str`).
*   **Response (200 OK - `ProcessResponse`):**
    ```json
    {
      "data": {
        "document_metadata": {
          "original_filename": "example.pdf",
          "content_type": "application/pdf",
          "total_pages_extracted": 10,
          "raw_text_length_chars": 15000,
          "processing_time_ms": 543.21,
          "num_chunks_generated": 15
        },
        "chunks": [
          {
            "text": "Contenido del primer chunk...",
            "source_metadata": { "page_number": 1 }
          }
          // ... más chunks
        ]
      }
    }
    ```
*   **Errores Comunes:**
    *   `400 Bad Request`: Campos requeridos faltantes.
    *   `415 Unsupported Media Type`: `content_type` no soportado.
    *   `422 Unprocessable Entity`: Archivo corrupto o error de extracción/chunking.
    *   `500 Internal Server Error`: Errores inesperados.

### `GET /health`

*   **Descripción:** Verifica la salud del servicio.
*   **Response (200 OK):**
    ```json
    {
      "status": "ok",
      "service": "Atenex Document Processing Service",
      "version": "0.1.0"
    }
    ```

## 6. Configuración

El servicio se configura mediante variables de entorno, con el prefijo `DOCPROC_`. Ver `.env.example` y `app/core/config.py` para una lista completa.

**Variables Clave:**

| Variable                             | Descripción                                                               | Por Defecto (`config.py`)                                                                 |
| :----------------------------------- | :------------------------------------------------------------------------ | :---------------------------------------------------------------------------------------- |
| `DOCPROC_LOG_LEVEL`                  | Nivel de logging (DEBUG, INFO, WARNING, ERROR, CRITICAL).                 | `INFO`                                                                                    |
| `DOCPROC_PORT`                       | Puerto en el que el servicio escuchará.                                   | `8005`                                                                                    |
| `DOCPROC_CHUNK_SIZE`                 | Tamaño de los chunks (generalmente en palabras/tokens).                   | `1000`                                                                                    |
| `DOCPROC_CHUNK_OVERLAP`              | Solapamiento entre chunks.                                                | `200`                                                                                     |
| `DOCPROC_SUPPORTED_CONTENT_TYPES`    | Lista de tipos MIME soportados (JSON array string o CSV).                 | `["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ...]` |


## 7. Ejecución Local (Desarrollo)

1.  Asegurarse de tener **Python 3.10+** y **Poetry** instalados.
2.  Clonar el repositorio (si aplica) y navegar al directorio `docproc-service/`.
3.  Ejecutar `poetry install` para instalar dependencias.
4.  (Opcional) Crear un archivo `.env` a partir de `.env.example` y modificar las variables.
5.  Ejecutar el servicio con Uvicorn para desarrollo:
    ```bash
    poetry run uvicorn app.main:app --host 0.0.0.0 --port ${DOCPROC_PORT:-8005} --reload
    ```
    El servicio estará disponible en `http://localhost:8005` (o el puerto configurado).

## 8. Construcción y Despliegue Docker

1.  **Construir la Imagen Docker:**
    Desde el directorio raíz `docproc-service/`:
    ```bash
    docker build -t atenex/docproc-service:latest .
    # O con un tag específico:
    # docker build -t ghcr.io/YOUR_ORG/atenex-docproc-service:$(git rev-parse --short HEAD) .
    ```

2.  **Ejecutar Localmente con Docker (para probar la imagen):**
    ```bash
    docker run -d -p 8005:8005 \
      --name docproc-svc-local \
      -e DOCPROC_LOG_LEVEL="DEBUG" \
      -e DOCPROC_PORT="8005" \
      atenex/docproc-service:latest
    ```

3.  **Push a un Registro de Contenedores.**

4.  **Despliegue en Kubernetes:**
    Se crearán manifiestos de Kubernetes (`Deployment`, `Service`, `ConfigMap`) para desplegar en el clúster.

## 9. Consideraciones

*   **Manejo de Errores:** El servicio está diseñado para capturar errores de extracción y devolver códigos HTTP apropiados.
*   **Timeouts:** Para archivos muy grandes, la llamada síncrona `POST /api/v1/process` puede tardar. El cliente (ej. `ingest-service`) debe tener configurado un timeout adecuado.
*   **Seguridad:** Al ser un servicio interno, se asume que las llamadas provienen de la red interna del clúster y están pre-autenticadas por el API Gateway si la cadena de llamadas se origina externamente. No implementa su propia capa de autenticación de usuario final.
