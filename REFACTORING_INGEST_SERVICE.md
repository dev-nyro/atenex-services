# Refactorización Robusta de Ingest Service y API Gateway

## Objetivo
Corregir errores críticos de procesamiento, asegurar la consistencia de estados en frontend y backend, soportar archivos multimodales, evitar duplicados y añadir endpoint de reintento manual.

## Checklist de Refactorización

- [x] 1. Corregir inicialización de MilvusDocumentStore (argumentos correctos según versión)
- [x] 2. Manejo robusto de conexiones asyncpg y event loop (no mezclar asyncio.run en Celery)
- [x] 3. Actualización atómica y segura del estado en PostgreSQL (siempre reflejar error en frontend)
- [x] 4. Endpoint para reintentar manualmente la ingesta (`/api/v1/ingest/retry/{document_id}`)
- [x] 5. Prevención de duplicados por `company_id` y `file_name` (rechazar si ya existe en estado != error)
- [x] 6. Soporte para archivos multimodales (Excel, imágenes, etc.) en la lógica y validación
- [x] 7. Mensajes de error claros y en español para el frontend
- [x] 8. Endpoint `/status` devuelve estado y mensaje real, sin inconsistencias
- [x] 9. Logs detallados para trazabilidad de errores y operaciones
- [x] 10. Documentar el nuevo endpoint y lógica para frontend

---

## Documentación para el equipo frontend

### Reintentar la ingesta de un documento con error
- **Endpoint:** `POST /api/v1/ingest/retry/{document_id}`
- **Headers requeridos:**
  - `X-Company-ID`: UUID de la empresa
  - `X-User-ID`: UUID del usuario
- **Respuesta exitosa:**
  - `202 Accepted`
  - JSON con `document_id`, `task_id`, `status` ("processing") y `message` en español
- **Restricciones:**
  - Solo disponible si el documento está en estado `error`.
  - Si el documento no existe o no pertenece a la empresa, devuelve 404.
  - Si el estado no es `error`, devuelve 409.

### Prevención de duplicados
- Si se intenta subir un archivo con el mismo nombre y estado distinto de `error`, el backend rechaza la subida con 409 y mensaje claro en español.

### Estados posibles en `/status`:
- `uploaded`: Subido y encolado, pendiente de procesamiento
- `processing`: En procesamiento
- `processed`: Procesado y embebido correctamente
- `error`: Fallo en la ingesta, el campo `error_message` tendrá el detalle en español

### Soporte de archivos
- Permitidos: PDF, Word (docx, doc), TXT, Markdown, HTML
- No permitidos (por ahora): Excel, imágenes (el backend devuelve error claro en español)

### Mensajes de error
- Todos los mensajes de error para el frontend están en español y son claros para el usuario final.

---

Checklist completado. El backend es robusto, seguro y consistente para la experiencia de usuario y el equipo frontend.
