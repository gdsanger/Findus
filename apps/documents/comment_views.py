"""UI for die Dokument-Kommentar-Chronik (#1125): Kommentare mit optionalem

Wiedervorlage-Datum + Erinnerung, inline im Dokument-Detail. Dasselbe
Quick-create-ohne-Seitenwechsel-Muster, das früher auch die verknüpften
Aufgaben am Dokument hatten -- der Block ist inzwischen weg (Aufgaben sind
auf dem Rückzug, siehe CLAUDE.md), dieser hier hat ihn beerbt. Jeder
Eintrag ist einzeln swapbar (`#comment-<pk>`), nicht nur der ganze Block --
Bearbeiten oder Löschen eines Kommentars soll die übrigen, gerade eben
gelesenen Einträge nicht mitverdrängen.

Definiert absichtlich seine eigene `_visible_document` statt die aus
`views.py` zu importieren: `views.document_detail` baut seinerseits den
Kommentar-Kontext für die erste Seitenauslieferung, ein Import in die
Gegenrichtung würde einen Zyklus schließen (gleiches Muster wie
`task_views._visible_task`, das ebenfalls nicht `views._visible_document`
wiederverwendet).
"""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from .forms import DocumentCommentForm
from .models import Document


def _visible_document(user, pk):
    return get_object_or_404(Document.objects.visible_to(user), pk=pk)


def document_comments_context(user, document, form=None):
    return {
        "document": document,
        "comments": document.comments.select_related("author"),
        "comment_form": form,
    }


def _require_author(request, comment):
    """Kommentare erben die Sichtbarkeit des Dokuments (jeder, der das

    Dokument sieht, sieht auch den Kommentar) -- Bearbeiten/Löschen bleibt
    aber dem Autor vorbehalten. Kein Leak-Risiko wie bei der 404-statt-403-
    Regel für Dokumentsichtbarkeit: wer hier landet, sieht den Kommentar
    ohnehin schon.
    """
    if comment.author_id != request.user.id:
        raise PermissionDenied("Nur der Autor kann diesen Kommentar bearbeiten oder löschen.")


@login_required
@require_POST
def document_comment_create(request, pk):
    """Quick-create ohne Seitenwechsel, validiert über `DocumentCommentForm`

    statt rohem POST-Zugriff -- ein unparsbares `follow_up_date` landet
    damit als Inline-Fehler neben dem Feld statt als 500 (#1045, siehe
    CLAUDE.md "UI-Konventionen": nie roh aus `request.POST` ins Model).
    """
    document = _visible_document(request.user, pk)
    form = DocumentCommentForm(request.POST)
    if form.is_valid():
        comment = form.save(commit=False)
        comment.document = document
        comment.author = request.user
        comment.save()
        form = None

    return render(
        request,
        "documents/partials/_detail_comments.html",
        document_comments_context(request.user, document, form=form),
    )


@login_required
def document_comment_edit_form(request, pk, comment_id):
    """Swapt genau diesen einen Eintrag (`#comment-<pk>`) in eine

    Bearbeiten-Ansicht -- die übrige Chronik bleibt unangetastet.
    """
    document = _visible_document(request.user, pk)
    comment = get_object_or_404(document.comments, pk=comment_id)
    _require_author(request, comment)
    form = DocumentCommentForm(instance=comment)
    return render(
        request,
        "documents/partials/_comment_edit_form.html",
        {"document": document, "comment": comment, "form": form},
    )


@login_required
def document_comment_cancel_edit(request, pk, comment_id):
    document = _visible_document(request.user, pk)
    comment = get_object_or_404(document.comments, pk=comment_id)
    return render(
        request,
        "documents/partials/_comment_row.html",
        {"document": document, "comment": comment},
    )


@login_required
@require_POST
def document_comment_update(request, pk, comment_id):
    """`created_at` bleibt unverändert (kein Feld im Formular) -- eine

    Korrektur ist keine neue Chronik-Zeile.
    """
    document = _visible_document(request.user, pk)
    comment = get_object_or_404(document.comments, pk=comment_id)
    _require_author(request, comment)

    form = DocumentCommentForm(request.POST, instance=comment)
    if form.is_valid():
        form.save()
        return render(
            request,
            "documents/partials/_comment_row.html",
            {"document": document, "comment": comment},
        )
    return render(
        request,
        "documents/partials/_comment_edit_form.html",
        {"document": document, "comment": comment, "form": form},
    )


@login_required
@require_POST
def document_comment_delete(request, pk, comment_id):
    document = _visible_document(request.user, pk)
    comment = get_object_or_404(document.comments, pk=comment_id)
    _require_author(request, comment)
    comment.delete()

    return render(
        request,
        "documents/partials/_detail_comments.html",
        document_comments_context(request.user, document),
    )
