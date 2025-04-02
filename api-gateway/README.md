# API Gateway (api-gateway) - Nyro SaaS B2B

## 1. Visión General

Este servicio actúa como el **API Gateway** para la plataforma SaaS B2B de Nyro. Es el punto de entrada único y seguro para todas las solicitudes externas destinadas a los microservicios backend (Ingesta, Consulta, etc.). Sus responsabilidades principales son:

*   **Routing:** Dirige las solicitudes entrantes al microservicio apropiado (`ingest-service`, `query-service`) basándose en la ruta de la URL (`/api/v1/ingest/*`, `/api/v1/query/*`).
*   **Autenticación y Autorización (Supabase JWT):**
    *   Verifica los tokens JWT emitidos por Supabase, presentados en el header `Authorization: Bearer <token>`.
    *   Valida la **firma** del token usando el secreto compartido (`GATEWAY_JWT_SECRET`).
    *   Valida la **expiración** (`exp`).
    *   Valida la **audiencia** (`aud` debe ser `'authenticated'`).
    *   Verifica la presencia de **claims requeridos** (`sub`, `aud`, `exp`).
    *   **Extrae y requiere el `company_id`** del claim `app_metadata` dentro del token. Si falta, la solicitud es rechazada (403 Forbidden).
    *   Asegura que solo las solicitudes autenticadas y autorizadas lleguen a los servicios protegidos.
*   **Inyección de Headers:** Añade headers críticos a las solicitudes reenviadas a los servicios backend, basados en el payload del token validado:
    *   `X-Company-ID`: Extraído de `app_metadata.company_id`.
    *   `X-User-ID`: Extraído del claim `sub` (Subject/User ID de Supabase).
    *   `X-User-Email`: Extraído del claim `email`.
*   **Proxy Inverso:** Reenvía eficientemente las solicitudes (incluyendo cuerpo y query params) a los servicios correspondientes utilizando un cliente HTTP asíncrono (`httpx`).
*   **Centralized CORS:** Gestiona la configuración de Cross-Origin Resource Sharing (CORS) en un solo lugar, permitiendo el acceso desde frontends específicos (localhost, Vercel, Ngrok).
*   **Punto de Observabilidad:**
    *   **Logging Estructurado (JSON):** Utiliza `structlog` para logs detallados y consistentes.
    *   **Request ID:** Genera/propaga un `X-Request-ID` para trazar solicitudes.
    *   **Timing:** Mide y loguea el tiempo de procesamiento de cada solicitud (`X-Process-Time`).
*   **Manejo de Errores:** Proporciona respuestas de error estandarizadas y loguea excepciones no controladas.
*   **Health Check:** Endpoint `/health` para verificaciones de estado (Kubernetes probes).

Este Gateway está diseñado para ser ligero y eficiente, enfocándose en la seguridad y el enrutamiento, y delegando la lógica de negocio a los microservicios correspondientes.

## 2. Arquitectura General del Proyecto (Posición del Gateway)

```mermaid
%%{init: {'theme': 'base', 'themeVariables': { 'primaryColor': '#ADD8E6', 'edgeLabelBackground':'#fff', 'tertiaryColor': '#FFFACD'}}}%%
flowchart TD
    A[Usuario/Cliente Externo<br/>(e.g., Frontend Vercel)] -->|HTTPS / REST API<br/>Authorization: Bearer <supabase_token>| C["<strong>API Gateway (FastAPI)</strong><br/>(Este Servicio)"]

    subgraph KubernetesCluster ["Kubernetes Cluster (nyro-develop ns)"]
        direction LR
        C -- Valida JWT -->|Añade Headers:<br/>X-Company-ID,<br/>X-User-ID,<br/>X-User-Email| I["Ingest Service API"]
        C -- Valida JWT -->|Añade Headers:<br/>X-Company-ID,<br/>X-User-ID,<br/>X-User-Email| Q["Query Service API"]
        C -- Proxy Directo (Opcional) -->|Sin validación JWT en Gateway| Auth["(Opcional) Auth Service<br/>o Supabase Directo"]

        I --> DBi[("Supabase (Metadata)")]
        I --> VDBi[(Milvus (Vectores)")]
        I --> S3i[(MinIO (Archivos)")]
        I --> Qi([Redis (Celery Queue)])

        Q --> DBq[("Supabase (Logs)")]
        Q --> VDBq[(Milvus (Vectores)")]
        Q --> LLM[("Google Gemini API")]
        Q --> Emb[("OpenAI Embedding API")]

        Auth --> DBa[("Supabase Auth")]
    end

    Supabase[("Supabase<br/>(Auth, DB)")] -->|Emite JWT| A
    Supabase -->|JWT Secret| C
```

## 3. Características Clave

*   **Proxy Inverso Asíncrono:** Reenvía tráfico a `ingest-service` y `query-service` usando `httpx`. Soporta streaming de request/response bodies.
*   **Validación de Tokens JWT de Supabase:** Verifica tokens usando `python-jose`. Valida firma, expiración, audiencia (`'authenticated'`), claims requeridos (`sub`, `aud`, `exp`), y la presencia obligatoria de `company_id` en `app_metadata`.
*   **Inyección de Headers de Contexto:** Añade `X-Company-ID`, `X-User-ID`, `X-User-Email` a las solicitudes downstream.
*   **Routing basado en Path:** Usa FastAPI para dirigir `/api/v1/ingest/*` e `/api/v1/query/*`.
*   **Autenticación por Ruta:** Utiliza dependencias (`Depends(require_user)`) de FastAPI para proteger rutas específicas.
*   **Cliente HTTP Asíncrono Reutilizable:** Gestiona un pool de conexiones `httpx` a través del ciclo de vida de FastAPI (`lifespan`) para eficiencia.
*   **Logging Estructurado (JSON):** Logs detallados con `structlog`, incluyendo `request_id`, `user_id`, `company_id` cuando aplique.
*   **Configuración Centralizada:** Carga configuración desde variables de entorno y `.env` (`pydantic-settings`), con prefijo `GATEWAY_`. **Incluye verificación crítica para el secreto JWT.**
*   **Middleware:** Incluye middleware para CORS, Request ID y timing.
*   **Manejo de Errores:** Handlers globales para `HTTPException` y errores genéricos (500).
*   **Despliegue Contenerizado:** Dockerfile y manifests de Kubernetes listos.
*   **Health Check:** Endpoint `/health` para K8s probes, verifica disponibilidad del cliente HTTP interno.
*   **(Opcional) Proxy de Autenticación:** Puede reenviar tráfico a un servicio de autenticación (`/api/v1/auth/*`) sin requerir validación de token en el gateway.

## 4. Pila Tecnológica Principal

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Cliente HTTP:** HTTPX
*   **Validación JWT:** python-jose[cryptography]
*   **Configuración:** pydantic-settings
*   **Logging:** structlog
*   **Despliegue:** Docker, Kubernetes, Gunicorn + Uvicorn
*   **Autenticación:** Supabase (emisor de JWTs)

## 5. Estructura de la Codebase

```
api-gateway/
├── app/
│   ├── __init__.py
│   ├── auth/                 # Lógica de autenticación y validación de tokens Supabase
│   │   ├── __init__.py
│   │   ├── auth_middleware.py # Dependencias FastAPI (get_current_user_payload, require_user)
│   │   └── jwt_handler.py     # Función verify_token (lógica central de validación Supabase JWT)
│   ├── core/                 # Configuración y Logging Core
│   │   ├── __init__.py
│   │   ├── config.py         # Carga de Settings (pydantic-settings), validación JWT_SECRET
│   │   └── logging_config.py # Configuración de structlog (JSON output)
│   ├── main.py               # Entrypoint FastAPI, lifespan (cliente httpx), middlewares (CORS, ID, Timing), health checks, exception handlers
│   └── routers/              # Definición de rutas proxy
│       ├── __init__.py
│       └── gateway_router.py # Lógica principal de proxying, inyección de headers, definición de rutas (/ingest, /query, /auth)
├── k8s/                      # Manifests de Kubernetes
│   ├── gateway-configmap.yaml
│   ├── gateway-deployment.yaml
│   ├── gateway-secret.yaml   # (Estructura, valor real en gestor de secretos)
│   └── gateway-service.yaml
├── Dockerfile                # Define cómo construir la imagen Docker
├── pyproject.toml            # Define dependencias (Poetry)
├── poetry.lock               # Lockfile de dependencias
└── README.md                 # Este archivo
```

## 6. Configuración

El servicio se configura principalmente mediante variables de entorno (o un archivo `.env` para desarrollo local), utilizando el prefijo `GATEWAY_`.

**Variables de Entorno Clave:**

| Variable                                | Descripción                                                                 | Ejemplo (Valor Esperado en K8s)                                  | Gestionado por |
| :-------------------------------------- | :-------------------------------------------------------------------------- | :--------------------------------------------------------------- | :------------- |
| `GATEWAY_LOG_LEVEL`                     | Nivel de logging (DEBUG, INFO, WARNING, ERROR).                             | `INFO`                                                           | ConfigMap      |
| `GATEWAY_INGEST_SERVICE_URL`            | URL base del Ingest Service API (dentro del cluster).                         | `http://ingest-api-service.nyro-develop.svc.cluster.local:80`  | ConfigMap      |
| `GATEWAY_QUERY_SERVICE_URL`             | URL base del Query Service API (dentro del cluster).                          | `http://query-service.nyro-develop.svc.cluster.local:80`     | ConfigMap      |
| `GATEWAY_AUTH_SERVICE_URL`              | (Opcional) URL base del Auth Service para proxy directo.                      | `http://auth-service.nyro-develop.svc.cluster.local:80`      | ConfigMap      |
| **`GATEWAY_JWT_SECRET`**                | **Clave secreta JWT de tu proyecto Supabase.** ¡Crítico para seguridad!       | *Valor secreto obtenido de Supabase*                             | **Secret**     |
| `GATEWAY_JWT_ALGORITHM`                 | Algoritmo usado para los tokens JWT (debe coincidir con Supabase).           | `HS256` (Típico en Supabase)                                     | ConfigMap      |
| `GATEWAY_HTTP_CLIENT_TIMEOUT`           | Timeout (segundos) para llamadas a servicios downstream.                      | `60`                                                             | ConfigMap      |
| `GATEWAY_HTTP_CLIENT_MAX_CONNECTIONS`   | Máximo número total de conexiones salientes.                                | `100`                                                            | ConfigMap      |
| `GATEWAY_HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS` | Máximo número de conexiones keep-alive.                           | `20`                                                             | ConfigMap      |
| `VERCEL_FRONTEND_URL`                   | (Opcional) URL del frontend en Vercel para CORS.                            | `https://<tu-app>.vercel.app`                                     | ConfigMap/Env  |
| `NGROK_URL`                             | (Opcional) URL de ngrok para desarrollo/pruebas CORS.                       | `https://<tu-id>.ngrok-free.app`                                 | ConfigMap/Env  |
| `PORT`                                  | Puerto en el que corre Gunicorn dentro del contenedor.                        | `8080` (coincide con Dockerfile/Deployment)                       | Deployment     |

**¡ADVERTENCIA DE SEGURIDAD JWT!**

*   La variable `GATEWAY_JWT_SECRET` **DEBE** configurarse con el secreto JWT real de tu proyecto Supabase (lo encuentras en la configuración de tu proyecto Supabase > API > JWT Settings).
*   **NUNCA** despliegues con el valor por defecto `"YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME_IN_ENV_OR_SECRET"`. El código en `app/core/config.py` incluye una verificación que emitirá logs CRÍTICOS si se detecta el valor por defecto.
*   En Kubernetes, este valor debe gestionarse a través de un `Secret` (`api-gateway-secrets`), no un `ConfigMap`.

**Kubernetes:**

*   La configuración general se inyecta a través de `api-gateway-config` (ConfigMap).
*   El secreto JWT se inyecta a través de `api-gateway-secrets` (Secret).
*   Ambos deben existir en el namespace `nyro-develop`. Revisa los archivos en `k8s/`.

## 7. Flujo de Autenticación (Detallado con Supabase JWT)

1.  **Solicitud del Cliente:** El cliente (ej: frontend) obtiene un token JWT de Supabase después de que el usuario inicia sesión. Envía una solicitud a una ruta protegida del Gateway (ej: `/api/v1/ingest/upload`) incluyendo el header: `Authorization: Bearer <supabase_jwt_token>`.
2.  **Recepción en Gateway:** FastAPI recibe la solicitud. Para rutas protegidas con `Depends(require_user)`, el middleware de autenticación se activa.
3.  **Extracción del Token:** La dependencia `get_current_user_payload` utiliza `HTTPBearer` para extraer el token del header `Authorization`.
4.  **Validación del Token (`verify_token`):**
    *   Se llama a la función `verify_token` en `app/auth/jwt_handler.py`.
    *   **Firma:** Se verifica la firma del token usando `GATEWAY_JWT_SECRET` y `GATEWAY_JWT_ALGORITHM`. (Fallo -> 401)
    *   **Expiración:** Se comprueba si el claim `exp` es futuro. (Fallo -> 401)
    *   **Audiencia:** Se comprueba si el claim `aud` es igual a `'authenticated'`. (Fallo -> 401)
    *   **Claims Requeridos:** Se verifica la presencia de `sub`, `aud`, `exp`. (Fallo -> 401)
    *   **Extracción y Validación de `company_id`:** Se busca `company_id` dentro del claim `app_metadata` del payload decodificado.
        *   Si `company_id` **no se encuentra**, se lanza `HTTPException(403, detail="User authenticated, but company association is missing in token.")`. El usuario está autenticado (token válido) pero no autorizado para proceder en este contexto.
        *   Si `company_id` **se encuentra**, se añade al diccionario del payload que será devuelto.
5.  **Resultado de Validación:**
    *   **Éxito:** `verify_token` devuelve el payload decodificado y validado (incluyendo `company_id`, `sub`, `email`, etc.).
    *   **Fallo:** `verify_token` lanza una `HTTPException` (normalmente 401 o 403) con detalles específicos y headers `WWW-Authenticate`. FastAPI detiene el procesamiento y devuelve el error al cliente.
6.  **Dependencia `require_user`:**
    *   Si `get_current_user_payload` devolvió un payload válido, `require_user` simplemente lo devuelve, permitiendo que la ejecución continúe hacia la función de la ruta.
    *   Si `get_current_user_payload` devolvió `None` (porque no se envió el header `Authorization`), `require_user` lanza `HTTPException(401, detail="Not authenticated")`.
7.  **Procesamiento en el Router (`gateway_router.py`):**
    *   La función de la ruta (ej: `proxy_ingest_service`) recibe el `user_payload` validado.
    *   La función `_proxy_request` extrae la información necesaria del `user_payload`:
        *   `user_payload['sub']` -> Header `X-User-ID`
        *   `user_payload['company_id']` -> Header `X-Company-ID`
        *   `user_payload['email']` -> Header `X-User-Email`
    *   Estos headers se añaden a la solicitud que se enviará al microservicio backend.
8.  **Proxy al Backend:** La solicitud, ahora enriquecida con los headers de contexto, se reenvía al microservicio correspondiente (Ingesta o Consulta).
9.  **Respuesta del Backend:** La respuesta del microservicio se devuelve al cliente a través del Gateway.

## 8. API Endpoints

*   **`GET /`**
    *   **Descripción:** Endpoint raíz para verificar que el Gateway está activo.
    *   **Autenticación:** No requerida.
    *   **Respuesta:** `{"message": "Nyro API Gateway is running!"}`

*   **`GET /health`**
    *   **Descripción:** Endpoint de Health Check para Kubernetes (Liveness/Readiness). Verifica si el servicio está operativo y el cliente HTTP interno está listo.
    *   **Autenticación:** No requerida.
    *   **Respuesta Exitosa (200 OK):** `{"status": "healthy", "service": "Nyro API Gateway"}`
    *   **Respuesta Fallida (503 Service Unavailable):** Si el cliente HTTP no está inicializado.

*   **`/api/v1/ingest/{path:path}` (Proxy)**
    *   **Métodos:** `GET`, `POST`, `PUT`, `DELETE`, `PATCH`
    *   **Descripción:** Reenvía todas las solicitudes bajo esta ruta al `Ingest Service` (`GATEWAY_INGEST_SERVICE_URL`).
    *   **Autenticación:** **Requerida.** Se necesita un `Authorization: Bearer <supabase_jwt_token>` válido. El token debe ser válido (firma, exp, aud='authenticated') y contener `company_id` en `app_metadata`.
    *   **Headers Inyectados:** `X-Company-ID`, `X-User-ID`, `X-User-Email`.
    *   **Ejemplo:** `POST /api/v1/ingest/documents`

*   **`/api/v1/query/{path:path}` (Proxy)**
    *   **Métodos:** `GET`, `POST`, `PUT`, `DELETE`, `PATCH`
    *   **Descripción:** Reenvía todas las solicitudes bajo esta ruta al `Query Service` (`GATEWAY_QUERY_SERVICE_URL`).
    *   **Autenticación:** **Requerida.** Mismos requisitos que para `/ingest`.
    *   **Headers Inyectados:** `X-Company-ID`, `X-User-ID`, `X-User-Email`.
    *   **Ejemplo:** `POST /api/v1/query/ask`

*   **`/api/v1/auth/{path:path}` (Proxy Opcional)**
    *   **Métodos:** `GET`, `POST`, `PUT`, `DELETE`, `PATCH`, `OPTIONS`
    *   **Descripción:** (Si `GATEWAY_AUTH_SERVICE_URL` está configurado) Reenvía todas las solicitudes bajo esta ruta al servicio de autenticación especificado. Útil para endpoints como login, signup, refresh token, etc., que manejan su propia lógica de autenticación/token.
    *   **Autenticación:** **No requerida por el Gateway.** El Gateway *no* valida el token JWT para estas rutas. Cualquier token enviado se pasa directamente al servicio de autenticación.
    *   **Headers Inyectados:** Ninguno (a menos que se modifique `_proxy_request` específicamente para este caso).
    *   **Si no está configurado:** Devuelve `501 Not Implemented`.

## 9. Ejecución Local (Desarrollo)

1.  **Asegúrate de tener Poetry instalado.** ([https://python-poetry.org/docs/#installation](https://python-poetry.org/docs/#installation))
2.  **Clona el repositorio** (si aún no lo has hecho).
3.  **Navega al directorio `api-gateway`:**
    ```bash
    cd api-gateway
    ```
4.  **Instala dependencias:**
    ```bash
    poetry install
    ```
5.  **Crea un archivo `.env`** en la raíz de `api-gateway/` con las variables necesarias. **¡Usa tu secreto JWT real de Supabase!**
    ```dotenv
    # api-gateway/.env

    # --- Logging ---
    GATEWAY_LOG_LEVEL=DEBUG # Más verboso para desarrollo

    # --- Service URLs (Ajusta si corres otros servicios localmente) ---
    GATEWAY_INGEST_SERVICE_URL="http://localhost:8001" # Ejemplo: Puerto del Ingest Service local
    GATEWAY_QUERY_SERVICE_URL="http://localhost:8002"  # Ejemplo: Puerto del Query Service local
    # GATEWAY_AUTH_SERVICE_URL="http://localhost:8000" # Descomenta si usas un Auth Service local

    # --- Supabase JWT Configuration (¡IMPORTANTE!) ---
    GATEWAY_JWT_SECRET="TU_SUPABASE_JWT_SECRET_REAL_AQUI" # ¡Pega tu secreto de Supabase!
    GATEWAY_JWT_ALGORITHM="HS256" # Verifica que coincida con Supabase

    # --- HTTP Client Settings (Opcional, usa defaults si no se especifican) ---
    GATEWAY_HTTP_CLIENT_TIMEOUT=60
    # GATEWAY_HTTP_CLIENT_MAX_CONNECTIONS=100
    # GATEWAY_HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS=20

    # --- CORS Settings (Opcional, para permitir otros orígenes locales) ---
    # VERCEL_FRONTEND_URL="http://localhost:3000" # Si tu frontend corre en 3000
    # NGROK_URL="https://<id>.ngrok-free.app" # Si usas ngrok

    ```
6.  **Ejecuta el servidor Uvicorn:**
    ```bash
    poetry run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
    ```
    *   `--host 0.0.0.0`: Escucha en todas las interfaces de red.
    *   `--port 8080`: Puerto estándar para el gateway (diferente a los microservicios).
    *   `--reload`: Uvicorn reiniciará automáticamente cuando detecte cambios en el código (útil en desarrollo).

7.  El API Gateway estará disponible en `http://localhost:8080`. Puedes probar los endpoints `/` y `/health`. Para probar rutas protegidas (`/api/v1/ingest/*`, `/api/v1/query/*`), necesitarás enviar una solicitud con un header `Authorization: Bearer <token>` válido obtenido de tu instancia de Supabase.

## 10. Despliegue

*   **Docker:**
    1.  Construye la imagen Docker usando el `Dockerfile` proporcionado:
        ```bash
        docker build -t ghcr.io/dev-nyro/api-gateway:latest .
        # O usa tu propio registro de contenedor
        # docker build -t tu-registro/api-gateway:v1.0.0 .
        ```
    2.  Empuja la imagen a tu registro de contenedores (ej: GHCR, Docker Hub, GCR):
        ```bash
        docker push ghcr.io/dev-nyro/api-gateway:latest
        # docker push tu-registro/api-gateway:v1.0.0
        ```

*   **Kubernetes:**
    1.  **Pre-requisito:** Asegúrate de que el namespace `nyro-develop` existe en tu cluster.
    2.  **Crear el Secret:** Crea el secreto `api-gateway-secrets` en el namespace `nyro-develop` con el valor real de `GATEWAY_JWT_SECRET`. **¡NO LO COMMITTEES AL REPOSITORIO!**
        ```bash
        kubectl create secret generic api-gateway-secrets \
          --namespace nyro-develop \
          --from-literal=GATEWAY_JWT_SECRET='TU_SUPABASE_JWT_SECRET_REAL_AQUI'
        ```
        *(Reemplaza `'TU_SUPABASE_JWT_SECRET_REAL_AQUI'` con tu secreto)*
    3.  **Aplicar los Manifests:** Aplica los archivos YAML de la carpeta `k8s/` en el orden correcto (ConfigMap/Secret antes que Deployment):
        ```bash
        kubectl apply -f k8s/gateway-configmap.yaml -n nyro-develop
        # El secreto ya fue creado en el paso anterior
        kubectl apply -f k8s/gateway-deployment.yaml -n nyro-develop
        kubectl apply -f k8s/gateway-service.yaml -n nyro-develop
        ```
    4.  Verifica que los pods se inicien correctamente y que el servicio esté disponible. El servicio `gateway-service` (tipo ClusterIP por defecto) expondrá el gateway dentro del cluster. Necesitarás un Ingress Controller o un Service tipo LoadBalancer para exponerlo externamente.

## 11. TODO / Mejoras Futuras

*   **Rate Limiting:** Implementar limitación de tasa (ej: usando `slowapi` o soluciones a nivel de Ingress) para proteger los servicios backend contra abuso.
*   **Tracing Distribuido:** Integrar OpenTelemetry para trazar solicitudes a través del Gateway y los microservicios downstream.
*   **Caching:** Considerar caching de respuestas para endpoints específicos si aplica (ej: configuración, datos raramente cambiantes).
*   **Tests de Integración:** Añadir tests que simulen solicitudes completas a través del gateway, incluyendo validación JWT y proxying.
*   **Refinar Manejo de Errores:** Mapear errores específicos de los servicios downstream a respuestas de error más informativas (sin exponer detalles internos).
*   **Configuración CORS más granular:** Ajustar `allow_methods`, `allow_headers` de forma más restrictiva para producción.
*   **WebSockets Proxy:** Si algún servicio backend requiere WebSockets, añadir soporte para proxy WebSocket.
