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
    *   **Para rutas protegidas estándar:** Extrae y **requiere** el `company_id` del claim `app_metadata` dentro del token. Si falta, la solicitud es rechazada (403 Forbidden).
    *   **Para asociación inicial:** Proporciona un endpoint (`/api/v1/users/me/ensure-company`) que valida el token pero *no* requiere `company_id` inicialmente, permitiendo asociar uno.
    *   Asegura que solo las solicitudes autenticadas y autorizadas lleguen a los servicios protegidos.
*   **Asociación de Compañía (Inicial):**
    *   Provee un endpoint (`POST /api/v1/users/me/ensure-company`) para que usuarios recién autenticados (sin `company_id` en su token) puedan ser asociados a una compañía.
    *   Utiliza el cliente **Supabase Admin** (con la `Service Role Key`) para actualizar el `app_metadata` del usuario con un `company_id` (generalmente uno por defecto configurable).
*   **Inyección de Headers:** Añade headers críticos a las solicitudes reenviadas a los servicios backend, basados en el payload del token validado:
    *   `X-Company-ID`: Extraído de `app_metadata.company_id`.
    *   `X-User-ID`: Extraído del claim `sub` (Subject/User ID de Supabase).
    *   `X-User-Email`: Extraído del claim `email`.
*   **Proxy Inverso:** Reenvía eficientemente las solicitudes (incluyendo cuerpo y query params) a los servicios correspondientes utilizando un cliente HTTP asíncrono (`httpx`).
*   **Centralized CORS:** Gestiona la configuración de Cross-Origin Resource Sharing (CORS) en un solo lugar.
*   **Punto de Observabilidad:** Logging estructurado (JSON), Request ID, Timing.
*   **Manejo de Errores:** Respuestas de error estandarizadas y logging de excepciones.
*   **Health Check:** Endpoint `/health` para verificaciones de estado (Kubernetes probes), incluyendo chequeo del cliente Supabase Admin.

Este Gateway está diseñado para ser ligero y eficiente, enfocándose en la seguridad y el enrutamiento, y delegando la lógica de negocio a los microservicios correspondientes, pero también manejando la lógica crítica de asociación inicial de compañía.

## 2. Arquitectura General del Proyecto (Posición del Gateway)

```mermaid
%%{init: {'theme': 'base', 'themeVariables': { 'primaryColor': '#ADD8E6', 'edgeLabelBackground':'#fff', 'tertiaryColor': '#FFFACD'}}}%%
flowchart TD
    A[Usuario/Cliente Externo<br/>(e.g., Frontend Vercel)] -->|HTTPS / REST API<br/>Authorization: Bearer <supabase_token>| C["<strong>API Gateway (FastAPI)</strong><br/>(Este Servicio)"]

    subgraph KubernetesCluster ["Kubernetes Cluster (nyro-develop ns)"]
        direction LR
        C -- Valida JWT (Requiere company_id) -->|Añade Headers:<br/>X-Company-ID,<br/>X-User-ID,<br/>X-User-Email| I["Ingest Service API"]
        C -- Valida JWT (Requiere company_id) -->|Añade Headers:<br/>X-Company-ID,<br/>X-User-ID,<br/>X-User-Email| Q["Query Service API"]
        C -- Valida JWT (SIN company_id) -->|Llama a Supabase Admin API<br/>(Actualiza app_metadata)| SupaAdmin["Supabase Admin API<br/>(via utils/supabase_admin)"]
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
        SupaAdmin --> DBa # El admin API modifica auth.users
    end

    Supabase[("Supabase<br/>(Auth, DB)")] -->|Emite JWT| A
    Supabase -->|JWT Secret, Service Key| C
```

## 3. Características Clave

*   **Proxy Inverso Asíncrono:** Reenvía tráfico a `ingest-service` y `query-service` usando `httpx`.
*   **Validación de Tokens JWT de Supabase:** Verifica tokens (`python-jose`). Valida firma, expiración, audiencia (`'authenticated'`), claims requeridos (`sub`, `aud`, `exp`). **Requiere `company_id` en `app_metadata` para rutas estándar.**
*   **Asociación Inicial de Compañía:** Endpoint dedicado (`/api/v1/users/me/ensure-company`) que valida el token sin requerir `company_id` y usa la **Supabase Service Role Key** para actualizar `app_metadata` del usuario.
*   **Inyección de Headers de Contexto:** Añade `X-Company-ID`, `X-User-ID`, `X-User-Email` a las solicitudes downstream.
*   **Routing basado en Path:** Usa FastAPI para dirigir `/api/v1/ingest/*`, `/api/v1/query/*`, y `/api/v1/users/*`.
*   **Autenticación por Ruta:** Utiliza dependencias (`StrictAuth`, `InitialAuth`) de FastAPI para proteger rutas con diferentes requisitos.
*   **Cliente HTTP Asíncrono Reutilizable:** Gestiona un pool de conexiones `httpx`.
*   **Cliente Supabase Admin Reutilizable:** Gestiona una instancia del cliente Supabase (`supabase-py`) inicializada con la Service Role Key.
*   **Logging Estructurado (JSON):** Logs detallados con `structlog`.
*   **Configuración Centralizada:** Carga configuración desde variables de entorno (`pydantic-settings`), prefijo `GATEWAY_`. **Incluye verificaciones críticas para secretos (JWT y Service Key).**
*   **Middleware:** CORS, Request ID, Timing.
*   **Manejo de Errores:** Handlers globales.
*   **Despliegue Contenerizado:** Dockerfile y manifests K8s listos.
*   **Health Check:** Endpoint `/health` verifica cliente HTTP y cliente Supabase Admin.
*   **(Opcional) Proxy de Autenticación:** Ruta `/api/v1/auth/*`.

## 4. Pila Tecnológica Principal

*   **Lenguaje:** Python 3.10+
*   **Framework API:** FastAPI
*   **Cliente HTTP:** HTTPX
*   **Validación JWT:** python-jose[cryptography]
*   **Cliente Supabase Admin:** supabase-py
*   **Configuración:** pydantic-settings
*   **Logging:** structlog
*   **Despliegue:** Docker, Kubernetes, Gunicorn + Uvicorn
*   **Autenticación:** Supabase (emisor de JWTs, proveedor de identidad)

## 5. Estructura de la Codebase

```
api-gateway/
├── app/
│   ├── __init__.py
│   ├── auth/                 # Lógica de autenticación y validación de tokens
│   │   ├── __init__.py
│   │   ├── auth_middleware.py # Dependencias (StrictAuth, InitialAuth, etc.)
│   │   └── jwt_handler.py     # Función verify_token (con require_company_id)
│   ├── core/                 # Configuración y Logging Core
│   │   ├── __init__.py
│   │   ├── config.py         # Carga de Settings (vars, secretos)
│   │   └── logging_config.py # Configuración de structlog (JSON)
│   ├── main.py               # Entrypoint FastAPI, lifespan (clientes), middlewares, health checks, handlers
│   ├── routers/              # Definición de rutas
│   │   ├── __init__.py
│   │   ├── gateway_router.py # Rutas proxy (/ingest, /query, /auth)
│   │   └── user_router.py    # Rutas de usuario (/users/me/ensure-company)
│   └── utils/                # Utilidades
│       └── supabase_admin.py # Cliente Supabase Admin
├── k8s/                      # Manifests de Kubernetes
│   ├── gateway-configmap.yaml
│   ├── gateway-deployment.yaml
│   ├── gateway-secret.yaml   # (Estructura, valores reales en gestor de secretos)
│   └── gateway-service.yaml
├── Dockerfile                # Define cómo construir la imagen Docker
├── pyproject.toml            # Define dependencias (Poetry) - ¡Incluye supabase!
├── poetry.lock               # Lockfile de dependencias
└── README.md                 # Este archivo
```

## 6. Configuración

Configuración mediante variables de entorno (prefijo `GATEWAY_`).

**Variables de Entorno Clave:**

| Variable                                | Descripción                                                                      | Ejemplo (Valor Esperado en K8s)                                  | Gestionado por |
| :-------------------------------------- | :------------------------------------------------------------------------------- | :--------------------------------------------------------------- | :------------- |
| `GATEWAY_LOG_LEVEL`                     | Nivel de logging (DEBUG, INFO, WARNING, ERROR).                                  | `INFO`                                                           | ConfigMap      |
| `GATEWAY_INGEST_SERVICE_URL`            | URL base del Ingest Service API.                                                 | `http://ingest-api-service.nyro-develop.svc.cluster.local:80`  | ConfigMap      |
| `GATEWAY_QUERY_SERVICE_URL`             | URL base del Query Service API.                                                  | `http://query-service.nyro-develop.svc.cluster.local:80`     | ConfigMap      |
| `GATEWAY_AUTH_SERVICE_URL`              | (Opcional) URL base del Auth Service para proxy directo.                           | `http://auth-service.nyro-develop.svc.cluster.local:80`      | ConfigMap      |
| **`GATEWAY_JWT_SECRET`**                | **Clave secreta JWT de tu proyecto Supabase.** (Para validar tokens de usuario)   | *Valor secreto obtenido de Supabase*                             | **Secret**     |
| `GATEWAY_JWT_ALGORITHM`                 | Algoritmo usado para los tokens JWT (debe coincidir con Supabase).                | `HS256`                                                          | ConfigMap      |
| **`GATEWAY_SUPABASE_URL`**              | **URL de tu proyecto Supabase.** (Necesaria para el cliente Admin)                  | `https://<tu-ref>.supabase.co`                                   | ConfigMap      |
| **`GATEWAY_SUPABASE_SERVICE_ROLE_KEY`** | **Clave de Servicio (Admin) de Supabase.** ¡MUY SENSIBLE! (Para actualizar users) | *Valor secreto obtenido de Supabase*                             | **Secret**     |
| **`GATEWAY_DEFAULT_COMPANY_ID`**        | **UUID de la compañía por defecto** a asignar a nuevos usuarios.                   | *UUID válido de una compañía*                                    | ConfigMap      |
| `GATEWAY_HTTP_CLIENT_TIMEOUT`           | Timeout (segundos) para llamadas HTTP downstream.                                | `60`                                                             | ConfigMap      |
| `GATEWAY_HTTP_CLIENT_MAX_CONNECTIONS`   | Máximo número total de conexiones HTTP salientes.                               | `200` (Ajustado)                                                 | ConfigMap      |
| `GATEWAY_HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS` | Máximo número de conexiones HTTP keep-alive.                            | `100` (Ajustado)                                                 | ConfigMap      |
| `VERCEL_FRONTEND_URL`                   | (Opcional) URL del frontend en Vercel para CORS.                                 | `https://<tu-app>.vercel.app`                                     | ConfigMap/Env  |
| `NGROK_URL`                             | (Opcional) URL de ngrok para desarrollo/pruebas CORS.                            | `https://<tu-id>.ngrok-free.app`                                 | ConfigMap/Env  |
| `PORT`                                  | Puerto interno del contenedor (Gunicorn).                                        | `8080`                                                           | Deployment     |

**¡ADVERTENCIAS DE SEGURIDAD IMPORTANTES!**

*   **`GATEWAY_JWT_SECRET`:** Debe ser el secreto JWT real de Supabase. **NUNCA** usar el valor por defecto. Gestionar vía K8s Secret.
*   **`GATEWAY_SUPABASE_SERVICE_ROLE_KEY`:** Esta clave otorga **acceso total** a tu backend de Supabase, ¡trátala con extremo cuidado! **NUNCA** usar el valor placeholder. **DEBE** gestionarse vía K8s Secret.
*   **`GATEWAY_SUPABASE_URL`:** Debe ser la URL correcta de tu proyecto Supabase.
*   **`GATEWAY_DEFAULT_COMPANY_ID`:** Debe ser un UUID válido correspondiente a una compañía real en tu sistema.

**Kubernetes:**

*   Configuración general (`GATEWAY_..._URL`, `...TIMEOUT`, `LOG_LEVEL`, `DEFAULT_COMPANY_ID`, `SUPABASE_URL`) -> `api-gateway-config` (ConfigMap).
*   Secretos (`GATEWAY_JWT_SECRET`, `GATEWAY_SUPABASE_SERVICE_ROLE_KEY`) -> `api-gateway-secrets` (Secret).
*   Ambos deben existir en el namespace `nyro-develop`.

## 7. Flujo de Autenticación y Asociación de Compañía

Este Gateway implementa dos flujos principales relacionados con la autenticación:

**A) Validación Estándar (Rutas Protegidas como `/ingest`, `/query`):**

1.  **Solicitud del Cliente:** Frontend envía request con `Authorization: Bearer <token_con_company_id>`.
2.  **Recepción y Extracción:** Gateway recibe la solicitud. La dependencia `StrictAuth` (que usa `require_user`) se activa. `get_current_user_payload` extrae el token.
3.  **Validación del Token (`verify_token` con `require_company_id=True`):**
    *   Verifica firma, expiración, audiencia (`'authenticated'`), claims (`sub`, `aud`, `exp`).
    *   Busca y **requiere** `company_id` en `app_metadata`. Si falta -> **403 Forbidden**.
    *   Si todo OK, devuelve el payload (incluyendo `company_id`).
4.  **Dependencia `StrictAuth`:** Confirma que se obtuvo un payload válido.
5.  **Inyección y Proxy:** El router extrae `X-User-ID`, `X-Company-ID`, `X-User-Email` del payload y los inyecta en la solicitud antes de reenviarla al servicio backend correspondiente.

**B) Asociación Inicial de Compañía (Endpoint `/users/me/ensure-company`):**

1.  **Contexto:** El frontend detecta (tras login/confirmación) que el token JWT del usuario *no* tiene `company_id` en `app_metadata`.
2.  **Solicitud del Cliente:** Frontend envía `POST /api/v1/users/me/ensure-company` con el token JWT actual (sin `company_id`).
3.  **Recepción y Extracción:** Gateway recibe la solicitud. La dependencia `InitialAuth` (que usa `require_authenticated_user_no_company_check`) se activa. `_get_user_payload_internal` extrae el token.
4.  **Validación del Token (`verify_token` con `require_company_id=False`):**
    *   Verifica firma, expiración, audiencia, claims (`sub`, `aud`, `exp`).
    *   **NO requiere** `company_id`. Si el token es válido por lo demás, devuelve el payload.
5.  **Dependencia `InitialAuth`:** Confirma que se obtuvo un payload válido (aunque no tenga `company_id`).
6.  **Lógica del Endpoint (`ensure_company_association`):**
    *   Obtiene el `user_id` (`sub`) del payload.
    *   Determina el `company_id` a asignar (usando `GATEWAY_DEFAULT_COMPANY_ID` de la configuración).
    *   Obtiene el cliente **Supabase Admin** (inicializado con `GATEWAY_SUPABASE_SERVICE_ROLE_KEY`).
    *   Llama a `supabase_admin.auth.admin.update_user_by_id()` para actualizar el `app_metadata` del usuario (`user_id`) con el `company_id` asignado.
7.  **Respuesta al Cliente:** Devuelve 200 OK si la actualización fue exitosa.
8.  **Refresco en Frontend:** El frontend, al recibir el 200 OK, debe llamar a `supabase.auth.refreshSession()` para obtener un *nuevo* token JWT que ahora incluirá el `company_id` en `app_metadata`. Las futuras llamadas usarán este token actualizado y pasarán la validación estándar (Flujo A).

## 8. API Endpoints

*   **`GET /`**
    *   **Descripción:** Endpoint raíz.
    *   **Autenticación:** No requerida.
    *   **Respuesta:** `{"message": "Nyro API Gateway is running!"}`

*   **`GET /health`**
    *   **Descripción:** Health Check (K8s probes). Verifica cliente HTTP y cliente Supabase Admin.
    *   **Autenticación:** No requerida.
    *   **Respuesta OK (200):** `{"status": "healthy", "service": "Nyro API Gateway"}`
    *   **Respuesta Error (503):** Si alguna dependencia (cliente HTTP, cliente Admin) no está lista.

*   **`/api/v1/ingest/{path:path}` (Proxy)**
    *   **Métodos:** `GET`, `POST`, `PUT`, `DELETE`, `PATCH`
    *   **Descripción:** Reenvía al `Ingest Service`.
    *   **Autenticación:** **Requerida (StrictAuth).** Token JWT válido con `company_id` en `app_metadata`.
    *   **Headers Inyectados:** `X-Company-ID`, `X-User-ID`, `X-User-Email`.

*   **`/api/v1/query/{path:path}` (Proxy)**
    *   **Métodos:** `GET`, `POST`, `PUT`, `DELETE`, `PATCH`
    *   **Descripción:** Reenvía al `Query Service`.
    *   **Autenticación:** **Requerida (StrictAuth).** Token JWT válido con `company_id` en `app_metadata`.
    *   **Headers Inyectados:** `X-Company-ID`, `X-User-ID`, `X-User-Email`.

*   **`POST /api/v1/users/me/ensure-company`**
    *   **Descripción:** Asocia un `company_id` por defecto al usuario autenticado si aún no tiene uno en su `app_metadata`. Usa privilegios de administrador (Service Role Key).
    *   **Autenticación:** **Requerida (InitialAuth).** Token JWT válido (firma, exp, aud), pero **no** requiere `company_id` preexistente.
    *   **Headers Inyectados:** Ninguno (este endpoint actúa sobre Supabase, no hace proxy).
    *   **Cuerpo (Request):** Vacío (el `company_id` se determina en el backend).
    *   **Respuesta OK (200):** `{"message": "Company association successful."}` o `{"message": "Company association already exists."}`.
    *   **Respuesta Error:** 401 (Token inválido/ausente), 500 (Error de configuración o al actualizar Supabase).

*   **`/api/v1/auth/{path:path}` (Proxy Opcional)**
    *   **Métodos:** Todos
    *   **Descripción:** (Si `GATEWAY_AUTH_SERVICE_URL` está configurado) Reenvía al servicio de autenticación.
    *   **Autenticación:** **No requerida por el Gateway.**
    *   **Headers Inyectados:** Ninguno.
    *   **Si no está configurado:** Devuelve `501 Not Implemented`.

## 9. Ejecución Local (Desarrollo)

1.  Instalar Poetry.
2.  Clonar repo, `cd api-gateway`.
3.  `poetry install`
4.  Crear archivo `.env` en `api-gateway/` con:
    ```dotenv
    # api-gateway/.env

    GATEWAY_LOG_LEVEL=DEBUG

    # URLs de servicios locales (ajustar puertos)
    GATEWAY_INGEST_SERVICE_URL="http://localhost:8001"
    GATEWAY_QUERY_SERVICE_URL="http://localhost:8002"
    # GATEWAY_AUTH_SERVICE_URL="http://localhost:8000"

    # --- Supabase Config (¡IMPORTANTE!) ---
    # Secreto para validar tokens de usuario
    GATEWAY_JWT_SECRET="TU_SUPABASE_JWT_SECRET_REAL_AQUI"
    GATEWAY_JWT_ALGORITHM="HS256"
    # URL del proyecto Supabase
    GATEWAY_SUPABASE_URL="https://<tu-ref>.supabase.co" # <-- TU URL REAL
    # Clave de Servicio (Admin) - ¡Tratar como secreto!
    GATEWAY_SUPABASE_SERVICE_ROLE_KEY="TU_SUPABASE_SERVICE_ROLE_KEY_REAL_AQUI"
    # ID de Compañía por defecto
    GATEWAY_DEFAULT_COMPANY_ID="TU_UUID_DE_COMPAÑIA_POR_DEFECTO" # <-- UUID VÁLIDO

    # HTTP Client (Opcional)
    GATEWAY_HTTP_CLIENT_TIMEOUT=60
    GATEWAY_HTTP_CLIENT_MAX_CONNECTIONS=100
    GATEWAY_HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS=20 # Ajusta si corregiste nombre

    # CORS (Opcional)
    # VERCEL_FRONTEND_URL="http://localhost:3000"
    # NGROK_URL="https://<id>.ngrok-free.app"
    ```
5.  Ejecutar Uvicorn:
    ```bash
    poetry run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
    ```
6.  Gateway disponible en `http://localhost:8080`.

## 10. Despliegue

*   **Docker:**
    1.  `docker build -t <tu-imagen> .`
    2.  `docker push <tu-imagen>`

*   **Kubernetes:**
    1.  Asegurar namespace `nyro-develop`.
    2.  **Crear Secretos:** Crear `api-gateway-secrets` en `nyro-develop` con los valores reales de `GATEWAY_JWT_SECRET` y `GATEWAY_SUPABASE_SERVICE_ROLE_KEY`. **¡NO COMMITEAR VALORES REALES!**
        ```bash
        # Crear o actualizar el secreto
        kubectl create secret generic api-gateway-secrets \
          --namespace nyro-develop \
          --from-literal=GATEWAY_JWT_SECRET='TU_SUPABASE_JWT_SECRET_REAL_AQUI' \
          --from-literal=GATEWAY_SUPABASE_SERVICE_ROLE_KEY='TU_SUPABASE_SERVICE_ROLE_KEY_REAL_AQUI' \
          --dry-run=client -o yaml | kubectl apply -f -
        ```
        *(Reemplaza los placeholders con tus claves reales)*
    3.  **Aplicar Manifests:**
        ```bash
        # Asegúrate que configmap.yaml tenga GATEWAY_SUPABASE_URL y GATEWAY_DEFAULT_COMPANY_ID
        kubectl apply -f k8s/gateway-configmap.yaml -n nyro-develop
        # El secreto ya fue creado/actualizado
        kubectl apply -f k8s/gateway-deployment.yaml -n nyro-develop
        kubectl apply -f k8s/gateway-service.yaml -n nyro-develop
        ```
    4.  Verificar pods y servicio.

## 11. TODO / Mejoras Futuras

*   **Rate Limiting.**
*   **Tracing Distribuido (OpenTelemetry).**
*   **Caching.**
*   **Tests de Integración** (incluyendo el flujo de asociación de compañía).
*   **Manejo de Errores más Refinado.**
*   **Configuración CORS más Granular.**
*   **Proxy WebSockets.**
*   **Lógica más robusta para determinar `company_id`** en lugar de usar solo `GATEWAY_DEFAULT_COMPANY_ID` (ej., basado en invitaciones, dominio de email, selección del usuario).