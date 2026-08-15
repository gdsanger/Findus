"""UI für den KI-Brief aus einer Vorlage (#1095): wählen → generieren →
prüfen → freigeben.

**Ein Schreiben ist immer eine Antwort auf ein Dokument** (#1138). Es gibt
keinen zweiten Modus mehr: ohne Bezugsdokument ist der Kontext für die
Platzhalter nicht eindeutig -- ein Vorgang bündelt mehrere Dokumente und
mehrere Kontakte, und „welches gewinnt?" wäre eine Rateregel. Der Einstieg
aus dem Vorgang bleibt erhalten, führt aber über
`letter_draft_pick_source` („Auf welches Dokument antworten?").

Der Ablauf in vier Seiten/Endpunkten:

0. `letter_draft_pick_source` -- nur aus dem Vorgang heraus: das
   Bezugsdokument wählen. Aus einem Dokument heraus entfällt der Schritt.
1. `letter_draft_start` -- Vorlage wählen, Empfänger/Vorgang bestätigen,
   Lücken füllen, Hinweise dazuschreiben. Was sich aus dem Bezugsdokument
   binden lässt, wird *angezeigt* (Provenienz); was fehlt, wird mit Grund
   angezeigt und als Eingabefeld abgefragt.
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
from .letter_bindings import build_context, resolve_bindings
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


def _start_context(request, form, *, document, vorgang, template, bindings):
    """Kontext der Einstiegsseite -- inklusive der *aufgelösten* Bindungen,
    damit vor dem Generieren sichtbar ist, welche Werte die KI bekommt,
    welche fehlen und warum.

    `resolved`/`missing_required` sind zwei Sichten auf dieselbe
    `resolve_bindings`-Antwort, keine zweite Herleitung -- was fehlt, hängt
    als Eingabefeld am Formular (`form.value_rows`).
    """
    return {
        "form": form,
        "document": document,
        "vorgang": vorgang,
        "template": template,
        "templates": _visible_templates(request.user),
        "bindings": bindings,
        # Nur die gebundenen Werte, die dastehen -- die Kontrollansicht.
        "resolved": [
            binding for binding in bindings if not binding.is_manual and not binding.is_missing
        ],
        "missing_required": [
            binding for binding in bindings if binding.required and binding.is_missing
        ],
    }


def _visible_vorgang_documents(user, vorgang):
    """Die für den Nutzer sichtbaren Dokumente eines Vorgangs, jüngste
    zuerst.

    Sortiert nach `display_date` und damit in Python: das ist eine
    Property (Dokumentdatum mit Rückfall aufs Upload-Datum, #1085), und die
    Liste eines Vorgangs ist klein genug, dass sie dafür nicht in die
    Datenbank muss.
    """
    documents = (
        Document.objects.visible_to(user)
        .filter(vorgaenge=vorgang)
        .select_related("correspondent")
        .distinct()
    )
    return sorted(documents, key=lambda document: document.display_date, reverse=True)


@login_required
def letter_draft_pick_source(request, vorgang_pk):
    """„Auf welches Dokument antworten?" -- der vorgeschaltete Schritt beim
    Einstieg aus dem Vorgang (#1138).

    Reine Auswahlseite, sie schreibt nichts. Angeboten werden ausschließlich
    Dokumente, die für den anfragenden Nutzer sichtbar sind -- der Vorgang
    selbst ist kein Sichtbarkeitsträger, seine Dokumente sind es.
    """
    vorgang = get_object_or_404(Vorgang, pk=vorgang_pk)
    return render(
        request,
        "documents/letters/pick_source.html",
        {
            "vorgang": vorgang,
            "documents": _visible_vorgang_documents(request.user, vorgang),
        },
    )


def _context_objects(request):
    """Bezugsdokument und Vorgang aus der Query/dem POST -- beide gescoped.

    Das Dokument ist seit #1138 Pflicht (der Aufrufer prüft das); der
    Vorgang ergibt sich daraus, wenn keiner genannt wurde: „Antwortschreiben
    zu diesem Bescheid" gehört in dieselbe Akte wie der Bescheid. Ein
    genannter Vorgang zählt nur, wenn das Bezugsdokument auch darin liegt --
    sonst käme über die URL ein fremder Vorgang in den Prompt und später an
    das abgelegte Schreiben.
    """
    document_id = (request.POST.get("document") or request.GET.get("document") or "").strip()
    vorgang_id = (request.POST.get("vorgang") or request.GET.get("vorgang") or "").strip()

    document = _visible_document(request.user, document_id) if document_id.isdigit() else None
    if document is None:
        return None, None

    vorgang = document.vorgaenge.filter(pk=vorgang_id).first() if vorgang_id.isdigit() else None
    return document, vorgang or document.vorgaenge.first()


def _selected_template(request, data):
    template_id = (data.get("template") or "").strip()
    if not template_id.isdigit():
        return None
    return _visible_templates(request.user).filter(pk=template_id).first()


def _selected_recipient(document, data):
    """Der Empfänger für diesen Brief: was das Formular sagt, sonst der
    Kontakt des Bezugsdokuments.

    Die Ableitung aus dem Dokument ist ein *Vorschlag* (#1138) -- wer
    „– kein Empfänger –" wählt, meint das, deshalb entscheidet das
    Vorhandensein des Feldes im POST und nicht die Leere seines Werts.
    """
    if "recipient" not in data:
        return document.correspondent
    recipient_id = (data.get("recipient") or "").strip()
    if not recipient_id.isdigit():
        return None
    return Correspondent.objects.filter(pk=recipient_id).first()


@login_required
def letter_draft_start(request):
    """Vorlage wählen und den Entwurf anstoßen (#1095, Schritt 1+2).

    Ohne sichtbares Bezugsdokument gibt es keinen Einstieg mehr (#1138):
    Wer mit einem Vorgang kommt (alter Link, Lesezeichen), landet auf der
    Dokumentauswahl statt in einem Formular, das anschließend die Hälfte
    seiner Werte nicht auflösen kann.

    Der POST legt den Entwurf an und reiht den Worker-Job ein -- er
    schreibt damit als einziger Nicht-Freigabe-Pfad überhaupt etwas, und
    zwar ausschließlich einen Entwurf, der ohne weiteres Zutun nirgendwo
    auftaucht.
    """
    document, vorgang = _context_objects(request)
    if document is None:
        vorgang_id = (request.GET.get("vorgang") or "").strip()
        if vorgang_id.isdigit() and Vorgang.objects.filter(pk=vorgang_id).exists():
            return redirect("documents:letter_draft_pick_source", vorgang_pk=int(vorgang_id))
        raise Http404(
            "Ein Schreiben entsteht immer als Antwort auf ein Dokument – "
            "es fehlt ein sichtbares Bezugsdokument."
        )

    data = request.POST if request.method == "POST" else request.GET
    template = _selected_template(request, data)
    recipient = _selected_recipient(document, data)
    # Bindungen ohne die Eingaben des Nutzers: sie bestimmen, welche Felder
    # das Formular überhaupt anbietet, und dürfen deshalb nicht davon
    # abhängen, was gerade darin steht.
    bindings = (
        resolve_bindings(
            template, build_context(document=document, vorgang=vorgang, kontakt=recipient)
        )
        if template is not None
        else []
    )
    form_kwargs = {
        "templates": _visible_templates(request.user),
        "template": template,
        "bindings": bindings,
        "recipients": Correspondent.objects.all(),
        "vorgaenge": document.vorgaenge.all(),
    }

    if request.method == "POST":
        form = LetterDraftStartForm(request.POST, **form_kwargs)
        if form.is_valid():
            # Bewusst `recipient` von oben und nicht `cleaned_data`: das ist
            # genau der Kontakt, gegen den die angezeigten Bindungen
            # aufgelöst wurden. Und ein POST ohne `recipient`-Feld (etwa aus
            # einem Skript) bekommt weiterhin den Vorschlag aus dem
            # Bezugsdokument, statt still ohne Empfänger dazustehen.
            _save_to_contact(recipient, form.contact_writeback())
            draft = _create_draft(
                request,
                template=form.cleaned_data["template"],
                document=document,
                vorgang=form.cleaned_data["vorgang"] or vorgang,
                recipient=recipient,
                notes=form.cleaned_data["notes"],
                manual_values=form.manual_values(),
            )
            return redirect("documents:letter_draft_detail", pk=draft.pk)
    else:
        form = LetterDraftStartForm(
            initial={"recipient": recipient, "vorgang": vorgang}, **form_kwargs
        )

    context = _start_context(
        request, form, document=document, vorgang=vorgang, template=template, bindings=bindings
    )
    if request.htmx:
        # Vorlagen-/Empfängerwechsel: nur der Teil, der davon abhängt
        # (Eingabefelder für Lücken + aufgelöste Bindungen), wird getauscht.
        return render(request, "documents/letters/partials/_template_fields.html", context)
    return render(request, "documents/letters/start.html", context)


def _save_to_contact(recipient, values):
    """Trägt angekreuzte Lücken dauerhaft am Empfänger nach (#1138).

    Damit ist die Lücke beim nächsten Schreiben an denselben Kontakt zu,
    statt jedes Mal von Hand gefüllt zu werden. Ohne Empfänger gibt es
    nichts zu speichern -- der eingetippte Wert gilt dann nur für diesen
    einen Brief.
    """
    if recipient is None or not values:
        return
    for field, value in values.items():
        setattr(recipient, field, value)
    recipient.save(update_fields=[*values, "updated_at"])


def _create_draft(request, *, template, document, vorgang, recipient, notes, manual_values):
    """Legt den Entwurf mit allen Snapshots an und reiht die Generierung ein.

    Absender ist die `is_self`-Identität (#1030), Empfänger der im Formular
    bestätigte Kontakt -- er geht ausdrücklich mit, damit Anschriftfeld und
    Platzhalter-Werte nicht auf zwei verschiedene Kontakte zeigen, wenn der
    Vorschlag aus dem Bezugsdokument überschrieben wurde.
    """
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
