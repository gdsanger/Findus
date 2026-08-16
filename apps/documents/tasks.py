from django.utils import timezone

from .analysis import analyze_document
from .extraction import extract_document, reextract_document_with_vision
from .letter_generation import generate_letter_draft
from .long_summary import generate_document_long_summary, generate_vorgang_long_summary
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
    run (`status="failed"` + `error`, shown in the panel) for an ordinary
    provider/parsing failure and does not raise for those -- but it *does*
    re-raise a Django-Q `TimeoutException` after recording it (#1134), so
    this wrapper has exactly one thing left to handle: nothing. The
    exception keeps going up to Django-Q's own worker loop, which is what
    actually needs to see it (recycles the process, feeds the `hook`
    below).

    Queued with a generous per-task `timeout` and a `hook` safety net --
    see `vorgang_recommendations_generate` in `vorgang_views.py` for both,
    and `generate_vorgang_recommendations_hook` below for what the hook
    covers that the in-task handling can't.
    """
    generate_vorgang_recommendations(vorgang_id, user_id)


def generate_vorgang_recommendations_hook(task):
    """Django-Q `hook` callback for `generate_vorgang_recommendations_task`
    (#1134): runs after *every* finished task, successful or not (see
    `django_q.signals.call_hook`, fired from `Task`'s `post_save`).

    This is a safety net, not the primary mechanism -- the primary one is
    `generate_vorgang_recommendations()` catching its own `TimeoutException`
    and marking the run `failed` before re-raising. That covers the
    observed bug (#1134: a worker killed by Django-Q's own timeout while
    still waiting on the provider). This hook exists for whatever that
    doesn't cover -- an exception type nobody anticipated, a process that
    dies before its `except` block runs -- so the run never gets stuck on
    `running` for a reason that never made it back to `recommendations.py`
    at all.

    Guarded with `status=RUNNING` in the `update()` filter so it never
    overwrites a run that already has its real result (or a more specific
    failure reason) -- a task can be reported unsuccessful here for
    reasons that have nothing to do with the run itself.
    """
    if task.success:
        return

    from .models import VorgangRecommendationRun

    vorgang_id = task.args[0] if task.args else None
    if vorgang_id is None:
        return

    VorgangRecommendationRun.objects.filter(
        vorgang_id=vorgang_id, status=VorgangRecommendationRun.Status.RUNNING
    ).update(
        status=VorgangRecommendationRun.Status.FAILED,
        error="Der Hintergrundjob wurde abgebrochen, ohne ein Ergebnis zurückzugeben.",
        updated_at=timezone.now(),
    )


def generate_document_long_summary_task(document_id):
    """Django-Q2 worker entry point fuer die ausfuehrliche Zusammenfassung
    eines Dokuments (#1135) -- nur auf Knopfdruck vom Detail aus, nie von
    der Ingest-Pipeline.

    `generate_document_long_summary()` zeichnet einen gewoehnlichen
    Provider-/Parsing-Fehler selbst als `status="failed"` + `error` am
    Dokument auf und wirft dafuer nicht -- reicht aber eine Django-Q-
    `TimeoutException` weiter (#1134), damit Django-Q den Worker-Prozess
    neu startet. Genau dieselbe Aufgabenteilung wie
    `generate_vorgang_recommendations_task`.
    """
    generate_document_long_summary(document_id)


def generate_document_long_summary_hook(task):
    """Sicherheitsnetz fuer `generate_document_long_summary_task` (#1134):
    faengt einen Worker ab, der abstuerzt, bevor der eigene except-Block
    im Modul den Fehler noch aufzeichnen konnte. `status=RUNNING` im
    Filter, damit ein bereits eingetroffenes Ergebnis (oder eine
    spezifischere Fehlermeldung) nicht ueberschrieben wird.
    """
    if task.success:
        return

    from .models import Document

    document_id = task.args[0] if task.args else None
    if document_id is None:
        return

    Document.objects.filter(
        pk=document_id, long_summary_status=Document.LongSummaryStatus.RUNNING
    ).update(
        long_summary_status=Document.LongSummaryStatus.FAILED,
        long_summary_error="Der Hintergrundjob wurde abgebrochen, ohne ein Ergebnis zurückzugeben.",
    )


def generate_vorgang_long_summary_task(vorgang_id, user_id):
    """Wie `generate_document_long_summary_task`, nur fuer den Vorgang --
    `user_id` wandert mit, weil die Datenbasis `visible_to`-gescoped ist
    und der Worker keinen Request hat.
    """
    generate_vorgang_long_summary(vorgang_id, user_id)


def generate_vorgang_long_summary_hook(task):
    """Wie `generate_document_long_summary_hook`, nur fuer den Vorgang."""
    if task.success:
        return

    from .models import Vorgang

    vorgang_id = task.args[0] if task.args else None
    if vorgang_id is None:
        return

    Vorgang.objects.filter(
        pk=vorgang_id, long_summary_status=Vorgang.LongSummaryStatus.RUNNING
    ).update(
        long_summary_status=Vorgang.LongSummaryStatus.FAILED,
        long_summary_error="Der Hintergrundjob wurde abgebrochen, ohne ein Ergebnis zurückzugeben.",
    )


def reextract_document_with_vision_task(document_id):
    """Django-Q2 worker entry point fuer die manuelle KI-Vision-
    Neuextraktion (#1143) -- nur auf Knopfdruck vom Detail aus, nie von der
    Ingest-Pipeline.

    `reextract_document_with_vision()` zeichnet einen gewoehnlichen
    Provider-/Parsing-Fehler selbst als `vision_reextraction_status="failed"`
    + `vision_reextraction_error` auf und wirft dafuer nicht -- reicht aber
    eine Django-Q-`TimeoutException` weiter, damit Django-Q den Worker-
    Prozess neu startet. Dieselbe Aufgabenteilung wie
    `generate_document_long_summary_task`.

    Nur bei Erfolg wird `analyze_document_task` angestossen (das seinerseits
    `process_document_task` anschliesst, siehe `extract_document_task`) --
    sonst durchsucht Findus weiterhin den alten Text, und Zusammenfassung/
    Key-Facts blieben die alten, obwohl sich `text_content` gar nicht
    geaendert hat.
    """
    document = reextract_document_with_vision(document_id)
    if document.vision_reextraction_status != document.VisionReextractionStatus.READY:
        return

    from django_q.tasks import async_task

    async_task(analyze_document_task, document_id)


def reextract_document_with_vision_hook(task):
    """Sicherheitsnetz fuer `reextract_document_with_vision_task`, analog
    `generate_document_long_summary_hook`: faengt einen Worker ab, der
    abstuerzt, bevor der eigene except-Block im Modul den Fehler noch
    aufzeichnen konnte.
    """
    if task.success:
        return

    from .models import Document

    document_id = task.args[0] if task.args else None
    if document_id is None:
        return

    Document.objects.filter(
        pk=document_id, vision_reextraction_status=Document.VisionReextractionStatus.RUNNING
    ).update(
        vision_reextraction_status=Document.VisionReextractionStatus.FAILED,
        vision_reextraction_error="Der Hintergrundjob wurde abgebrochen, ohne ein Ergebnis zurückzugeben.",
    )
