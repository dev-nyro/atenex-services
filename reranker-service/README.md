# Atenex Reranker Service

## 1. Visión General

El **Reranker Service** es un microservicio de Atenex dedicado exclusivamente a reordenar una lista de fragmentos de texto (documentos o chunks) basándose en su relevancia para una consulta dada. Utiliza un modelo Cross-Encoder de la librería `sentence-transformers` (por defecto, `BAAI/bge-reranker-base`).

Este servicio es consumido internamente por `query-service` para mejorar la calidad de los resultados del pipeline RAG antes de que sean presentados al LLM o al usuario.

## 2. Funcionalidades Principales

*   **Reranking de Documentos:** Recibe una consulta y una lista de documentos, y devuelve los documentos reordenados por su score de relevancia.
*   **Modelo Configurable:** El nombre del modelo (`RERANKER_MODEL_NAME`), dispositivo (`RERANKER_MODEL_DEVICE`), etc., son configurables.
*   **Eficiencia:** Carga el modelo una vez al inicio. Las predicciones se ejecutan en un thread pool para no bloquear el event loop de FastAPI.
*   **API Sencilla:** Expone un único endpoint principal (`POST /api/v1/rerank`).
*   **Health Check:** Proporciona un endpoint `GET /health`.
*   **Arquitectura Limpia:** Estructurado siguiendo principios de arquitectura limpia/hexagonal.

## 3. Pila Tecnológica

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Motor de Reranking:** `sentence-transformers` (con `transformers` y PyTorch)
*   **Modelo por Defecto:** `BAAI/bge-reranker-base`
*   **Servidor ASGI/WSGI:** Uvicorn con Gunicorn
*   **Contenerización:** Docker
*   **Gestión de Dependencias:** Poetry
*   **Logging:** Structlog

## 4. Estructura del Proyecto

(Ver `refactor.md` para la estructura detallada del proyecto)

## 5. API Endpoints

### `POST /api/v1/rerank`

*   **Descripción:** Reordena documentos basados en una consulta.
*   **Request Body:**
    ```json
    {
      "query": "consulta del usuario",
      "documents": [
        {"id": "doc1", "text": "texto del doc1", "metadata": {"key": "value"}},
        {"id": "doc2", "text": "texto del doc2", "metadata": {}}
      ],
      "top_n": 5 // Opcional
    }
    ```
*   **Response Body (200 OK):**
    ```json
    {
      "reranked_documents": [
        {"id": "doc2", "text": "texto del doc2", "score": 0.95, "metadata": {}},
        {"id": "doc1", "text": "texto del doc1", "score": 0.88, "metadata": {"key": "value"}}
      ],
      "model_info": {
        "model_name": "BAAI/bge-reranker-base"
      }
    }
    ```

### `GET /health`

*   **Descripción:** Verifica la salud del servicio.
*   **Response Body (200 OK - Saludable):**
    ```json
    {
      "status": "ok",
      "service": "Atenex Reranker Service",
      "model_status": "loaded",
      "model_name": "BAAI/bge-reranker-base"
    }
    ```

## 6. Configuración

Ver `.env.example` y `app/core/config.py`. Variables de entorno con prefijo `RERANKER_`.

## 7. Ejecución Local (Desarrollo)

1.  Asegurar Poetry instalado.
2.  `poetry install`
3.  Crear `.env` a partir de `.env.example` si es necesario.
4.  Ejecutar: `poetry run uvicorn app.main:app --host 0.0.0.0 --port ${RERANKER_PORT:-8004} --reload`

## 8. Construcción y Despliegue Docker

1.  Construir: `docker build -t atenex/reranker-service:latest .`
2.  (Ver `refactor.md` para más detalles sobre despliegue K8s).