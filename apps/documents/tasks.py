from .analysis import analyze_document
from .extraction import extract_document
from .processing import process_document
from .recommendations import generate_vorgang_recommendations


def extract_document_task(document_id):
    """Django-Q2 worker entry point for the extraction cascade (#1009),
    queued by `apps.ingest.service.ingest_file` right after a `Document`
    is created (`processing_status="pending"`). On success it enqueues
    `analyze_document_task` (#1020, KI-Analyse) as the pipeline's next
    stage.

    `extract_document()` already records failures on the `Document`
    itself (`processing_status="failed"` + `processing_error`) and
    re-raises, so Django-Q records this task as failed too and
    `analyze_document_task` is never enqueued for a failed extraction.
    """
    extract_document(document_id)

    from django_q.tasks import async_task

    async_task(analyze_document_task, document_id)


def analyze_document_task(document_id):
    """Django-Q2 worker entry point for the KI-Analyse (#1020), queued by

    `extract_document_task` once extraction has populated `text_content`.
    Unlike its neighbours, `analyze_document()` never raises -- a failed
    analysis is enrichment lost, not a broken document -- so this always
    enqueues `process_document_task` (#1010, chunking/embedding) next,
    regardless of whether the analysis itself succeeded.
    """
    analyze_document(document_id)

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


def generate_vorgang_recommendations_task(vorgang_id, user_id):
    """Django-Q2 worker entry point for the Handlungsempfehlungen (#1093),
    queued by the Vorgang-Hub's "Empfehlungen generieren"-Button --
    never by the ingest pipeline, this only ever runs on demand.

    `user_id` travels along because the Datenbasis is `visible_to`-scoped:
    the worker has no request, so the triggering user is the only thing
    that says which documents may go into the prompt.

    `generate_vorgang_recommendations()` records its own failures on the
    run (`status="failed"` + `error`, shown in the panel) and never
    raises, so there is nothing left for this wrapper to handle.
    """
    generate_vorgang_recommendations(vorgang_id, user_id)
