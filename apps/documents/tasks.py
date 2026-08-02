from .extraction import extract_document
from .processing import process_document


def extract_document_task(document_id):
    """Django-Q2 worker entry point for the extraction cascade (#1009),
    queued by `apps.ingest.service.ingest_file` right after a `Document`
    is created (`processing_status="pending"`). On success it enqueues
    `process_document_task` (#1010, chunking/embedding) as the pipeline's
    next stage.

    `extract_document()` already records failures on the `Document`
    itself (`processing_status="failed"` + `processing_error`) and
    re-raises, so Django-Q records this task as failed too and
    `process_document_task` is never enqueued for a failed extraction.
    """
    extract_document(document_id)

    from django_q.tasks import async_task

    async_task(process_document_task, document_id)


def process_document_task(document_id):
    """Django-Q2 worker entry point for chunking/embedding (#1010), queued
    by `extract_document_task` once extraction has populated
    `text_content`, and reused by `manage.py reindex_documents --queue`
    for re-embedding an already-extracted document.

    `process_document()` already records failures on the `Document`
    itself (`processing_status="failed"` + `processing_error`) and
    re-raises, so Django-Q also records the task as failed instead of
    silently dropping it.
    """
    process_document(document_id)
