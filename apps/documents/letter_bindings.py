"""Daten-Bindungen für Brief-Vorlagen (#1094).

Eine Bindung beantwortet genau eine Frage: *woher kommt der Wert für
diesen Platzhalter?* -- z. B. "``{{empfaenger_adresse}}`` ist die Adresse
des Kontakts, der zum beantworteten Dokument gehört". In v1 sind alle
Quellen Findus-intern (``self`` / ``kontakt`` / ``document`` / ``vorgang``
/ ``heute`` / ``manual``).

Aufgebaut als Registry von *Namespaces* statt als flache Liste fester
Schlüssel: eine Quelle ist ein Präfix (``kontakt``) plus ein Restpfad
(``address``), den der Namespace selbst auflöst. Eine spätere externe
API-Quelle ist damit nur eine weitere `SourceNamespace` mit
freiem Restpfad (`resolve` gesetzt, `fields` leer) -- Model, Formular,
UI und Auflösung bleiben unverändert. Genau deshalb liegt die Schicht
hier und nicht als `choices`-Liste am Model.

Bewusst ohne Import von `models` auf Modulebene: `models` importiert
`validate_source` als Feld-Validator, und die Getter kommen mit
duck-typed Attributen des Kontexts aus. Das Rendern des fertigen
Schreibens ist *nicht* Teil dieser Schicht -- hier entstehen nur Werte
(#4b setzt darauf auf).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.formats import date_format

SOURCE_SEPARATOR = "."


@dataclass
class LetterContext:
    """Was zum Zeitpunkt der Brief-Erzeugung bekannt ist.

    Jedes Feld ist optional -- eine Vorlage kann an einem Vorgang ohne
    Dokument hängen oder an einem Dokument ohne Vorgang. Fehlt die Quelle
    eines Platzhalters, liefert die Auflösung einen leeren String; ob das
    ein Problem ist, entscheidet `missing_required_keys`.
    """

    self_correspondent: object | None = None
    kontakt: object | None = None
    document: object | None = None
    vorgang: object | None = None
    manual_values: dict = field(default_factory=dict)
    today: date | None = None

    def resolve_today(self):
        return self.today or timezone.localdate()


@dataclass(frozen=True)
class SourceField:
    """Ein konkret anwählbarer Wert innerhalb eines Namespace.

    Trägt Label (für die Auswahl in der UI) *und* Getter (für die
    Auflösung) in einer Deklaration -- so können die beiden nicht
    auseinanderlaufen. Der Getter bekommt neben dem Kontext den
    Platzhalter-`key`, den `manual` braucht, um seinen Wert im Kontext zu
    finden.
    """

    path: str
    label: str
    getter: Callable[[LetterContext, str], object]


@dataclass(frozen=True)
class SourceNamespace:
    prefix: str
    label: str
    fields: tuple[SourceField, ...] = ()
    resolve: Callable[[str, LetterContext, str], object] | None = None

    def field_map(self):
        return {source_field.path: source_field for source_field in self.fields}

    def resolve_path(self, path, context, key=""):
        source_field = self.field_map().get(path)
        if source_field is not None:
            return source_field.getter(context, key)
        if self.resolve is None:
            raise UnknownSourceError(
                f"Unbekanntes Feld „{path}“ in Quelle „{self.prefix}“."
            )
        return self.resolve(path, context, key)


class UnknownSourceError(ValueError):
    """Eine Quelle, die keine registrierte Namespace/Feld-Kombination ist."""


_NAMESPACES: dict[str, SourceNamespace] = {}


def register_source_namespace(namespace):
    """Registriert eine Quellen-Gruppe. Der Erweiterungspunkt für spätere,
    nicht-Findus-interne Quellen (externe API o. ä.).
    """
    _NAMESPACES[namespace.prefix] = namespace
    return namespace


def source_namespaces():
    return list(_NAMESPACES.values())


def split_source(source):
    """``"document.keyfacts.amount"`` -> ``("document", "keyfacts.amount")``.

    Nur am *ersten* Punkt getrennt: der Rest gehört dem Namespace, der ihn
    beliebig tief interpretieren darf.
    """
    prefix, separator, path = (source or "").strip().partition(SOURCE_SEPARATOR)
    return prefix, path if separator else ""


def source_value(prefix, path):
    return f"{prefix}{SOURCE_SEPARATOR}{path}" if path else prefix


def get_namespace(source):
    prefix, _ = split_source(source)
    namespace = _NAMESPACES.get(prefix)
    if namespace is None:
        raise UnknownSourceError(f"Unbekannte Quelle „{source}“.")
    return namespace


def validate_source(value):
    """Feld-Validator für `LetterTemplatePlaceholder.source`.

    Liegt hier statt als `choices` am Model, damit eine später
    registrierte Quelle keine Migration braucht.
    """
    try:
        namespace = get_namespace(value)
        _, path = split_source(value)
        namespace.resolve_path(path, LetterContext())
    except UnknownSourceError as error:
        raise ValidationError(str(error)) from error


def source_choices():
    """Gruppierte ``(Gruppe, [(Wert, Label), …])``-Liste für das Select."""
    return [
        (
            namespace.label,
            [
                (source_value(namespace.prefix, source_field.path), source_field.label)
                for source_field in namespace.fields
            ],
        )
        for namespace in _NAMESPACES.values()
        if namespace.fields
    ]


def source_label(source):
    """Menschenlesbares „Gruppe · Feld“ für Listen/Detailansicht."""
    try:
        namespace = get_namespace(source)
    except UnknownSourceError:
        return source
    _, path = split_source(source)
    source_field = namespace.field_map().get(path)
    if source_field is None:
        return f"{namespace.label} · {path}"
    return f"{namespace.label} · {source_field.label}"


def resolve_source(source, context, key=""):
    """Wert einer einzelnen Quelle als String (leer, wenn nicht ermittelbar)."""
    namespace = get_namespace(source)
    _, path = split_source(source)
    return _stringify(namespace.resolve_path(path, context, key))


def resolve_placeholders(template, context):
    """``{Platzhalter-key: Wert}`` für alle Platzhalter einer Vorlage.

    Eine unbekannte Quelle (etwa aus einer Vorlage, deren Quellen-Typ es
    nicht mehr gibt) fällt auf einen leeren Wert zurück, statt die
    Erzeugung des gesamten Schreibens zu sprengen.
    """
    values = {}
    for placeholder in template.placeholders.all():
        try:
            values[placeholder.key] = resolve_source(
                placeholder.source, context, placeholder.key
            )
        except UnknownSourceError:
            values[placeholder.key] = ""
    return values


def missing_required_keys(template, context):
    """Pflicht-Platzhalter, für die (noch) kein Wert da ist -- was die
    Erzeugung in #4b vom Nutzer nachfordern muss.
    """
    values = resolve_placeholders(template, context)
    return [
        placeholder.key
        for placeholder in template.placeholders.all()
        if placeholder.required and not values.get(placeholder.key)
    ]


def build_context(
    *, document=None, vorgang=None, kontakt=None, manual_values=None, today=None
):
    """Baut den `LetterContext` aus dem, was der Aufrufer hat.

    Absender ist immer die **is_self**-Identität (#1030); Empfänger ist
    der explizit übergebene Kontakt, sonst der Kontakt des beantworteten
    Dokuments. Der Import von `models` steckt absichtlich in der Funktion
    (siehe Modul-Docstring).
    """
    from .models import Correspondent

    if kontakt is None and document is not None:
        kontakt = document.correspondent
    if vorgang is None and document is not None:
        vorgang = document.vorgaenge.first()

    return LetterContext(
        self_correspondent=Correspondent.objects.filter(is_self=True).first(),
        kontakt=kontakt,
        document=document,
        vorgang=vorgang,
        manual_values=dict(manual_values or {}),
        today=today,
    )


def _stringify(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    if isinstance(value, date):
        return date_format(value, "DATE_FORMAT")
    return str(value).strip()


def _correspondent_getter(attribute):
    return lambda context, _key="": getattr(context.self_correspondent, attribute, "")


def _kontakt_getter(attribute):
    return lambda context, _key="": getattr(context.kontakt, attribute, "")


def _vorgang_getter(attribute):
    return lambda context, _key="": getattr(context.vorgang, attribute, "")


def _key_fact_getter(name):
    def getter(context, _key=""):
        if context.document is None:
            return ""
        return (context.document.key_facts or {}).get(name, "")

    return getter


# `self.*` -- Absenderblock aus der is_self-Identität (#1030).
register_source_namespace(
    SourceNamespace(
        prefix="self",
        label="Absender (eigene Identität)",
        fields=(
            SourceField("name", "Name", _correspondent_getter("name")),
            SourceField("address", "Adresse", _correspondent_getter("address")),
            SourceField("email", "E-Mail", _correspondent_getter("email")),
            SourceField("vat_id", "USt-IdNr.", _correspondent_getter("vat_id")),
            SourceField("tax_number", "Steuernummer", _correspondent_getter("tax_number")),
            SourceField("iban", "IBAN", _correspondent_getter("iban")),
        ),
    )
)

# `kontakt.*` -- Empfängerblock: Vorgang-Kontakt bzw. Kontakt des
# beantworteten Dokuments (siehe `build_context`).
register_source_namespace(
    SourceNamespace(
        prefix="kontakt",
        label="Empfänger (Kontakt)",
        fields=(
            SourceField("name", "Name", _kontakt_getter("name")),
            SourceField("address", "Adresse", _kontakt_getter("address")),
            SourceField("email", "E-Mail", _kontakt_getter("email")),
        ),
    )
)

# `document.*` -- Bezugsdokument samt Key-Facts (#1020). Die
# `keyfacts.*`-Pfade spiegeln `apps.documents.analysis._KEY_FACT_FIELDS`;
# `test_letter_bindings` hält beide Listen deckungsgleich. Ein eigenes
# Feld für Aktenzeichen/Betreff gibt es dort (noch) nicht -- Betreff deckt
# `document.title` ab, ein Aktenzeichen ist bis dahin ein `manual`-Feld.
register_source_namespace(
    SourceNamespace(
        prefix="document",
        label="Dokument",
        fields=(
            SourceField(
                "title",
                "Titel (Betreff)",
                lambda context, _key="": getattr(context.document, "title", ""),
            ),
            SourceField(
                "date",
                "Dokumentdatum",
                lambda context, _key="": getattr(context.document, "display_date", None),
            ),
            SourceField(
                "keyfacts.sender_name", "Key-Fact: Absender", _key_fact_getter("sender_name")
            ),
            SourceField(
                "keyfacts.sender_email",
                "Key-Fact: Absender E-Mail",
                _key_fact_getter("sender_email"),
            ),
            SourceField(
                "keyfacts.sender_vat_id",
                "Key-Fact: Absender USt-IdNr.",
                _key_fact_getter("sender_vat_id"),
            ),
            SourceField(
                "keyfacts.sender_iban",
                "Key-Fact: Absender IBAN",
                _key_fact_getter("sender_iban"),
            ),
            SourceField(
                "keyfacts.recipient_name",
                "Key-Fact: Empfänger",
                _key_fact_getter("recipient_name"),
            ),
            SourceField(
                "keyfacts.recipient_email",
                "Key-Fact: Empfänger E-Mail",
                _key_fact_getter("recipient_email"),
            ),
            SourceField(
                "keyfacts.recipient_vat_id",
                "Key-Fact: Empfänger USt-IdNr.",
                _key_fact_getter("recipient_vat_id"),
            ),
            SourceField(
                "keyfacts.recipient_iban",
                "Key-Fact: Empfänger IBAN",
                _key_fact_getter("recipient_iban"),
            ),
            SourceField(
                "keyfacts.document_date",
                "Key-Fact: Datum",
                _key_fact_getter("document_date"),
            ),
            SourceField(
                "keyfacts.document_type",
                "Key-Fact: Dokumentart",
                _key_fact_getter("document_type"),
            ),
            SourceField("keyfacts.amount", "Key-Fact: Betrag", _key_fact_getter("amount")),
            SourceField(
                "keyfacts.currency", "Key-Fact: Währung", _key_fact_getter("currency")
            ),
            SourceField(
                "keyfacts.due_date", "Key-Fact: Frist", _key_fact_getter("due_date")
            ),
        ),
    )
)

# `vorgang.*` -- der Vorgang, in dem das Schreiben entsteht (#1040).
register_source_namespace(
    SourceNamespace(
        prefix="vorgang",
        label="Vorgang",
        fields=(
            SourceField("name", "Name", _vorgang_getter("name")),
            SourceField(
                "status",
                "Status",
                lambda context, _key="": (
                    context.vorgang.get_status_display() if context.vorgang else ""
                ),
            ),
            SourceField("description", "Beschreibung", _vorgang_getter("description")),
        ),
    )
)

# `heute` -- pfadlose Quelle: das Datum der Brief-Erzeugung.
register_source_namespace(
    SourceNamespace(
        prefix="heute",
        label="Datum",
        fields=(SourceField("", "Heutiges Datum", lambda context, _key="": context.resolve_today()),),
    )
)

# `manual` -- pfadlos: der Wert wird bei der Erzeugung vom Nutzer
# eingetragen und über den Platzhalter-`key` im Kontext gefunden.
register_source_namespace(
    SourceNamespace(
        prefix="manual",
        label="Manuelle Eingabe",
        fields=(
            SourceField(
                "",
                "Bei der Erzeugung ausfüllen",
                lambda context, key="": context.manual_values.get(key, ""),
            ),
        ),
    )
)
