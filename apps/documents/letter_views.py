"""UI für den KI-Brief aus einer Vorlage (#1095): wählen → generieren →
prüfen → freigeben.

Der Ablauf in drei Seiten/Endpunkten:

1. `letter_draft_start` -- Einstieg vom Dokument-Detail („Antwortschreiben
   erstellen", der Hauptweg) oder vom Vorgang-Hub. Vorlage wählen, die
   `manual`-Platzhalter ausfüllen, Hinweise dazuschreiben. Alles, was sich
   aus Findus-Daten binden lässt, wird hier nur *angezeigt* (Provenienz),
   nicht abgefragt.
2. `letter_draft_detail` + `letter_draft_panel` -- die Werkbank. Der
   Worker formuliert (async, ein `generate()`-Call), das Panel pollt sich
   selbst, danach steht der Text editierbar da und lässt sich beliebig oft
   neu rendern oder neu generieren.
3. `letter_draft_finalize` -- die Freigabe. Erst hier entsteht ein
   Dokument (`apps.documents.letter_filing`).

**Review-first ist hier eine Struktureigenschaft, keine Ermahnung:** es
gibt in diesem Modul genau einen Pfad, der ein `Document` anlegt, und der
hängt an einem POST mit ausdrücklicher Bestätigung. Kein Poll, kein
Rendern und kein Generieren legt etwas ab, und verschickt wird gar nichts
(das ist #5).

Alles `visible_to`-gescoped: Entwurf, Vorlage und Bezugsdokument werden
jeweils einzeln durch ihren eigenen Scope gezogen -- ein sichtbarer
Entwurf beweist nicht, dass sein Bezugsdokument sichtbar ist (dessen
Scope kann sich nachträglich geändert haben).
"""

from __future__ import annotations

import logging

from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import LetterDraftEditForm, LetterDraftStartForm
from .letter_bindings import MANUAL_SOURCE, build_context, resolve_placeholders
from .letter_filing import finalize_letter_draft
from .letter_render import render_draft_files
from .models import Correspondent, Document, LetterDraft, LetterTemplate, Vorgang
from .task_views import task_departments_and_visibility
from .views import _visible_document

logger = logging.getLogger(__name__)

# Was der Download je Format ausliefert -- Feld am Entwurf plus der
# Content-Type, den der Browser bekommen soll.
DOWNLOAD_FORMATS = {
    "docx": (
        "docx_file",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    "pdf": ("pdf_file", "application/pdf"),
}


def _visible_draft(user, pk):
    return get_object_or_404(
        LetterDraft.objects.visible_to(user).select_related(
            "template", "recipient", "sender", "vorgang", "document"
        ),
        pk=pk,
    )


def _visible_templates(user):
    return LetterTemplate.objects.visible_to(user).prefetch_related("placeholders")


def _address_block(correspondent) -> str:
    """Name + Adresse als Anschriftblock.

    Aus den Kontaktdaten, nicht aus den Platzhaltern: der Block muss auch
    dann stehen, wenn die Vorlage gar keine Adress-Bindung definiert hat.
    """
    if correspondent is None:
        return ""
    lines = [correspondent.name.strip()]
    lines.extend(
        line.strip() for line in (correspondent.address or "").splitlines() if line.strip()
    )
    return "\n".join(lines)


def _layout_snapshot(template) -> dict:
    """Das Layout der Vorlage, eingefroren für diesen Entwurf -- inklusive
    der Schlüssel, die die Vorlage (noch) nicht selbst gesetzt hat, damit
    der Snapshot vollständig ist und nicht später auf einen veränderten
    Default zurückfällt.
    """
    from .models import DEFAULT_LETTER_LAYOUT

    return {key: template.layout_value(key) for key in DEFAULT_LETTER_LAYOUT}


def _start_context(request, form, *, document, vorgang, template):
    """Kontext der Einstiegsseite -- inklusive der *aufgelösten*
    Auto-Bindungen, damit vor dem Generieren sichtbar ist, welche Werte
    die KI bekommt und welche fehlen.
    """
    bindings = []
    missing_required = []
    if template is not None:
        recipient = document.correspondent if document is not None else None
        values = resolve_placeholders(
            template,
            build_context(document=document, vorgang=vorgang, kontakt=recipient),
        )
        for placeholder in template.placeholders.all():
            if placeholder.source == MANUAL_SOURCE:
                continue
            value = values.get(placeholder.key, "")
            bindings.append({"placeholder": placeholder, "value": value})
            if placeholder.required and not value:
                missing_required.append(placeholder)

    return {
        "form": form,
        "document": document,
        "vorgang": vorgang,
        "template": template,
        "templates": _visible_templates(request.user),
        "bindings": bindings,
        "missing_required": missing_required,
    }


def _context_objects(request):
    """Bezugsdokument und Vorgang aus der Query/dem POST -- beide gescoped.

    Der Vorgang ergibt sich aus dem Dokument, wenn keiner genannt wurde:
    „Antwortschreiben zu diesem Bescheid" gehört in dieselbe Akte wie der
    Bescheid.
    """
    document_id = (request.POST.get("document") or request.GET.get("document") or "").strip()
    vorgang_id = (request.POST.get("vorgang") or request.GET.get("vorgang") or "").strip()

    document = _visible_document(request.user, document_id) if document_id.isdigit() else None
    vorgang = get_object_or_404(Vorgang, pk=vorgang_id) if vorgang_id.isdigit() else None
    if vorgang is None and document is not None:
        vorgang = document.vorgaenge.first()
    return document, vorgang


def _selected_template(request, data):
    template_id = (data.get("template") or "").strip()
    if not template_id.isdigit():
        return None
    return _visible_templates(request.user).filter(pk=template_id).first()


@login_required
def letter_draft_start(request):
    """Vorlage wählen und den Entwurf anstoßen (#1095, Schritt 1+2).

    Der POST legt den Entwurf an und reiht den Worker-Job ein -- er
    schreibt damit als einziger Nicht-Freigabe-Pfad überhaupt etwas, und
    zwar ausschließlich einen Entwurf, der ohne weiteres Zutun nirgendwo
    auftaucht.
    """
    document, vorgang = _context_objects(request)

    if request.method == "POST":
        template = _selected_template(request, request.POST)
        form = LetterDraftStartForm(
            request.POST, templates=_visible_templates(request.user), template=template
        )
        if form.is_valid():
            draft = _create_draft(
                request,
                template=form.cleaned_data["template"],
                document=document,
                vorgang=vorgang,
                notes=form.cleaned_data["notes"],
                manual_values=form.manual_values(),
            )
            return redirect("documents:letter_draft_detail", pk=draft.pk)
    else:
        template = _selected_template(request, request.GET)
        form = LetterDraftStartForm(
            templates=_visible_templates(request.user), template=template
        )

    context = _start_context(request, form, document=document, vorgang=vorgang, template=template)
    if request.htmx:
        # Vorlagenwechsel: nur der Teil, der von der Vorlage abhängt
        # (manuelle Felder + aufgelöste Bindungen), wird ausgetauscht.
        return render(request, "documents/letters/partials/_template_fields.html", context)
    return render(request, "documents/letters/start.html", context)


def _create_draft(request, *, template, document, vorgang, notes, manual_values):
    """Legt den Entwurf mit allen Snapshots an und reiht die Generierung ein.

    Absender ist die `is_self`-Identität (#1030), Empfänger der Kontakt des
    beantworteten Dokuments -- dieselbe Herleitung wie in
    `letter_bindings.build_context`, damit Anschriftfeld und
    Platzhalter-Werte nicht auf zwei verschiedene Kontakte zeigen.
    """
    recipient = document.correspondent if document is not None else None
    sender = Correspondent.objects.filter(is_self=True).first()
    departments, visibility = task_departments_and_visibility(request.user)

    draft = LetterDraft.objects.create(
        template=template,
        template_name=template.name,
        source_document=document,
        vorgang=vorgang,
        recipient=recipient,
        sender=sender,
        status=LetterDraft.Status.RUNNING,
        notes=notes,
        letter_date=timezone.localdate(),
        sender_block=_address_block(sender),
        recipient_block=_address_block(recipient),
        signature=template.signature,
        layout=_layout_snapshot(template),
        manual_values=manual_values,
        owner=request.user,
        visibility=visibility,
    )
    draft.departments.set(departments)
    _enqueue_generation(draft, request.user)
    return draft


def _enqueue_generation(draft, user):
    """Django-Q-Job für die Formulierung. `user.pk` reist mit: die
    Datenbasis ist `visible_to`-gescoped und der Worker hat keinen Request.
    """
    from django_q.tasks import async_task

    from .tasks import generate_letter_draft_task

    async_task(generate_letter_draft_task, draft.pk, user.pk)


def _draft_context(request, draft, *, form=None, message=""):
    source_document = None
    if draft.source_document_id is not None:
        source_document = (
            Document.objects.visible_to(request.user)
            .filter(pk=draft.source_document_id)
            .first()
        )
    return {
        "draft": draft,
        "form": form if form is not None else LetterDraftEditForm(instance=draft),
        "source_document": source_document,
        "message": message,
        # Was die Bindungen ergeben haben -- im Review als Provenienz
        # sichtbar, damit ein falscher Wert auffällt, bevor der Brief rausgeht.
        "placeholder_values": sorted((draft.placeholder_values or {}).items()),
    }


def _render_panel(request, draft, *, form=None, message=""):
    return render(
        request,
        "documents/letters/partials/_draft_panel.html",
        _draft_context(request, draft, form=form, message=message),
    )


@login_required
def letter_draft_detail(request, pk):
    draft = _visible_draft(request.user, pk)
    return render(request, "documents/letters/detail.html", _draft_context(request, draft))


@login_required
def letter_draft_panel(request, pk):
    """Poll-/Swap-Ziel des Entwurfs-Panels -- solange der Worker formuliert,
    holt sich das Fragment selbst wieder ab (`hx-trigger="every ...s"`),
    genau wie das Empfehlungs-Panel (#1093).
    """
    draft = _visible_draft(request.user, pk)
    return _render_panel(request, draft)


@login_required
@require_POST
def letter_draft_update(request, pk):
    """„Text übernehmen & neu rendern" -- speichert Betreff/Brieftext und
    baut Word und PDF neu.

    Synchron und ohne KI-Call: es wird nur neu gesetzt, was der Nutzer
    gerade selbst geschrieben hat, und darauf will er nicht warten müssen.
    """
    draft = _visible_draft(request.user, pk)
    if not draft.is_editable:
        raise Http404("Dieser Entwurf ist nicht (mehr) bearbeitbar.")

    form = LetterDraftEditForm(request.POST, instance=draft)
    if not form.is_valid():
        return _render_panel(request, draft, form=form)

    draft = form.save()
    # Ein gescheiterter KI-Lauf ist mit einem selbst geschriebenen Text
    # geheilt -- sonst bliebe die alte Fehlermeldung über einem Brief
    # stehen, den es längst gibt.
    draft.status = LetterDraft.Status.READY
    draft.error = ""
    draft.save(update_fields=["status", "error", "updated_at"])

    render_draft_files(draft)
    return _render_panel(request, draft, message="Word und PDF wurden neu erzeugt.")


@login_required
@require_POST
def letter_draft_regenerate(request, pk):
    """„Neu generieren": noch einmal formulieren lassen, mit denselben
    Bindungen und Hinweisen.

    Der bisherige Text bleibt bis zum Eintreffen des neuen stehen (dasselbe
    Muster wie bei den Handlungsempfehlungen) -- er ist bis dahin die beste
    verfügbare Fassung.
    """
    draft = _visible_draft(request.user, pk)
    if draft.is_finalized:
        raise Http404("Dieser Brief ist bereits abgelegt.")

    notes = (request.POST.get("notes") or "").strip()
    if notes != draft.notes:
        draft.notes = notes[:2000]
    draft.status = LetterDraft.Status.RUNNING
    draft.error = ""
    draft.save(update_fields=["notes", "status", "error", "updated_at"])

    _enqueue_generation(draft, request.user)
    return _render_panel(request, draft)


@login_required
def letter_draft_download(request, pk, fmt):
    """Word bzw. PDF des Entwurfs, auth-gated -- wie beim Original eines
    Dokuments (#1024) führt kein Weg an dieser View vorbei: die Storage-URL
    des Buckets wäre ein öffentlicher Link ohne jede Sichtbarkeitsprüfung.
    """
    if fmt not in DOWNLOAD_FORMATS:
        raise Http404(f"Unbekanntes Format: {fmt}")

    draft = _visible_draft(request.user, pk)
    field_name, content_type = DOWNLOAD_FORMATS[fmt]
    file_field = getattr(draft, field_name)
    if not file_field:
        raise Http404("Diese Fassung wurde noch nicht erzeugt.")

    response = FileResponse(
        file_field.open("rb"),
        content_type=content_type,
        as_attachment=True,
        filename=file_field.name.rsplit("/", 1)[-1],
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_POST
def letter_draft_finalize(request, pk):
    """Freigabe: Ablage als Dokument am Vorgang.

    Der einzige Pfad in diesem Modul, der etwas Bleibendes anlegt -- und
    auch er verschickt nichts.
    """
    draft = _visible_draft(request.user, pk)
    if not draft.is_editable and not draft.is_finalized:
        raise Http404("Dieser Entwurf ist noch nicht freigabefähig.")

    document = finalize_letter_draft(draft, request.user)
    return redirect("documents:detail", pk=document.pk)


@login_required
@require_POST
def letter_draft_delete(request, pk):
    """Entwurf verwerfen. Ein abgelegter Brief bleibt davon unberührt: das
    Dokument ist dann eigenständig, gelöscht wird nur die Werkbank.
    """
    draft = _visible_draft(request.user, pk)
    document = draft.document
    for file_field in (draft.docx_file, draft.pdf_file):
        if file_field:
            file_field.delete(save=False)
    draft.delete()

    if document is not None:
        return redirect("documents:detail", pk=document.pk)
    if draft.source_document_id is not None:
        return redirect("documents:detail", pk=draft.source_document_id)
    return redirect("documents:home")
