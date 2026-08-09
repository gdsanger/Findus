"""Kennungen bekommen ein Zuhause -- und ordnen neue Dokumente zu (#1100).

Baut direkt auf #1099 auf: dort wurden Kennungen am *Dokument* typisiert
erfasst und Dokument gegen Dokument exakt verglichen. Hier bekommen sie
eine Gegenseite, die sie besitzt -- der Vorgang seine fallbezogenen
(Aktenzeichen, Forderungs-/Vertragsnummer), der Kontakt seine
parteibezogenen (Kundennummer, IBAN), entlang desselben Typ->Scope-
Mappings (`references.scope_for_type`). Erst damit lässt sich die Frage
beantworten, die #1099 offen ließ: *wohin* gehört ein neues Dokument.

Drei Teile, in dieser Reihenfolge zu lesen:

1. **Lernen** (`learn_references_from_document`): wird ein Dokument einem
   Vorgang/Kontakt zugeordnet, übernimmt das Ziel dessen passende
   Kennungen. Der Bestand pflegt sich dadurch im Vorbeigehen selbst --
   niemand tippt Aktenzeichen ab, und trotzdem kennt der Vorgang seins,
   sobald das erste Schreiben darin liegt.
2. **Vorschlagen** (`assignment_suggestions`): die Kennungen eines
   Dokuments exakt gegen diesen Bestand. Ein Treffer ist ein *Vorschlag*,
   keine Zuordnung -- eine Kennung ist ein Fremdformat, und "123/45" ist
   an zwei verschiedenen Stellen zwei verschiedene Akten.
3. **Automatisch zuordnen** (`auto_assign_from_references`): dieselbe
   Mechanik ohne Klick, aber nur opt-in, nur auf einer starken
   Kennungsart und nur bei genau einem Kandidaten.

Sichtbarkeit: der Vorschlag hängt an `OwnerReference.learned_from`. Eine
Kennung, die ein Vorgang aus einem Dokument gelernt hat, das dieser Nutzer
nicht sehen darf, erzeugt für ihn keinen Vorschlag -- sonst wäre der
Vorschlag ein Fenster auf den Inhalt genau dieses Dokuments ("Vorgang X
trägt dieselbe Forderungsnummer"). Von Hand gepflegte Zeilen haben keine
solche Herkunft und gelten wie die Stammdaten, an denen sie hängen.
"""

from __future__ import annotations

from typing import Optional

from django.conf import settings
from django.db.models import Q

from .models import (
    Correspondent,
    CorrespondentReference,
    Document,
    DocumentReference,
    OwnerReference,
    Vorgang,
    VorgangReference,
    normalize_reference_value,
)
from .references import (
    REFERENCE_TYPE_SCOPES,
    SCOPE_KONTAKT,
    SCOPE_VORGANG,
    normalize_type,
    scope_for_type,
)

# Scope -> (Modell, Feldname des Ziels, Ziel-Modell). Einziger Ort, an dem
# "Vorgang oder Kontakt?" ausbuchstabiert wird -- Lernen, Vorschlagen und
# Zuordnen laufen darüber alle drei über denselben Code, statt jede Regel
# (Dedup, Sichtbarkeit, exakter Match) zweimal zu formulieren.
REFERENCE_OWNERS = {
    SCOPE_VORGANG: (VorgangReference, "vorgang", Vorgang),
    SCOPE_KONTAKT: (CorrespondentReference, "correspondent", Correspondent),
}

SCOPE_LABELS = {SCOPE_VORGANG: "Vorgang", SCOPE_KONTAKT: "Kontakt"}


def scope_of(target) -> Optional[str]:
    """Der Scope eines Ziel-Objekts (Vorgang/Correspondent)."""
    if isinstance(target, Vorgang):
        return SCOPE_VORGANG
    if isinstance(target, Correspondent):
        return SCOPE_KONTAKT
    return None


def types_for_scope(scope: str) -> list[tuple]:
    """Die `(value, label)`-Choices, die an ein Ziel dieses Scopes gehören.

    Die Pflege-Blöcke der Hubs bieten bewusst nur diese an: eine
    Kundennummer am Vorgang wäre keine falsche Eingabe, die man später
    korrigiert -- sie wäre eine *wirkungslose*, weil der Abgleich
    Kundennummern per Typ->Scope-Mapping nur gegen Kontakte laufen lässt.
    Eine Kennungsart, die nirgends matcht, gehört gar nicht erst ins
    Formular.
    """
    return [
        (value, label)
        for value, label in DocumentReference.Type.choices
        if REFERENCE_TYPE_SCOPES.get(value) == scope
    ]


def strong_reference_types() -> set:
    """Kennungsarten, die für die Auto-Zuordnung stark genug sind
    (`FINDUS_REFERENCE_AUTO_ASSIGN_TYPES`).

    "Stark" heißt: der Wert identifiziert die Gegenseite für sich allein,
    ohne dass man wissen muss, wer ihn vergeben hat. Eine IBAN erfüllt das
    global, ein Aktenzeichen praktisch (es steht für genau eine Akte).
    Eine Rechnungs- oder Belegnummer erfüllt es nicht -- "2024/17" vergibt
    jeder Rechnungssteller einmal im Jahr.
    """
    return {
        normalize_type(value)
        for value in settings.FINDUS_REFERENCE_AUTO_ASSIGN_TYPES
    }


def _visible_reference_q(user) -> Q:
    """Welche Ziel-Kennungen darf `user` als Vorschlag zu sehen bekommen?

    Ohne Nutzer (Analyse-Lauf im Worker eines Dokuments ohne Besitzer)
    bleiben nur die herkunftslosen Zeilen -- lieber ein Vorschlag zu wenig
    als einer, der aus einem fremden Dokument stammt.
    """
    if user is None:
        return Q(learned_from__isnull=True)
    return Q(learned_from__isnull=True) | Q(
        learned_from__in=Document.objects.visible_to(user)
    )


def set_owner_reference(
    target,
    *,
    reference_type: object,
    value: str,
    source: str = OwnerReference.Source.MANUAL,
    learned_from: Optional[Document] = None,
):
    """Eine Kennung an einem Vorgang/Kontakt hinterlegen. Gibt None zurück,
    wenn nichts Verwertbares übrig bleibt oder die Kennungsart nicht zu
    diesem Ziel gehört.

    `get_or_create` auf `(ziel, type, value_normalized)`: dieselbe Kennung
    ein zweites Mal ist keine neue Information -- genau das verlangt
    "ohne Dubletten" für den Auto-Lern-Weg, auf dem jedes weitere Dokument
    desselben Vorgangs dieselbe Nummer noch einmal anbietet.

    Eine bestehende Zeile wird *nicht* überschrieben, mit einer Ausnahme:
    trägt jemand von Hand nach, was schon gelernt war, gilt sie danach als
    manuell gepflegt. Andersherum nie -- sonst würde der nächste
    Auto-Lern-Lauf eine Nutzerentscheidung zurück auf "gelernt" stufen.
    """
    scope = scope_of(target)
    if scope is None:
        raise TypeError(f"Kein Kennungs-Ziel: {target!r}")
    model, field, _target_model = REFERENCE_OWNERS[scope]

    reference_type = normalize_type(reference_type)
    if scope_for_type(reference_type) != scope:
        return None

    value_raw = str(value or "").strip()[
        : model._meta.get_field("value_raw").max_length
    ]
    value_normalized = normalize_reference_value(value_raw)
    if not value_normalized:
        return None

    reference, created = model.objects.get_or_create(
        **{field: target},
        type=reference_type,
        value_normalized=value_normalized,
        defaults={
            "value_raw": value_raw,
            "source": source,
            "learned_from": learned_from,
        },
    )
    if (
        not created
        and source == OwnerReference.Source.MANUAL
        and reference.source != OwnerReference.Source.MANUAL
    ):
        reference.source = OwnerReference.Source.MANUAL
        reference.learned_from = None
        reference.save(update_fields=["source", "learned_from", "updated_at"])
    return reference


def _targets_by_scope(document: Document) -> dict:
    return {
        SCOPE_VORGANG: list(document.vorgaenge.all()),
        SCOPE_KONTAKT: [document.correspondent] if document.correspondent_id else [],
    }


def learn_references_from_document(document: Document) -> list:
    """Die Kennungen eines zugeordneten Dokuments an seine Ziele
    übernehmen: fallbezogene an jeden verknüpften Vorgang, parteibezogene
    an den Kontakt.

    Der Grund, warum das automatisch passiert und nicht als Pflege-Aufgabe
    beim Nutzer landet: die Zuordnung eines Dokuments *ist* bereits die
    Aussage "diese Nummern gehören hierher". Ein zweiter Handgriff, der
    dieselbe Aussage noch einmal abtippt, würde in der Praxis unterbleiben
    -- und dann bliebe der Abgleich für neue Post leer.

    Kennungen ohne Scope ("Ihr/Unser Zeichen", "Sonstige") lernt niemand:
    sie zeigen weder verlässlich auf den Fall noch auf die Partei, und ein
    daraus abgeleiteter Vorschlag wäre geraten. Als Verknüpfung zwischen
    Dokumenten (#1099) bleiben sie unverändert sichtbar.
    """
    references = list(document.references.all())
    if not references:
        return []

    targets_by_scope = _targets_by_scope(document)
    learned = []
    for reference in references:
        scope = scope_for_type(reference.type)
        for target in targets_by_scope.get(scope, ()):
            row = set_owner_reference(
                target,
                reference_type=reference.type,
                value=reference.value_raw,
                source=OwnerReference.Source.LEARNED,
                learned_from=document,
            )
            if row is not None:
                learned.append(row)
    return learned


def assignment_suggestions(user, document: Document) -> list[dict]:
    """"Gehört vermutlich zu Vorgang X" -- die Kennungen dieses Dokuments
    exakt gegen die hinterlegten Kennungen aller Vorgänge/Kontakte.

    Vorschlag statt Automatik: der Match ist zwar exakt, aber die Kennung
    selbst ist es nicht zwingend -- sie stammt aus einem Fremdsystem,
    dessen Nummernkreis Findus nicht kennt, und aus einer Extraktion, die
    sich verlesen kann. Wer zuordnet, entscheidet; das System bereitet die
    Entscheidung nur so weit vor, dass ein Klick reicht.

    Was hier bewusst *nicht* vorgeschlagen wird:

    * ein bereits zugeordneter Vorgang -- der Vorschlag wäre ein No-Op,
    * irgendein Kontakt, sobald das Dokument schon einen hat: ein gesetzter
      Kontakt ist eine getroffene Entscheidung (dieselbe Regel wie in der
      KI-Analyse, die einen vorhandenen `correspondent` nie überschreibt),
      und "zuordnen" hieße hier *ersetzen*, nicht ergänzen.

    Je Ziel entsteht höchstens ein Eintrag, auch wenn zwei Kennungen
    gleichzeitig darauf zeigen -- zweimal derselbe Knopf ist kein
    doppelter Hinweis.
    """
    references = list(document.references.all())
    if not references:
        return []

    assigned_vorgang_ids = set(document.vorgaenge.values_list("id", flat=True))
    strong_types = strong_reference_types()
    visible = _visible_reference_q(user)

    suggestions = []
    seen = set()
    for reference in references:
        scope = scope_for_type(reference.type)
        if scope is None:
            continue
        if scope == SCOPE_KONTAKT and document.correspondent_id is not None:
            continue
        model, field, _target_model = REFERENCE_OWNERS[scope]
        matches = (
            model.objects.filter(
                type=reference.type, value_normalized=reference.value_normalized
            )
            .filter(visible)
            .select_related(field)
        )
        for match in matches:
            target = getattr(match, field)
            if scope == SCOPE_VORGANG and target.pk in assigned_vorgang_ids:
                continue
            key = (scope, target.pk)
            if key in seen:
                continue
            seen.add(key)
            suggestions.append(
                {
                    "reference": reference,
                    "scope": scope,
                    "scope_label": SCOPE_LABELS[scope],
                    "target": target,
                    "strong": reference.type in strong_types,
                }
            )
    return suggestions


def assign_suggested(document: Document, suggestion: dict) -> None:
    """Einen Vorschlag umsetzen -- und sofort daraus lernen.

    Das Lernen gehört hier direkt dran: mit der Zuordnung wird dieses
    Dokument selbst zur Quelle: seine übrigen fallbezogenen Kennungen
    gehören ab jetzt genauso zum Ziel wie die, über die der Vorschlag
    zustande kam. Das nächste Schreiben derselben Sache trifft damit auch
    dann, wenn es nur die andere Nummer trägt.
    """
    target = suggestion["target"]
    if suggestion["scope"] == SCOPE_VORGANG:
        document.vorgaenge.add(target)
    else:
        document.correspondent = target
        document.save(update_fields=["correspondent", "updated_at"])
    learn_references_from_document(document)


def auto_assign_from_references(document: Document, user=None) -> list[dict]:
    """Opt-in-Auto-Zuordnung (`FINDUS_REFERENCE_AUTO_ASSIGN`, Default aus).

    Drei Bedingungen, alle drei nötig:

    * der Schalter steht an -- Default aus, weil eine falsch zugeordnete
      Akte teurer ist als eine ungeordnete: der Fehler ist unsichtbar, das
      Dokument liegt ja *irgendwo*;
    * die Kennung ist eine starke (`strong_reference_types`);
    * es gibt genau einen Kandidaten in diesem Scope. Bei zwei Treffern
      bleibt es beim Vorschlag -- "eindeutig" ist die eigentliche
      Leitplanke, nicht die Trefferzahl.

    Ein bereits gesetzter Kontakt wird auch hier nie ersetzt (siehe
    `assignment_suggestions`).
    """
    if not settings.FINDUS_REFERENCE_AUTO_ASSIGN:
        return []

    strong = [
        suggestion
        for suggestion in assignment_suggestions(user, document)
        if suggestion["strong"]
    ]

    applied = []
    for scope in (SCOPE_VORGANG, SCOPE_KONTAKT):
        candidates = {
            suggestion["target"].pk: suggestion
            for suggestion in strong
            if suggestion["scope"] == scope
        }
        if len(candidates) != 1:
            continue
        suggestion = next(iter(candidates.values()))
        assign_suggested(document, suggestion)
        applied.append(suggestion)
    return applied
