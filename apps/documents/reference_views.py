"""Der Kennungen-Block an den Hubs (#1100): Vorgang (#1040) und Kontakt
(#1041) pflegen hier ihre eigenen Referenznummern.

Ein Modul für beide Seiten statt je eines in `vorgang_views`/
`correspondent_views`: die Regeln sind identisch (exakt derselbe
Normalisierungs-Schlüssel, dieselbe Dedup-Logik, dieselbe Trennung
manuell/gelernt) und unterscheiden sich nur im Ziel-Objekt. Zweimal
formuliert würden sie irgendwann auseinanderlaufen -- und ein Vorgang, der
anders normalisiert als ein Kontakt, fällt niemandem auf: der Abgleich
findet dann einfach nichts.

Angeboten werden je Ziel nur die Kennungsarten seines Scopes
(`reference_matching.types_for_scope`) -- eine Kundennummer am Vorgang
wäre eine Eingabe ohne Wirkung.
"""

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from .models import Correspondent, Vorgang
from .reference_matching import (
    scope_of,
    set_owner_reference,
    types_for_scope,
)

VORGANG_REFERENCES_BLOCK = "vorgang-references"
CORRESPONDENT_REFERENCES_BLOCK = "correspondent-references"


def owner_references_context(target, *, error=None):
    """Kontext des Kennungen-Blocks für einen Vorgang oder einen Kontakt.

    Die URL-Namen wandern als Werte mit in den Kontext, damit *ein*
    Partial beide Hubs bedient -- `{% url %}` nimmt den Namen auch als
    Variable.
    """
    scope = scope_of(target)
    is_vorgang = isinstance(target, Vorgang)
    return {
        "owner_references": list(target.references.all()),
        "owner_reference_scope": scope,
        "owner_reference_owner_pk": target.pk,
        "owner_reference_type_choices": types_for_scope(scope),
        "owner_reference_block_id": (
            VORGANG_REFERENCES_BLOCK if is_vorgang else CORRESPONDENT_REFERENCES_BLOCK
        ),
        "owner_reference_create_url_name": (
            "documents:vorgang_reference_create"
            if is_vorgang
            else "documents:correspondent_reference_create"
        ),
        "owner_reference_delete_url_name": (
            "documents:vorgang_reference_delete"
            if is_vorgang
            else "documents:correspondent_reference_delete"
        ),
        "owner_reference_error": error,
    }


def _render_block(request, target, *, error=None):
    return render(
        request,
        "documents/partials/_owner_references.html",
        owner_references_context(target, error=error),
    )


def _create(request, target):
    """Eine Kennung von Hand hinterlegen.

    Zwei unterscheidbare Fehlerfälle statt eines stillen No-Ops: ein
    leeres Feld ist ein Vertipper, eine unpassende Kennungsart dagegen
    eine Eingabe, die zwar gespeichert *aussähe*, aber nie einen Treffer
    erzeugen könnte (Typ->Scope-Mapping, #1099).
    """
    value = request.POST.get("value", "")
    reference = set_owner_reference(
        target,
        reference_type=request.POST.get("type", ""),
        value=value,
    )
    if reference is not None:
        return _render_block(request, target)
    error = (
        "Bitte eine Kennung eingeben."
        if not value.strip()
        else "Diese Kennungsart gehört nicht an dieses Ziel."
    )
    return _render_block(request, target, error=error)


def _delete(request, target, reference_id):
    """Gescoped über die Reverse-Relation: eine ID aus der URL kann nur
    eine Kennung *dieses* Ziels treffen.
    """
    reference = get_object_or_404(target.references, pk=reference_id)
    reference.delete()
    return _render_block(request, target)


@login_required
def vorgang_references(request, pk):
    return _render_block(request, get_object_or_404(Vorgang, pk=pk))


@login_required
@require_POST
def vorgang_reference_create(request, pk):
    return _create(request, get_object_or_404(Vorgang, pk=pk))


@login_required
@require_POST
def vorgang_reference_delete(request, pk, reference_id):
    return _delete(request, get_object_or_404(Vorgang, pk=pk), reference_id)


@login_required
def correspondent_references(request, pk):
    return _render_block(request, get_object_or_404(Correspondent, pk=pk))


@login_required
@require_POST
def correspondent_reference_create(request, pk):
    return _create(request, get_object_or_404(Correspondent, pk=pk))


@login_required
@require_POST
def correspondent_reference_delete(request, pk, reference_id):
    return _delete(request, get_object_or_404(Correspondent, pk=pk), reference_id)
