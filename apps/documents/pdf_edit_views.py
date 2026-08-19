"""Seitenansicht und Bearbeitungs-Endpunkte der Scan-Korrektur (#1155).

Ein misslungener Scan (leere Rückseiten, verdrehte Seiten, zwei Vorgänge
in einem Stapel) ließ sich bisher nur außerhalb von Findus reparieren --
mit dem Preis, dass Kommentare, Wiedervorlagen, Verknüpfungen und die
gepflegte Zuordnung beim Neu-Hochladen verloren gehen. Diese Ansicht ist
der Ort, an dem alle drei Operationen zusammen stattfinden.

Der Ablauf ist bewusst zweistufig: die Seitenansicht **ändert nichts**,
erst der Bestätigungsschritt (der in Worten zusammenfasst, was passieren
wird, samt dem, was am Original verloren geht) löst den Hintergrundjob
aus. Es gibt kein Rückgängig -- der Schutz liegt davor, nicht dahinter
(siehe CLAUDE.md, "PDF-Grundbearbeitung").

Sichtbarkeit wie überall über `views._visible_document`: ein fremdes
Dokument ist 404, nicht 403. Die Seitenbilder laufen über den
auth-gestützten Stream mit `nosniff` (`document_page_image`), nie über
eine Storage-URL -- die ist ein öffentlicher S3-/MinIO-Link und umgeht
die ACL vollständig.
"""

from __future__ import annotations

import logging
import mimetypes

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import PdfPageEditForm
from .models import Document, DocumentPagePreview, DocumentPdfEditRun
from .page_previews import page_previews_for
from .pdf_edit import plan_part_count
from .pdf_editing import PdfEditError, inspect_pdf, summarize_plan
from .views import _visible_document

logger = logging.getLogger(__name__)


def _page_editable_document(user, pk):
    """Wie `_visible_document`, zusätzlich auf PDF eingeschränkt.

    Ein anderer Dateityp ist hier ein 404 und keine Fehlermeldung: die
    Seitenansicht existiert für dieses Dokument schlicht nicht, der
    Einstieg wird gar nicht erst angeboten. (Anders als beim POST auf
    einen Bearbeitungs-Endpunkt eines fremden Dokuments -- der ist aus dem
    umgekehrten Grund 404, siehe Modulkopf.)
    """
    document = _visible_document(user, pk)
    if not document.supports_page_editing:
        raise Http404("Seitenansicht gibt es nur für PDF-Dokumente.")
    return document


def _inspect(document):
    """Seitenzahl + Signatur-Merkmal der Originaldatei, oder ein
    `PdfEditError` mit lesbarer Meldung (passwortgeschützt/beschädigt)."""
    document.original_file.open("rb")
    try:
        return inspect_pdf(document.original_file)
    finally:
        document.original_file.close()


def _previews_context(document, page_count):
    previews = page_previews_for(document, page_count=page_count)
    ready = sum(1 for preview in previews if preview.has_image)
    return {
        "document": document,
        "page_count": page_count,
        "previews": previews,
        "previews_ready_count": ready,
        "previews_complete": ready >= page_count,
        # Die Drehstufen kommen aus dem Formular, nicht aus dem Template:
        # dieselbe Liste validiert die Eingabe, also darf sie nicht zweimal
        # geschrieben stehen.
        "rotation_choices": PdfPageEditForm.ROTATION_CHOICES,
    }


def _enqueue_previews(document, page_count, ready_count):
    """Fehlende Seitenbilder erzeugen: klein synchron, groß im Hintergrund.

    Ein zwei- oder dreiseitiger Beleg soll die Seitenansicht nicht durch
    einen Poll-Zyklus schicken, nur um zwei Bilder zu zeigen; ein
    200-Seiten-Scan darf umgekehrt den Request nicht blockieren (dann
    zeigt die Ansicht einen Ladezustand und lädt nach).

    Bewusst nur hier, beim Aufruf der ganzen Seite -- nicht im Poller:
    sonst reiht jeder Poll-Durchlauf einen weiteren Job ein, solange noch
    Bilder fehlen. Bleibt ein Lauf stecken, genügt ein Neuladen der Seite.
    Ein doppelt angestoßener Lauf ist harmlos: `generate_page_previews`
    überspringt vorhandene Seiten und fängt das Rennen um dieselbe Seite
    über die Unique-Constraint ab.
    """
    if ready_count >= page_count:
        return
    from .page_previews import generate_page_previews

    if page_count <= settings.FINDUS_PAGE_PREVIEW_SYNC_MAX_PAGES:
        generate_page_previews(document.id)
        return

    from django_q.tasks import async_task

    from .tasks import generate_page_previews_task

    async_task(
        generate_page_previews_task,
        document.id,
        timeout=settings.FINDUS_PAGE_PREVIEW_TASK_TIMEOUT_SECONDS,
    )


@login_required
def document_pages(request, pk):
    """Die Seitenansicht: je Seite Vorschaubild, "entfernen", "drehen", und
    dazwischen die Schnittmarken.

    Erst möglich, wenn die Verarbeitung abgeschlossen ist -- mitten in der
    Extraktion die Datei unter der Pipeline auszutauschen, wäre ein Rennen
    mit unklarem Ausgang. Eine passwortgeschützte oder beschädigte Datei
    endet hier in einer verständlichen Meldung statt in einer 500er-Seite.
    """
    document = _page_editable_document(request.user, pk)

    if not document.is_processing_complete:
        return render(
            request,
            "documents/pdf_edit.html",
            {"document": document, "blocked_reason": "processing"},
        )

    try:
        info = _inspect(document)
    except PdfEditError as exc:
        return render(
            request,
            "documents/pdf_edit.html",
            {"document": document, "file_error": str(exc)},
        )

    # Erst erzeugen (klein synchron, groß im Hintergrund), dann lesen --
    # sonst zeigt die frisch geöffnete Ansicht grundlos Platzhalter für
    # Bilder, die es beim Rendern längst gibt.
    _enqueue_previews(document, info.page_count, document.page_previews.count())
    context = _previews_context(document, info.page_count)

    running_run = (
        DocumentPdfEditRun.objects.filter(
            document=document, status=DocumentPdfEditRun.Status.RUNNING
        )
        .order_by("-created_at")
        .first()
    )

    return render(
        request,
        "documents/pdf_edit.html",
        {
            **context,
            "form": PdfPageEditForm(page_count=info.page_count),
            "has_signature": info.has_signature,
            "running_run": running_run,
        },
    )


@login_required
def document_page_previews(request, pk):
    """Poll-Endpunkt der Seitenvorschauen (`outerHTML`-Selbsttausch).

    Reines Lesen: er stößt nichts an (siehe `_enqueue_previews`) und
    liefert einfach den aktuellen Stand -- fertige Seiten sofort sichtbar,
    fehlende als Platzhalter. Sind alle Bilder da, trägt das Partial kein
    `hx-trigger` mehr und der Poller endet von selbst.
    """
    document = _page_editable_document(request.user, pk)
    try:
        info = _inspect(document)
    except PdfEditError as exc:
        raise Http404(str(exc)) from exc
    return render(
        request,
        "documents/partials/_pdf_edit_pages.html",
        _previews_context(document, info.page_count),
    )


@login_required
def document_page_image(request, pk, page):
    """Ein Seitenvorschaubild über den auth-gestützten Stream (#1155).

    Wie `views.document_thumbnail` (#1123/#1024): die Storage-URL wäre ein
    öffentlicher S3-/MinIO-Link und umginge die Sichtbarkeitsprüfung
    vollständig, deshalb kommt das Bild nur hier heraus. Ein noch nicht
    gerastertes Bild ist ein 404, kein Fehler -- die Ansicht zeigt dort
    einen Platzhalter und lädt nach.
    """
    document = _page_editable_document(request.user, pk)
    preview = get_object_or_404(DocumentPagePreview, document=document, page_number=page)
    content_type = mimetypes.guess_type(preview.image.name)[0] or "image/webp"
    response = FileResponse(preview.image.open("rb"), content_type=content_type)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = f"private, max-age={settings.FINDUS_THUMBNAIL_CACHE_MAX_AGE}"
    return response


def _confirm_context(document, plan, *, page_count, has_signature):
    """Was der Bestätigungsschritt anzeigt -- in Worten, nicht als
    Datensatz.

    Beim Aufteilen benennt er ausdrücklich, was am Original verloren geht
    (Kommentare und Verknüpfungen; bei 0 entfällt der Hinweis): ohne diese
    Warnung löscht man ahnungslos eine Wiedervorlage mit.
    """
    part_count = plan_part_count(plan, page_count=page_count)
    splits_document = part_count > 1
    comment_count = document.comments.count() if splits_document else 0
    link_count = (
        document.links_as_a.count() + document.links_as_b.count() if splits_document else 0
    )
    # Unterdokumente hängen per `on_delete=CASCADE` am Original und gingen
    # beim Aufteilen wortlos mit -- sie gehören deshalb in dieselbe Warnung
    # wie Kommentare und Verknüpfungen, auch wenn sie beim typischen
    # Fehlscan (einem Stapel aus dem Einzug) gar nicht vorkommen.
    child_count = document.children.count() if splits_document else 0
    return {
        "document": document,
        "plan_summary": summarize_plan(plan, page_count=page_count),
        "plan_fields": plan.as_form_data(),
        "part_count": part_count,
        "splits_document": splits_document,
        "comment_count": comment_count,
        "link_count": link_count,
        "child_count": child_count,
        "has_signature": has_signature,
    }


@login_required
@require_POST
def document_pdf_edit_confirm(request, pk):
    """Schritt 2: die Auswahl in Worte fassen und bestätigen lassen.

    Ändert nichts. Der Schritt rendert dieselben Eingaben noch einmal als
    versteckte Felder, sodass der Ausführen-Endpunkt **dasselbe** Formular
    ein zweites Mal validiert -- die Bestätigung ist eine Zwischenanzeige,
    kein Nebenkanal mit eigener Datenhaltung.
    """
    document = _page_editable_document(request.user, pk)
    try:
        info = _inspect(document)
    except PdfEditError as exc:
        return render(
            request,
            "documents/partials/_pdf_edit_confirm.html",
            {"document": document, "file_error": str(exc)},
        )

    form = PdfPageEditForm(request.POST, page_count=info.page_count)
    if not form.is_valid():
        return render(
            request,
            "documents/partials/_pdf_edit_confirm.html",
            {"document": document, "form_errors": form.errors},
        )

    return render(
        request,
        "documents/partials/_pdf_edit_confirm.html",
        _confirm_context(
            document,
            form.plan,
            page_count=info.page_count,
            has_signature=info.has_signature,
        ),
    )


@login_required
@require_POST
def document_pdf_edit_apply(request, pk):
    """Schritt 3: den bestätigten Plan als Hintergrundjob ausführen.

    Eigener, großzügiger `timeout` statt des knappen Cluster-Defaults --
    ein 200-Seiten-Scan wird geschrieben, neu abgelegt und die Teile
    laufen durch den Ingest. Die vorgeschriebene Schachtelung (Task-Timeout
    < `Q_CLUSTER["retry"]`) sichert ein Vertragstest ab; ohne sie startet
    Django-Q denselben Auftrag ein zweites Mal, während der erste noch
    läuft.

    Läuft für dieses Dokument bereits ein Lauf, entsteht kein zweiter --
    sonst gäbe es beim Aufteilen doppelte Teile. Der Job selbst schützt
    sich zusätzlich (`DocumentPdfEditRun.claimed_at`).
    """
    document = _page_editable_document(request.user, pk)
    if not document.is_processing_complete:
        return render(
            request,
            "documents/partials/_pdf_edit_run.html",
            {"document": document, "blocked_reason": "processing"},
        )

    try:
        info = _inspect(document)
    except PdfEditError as exc:
        return render(
            request,
            "documents/partials/_pdf_edit_run.html",
            {"document": document, "file_error": str(exc)},
        )

    form = PdfPageEditForm(request.POST, page_count=info.page_count)
    if not form.is_valid():
        return render(
            request,
            "documents/partials/_pdf_edit_run.html",
            {"document": document, "form_errors": form.errors},
        )

    existing = (
        DocumentPdfEditRun.objects.filter(
            document=document, status=DocumentPdfEditRun.Status.RUNNING
        )
        .order_by("-created_at")
        .first()
    )
    if existing is not None:
        return render(
            request,
            "documents/partials/_pdf_edit_run.html",
            _run_context(existing, user=request.user),
        )

    run = DocumentPdfEditRun.objects.create(
        document=document,
        document_title=document.title,
        created_by=request.user,
        plan=form.plan.as_dict(),
        status=DocumentPdfEditRun.Status.RUNNING,
    )

    from django_q.tasks import async_task

    from .tasks import apply_pdf_edit_hook, apply_pdf_edit_task

    async_task(
        apply_pdf_edit_task,
        run.id,
        timeout=settings.FINDUS_PDF_EDIT_TASK_TIMEOUT_SECONDS,
        hook=apply_pdf_edit_hook,
    )
    return render(
        request, "documents/partials/_pdf_edit_run.html", _run_context(run, user=request.user)
    )


def expire_pdf_edit_run_if_stalled(run):
    """Netz gegen einen Job, der spurlos verschwindet (Worker-Neustart,
    OOM-Kill), bevor der eigene except-Block *oder* der Django-Q-`hook`
    greift -- dasselbe Prinzip wie
    `extraction.expire_vision_reextraction_if_stalled`. Ohne das bliebe die
    Oberfläche auf einem endlosen Ladeindikator stehen.
    """
    if run.status != DocumentPdfEditRun.Status.RUNNING:
        return
    age = (timezone.now() - run.created_at).total_seconds()
    if age < settings.FINDUS_PDF_EDIT_POLL_TIMEOUT_SECONDS:
        return
    run.status = DocumentPdfEditRun.Status.FAILED
    run.error = (
        "Der Hintergrundjob hat sich nicht zurückgemeldet (Zeitüberschreitung "
        "beim Warten auf ein Ergebnis)."
    )
    run.completed_at = timezone.now()
    run.save(update_fields=["status", "error", "completed_at", "updated_at"])


def _run_context(run, *, user):
    """Anzeigekontext eines Laufs -- inklusive der angelegten Teile.

    Die Teile werden über `Document.objects.visible_to(user)` aufgelöst,
    nicht direkt über die IDs im Ergebnis: Sichtbarkeit gilt auch für ein
    Ergebnis, das man selbst ausgelöst hat, und ein inzwischen gelöschtes
    Teil fällt dabei still heraus statt als toter Link stehen zu bleiben.
    """
    entries = (run.result or {}).get("parts") or []
    documents = Document.objects.visible_to(user).in_bulk(
        [entry["document_id"] for entry in entries if entry.get("document_id")]
    )
    parts = [{**entry, "document": documents.get(entry.get("document_id"))} for entry in entries]
    return {"run": run, "parts": parts, "document": run.document}


@login_required
def document_pdf_edit_status(request, run_id):
    """Poll-Endpunkt des Laufs (`outerHTML`-Selbsttausch).

    Auf den Auslöser eingegrenzt (`created_by`), nicht auf das Dokument:
    beim Aufteilen ist das Dokument am Ende gelöscht: eine Sichtbarkeits-
    prüfung daran ginge genau dann ins Leere, wenn das Ergebnis angezeigt
    werden soll. Die angelegten Teile werden trotzdem einzeln gegen
    `visible_to` aufgelöst (siehe `_run_context`).
    """
    run = get_object_or_404(DocumentPdfEditRun, pk=run_id, created_by=request.user)
    expire_pdf_edit_run_if_stalled(run)
    return render(
        request, "documents/partials/_pdf_edit_run.html", _run_context(run, user=request.user)
    )
