import logging

from .processing import process_document

logger = logging.getLogger(__name__)


def process_document_task(document_id):
    """Django-Q2 worker entry point, queued by `apps.ingest.service.ingest_file`
    right after a `Document` is created (`processing_status="pending"`),
    and reused by `manage.py reindex_documents --queue` for re-embedding.

    `process_document()` already records failures on the `Document`
    itself (`processing_status="failed"` + `processing_error`) and
    re-raises, so Django-Q also records the task as failed instead of
    silently dropping it.
    """
    process_document(document_id)


def example_ping_task(message="pong"):
    """Minimal Django-Q2 demo task, queued from the HTMX example page.

    Confirms the redis broker + worker wiring end to end: enqueue here,
    watch the `worker` container logs for the "Findus worker" line below.
    """
    logger.info("Findus worker processed background task: %s", message)
    return message
