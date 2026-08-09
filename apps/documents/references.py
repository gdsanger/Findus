"""Kennungen/Referenznummern (#1099): pflegen und exakt verknüpfen.

Der präzise Bruder der semantischen Ähnlichkeit (#1088). Dort entscheidet
ein Schwellwert über „verwandt", hier entscheidet Gleichheit: zwei
Dokumente mit demselben `(type, value_normalized)` tragen dieselbe
Kennung, und das ist -- anders als jede Vektor-Nähe -- ein nahezu
sicherer Zusammenhang. Entsprechend gibt es hier keinen Score, keine
Kappung nach Relevanz und keinen LLM-Call, sondern einen indizierten
Lookup.

Die Extraktion selbst hängt an der KI-Analyse (`apps.documents.analysis`,
#1020) -- kein eigener `generate()`-Call, nur ein weiteres Feld in
derselben Antwort. Was sie liefert, ist korrigierbar: `set_reference()`
legt manuelle Zeilen mit `Source.MANUAL` an, die ein erneuter
Analyse-Lauf nicht anfasst.

Sichtbarkeit: `shared_reference_groups()` zieht die Gegenseite
ausnahmslos durch `Document.objects.visible_to(user)`. Eine geteilte
Kennung ist ein Hinweis auf ein Dokument -- und damit auf dessen Titel,
Kontakt und Datum -- den jemand ohne Sichtbarkeit nicht bekommen darf.
"""

from __future__ import annotations

from typing import Optional

from django.conf import settings
from django.db.models import DateField, Q
from django.db.models.functions import Coalesce, TruncDate

from .models import Document, DocumentReference, normalize_reference_value

# --------------------------------------------------------------------------
# Typ -> Scope: worauf deutet eine Kennung dieser Art?
#
# Schon hier hinterlegt, obwohl in dieser Stufe noch nichts automatisch
# gruppiert wird: die Folge-Runde (Auto-Gruppierung/Vorschläge fürs
# Alt-Archiv) braucht genau diese Unterscheidung -- ein gemeinsames
# Aktenzeichen heißt "gehört in denselben *Vorgang*", eine gemeinsame
# Kundennummer dagegen nur "gehört zum selben *Kontakt*" (dieselbe
# Kundennummer taucht über Jahre in völlig verschiedenen Vorgängen auf).
# Wer das erst später auseinanderhält, baut die Auto-Gruppierung auf einer
# Annahme, die für die halben Kennungsarten falsch ist.
#
# Bewusst *nicht* gemappt: `ihr_zeichen`/`unser_zeichen` (je nach Absender
# mal Fall-, mal Parteikennung -- die Beschriftung allein sagt es nicht)
# und `sonstige` (per Definition unbestimmt). `scope_for_type()` gibt für
# sie `None` zurück; sie bleiben als Verknüpfung im Detail/Graph sichtbar,
# taugen aber nicht als Grundlage für einen automatischen Vorschlag.
# --------------------------------------------------------------------------
SCOPE_VORGANG = "vorgang"
SCOPE_KONTAKT = "kontakt"

REFERENCE_TYPE_SCOPES = {
    # fallbezogen
    DocumentReference.Type.AKTENZEICHEN: SCOPE_VORGANG,
    DocumentReference.Type.FORDERUNGSNUMMER: SCOPE_VORGANG,
    DocumentReference.Type.VERTRAGSNUMMER: SCOPE_VORGANG,
    DocumentReference.Type.BELEGNUMMER: SCOPE_VORGANG,
    DocumentReference.Type.RECHNUNGSNUMMER: SCOPE_VORGANG,
    # parteibezogen
    DocumentReference.Type.KUNDENNUMMER: SCOPE_KONTAKT,
    DocumentReference.Type.IBAN: SCOPE_KONTAKT,
}


def scope_for_type(reference_type: str) -> Optional[str]:
    """`SCOPE_VORGANG`, `SCOPE_KONTAKT` oder None -- siehe
    `REFERENCE_TYPE_SCOPES`.
    """
    return REFERENCE_TYPE_SCOPES.get(reference_type)


def normalize_type(value: object) -> str:
    """Ein KI-gelieferter Typ-String auf eine gültige Choice, sonst
    `SONSTIGE` -- eine unbekannte Kennungsart ist kein Grund, die Kennung
    wegzuwerfen (sie verknüpft weiterhin exakt, nur ohne Scope).
    """
    text = str(value or "").strip().lower()
    return (
        text
        if text in DocumentReference.Type.values
        else DocumentReference.Type.SONSTIGE
    )


def normalize_role(value: object) -> str:
    """Gültige Rolle oder leer -- „unklar" ist ein zulässiger Zustand und
    besser als eine geratene Zuordnung (siehe `DocumentReference.Role`).
    """
    text = str(value or "").strip().lower()
    return text if text in DocumentReference.Role.values else ""


def set_reference(
    document: Document,
    *,
    reference_type: object,
    value: str,
    role: object = "",
    source: str = DocumentReference.Source.MANUAL,
) -> Optional[DocumentReference]:
    """Kennung anlegen oder -- bei gleichem `(type, value_normalized)` --
    aktualisieren. Gibt None zurück, wenn nichts Verwertbares übrig bleibt.

    `get_or_create` statt blindem `create`: dieselbe Kennung zweimal am
    selben Dokument ist keine neue Information, und die UniqueConstraint
    würde den zweiten Versuch ohnehin abweisen. Ein Treffer übernimmt die
    neue Schreibweise/Rolle/Herkunft -- „nochmal eintragen" ist die
    naheliegende Nutzergeste für „korrigieren".
    """
    reference_type = normalize_type(reference_type)
    value_raw = str(value or "").strip()[
        : DocumentReference._meta.get_field("value_raw").max_length
    ]
    value_normalized = normalize_reference_value(value_raw)
    if not value_normalized:
        return None

    reference, created = DocumentReference.objects.get_or_create(
        document=document,
        type=reference_type,
        value_normalized=value_normalized,
        defaults={
            "value_raw": value_raw,
            "role": normalize_role(role),
            "source": source,
        },
    )
    if not created:
        reference.value_raw = value_raw
        reference.role = normalize_role(role)
        reference.source = source
        reference.save(update_fields=["value_raw", "role", "source", "updated_at"])
    return reference


def shared_reference_groups(user, document: Document) -> list[dict]:
    """Je Kennung dieses Dokuments: die anderen sichtbaren Dokumente, die
    exakt dieselbe tragen.

    Gruppiert nach Kennung statt als flache Trefferliste, weil die Kennung
    die *Begründung* ist -- „auch Aktenzeichen 123/45" sagt etwas, „auch
    verwandt" nichts. Trägt ein Dokument zwei gemeinsame Kennungen, steht
    es bewusst in beiden Gruppen.

    Eine Query je Kennung (statt einer großen mit OR-Kette): ein Dokument
    hat eine Handvoll Kennungen, jede Query ist ein Index-Lookup auf
    `(type, value_normalized)`, und nur so lässt sich das Cap je Gruppe
    exakt ziehen und ehrlich als „gekürzt" melden -- eine gemeinsame
    Kappung könnte eine Gruppe komplett verhungern lassen.
    """
    references = list(document.references.all())
    if not references:
        return []

    visible_documents = Document.objects.visible_to(user).select_related(
        "correspondent"
    )
    limit = settings.FINDUS_REFERENCE_MATCH_LIMIT

    groups = []
    for reference in references:
        matches = (
            visible_documents.filter(
                references__type=reference.type,
                references__value_normalized=reference.value_normalized,
            )
            .exclude(pk=document.pk)
            .annotate(
                effective_date=Coalesce(
                    "document_date", TruncDate("created_at"), output_field=DateField()
                )
            )
            .order_by("-effective_date", "-created_at")
            .distinct()
        )
        rows = list(matches[: limit + 1])
        groups.append(
            {
                "reference": reference,
                "documents": rows[:limit],
                "truncated": len(rows) > limit,
            }
        )
    return groups


def shared_reference_matches(user, document: Document) -> list[tuple]:
    """Flache `(other_document, reference)`-Paare für Aufrufer, die keine
    Gruppierung brauchen (Graph-Kanten, #1091) -- je Gegenstelle nur ein
    Paar, mit der ersten gemeinsamen Kennung als Beschriftung.
    """
    seen = set()
    matches = []
    for group in shared_reference_groups(user, document):
        for other in group["documents"]:
            if other.pk in seen:
                continue
            seen.add(other.pk)
            matches.append((other, group["reference"]))
    return matches


def documents_with_reference(user, reference_type: str, value: str):
    """Alle sichtbaren Dokumente mit genau dieser Kennung -- der exakte
    Match als eigenständige Abfrage, unabhängig von einem Ausgangsdokument
    (Grundlage für die spätere Auto-Gruppierung).
    """
    value_normalized = normalize_reference_value(value)
    if not value_normalized:
        return Document.objects.none()
    return (
        Document.objects.visible_to(user)
        .filter(
            Q(references__type=normalize_type(reference_type))
            & Q(references__value_normalized=value_normalized)
        )
        .distinct()
    )
