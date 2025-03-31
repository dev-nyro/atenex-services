# API Gateway (api-gateway)

## 1. Visión General

Este servicio actúa como el **API Gateway** para la plataforma SaaS B2B de Nyro. Es el punto de entrada único para todas las solicitudes externas destinadas a los microservicios backend (Ingesta, Consulta, Autenticación, etc.). Sus responsabilidades principales son:

*   **Routing:** Dirige las solicitudes entrantes al microservicio apropiado basándose en la ruta de la URL.
*   **Autenticación/Autorización:** Verifica los tokens JWT (JSON Web Tokens) presentados en el header `Authorization: Bearer <token>`. Asegura que solo las solicitudes autenticadas y autorizadas (basado en claims del token como `company_id`) lleguen a los servicios protegidos.
*   **Rate Limiting (Futuro):** Podría implementar limitación de tasa para proteger los servicios backend.
*   **Request/Response Transformation (Mínimo):** Realiza transformaciones mínimas, como añadir/modificar headers (`X-Company-ID`) necesarios para los servicios downstream.
*   **Centralized CORS:** Gestiona la configuración de Cross-Origin Resource Sharing (CORS) en un solo lugar.
*   **Punto de Observabilidad:** Sirve como un punto centralizado para logging y (futuramente) tracing de solicitudes.

Este Gateway está diseñado para ser ligero y eficiente, delegando la lógica de negocio a los microservicios correspondientes.

## 2. Arquitectura General del Proyecto (Posición del Gateway)

```mermaid
%%{init: {'theme': 'base', 'themeVariables': { 'primaryColor': '#ADD8E6', 'edgeLabelBackground':'#fff', 'tertiaryColor': '#FFFACD'}}}%%
flowchart TD
    A[Usuario/Cliente Externo] -->|HTTPS / REST API| C["<strong>API Gateway (FastAPI)</strong><br/>(Este Servicio)"]

    subgraph KubernetesCluster ["Kubernetes Cluster (nyro-develop ns)"]
        direction LR
        C -->|Valida JWT, Añade Headers| I["Ingest Service API"]
        C -->|Valida JWT, Añade Headers| Q["Query Service API"]
        C -->|Valida JWT? (o proxy directo)| Auth["(Opcional) Auth Service"]

        I --> DBi[("Supabase (Metadata)")]
        I --> VDBi[(Milvus (Vectores)")]
        I --> S3i[(MinIO (Archivos)")]
        I --> Qi([Redis (Celery Queue)])

        Q --> DBq[("Supabase (Logs)")]
        Q --> VDBq[(Milvus (Vectores)")]
        Q --> LLM[("Google Gemini API")]
        Q --> Emb[("OpenAI Embedding API")]

        Auth --> DBa[("Supabase (Usuarios)")]
    end

```

## 3. Características Clave

*   **Proxy Inverso:** Reenvía tráfico a los servicios `ingest-service` y `query-service`.
*   **Validación JWT:** Verifica tokens usando `python-jose` y el secreto compartido.
*   **Inyección de Headers:** Añade `X-Company-ID` a las solicitudes downstream basado en el payload del token.
*   **Routing basado en Path:** Usa FastAPI para dirigir `/api/v1/ingest/*` e `/api/v1/query/*`.
*   **Cliente HTTP Asíncrono:** Utiliza `httpx` para comunicación no bloqueante con los microservicios.
*   **Logging Estructurado:** Logs en JSON usando `structlog`.
*   **Configuración Centralizada:** Carga configuración desde variables de entorno y `.env` (`pydantic-settings`).
*   **Despliegue contenerizado:** Dockerfile y manifests de Kubernetes listos.
*   **Health Check:** Endpoint `/health` para K8s probes.

## 4. Pila Tecnológica Principal

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Cliente HTTP:** HTTPX
*   **Validación JWT:** python-jose
*   **Configuración:** pydantic-settings
*   **Logging:** structlog
*   **Despliegue:** Docker, Kubernetes, Gunicorn + Uvicorn

## 5. Estructura de la Codebase

```
api-gateway/
├── app/
│   ├── __init__.py
│   ├── auth/                 # Lógica de verificación de JWT
│   │   ├── __init__.py
│   │   ├── auth_middleware.py # Dependencias FastAPI para requerir auth
│   │   └── jwt_handler.py     # Función verify_token
│   ├── core/                 # Configuración y Logging Core
│   │   ├── __init__.py
│   │   ├── config.py         # Carga de Settings (pydantic-settings)
│   │   └── logging_config.py # Configuración de structlog
│   ├── main.py               # Entrypoint FastAPI, lifespan, middlewares, health checks
│   └── routers/              # Definición de rutas proxy
│       ├── __init__.py
│       └── gateway_router.py # Lógica principal de proxying
├── k8s/                      # Manifests de Kubernetes
│   ├── gateway-configmap.yaml
│   ├── gateway-deployment.yaml
│   ├── gateway-secret.yaml # (Estructura, valor real en gestor de secretos)
│   └── gateway-service.yaml
├── Dockerfile                # Define cómo construir la imagen Docker
├── pyproject.toml            # Define dependencias (Poetry)
├── poetry.lock               # Lockfile de dependencias
└── README.md                 # Este archivo
```

## 6. Configuración

El servicio se configura principalmente mediante variables de entorno (o un archivo `.env` para desarrollo local), utilizando el prefijo `GATEWAY_`.

**Variables de Entorno Clave:**

| Variable                   | Descripción                                           | Ejemplo (Valor Esperado en K8s)                                  | Gestionado por |
| :------------------------- | :---------------------------------------------------- | :--------------------------------------------------------------- | :------------- |
| `GATEWAY_LOG_LEVEL`        | Nivel de logging (DEBUG, INFO, WARNING, ERROR).       | `INFO`                                                           | ConfigMap      |
| `GATEWAY_INGEST_SERVICE_URL`| URL base del Ingest Service API.                     | `http://ingest-api-service.nyro-develop.svc.cluster.local:80`  | ConfigMap      |
| `GATEWAY_QUERY_SERVICE_URL` | URL base del Query Service API.                      | `http://query-service.nyro-develop.svc.cluster.local:80`     | ConfigMap      |
| `GATEWAY_JWT_SECRET`       | Clave secreta compartida para verificar tokens JWT.   | *Valor secreto*                                                  | Secret         |
| `GATEWAY_JWT_ALGORITHM`    | Algoritmo usado para los tokens JWT.                  | `HS256`                                                          | ConfigMap      |
| `GATEWAY_HTTP_CLIENT_TIMEOUT` | Timeout (segundos) para llamadas a servicios downstream. | `30`                                                             | ConfigMap      |
| `PORT`                     | Puerto en el que corre Gunicorn dentro del contenedor. | `8080` (coincide con Dockerfile/Deployment)                       | Deployment     |

**Kubernetes:**

*   La configuración se inyecta a través de `api-gateway-config` (ConfigMap) y `api-gateway-secrets` (Secret) en el namespace `nyro-develop`.
*   Revisa los archivos en la carpeta `k8s/` para detalles del despliegue.

## 7. API Endpoints

*   **`GET /`**: Endpoint raíz, devuelve un mensaje indicando que el gateway está activo.
*   **`GET /health`**: Endpoint de Health Check para Kubernetes. Devuelve `{"status": "healthy"}` si el gateway está listo.
*   **`/api/v1/ingest/{path:path}` (Proxy)**: (Requiere Auth) Reenvía todas las solicitudes bajo esta ruta al `Ingest Service`. Métodos soportados: GET, POST, PUT, DELETE, PATCH.
*   **`/api/v1/query/{path:path}` (Proxy)**: (Requiere Auth) Reenvía todas las solicitudes bajo esta ruta al `Query Service`. Métodos soportados: GET, POST, PUT, DELETE, PATCH.
*   **(Opcional) `/api/auth/{path:path}` (Proxy)**: (No requiere Auth por defecto) Reenvía solicitudes al servicio de autenticación si está configurado (`GATEWAY_AUTH_SERVICE_URL`).

## 8. Ejecución Local (Desarrollo)

1.  **Asegúrate de tener Poetry instalado.**
2.  **Instala dependencias:**
    ```bash
    cd api-gateway
    poetry install
    ```
3.  **Crea un archivo `.env`** en la raíz de `api-gateway/` con las variables necesarias (al menos `GATEWAY_JWT_SECRET` y las URLs de los servicios si no corren en K8s localmente):
    ```dotenv
    # api-gateway/.env
    GATEWAY_LOG_LEVEL=DEBUG
    GATEWAY_JWT_SECRET="tu_super_secreto_compartido" # Usa el mismo que los otros servicios
    # Si corres los microservicios localmente fuera de K8s:
    GATEWAY_INGEST_SERVICE_URL="http://localhost:8000"
    GATEWAY_QUERY_SERVICE_URL="http://localhost:8001"
    # Si usas un Auth service local:
    # GATEWAY_AUTH_SERVICE_URL="http://localhost:8002"
    ```
4.  **Ejecuta el servidor Uvicorn:**
    ```bash
    poetry run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
    ```
    El gateway estará disponible en `http://localhost:8080`.

## 9. Despliegue

*   **Docker:** Construye la imagen usando el `Dockerfile` proporcionado:
    ```bash
    docker build -t ghcr.io/dev-nyro/api-gateway:latest .
    ```
*   **Kubernetes:** Aplica los manifests YAML en la carpeta `k8s/` (asegúrate de crear el Secret `api-gateway-secrets` con el valor correcto):
    ```bash
    kubectl apply -f k8s/gateway-configmap.yaml
    kubectl apply -f k8s/gateway-secret.yaml # Asegúrate que existe y tiene el secreto
    kubectl apply -f k8s/gateway-deployment.yaml
    kubectl apply -f k8s/gateway-service.yaml
    ```

## 10. TODO / Mejoras Futuras

*   Implementar Rate Limiting.
*   Añadir Tracing distribuido (OpenTelemetry).
*   Refinar manejo de errores específicos de los servicios downstream.
*   Implementar caching de respuestas si aplica.
*   Mejorar la configuración de CORS para producción.
*   Añadir tests de integración.
```