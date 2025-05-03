# Plan de Refactorización: Implementación del Admin Dashboard en API Gateway

## 1. Introducción

Este documento detalla los pasos necesarios para refactorizar el servicio `api-gateway` con el fin de implementar los endpoints y la lógica de autorización requeridos por el nuevo Admin Dashboard del frontend de Atenex, según las especificaciones proporcionadas.

El plan se basa en el análisis del esquema actual de la base de datos PostgreSQL, el código existente del `api-gateway` (FastAPI) y las especificaciones del frontend.

**Objetivo Principal:** Permitir a un usuario con el rol 'admin' acceder a nuevas rutas bajo `/api/v1/admin` para ver estadísticas y gestionar compañías y usuarios.

## 2. Prerrequisitos

*   **Backup de Base de Datos:** Realizar un backup completo de la base de datos PostgreSQL antes de aplicar cualquier migración.
*   **Entorno de Desarrollo/Staging:** Realizar y probar todos los cambios en un entorno aislado antes de desplegar a producción.
*   **Usuario Administrador:** Asegurarse de que el usuario `atenex@gmail.com` (o el designado) exista en la tabla `USERS` antes o después de la migración, y que se le asigne el rol `admin` correctamente.

## 3. Pasos de Implementación Detallados

### Paso 3.1: Migración de la Base de Datos

**Acción:** Modificar la tabla `USERS` para cambiar el campo `role` (text) a `roles` (array de texto) y asegurar la existencia de otros campos requeridos.

**Tareas:**

1.  **Crear Script de Migración SQL:**
    *   Renombrar la columna existente (opcional, para preservar datos temporalmente si es necesario):
        ```sql
        -- Opcional: Renombrar columna antigua si se quiere migrar datos existentes
        -- ALTER TABLE users RENAME COLUMN role TO old_role;
        ```
    *   Añadir la nueva columna `roles`:
        ```sql
        ALTER TABLE users ADD COLUMN roles TEXT[] NULL;
        -- Opcional: Establecer un valor por defecto si se prefiere
        -- ALTER TABLE users ADD COLUMN roles TEXT[] DEFAULT ARRAY['user']::TEXT[];
        ```
    *   *(Opcional)* Migrar datos existentes: Si se renombró `old_role`, crear un script para poblar `roles` basado en `old_role`. Ejemplo simple:
        ```sql
        -- Ejemplo: Asignar rol 'user' a todos los que tenían un rol, y 'admin' a uno específico
        -- UPDATE users SET roles = ARRAY['user']::TEXT[] WHERE old_role IS NOT NULL;
        -- UPDATE users SET roles = ARRAY['admin', 'user']::TEXT[] WHERE email = 'atenex@gmail.com'; -- O el email admin
        -- Considerar roles existentes y mapearlos adecuadamente.
        ```
    *   *(Opcional)* Eliminar la columna antigua si se renombró y los datos se migraron:
        ```sql
        -- ALTER TABLE users DROP COLUMN old_role;
        ```
    *   Asegurar que la columna `company_id` permite `NULL` (ya debería ser así según el esquema).
    *   Verificar que `hashed_password` existe (schema usa este nombre, plan usa `password_hash` - usar `hashed_password`).
    *   Verificar que `is_active` existe en `USERS` y `COMPANIES`.

2.  **Aplicar Migración:** Ejecutar el script SQL en la base de datos (desarrollo, staging, producción con cuidado).

### Paso 3.2: Actualizar Cliente de Base de Datos (`app/db/postgres_client.py`)

**Acción:** Modificar funciones existentes y crear nuevas para reflejar el cambio a `roles` y añadir operaciones de admin.

**Tareas:**

1.  **Modificar `get_user_by_email` y `get_user_by_id`:**
    *   Cambiar `SELECT ..., role, ...` por `SELECT ..., roles, ...`.
    *   Asegurarse de que el `dict(row)` devuelva `roles` como una lista de Python. `asyncpg` debería manejar esto automáticamente para `TEXT[]`.
2.  **Crear Nuevas Funciones:**
    *   `create_company(name: str) -> Dict[str, Any]`: Inserta en `COMPANIES` y devuelve los datos de la nueva compañía (`id`, `name`, `created_at`).
    *   `get_active_companies_select() -> List[Dict[str, Any]]`: Ejecuta `SELECT id, name FROM companies WHERE is_active = TRUE ORDER BY name;` y devuelve una lista de diccionarios.
    *   `create_user(email: str, hashed_password: str, name: Optional[str], company_id: uuid.UUID, roles: List[str]) -> Dict[str, Any]`: Inserta en `USERS` con `is_active=TRUE` y los datos proporcionados. Devuelve los datos del usuario creado (sin hash).
    *   `count_active_companies() -> int`: Ejecuta `SELECT COUNT(*) FROM companies WHERE is_active = TRUE;`.
    *   `count_active_users_per_active_company() -> List[Dict[str, Any]]`: Ejecuta la consulta `SELECT c.id as company_id, c.name, COUNT(u.id) AS user_count FROM companies c LEFT JOIN users u ON c.id = u.company_id WHERE c.is_active = TRUE AND u.is_active = TRUE GROUP BY c.id, c.name ORDER BY c.name;` y devuelve la lista de diccionarios.
    *   `get_company_by_id(company_id: uuid.UUID) -> Optional[Dict[str, Any]]`: (Necesario para validar `company_id` al crear usuario) Selecciona una compañía por ID.
    *   `check_email_exists(email: str) -> bool`: Verifica si un email ya existe en la tabla `users`.

### Paso 3.3: Actualizar Servicio de Autenticación (`app/auth/auth_service.py`)

**Acción:** Modificar la creación de tokens para incluir `roles`.

**Tareas:**

1.  **Modificar `authenticate_user`:** Asegurarse de que devuelve el campo `roles` (lista de strings) obtenido de `postgres_client`.
2.  **Modificar `create_access_token`:**
    *   Añadir un parámetro `roles: Optional[List[str]] = None`.
    *   Dentro de la función, si `roles` se proporciona y no es `None`, añadirlo al diccionario `to_encode`: `to_encode["roles"] = roles`.
3.  **Verificar `verify_token`:** No necesita cambios directos para *verificar* `roles` (eso lo hará `AdminAuth`), pero debe seguir funcionando correctamente para validar la estructura base del token y la existencia/estado del usuario.

### Paso 3.4: Implementar Autorización de Administrador (`app/auth/auth_middleware.py`)

**Acción:** Crear la nueva dependencia `AdminAuth`.

**Tareas:**

1.  **Crear `require_admin_user`:**
    *   Esta nueva función asíncrona debe depender de `InitialAuth` (para asegurar que el token es válido y el usuario existe/está activo, sin requerir `company_id` inicialmente).
        ```python
        async def require_admin_user(
            # Depende de InitialAuth para validación base
            user_payload: InitialAuth
        ) -> Dict[str, Any]:
            # user_payload ya está validado (firma, exp, existe, activo) por InitialAuth
            # Ahora verificamos el rol específico de admin
            roles = user_payload.get("roles")
            if not roles or "admin" not in roles:
                log.warning("Admin access denied: User does not have 'admin' role.",
                           user_id=user_payload.get("sub"),
                           roles=roles)
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Insufficient permissions. Administrator role required.",
                )
            log.info("Admin access granted.", user_id=user_payload.get("sub"))
            return user_payload # Devuelve el payload si es admin
        ```
2.  **Definir `AdminAuth`:**
    ```python
    AdminAuth = Annotated[Dict[str, Any], Depends(require_admin_user)]
    ```

### Paso 3.5: Crear Modelos Pydantic para Admin API

**Acción:** Definir los modelos de datos para las solicitudes y respuestas de los nuevos endpoints.

**Tareas:**

1.  Crear un nuevo archivo (p. ej., `app/models/admin_models.py`) o definir dentro de `admin_router.py`.
2.  Definir los siguientes modelos:
    *   `CompanyCreateRequest(BaseModel)`: `name: str`
    *   `CompanyResponse(BaseModel)`: `id: uuid.UUID`, `name: str`, `created_at: datetime`
    *   `CompanySelectItem(BaseModel)`: `id: uuid.UUID`, `name: str`
    *   `CompanyListSelectResponse(BaseModel)`: `data: List[CompanySelectItem]` (O simplemente devolver `List[CompanySelectItem]`)
    *   `UserCreateRequest(BaseModel)`: `email: EmailStr`, `password: str = Field(..., min_length=8)`, `name: Optional[str] = None`, `company_id: uuid.UUID`, `roles: Optional[List[str]] = ['user']`
    *   `UserResponse(BaseModel)`: `id: uuid.UUID`, `email: EmailStr`, `name: Optional[str] = None`, `company_id: uuid.UUID`, `roles: List[str]`, `is_active: bool`, `created_at: datetime`
    *   `UsersPerCompanyStat(BaseModel)`: `company_id: uuid.UUID`, `name: str`, `user_count: int`
    *   `AdminStatsResponse(BaseModel)`: `company_count: int`, `users_per_company: List[UsersPerCompanyStat]`

### Paso 3.6: Crear Router de Administrador (`app/routers/admin_router.py`)

**Acción:** Implementar los nuevos endpoints API protegidos por `AdminAuth`.

**Tareas:**

1.  Crear el archivo `app/routers/admin_router.py`.
2.  Importar `APIRouter`, `Depends`, `HTTPException`, `status`, `AdminAuth`, modelos Pydantic, funciones de `postgres_client`, `get_password_hash` de `auth_service`.
3.  Crear una instancia del router: `router = APIRouter()`.
4.  Implementar `GET /stats`:
    *   Depender de `AdminAuth`.
    *   Llamar a `postgres_client.count_active_companies()` y `postgres_client.count_active_users_per_active_company()`.
    *   Construir y devolver `AdminStatsResponse`.
    *   Loggear la acción con el ID del admin (`user_payload['sub']`).
5.  Implementar `POST /companies`:
    *   Depender de `AdminAuth`.
    *   Recibir `CompanyCreateRequest`.
    *   Validar nombre (no vacío). (Pydantic lo hace si se define bien el modelo).
    *   Llamar a `postgres_client.create_company()`.
    *   Devolver `CompanyResponse` con status `201 Created`.
    *   Loggear la acción con el ID del admin.
6.  Implementar `GET /companies/select`:
    *   Depender de `AdminAuth`.
    *   Llamar a `postgres_client.get_active_companies_select()`.
    *   Devolver `List[CompanySelectItem]`.
    *   Loggear la acción con el ID del admin.
7.  Implementar `POST /users`:
    *   Depender de `AdminAuth`.
    *   Recibir `UserCreateRequest`.
    *   **Validación Adicional:**
        *   Verificar que `company_id` existe llamando a `postgres_client.get_company_by_id()`. Si no, 400 Bad Request.
        *   Verificar que el `email` no existe llamando a `postgres_client.check_email_exists()`. Si existe, 409 Conflict.
        *   Validar fortaleza de contraseña (Pydantic `min_length` ayuda, se pueden añadir regex si es necesario).
    *   Hashear la contraseña: `hashed_password = get_password_hash(user_data.password)`.
    *   Llamar a `postgres_client.create_user()` con los datos validados y hasheados. Asegurarse de pasar `user_data.roles` (que tiene default `['user']`).
    *   Devolver `UserResponse` con status `201 Created`.
    *   Loggear la acción con el ID del admin.

### Paso 3.7: Registrar Nuevo Router (`app/main.py`)

**Acción:** Incluir el router de administración en la aplicación FastAPI.

**Tareas:**

1.  Importar el router: `from app.routers.admin_router import router as admin_router_instance`
2.  Incluir el router con el prefijo correcto: `app.include_router(admin_router_instance, prefix="/api/v1/admin", tags=["Admin"])`

### Paso 3.8: Actualizar Router de Usuario (`app/routers/user_router.py`)

**Acción:** Modificar el endpoint de login para usar los roles.

**Tareas:**

1.  En la función `login_for_access_token`:
    *   Después de llamar a `authenticate_user`, obtener los `roles` del diccionario `user`.
    *   Pasar los `roles` a la función `create_access_token`: `access_token = create_access_token(..., roles=user.get('roles'))`.
    *   *(Opcional)* Incluir `roles` en `LoginResponse` si el frontend lo necesita post-login.

### Paso 3.9: Configuración y Seeding

**Acción:** Asegurar que el usuario admin esté correctamente configurado en la DB.

**Tareas:**

1.  Después de aplicar la migración (Paso 3.1), verificar o insertar/actualizar el registro del usuario administrador (`atenex@gmail.com`).
2.  Asegurarse de que su columna `roles` contenga `ARRAY['admin']` (puede contener otros roles también, como `ARRAY['admin', 'user']`).
3.  Asegurarse de que `is_active` sea `TRUE`.

### Paso 3.10: Pruebas

**Acción:** Escribir y ejecutar pruebas para validar los cambios.

**Tareas:**

1.  **Unitarias:** Probar las nuevas funciones de `postgres_client`, la lógica de `AdminAuth`, y el hasheo de contraseñas.
2.  **Integración:** Probar los nuevos endpoints `/api/v1/admin/*` con:
    *   Token de admin válido (éxito esperado).
    *   Token de usuario normal (error 403 esperado).
    *   Sin token (error 401 esperado).
    *   Token inválido/expirado (error 401 esperado).
    *   Casos de borde (email duplicado, compañía no encontrada, etc.).
3.  Verificar que el endpoint `/login` ahora devuelve un token que contiene el claim `roles`.

### Paso 3.11: Despliegue

**Acción:** Desplegar los cambios actualizados.

**Tareas:**

1.  Construir y pushear la nueva imagen Docker.
2.  Aplicar la migración de base de datos al entorno destino.
3.  Actualizar el despliegue de Kubernetes (`gateway-deployment.yaml`) con la nueva imagen.
4.  Monitorizar logs y rendimiento después del despliegue.

## 4. Checklist de Refactorización

-   [ ] **DB:** Backup Realizado
-   [ ] **DB:** Script de Migración (`role` -> `roles`) Creado
-   [ ] **DB:** Migración Aplicada (Desarrollo/Staging)
-   [ ] **Código:** `postgres_client.py` actualizado (select `roles`)
-   [ ] **Código:** Nuevas funciones CRUD/query en `postgres_client.py` implementadas
-   [ ] **Código:** `auth_service.py` (`create_access_token`) modificado para incluir `roles`
-   [ ] **Código:** `auth_middleware.py` implementado (`require_admin_user`, `AdminAuth`)
-   [ ] **Código:** Modelos Pydantic para Admin API creados (`admin_models.py` o similar)
-   [ ] **Código:** `admin_router.py` creado con endpoints (`/stats`, `/companies`, `/companies/select`, `/users`)
-   [ ] **Código:** Endpoints de Admin protegidos con `AdminAuth`
-   [ ] **Código:** Lógica de validación y creación de usuarios/compañías en `admin_router.py` implementada
-   [ ] **Código:** Logging del ID de admin en acciones implementado
-   [ ] **Código:** `main.py` actualizado para incluir `admin_router`
-   [ ] **Código:** `user_router.py` (`/login`) actualizado para pasar `roles` a `create_access_token`
-   [ ] **Config:** Usuario Admin (`atenex@gmail.com`) verificado/creado en DB con rol `admin`
-   [ ] **Test:** Pruebas Unitarias escritas y pasadas
-   [ ] **Test:** Pruebas de Integración escritas y pasadas
-   [ ] **Despliegue:** Migración Aplicada (Producción)
-   [ ] **Despliegue:** Imagen Docker construida y pusheada
-   [ ] **Despliegue:** Despliegue K8s actualizado
-   [ ] **Monitorización:** Logs y rendimiento verificados post-despliegue

---