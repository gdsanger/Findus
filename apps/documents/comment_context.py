"""Gemeinsamer Baustein fuer den "Notizen des Nutzers"-Abschnitt in
KI-Prompts -- urspruenglich fuer die Handlungsempfehlungen gebaut (#1132),
seit #1135 auch von der ausfuehrlichen Zusammenfassung genutzt.

Kommentare sind Notizen UEBER ein Dokument, kein Dokumentinhalt (CLAUDE.md
"Pipelines & Services"): diese Datei ist die eine Stelle, die daraus
Prompt-Zeilen macht, damit jeder Aufrufer exakt dieselbe Auswahl-/
Kuerzungslogik teilt statt sie mehrfach zu pflegen. `apps.documents.
recommendations.build_vorgang_comment_context` und `apps.documents.
long_summary` setzen beide hier auf, jeweils mit ihrer eigenen
Abschnitts-Ueberschrift.
"""

from __future__ import annotations

from django.utils import timezone

from .models import DocumentComment


def followup_label(comment: DocumentComment) -> str:
    """Ueberfaellig/heute faellig/geplant -- Textform von
    `DocumentComment.is_overdue`/`is_due_today` fuer den Prompt (#1132).
    """
    if comment.is_overdue:
        return "ueberfaellig"
    if comment.is_due_today:
        return "heute faellig"
    return "geplant"


def comment_line(comment: DocumentComment, *, show_author: bool) -> str:
    """Eine Zeile der Notizen-Sektion: Datum, Dokument, ggf. Autor, Text,
    ggf. Wiedervorlage mit Status.

    Absolute Datumsangaben (`15.08.2026`), nicht relativ -- die Antwort
    soll nicht davon abhaengen, wann der Prompt gebaut wurde.
    """
    created = timezone.localtime(comment.created_at).date()
    line = f"{created.strftime('%d.%m.%Y')} [{comment.document.title}]"
    if show_author and comment.author_id:
        line += f" ({comment.author.get_username()})"
    line += f": {comment.body.strip()}"
    if comment.follow_up_date is not None:
        line += (
            f" -- Wiedervorlage {comment.follow_up_date.strftime('%d.%m.%Y')} "
            f"({followup_label(comment)})"
        )
    return line


def basis_comments(documents) -> list[DocumentComment]:
    """Kommentare der uebergebenen Dokumente, chronologisch (aeltestes
    zuerst).

    Kein eigener Sichtbarkeits-Check: `documents` kommt bereits aus einer
    `visible_to`-gescopten Auswahl, ist also schon auf das begrenzt, was
    der anfragende Nutzer sehen darf *und* was tatsaechlich als
    Dokumentblock im Prompt steht -- ein Kommentar zu einem
    herausgekuerzten Dokument wuerde im Prompt auf einen Titel verweisen,
    der dort gar nicht auftaucht.
    """
    document_ids = [document.pk for document in documents]
    if not document_ids:
        return []
    return list(
        DocumentComment.objects.filter(document_id__in=document_ids)
        .select_related("document", "author")
        .order_by("created_at")
    )


def limited_comments(comments: list[DocumentComment], limit: int) -> list[DocumentComment]:
    """Bei vielen Kommentaren nur die juengsten `limit` behalten -- offene
    Wiedervorlagen bleiben unabhaengig von ihrem Alter immer dabei: ein
    geplanter Termin ist relevanter als eine beilaeufige Notiz von gestern
    (#1132). Die Reihenfolge (chronologisch) bleibt dabei erhalten.
    """
    recent = comments[-limit:] if len(comments) > limit else comments
    kept_ids = {comment.pk for comment in recent}
    kept_ids.update(comment.pk for comment in comments if comment.follow_up_date is not None)
    return [comment for comment in comments if comment.pk in kept_ids]


def build_comment_context(documents, limit: int, *, heading: str) -> list[str]:
    """Der "Notizen des Nutzers"-Abschnitt -- als eigene, testbare Funktion
    gebaut statt inline im Prompt-String, damit sich pruefen laesst, was
    tatsaechlich hineingeht, ohne ein Modell aufzurufen (#1132).

    Bewusst ein eigener, benannter Abschnitt statt Vermischung mit den
    Dokumentbloecken. Fehlen Kommentare komplett, ist das Ergebnis eine
    leere Liste -- der Prompt bleibt dann unveraendert ohne diesen
    Abschnitt.
    """
    comments = limited_comments(basis_comments(documents), limit)
    if not comments:
        return []

    distinct_authors = {comment.author_id for comment in comments if comment.author_id}
    show_author = len(distinct_authors) > 1

    lines = ["", heading]
    lines.extend(comment_line(comment, show_author=show_author) for comment in comments)
    return lines
