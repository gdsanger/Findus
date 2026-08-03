"""CRUD for the shared Absender/Vorgang/Tag vocabulary (#1021).

All three (`Correspondent`, `Vorgang`, `Tag`) are department-agnostic shared
vocabulary (see Architektur.md, "Wissen teilen innerhalb der Abteilung") --
unlike `Document`/`Task` they carry no `visibility`/`departments` fields, so
there is nothing to scope here beyond `login_required`. The three models are
close enough in shape (a name plus zero or one extra field) that one
config-driven set of views covers all of them instead of tripling the code.
"""

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import CorrespondentForm, TagForm, VorgangForm
from .models import Correspondent, Tag, Vorgang

STAMMDATEN_KINDS = {
    "correspondents": {
        "model": Correspondent,
        "form": CorrespondentForm,
        "label": "Absender",
        "label_plural": "Absender",
        "list_headers": ("Name", "E-Mail", "Das bin ich"),
        "list_fields": ("name", "email", "is_self"),
    },
    "vorgaenge": {
        "model": Vorgang,
        "form": VorgangForm,
        "label": "Vorgang",
        "label_plural": "Vorgänge",
        "list_headers": ("Name",),
        "list_fields": ("name",),
    },
    "tags": {
        "model": Tag,
        "form": TagForm,
        "label": "Tag",
        "label_plural": "Tags",
        "list_headers": ("Name", "Dimension"),
        "list_fields": ("name", "dimension"),
    },
}

STAMMDATEN_NAV = [(kind, config["label_plural"]) for kind, config in STAMMDATEN_KINDS.items()]


def _stammdaten_config(kind):
    try:
        return STAMMDATEN_KINDS[kind]
    except KeyError:
        raise Http404(f"Unbekannte Stammdaten-Art: {kind}")


def _list_column_value(obj, field_name):
    """A bare boolean (e.g. `Correspondent.is_self`) would render as "—" via
    the list template's `default:"—"` filter, since `False` is falsy --
    spell it out as Ja/Nein instead.
    """
    value = getattr(obj, field_name)
    if isinstance(value, bool):
        return "Ja" if value else "Nein"
    return value


@login_required
def stammdaten_home(request):
    first_kind = next(iter(STAMMDATEN_KINDS))
    return redirect("documents:stammdaten_list", kind=first_kind)


@login_required
def stammdaten_list(request, kind):
    config = _stammdaten_config(kind)

    if request.method == "POST":
        form = config["form"](request.POST)
        if form.is_valid():
            form.save()
            return redirect("documents:stammdaten_list", kind=kind)
    else:
        form = config["form"]()

    rows = [
        {
            "pk": obj.pk,
            "columns": [_list_column_value(obj, field) for field in config["list_fields"]],
        }
        for obj in config["model"].objects.all()
    ]

    context = {
        "kind": kind,
        "label": config["label"],
        "label_plural": config["label_plural"],
        "list_headers": config["list_headers"],
        "rows": rows,
        "form": form,
        "nav": STAMMDATEN_NAV,
    }
    return render(request, "documents/stammdaten/list.html", context)


@login_required
def stammdaten_edit(request, kind, pk):
    config = _stammdaten_config(kind)
    instance = get_object_or_404(config["model"], pk=pk)

    if request.method == "POST":
        form = config["form"](request.POST, instance=instance)
        if form.is_valid():
            form.save()
            return redirect("documents:stammdaten_list", kind=kind)
    else:
        form = config["form"](instance=instance)

    context = {
        "kind": kind,
        "label": config["label"],
        "label_plural": config["label_plural"],
        "form": form,
        "instance": instance,
    }
    return render(request, "documents/stammdaten/edit.html", context)


@login_required
@require_POST
def stammdaten_delete(request, kind, pk):
    config = _stammdaten_config(kind)
    instance = get_object_or_404(config["model"], pk=pk)
    instance.delete()
    return redirect("documents:stammdaten_list", kind=kind)
