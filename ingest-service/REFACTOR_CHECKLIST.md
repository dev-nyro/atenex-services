# Refactorización del Ingest Service

Este documento describe los pasos necesarios para corregir y completar el microservicio de ingesta.

## 1. Endpoints y Router
- [x] Verificar que todas las rutas del router estén implementadas y que responden con los modelos Pydantic correctos.
- [x] Asegurar que `/status/{document_id}` devuelva el Pydantic `StatusResponse`, no el diccionario bruto.
- [x] Asegurar que `/status` devuelva lista de `StatusResponse`, no lista de dicts.
- [x] Comprobar el endpoint `/upload` encola la tarea Celery y devuelve `task_id`.
- [x] Comprobar `/retry/{document_id}` y `/delete/{document_id}` actualizan estado y encolan tareas.

## 2. Cliente de MinIO
- [x] Implementar `upload_file` para usar `run_in_executor` y `self.client.put_object(...)`.
- [x] Completar método `delete_file` para usar `_remove_object` real.
- [x] Asegurar `file_exists` llama a `stat_object` y devuelve boolean.

## 3. Cliente HTTP Base (services/base_client.py)
- [x] Completar la construcción de la petición (`self.client.request(...)`).
- [x] Manejar reintentos con Tenacity.

## 4. Database Client (postgres_client.py)
- [x] Verificar `get_document_status` retorna todos campos, incluido `error_message`.
- [x] Ajustar `list_documents_by_company` para incluir `error_message` y respetar esquema.
- [x] Implementar `create_document`, `update_document_status`, `delete_document` correctamente.

## 5. Tarea Celery (tasks/process_document.py)
- [x] Completar flujo asíncrono: descarga de MinIO, conversión, embedding, escritura en Milvus.
- [x] Marcar estado en la BD antes/después de cada etapa.
- [x] Retornar resultado correcto y manejar excepciones no reintentables.
- [x] Establecer `REAL_CHUNK_COUNT` y actualizarlo en BD tras procesar.

## 6. Configuración y Logging
- [x] Revisar `API_V1_STR` en config.py: debe ser `/api/v1/ingest`.
- [x] Confirmar que `logging_config.py` carga correctamente logging en FastAPI y Celery.

## 7. Tests (opcional)
- [ ] Añadir pruebas básicas de integración para los endpoints de ingest.
