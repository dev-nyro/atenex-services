# Esquema de Base de Datos - SaaS B2B Multi-tenant (Supabase + Milvus)

## Tablas Principales

### COMPANIES
- `id` (UUID, PK): ID único de la empresa.
- `name` (string): Nombre.
- `email` (string): Correo de contacto.
- `created_at`, `updated_at` (timestamp): Fechas de creación y actualización.
- `is_active` (boolean): Estado de activación.

### USERS
- `id` (UUID, PK): ID único del usuario.
- `company_id` (UUID, FK → COMPANIES.id): Empresa a la que pertenece.
- `email` (string): Email del usuario.
- `hashed_password` (string): Contraseña (opcional si se usa Supabase Auth).
- `full_name` (string): Nombre completo.
- `role` (string): Rol (admin, user, etc.).
- `created_at`, `last_login` (timestamp): Fechas de registro y último acceso.
- `is_active` (boolean): Estado del usuario.

### SUBSCRIPTIONS
- `id` (UUID, PK): ID de suscripción.
- `company_id` (UUID, FK → COMPANIES.id): Empresa suscripta.
- `plan_type` (string): Tipo de plan.
- `start_date`, `end_date` (timestamp): Vigencia.
- `max_documents`, `max_queries` (int): Límites del plan.

### DOCUMENTS
- `id` (UUID, PK): ID del documento.
- `company_id` (UUID, FK → COMPANIES.id): Empresa propietaria.
- `file_name`, `file_type`, `file_path` (string): Info del archivo.
- `metadata` (jsonb): Datos adicionales (idioma, tamaño, etc.).
- `chunk_count` (int): Total de chunks generados.
- `uploaded_at`, `updated_at` (timestamp): Tiempos de carga y edición.
- `status` (string): Estado del documento.

### DOCUMENT_CHUNKS
- `id` (UUID, PK): ID del chunk.
- `document_id` (UUID, FK → DOCUMENTS.id): Documento de origen.
- `chunk_index` (int): Índice de chunk.
- `content` (text): Texto del chunk.
- `metadata` (jsonb): Info adicional (página, idioma, etc.).
- `embedding_id` (string): ID en Milvus (opcional).
- `embedding_vector` (float[]): Vector en pgvector (opcional).
- `created_at` (timestamp): Fecha de creación.

### QUERY_LOGS
- `id` (UUID, PK): ID del log.
- `user_id` (UUID, FK → USERS.id): Usuario que consultó.
- `company_id` (UUID, FK → COMPANIES.id): Empresa asociada.
- `query` (text): Consulta hecha.
- `response` (text): Respuesta generada.
- `relevance_score` (float): Score de relevancia (opcional).
- `created_at` (timestamp): Fecha/hora.
- `metadata` (jsonb): Info extra (documentos, tiempos, etc.).

## Relaciones Clave

- `COMPANIES` → 1:N → `USERS`, `DOCUMENTS`, `SUBSCRIPTIONS`, `QUERY_LOGS`
- `DOCUMENTS` → 1:N → `DOCUMENT_CHUNKS`
- `USERS` → 1:N → `QUERY_LOGS`

## Notas Especiales

- Multi-tenant: Todas las tablas principales tienen `company_id` como FK.
- Auth: Supabase Auth puede integrarse con `USERS` vía `auth_user_id`.
- Embeddings: Opcionalmente en Postgres (`embedding_vector`) o referenciado (`embedding_id`) en Milvus.
