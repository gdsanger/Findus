import logging

logger = logging.getLogger(__name__)


def process_document_task(document_id):
    """Django-Q2 worker entry point, queued by `apps.ingest.service.ingest_file`
    right after a `Document` is created (`processing_status="pending"`).

    OCR/Markdown/Chunking/Embedding are separate issues and plug in here
    later; for now this only confirms the document reached the worker.
    """
    logger.info(
        "Findus worker received document %s for processing (pipeline not yet implemented)",
        document_id,
    )


def example_ping_task(message="pong"):
    """Minimal Django-Q2 demo task, queued from the HTMX example page.

    Confirms the redis broker + worker wiring end to end: enqueue here,
    watch the `worker` container logs for the "Findus worker" line below.
    """
    logger.info("Findus worker processed background task: %s", message)
    return message
