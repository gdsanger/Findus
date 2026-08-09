"""UI für Brief-Vorlagen (#1094): CRUD plus Inline-Pflege der
Daten-Bindungen (Platzhalter), seit #1097 mit „Mit KI erstellen".

Schwesterseite von `task_template_views` -- gleiche Struktur (Liste,
Detail = Bearbeiten, HTMX-Partial für die Zeilen), aber ein anderer
Gegenstand: hier geht es um Schreiben, nicht um Aufgaben. Erzeugt noch
kein Schreiben; das ist #4b.

Der KI-Entwurf (#1097, `letter_template_ai`) hängt bewusst an denselben
Formularen und speichert nichts: `letter_template_draft` rendert den
Editor mit vorbefüllten Feldern und den Platzhalter-Vorschlägen als
editierbare Zeilen *innerhalb* des Formulars zurück. Erst „Speichern"
legt Vorlage und angehakte Vorschläge an -- deshalb reisen die
Vorschläge als `suggested_*`-Felder im POST mit, statt schon beim
Generieren Zeilen in der Datenbank anzulegen.
"""

import logging

from django.contrib.auth.decorators import login_required
from django.db.models import Max
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .forms import (
    LetterTemplateDraftForm,
    LetterTemplateForm,
    LetterTemplatePlaceholderForm,
)
from .letter_bindings import source_choices, source_label
from .letter_template_ai import LetterTemplateDraftError, draft_letter_template
from .models import LetterTemplate
from .task_views import task_departments_and_visibility

logger = logging.getLogger(__name__)

# Vorschlagszeilen reisen als gleichlange Parallel-Listen im POST; die
# beiden Checkboxen tragen den Zeilenindex als Wert, weil ein nicht
# angehaktes Kästchen gar nicht mitgesendet wird und die Listen sonst
# gegeneinander verrutschen würden.
_SUGGESTION_PREFIX = "suggested_"


def _visible_template(user, pk):
    return get_object_or_404(LetterTemplate.objects.visible_to(user), pk=pk)


def _form_context(form):
    return {
        "form": form,
        "category_suggestions": LetterTemplate.CATEGORY_SUGGESTIONS,
    }


def _editor_context(
    template,
    form,
    *,
    draft_form=None,
    draft=None,
    draft_error="",
    suggestions=None,
    suggestion_errors=(),
):
    """Alles, was das Editor-Fragment (KI-Panel + Formular + Vorschläge)
    braucht -- für die Neu-Seite (`template is None`) wie für die
    Bearbeiten-Seite.
    """
    context = {
        "template": template,
        "draft_form": draft_form if draft_form is not None else LetterTemplateDraftForm(),
        "draft": draft,
        "draft_error": draft_error,
        "suggestions": suggestions or [],
        "suggestion_errors": list(suggestion_errors),
        "source_groups": source_choices(),
        "save_label": "Speichern" if template else "Anlegen",
        # Explizit statt „an die aktuelle URL": nach einem Entwurf ohne
        # JavaScript steht im Browser die Entwurfs-URL, und dorthin darf das
        # Speichern-Formular nicht zeigen.
        "save_url": (
            reverse("documents:letter_template_detail", args=[template.pk])
            if template
            else reverse("documents:letter_template_create")
        ),
    }
    context.update(_form_context(form))
    return context


def _render_editor(request, template, form, **kwargs):
    """Das Editor-Fragment für HTMX, sonst die komplette Seite.

    Ohne den Fallback wäre die Antwort auf einen Nicht-HTMX-Post (kein
    JavaScript) ein nacktes Fragment ohne Navigation.
    """
    context = _editor_context(template, form, **kwargs)
    if request.htmx:
        return render(
            request, "documents/letter_templates/partials/_editor.html", context
        )
    if template is None:
        return render(request, "documents/letter_templates/form.html", context)
    context.update(_placeholders_context(template))
    return render(request, "documents/letter_templates/detail.html", context)


@login_required
def letter_template_list(request):
    templates = (
        LetterTemplate.objects.visible_to(request.user)
        .prefetch_related("placeholders")
        .order_by("category", "name")
    )
    return render(
        request, "documents/letter_templates/list.html", {"templates": templates}
    )


@login_required
def letter_template_create(request):
    if request.method == "POST":
        form = LetterTemplateForm(request.POST, request.FILES)
        suggestions = _suggestion_rows(request.POST)
        errors = _suggestion_errors(suggestions)
        if form.is_valid() and not errors:
            template = form.save(commit=False)
            template.owner = request.user
            # Dieselbe Abteilungs-/Privat-Ableitung wie bei Aufgaben(-Vorlagen):
            # eine Vorlage gehört in den Scope dessen, der sie anlegt.
            departments, visibility = task_departments_and_visibility(request.user)
            template.visibility = visibility
            template.save()
            template.departments.set(departments)
            _create_suggested_placeholders(template, suggestions)
            return redirect("documents:letter_template_detail", pk=template.pk)
        return _render_editor(
            request,
            None,
            form,
            suggestions=suggestions,
            suggestion_errors=errors,
        )

    return _render_editor(request, None, LetterTemplateForm())


@login_required
def letter_template_detail(request, pk):
    template = _visible_template(request.user, pk)

    if request.method == "POST":
        form = LetterTemplateForm(request.POST, request.FILES, instance=template)
        suggestions = _suggestion_rows(request.POST)
        errors = _suggestion_errors(suggestions, template=template)
        if form.is_valid() and not errors:
            form.save()
            _create_suggested_placeholders(template, suggestions)
            return redirect("documents:letter_template_detail", pk=template.pk)
    else:
        form = LetterTemplateForm(instance=template)
        suggestions = []
        errors = []

    context = _editor_context(
        template,
        form,
        suggestions=suggestions,
        suggestion_errors=errors,
    )
    context.update(_placeholders_context(template))
    return render(request, "documents/letter_templates/detail.html", context)


@login_required
@require_POST
def letter_template_draft(request):
    """„Mit KI erstellen" (#1097): ein Entwurf, der die Formularfelder
    vorbefüllt -- und sonst nichts.

    Bewusst synchron und ohne jede Schreiboperation: es entsteht kein
    Datensatz, den ein Worker-Job befüllen könnte, und was zurückkommt,
    ist ein ungebundenes Formular -- der Entwurf soll den Nutzer nicht
    mit Validierungsfehlern begrüßen, sondern mit Vorschlägen.

    Bereits eingetippte Werte gehen nicht verloren: der Button schickt das
    Formular mit (`hx-include`), die Anleitung wird ersetzt (sie ist das
    Bestellte), Name/Kategorie/Beschreibung nur dort gesetzt, wo noch
    nichts steht.
    """
    template = None
    template_pk = (request.POST.get("template_pk") or "").strip()
    if template_pk.isdigit():
        template = _visible_template(request.user, template_pk)

    draft_form = LetterTemplateDraftForm(request.POST)
    initial = _posted_initial(request.POST)
    # Vorschläge aus einem früheren Entwurf überleben einen Fehlschlag --
    # sie sind das, was der Nutzer gerade vor sich hat.
    suggestions = _suggestion_rows(request.POST)

    if not draft_form.is_valid():
        return _render_editor(
            request,
            template,
            LetterTemplateForm(initial=initial, instance=template),
            draft_form=draft_form,
            suggestions=suggestions,
        )

    try:
        draft = draft_letter_template(
            intent=draft_form.cleaned_data["intent"],
            category=draft_form.cleaned_data["category_hint"],
            tone=draft_form.cleaned_data["tone"],
        )
    except LetterTemplateDraftError as error:
        return _render_editor(
            request,
            template,
            LetterTemplateForm(initial=initial, instance=template),
            draft_form=draft_form,
            draft_error=str(error),
            suggestions=suggestions,
        )

    initial["instructions"] = draft.instructions
    for field, value in (
        ("name", draft.name),
        ("category", draft.category or draft_form.cleaned_data["category_hint"]),
        ("description", draft.description),
    ):
        if value and not (initial.get(field) or "").strip():
            initial[field] = value

    return _render_editor(
        request,
        template,
        LetterTemplateForm(initial=initial, instance=template),
        draft_form=draft_form,
        draft=draft,
        suggestions=_draft_suggestion_rows(draft, template),
    )


def _posted_initial(post):
    """Die aktuell im Formular stehenden Werte als `initial` für ein
    ungebundenes Formular.

    Ungebunden, damit das vorbefüllte Formular keine Validierungsfehler
    anzeigt, die der Nutzer noch gar nicht verursacht hat -- ein Entwurf
    ist noch kein Speicherversuch. Das Logo fehlt hier zwangsläufig: eine
    Datei lässt sich nicht als `initial` zurückgeben (und liegt beim
    Bearbeiten ohnehin schon am `instance`).
    """
    return {name: post.get(name) for name in LetterTemplateForm().fields if name in post}


def _suggestion_row(key, label, source, required, take, existing=False):
    return {
        "key": key,
        "label": label,
        "source": source,
        "source_label": source_label(source),
        "required": required,
        "take": take,
        "existing": existing,
    }


def _draft_suggestion_rows(draft, template):
    """Die Platzhalter-Vorschläge eines frischen Entwurfs als Zeilen.

    Ein Schlüssel, den die Vorlage schon hat, kommt entkreuzt und mit
    Hinweis -- „übernehmen" würde ihn nicht überschreiben, sondern an der
    Unique-Constraint scheitern.
    """
    existing_keys = (
        set(template.placeholders.values_list("key", flat=True)) if template else set()
    )
    return [
        _suggestion_row(
            suggestion.key,
            suggestion.label,
            suggestion.source,
            suggestion.required,
            take=suggestion.key not in existing_keys,
            existing=suggestion.key in existing_keys,
        )
        for suggestion in draft.placeholders
    ]


def _suggestion_rows(post):
    """Alle Vorschlagszeilen aus dem POST, jede mit ihrem „übernehmen"-Haken.

    Auch die entkreuzten -- sie werden zwar nicht gespeichert, gehören aber
    beim erneuten Rendern (Validierungsfehler, zweiter Entwurf) wieder auf
    den Bildschirm, sonst verschwinden Zeilen, die der Nutzer nur
    zurückgestellt hat.
    """
    keys = post.getlist(f"{_SUGGESTION_PREFIX}key")
    labels = post.getlist(f"{_SUGGESTION_PREFIX}label")
    sources = post.getlist(f"{_SUGGESTION_PREFIX}source")
    taken = set(post.getlist(f"{_SUGGESTION_PREFIX}take"))
    required = set(post.getlist(f"{_SUGGESTION_PREFIX}required"))

    return [
        _suggestion_row(
            key.strip(),
            labels[index].strip() if index < len(labels) else "",
            sources[index] if index < len(sources) else "",
            required=str(index) in required,
            take=str(index) in taken,
        )
        for index, key in enumerate(keys)
    ]


def _suggestion_placeholder_form(row, template=None):
    return LetterTemplatePlaceholderForm(
        {
            "key": row["key"],
            "label": row["label"],
            "source": row["source"],
            "required": row["required"],
        },
        template=template,
    )


def _suggestion_errors(rows, template=None):
    """Prüft die angehakten Vorschlagszeilen, *bevor* irgendetwas
    gespeichert wird.

    Ohne Vorlage (Neu-Seite) prüft `LetterTemplatePlaceholderForm` die
    Eindeutigkeit nicht -- es gibt noch keine Zeilen, gegen die sie prüfen
    könnte --, also übernimmt das hier die Schleife für die Zeilen
    untereinander. Eine ungültige Zeile bricht das Speichern ab, statt
    still unter den Tisch zu fallen: der Nutzer hat sie angehakt.
    """
    errors = []
    seen = set()
    for row in rows:
        if not row["take"]:
            continue
        form = _suggestion_placeholder_form(row, template=template)
        name = row["key"] or "(ohne Schlüssel)"
        if not form.is_valid():
            errors.append(f"Platzhalter-Vorschlag „{name}“: {_first_error(form)}")
            continue
        key = form.cleaned_data["key"]
        if key in seen:
            errors.append(
                f"Platzhalter-Vorschlag „{name}“: Der Schlüssel ist doppelt vergeben."
            )
        seen.add(key)
    return errors


def _create_suggested_placeholders(template, rows):
    """Legt die angehakten, bereits geprüften Vorschläge als Zeilen an --
    hinter den vorhandenen Platzhaltern.
    """
    order = template.placeholders.aggregate(Max("order"))["order__max"] or 0
    for row in rows:
        if not row["take"]:
            continue
        form = _suggestion_placeholder_form(row, template=template)
        if not form.is_valid():
            # Kann nach `_suggestion_errors` nur noch passieren, wenn sich
            # der Bestand zwischen Prüfung und Anlage geändert hat.
            logger.warning(
                "Brief-Vorlage %s: Platzhalter-Vorschlag %r übersprungen (%s)",
                template.pk,
                row["key"],
                _first_error(form),
            )
            continue
        placeholder = form.save(commit=False)
        placeholder.template = template
        order += 1
        placeholder.order = order
        placeholder.save()


@login_required
@require_POST
def letter_template_delete(request, pk):
    template = _visible_template(request.user, pk)
    template.delete()
    return redirect("documents:letter_template_list")


def _placeholders_context(template, add_form=None, error=""):
    return {
        "template": template,
        "placeholders": list(template.placeholders.all()),
        "add_form": add_form if add_form is not None else LetterTemplatePlaceholderForm(),
        "placeholder_error": error,
        # Die Auswahl kommt aus der Quellen-Registry, nicht aus dem Model --
        # die Zeilen-Selects rendern dieselben Gruppen wie das Formular.
        "source_groups": source_choices(),
    }


def _render_placeholders(request, template, add_form=None, error=""):
    return render(
        request,
        "documents/letter_templates/partials/_placeholders.html",
        _placeholders_context(template, add_form=add_form, error=error),
    )


@login_required
@require_POST
def letter_template_placeholder_add(request, pk):
    template = _visible_template(request.user, pk)
    form = LetterTemplatePlaceholderForm(request.POST, template=template)
    if not form.is_valid():
        return _render_placeholders(request, template, add_form=form)

    placeholder = form.save(commit=False)
    placeholder.template = template
    placeholder.order = (
        template.placeholders.aggregate(Max("order"))["order__max"] or 0
    ) + 1
    placeholder.save()
    return _render_placeholders(request, template)


@login_required
@require_POST
def letter_template_placeholder_update(request, pk, placeholder_id):
    template = _visible_template(request.user, pk)
    placeholder = get_object_or_404(template.placeholders, pk=placeholder_id)
    form = LetterTemplatePlaceholderForm(
        request.POST, instance=placeholder, template=template
    )
    if not form.is_valid():
        # Die Fehler landen als Meldung über der Liste statt an der Zeile:
        # die Zeile wird beim Partial-Swap ohnehin neu gerendert, ein
        # gebundenes Formular je Zeile wäre nur Zustand, den niemand liest.
        return _render_placeholders(
            request, template, error=_first_error(form) or "Ungültige Eingabe."
        )
    form.save()
    return _render_placeholders(request, template)


@login_required
@require_POST
def letter_template_placeholder_delete(request, pk, placeholder_id):
    template = _visible_template(request.user, pk)
    placeholder = get_object_or_404(template.placeholders, pk=placeholder_id)
    placeholder.delete()
    return _render_placeholders(request, template)


@login_required
@require_POST
def letter_template_placeholder_move(request, pk, placeholder_id, direction):
    """Tauscht `order` mit dem Vorgänger/Nachfolger -- dasselbe
    Kein-Drag-and-Drop-Vorgehen wie `task_template_views`.
    """
    if direction not in ("up", "down"):
        raise Http404

    template = _visible_template(request.user, pk)
    placeholder = get_object_or_404(template.placeholders, pk=placeholder_id)
    placeholders = list(template.placeholders.all())
    index = placeholders.index(placeholder)

    neighbor_index = index - 1 if direction == "up" else index + 1
    if 0 <= neighbor_index < len(placeholders):
        neighbor = placeholders[neighbor_index]
        placeholder.order, neighbor.order = neighbor.order, placeholder.order
        template.placeholders.model.objects.bulk_update(
            [placeholder, neighbor], ["order"]
        )

    return _render_placeholders(request, template)


def _first_error(form):
    for errors in form.errors.values():
        if errors:
            return errors[0]
    return ""
