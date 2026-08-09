"""UI für Brief-Vorlagen (#1094): CRUD plus Inline-Pflege der
Daten-Bindungen (Platzhalter).

Schwesterseite von `task_template_views` -- gleiche Struktur (Liste,
Detail = Bearbeiten, HTMX-Partial für die Zeilen), aber ein anderer
Gegenstand: hier geht es um Schreiben, nicht um Aufgaben. Erzeugt noch
kein Schreiben; das ist #4b.
"""

from django.contrib.auth.decorators import login_required
from django.db.models import Max
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import LetterTemplateForm, LetterTemplatePlaceholderForm
from .letter_bindings import source_choices
from .models import LetterTemplate
from .task_views import task_departments_and_visibility


def _visible_template(user, pk):
    return get_object_or_404(LetterTemplate.objects.visible_to(user), pk=pk)


def _form_context(form):
    return {
        "form": form,
        "category_suggestions": LetterTemplate.CATEGORY_SUGGESTIONS,
    }


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
        if form.is_valid():
            template = form.save(commit=False)
            template.owner = request.user
            # Dieselbe Abteilungs-/Privat-Ableitung wie bei Aufgaben(-Vorlagen):
            # eine Vorlage gehört in den Scope dessen, der sie anlegt.
            departments, visibility = task_departments_and_visibility(request.user)
            template.visibility = visibility
            template.save()
            template.departments.set(departments)
            return redirect("documents:letter_template_detail", pk=template.pk)
    else:
        form = LetterTemplateForm()

    return render(request, "documents/letter_templates/form.html", _form_context(form))


@login_required
def letter_template_detail(request, pk):
    template = _visible_template(request.user, pk)

    if request.method == "POST":
        form = LetterTemplateForm(request.POST, request.FILES, instance=template)
        if form.is_valid():
            form.save()
            return redirect("documents:letter_template_detail", pk=template.pk)
    else:
        form = LetterTemplateForm(instance=template)

    context = {"template": template}
    context.update(_form_context(form))
    context.update(_placeholders_context(template))
    return render(request, "documents/letter_templates/detail.html", context)


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
