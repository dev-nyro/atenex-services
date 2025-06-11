from app.application.ports.task_queue_port import TaskQueuePort
from app.infrastructure.tasks.celery_app_config import celery_app

class CeleryTaskAdapter(TaskQueuePort):
    async def enqueue_process_document_task(self, document_id: str, company_id: str, filename: str, content_type: str) -> str:
        # Celery tasks are always called in a background thread/process, so we use apply_async
        result = celery_app.send_task(
            'app.infrastructure.tasks.celery_worker.process_document_task',
            args=[document_id, company_id, filename, content_type]
        )
        return result.id
