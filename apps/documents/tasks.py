from .analysis import analyze_document
from .extraction import extract_document
from .letter_generation import generate_letter_draft
from .processing import process_document
from .recommendations import generate_vorgang_recommendations
from .thumbnails import generate_thumbnail_for_document


def extract_document_task(document_id):
    """Django-Q2 worker entry point for the extraction cascade (#1009),
    queued by `apps.ingest.service.ingest_file` right after a `Document`
    is created (`processing_status="pending"`). On success it renders the
    first-page thumbnail (#1123) and then enqueues `analyze_document_task`
    (#1020, KI-Analyse) as the pipeline's next stage.

    `extract_document()` already records failures on the `Document`
    itself (`processing_status="failed"` + `processing_error`) and
    re-raises, so Django-Q records this task as failed too and
    `analyze_document_task` is never enqueued for a failed extraction.

    The thumbnail runs *after* a successful extraction (the original file is
    already downloaded and its MIME type normalised) but is deliberately
    fault-tolerant -- `generate_thumbnail_for_document` never raises, so a
    render failure leaves the document without a thumbnail (UI shows a
    placeholder) instead of breaking the pipeline, exactly like the
    KI-Analyse.
    """
    extract_document(document_id)
    generate_thumbnail_for_document(document_id)

    from django_q.tasks import async_task

    async_task(analyze_document_task, document_id)


def generate_thumbnail_task(document_id, force=False):
    """Django-Q2 worker entry point for the thumbnail backfill (#1123),
    queued by `manage.py generate_thumbnails --queue` for a large existing
    stock, so the command doesn't block for the whole run.

    Reuses the same fault-tolerant helper as the ingest pipeline -- a failed
    thumbnail is enrichment lost, not a broken document, so this never marks
    the task failed either.
    """
    generate_thumbnail_for_document(document_id, force=force)


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


def generate_letter_draft_task(draft_id, user_id):
    """Django-Q2 worker entry point for the KI-Brief (#1095), queued when a
    Brief-Entwurf is started or re-generated -- never by the ingest
    pipeline, this only ever runs on demand.

    `user_id` travels along for the same reason as in
    `generate_vorgang_recommendations_task`: the Kontext des beantworteten
    Dokuments is `visible_to`-scoped and the worker has no request.

    `generate_letter_draft()` records its own failures on the draft
    (`status="failed"` + `error`, shown in the review panel) and never
    raises, so there is nothing left for this wrapper to handle.
    """
    generate_letter_draft(draft_id, user_id)


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
