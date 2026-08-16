import datetime
import mimetypes
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.formats import date_format
from pgvector.django import HnswIndex, VectorField

from apps.accounts.models import Department

from . import document_dates
from .letter_bindings import source_label, validate_source

_HEX_COLOR_VALIDATOR = RegexValidator(
    regex=r"^#[0-9a-fA-F]{6}$", message="Farbe muss ein Hex-Code sein, z. B. #a1b2c3."
)

# Sentinel for `DocumentQuerySet.create_child()` -- distinguishes "caller
# didn't pass this" (inherit from parent) from an explicit override of
# `None`/empty, which `departments=[]` or `owner=None` must still be able to
# express.
_UNSET = object()


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Correspondent(TimeStampedModel):
    """Absender/Empfänger eines Dokuments (z. B. Firma oder Person).

    `is_self` markiert eine eigene Identität ("das bin ich") -- mehrere
    erlaubt, z. B. eine Firma und eine Privatperson. Die KI-Analyse
    (#1030, apps.documents.analysis) nutzt das, um die Dokumentrichtung
    abzuleiten: Empfänger ist `is_self` -> Eingang, Aussteller ist
    `is_self` -> Ausgang. `vat_id`/`tax_number`/`iban` sind fuer
    verlaessliches Matching gedacht -- ein Name allein ist nicht
    eindeutig (OCR-Schreibweisen, Tochterfirmen etc.), waehrend eine
    USt-IdNr/IBAN eine Identitaet robust identifiziert. Gilt fuer alle
    Correspondents, nicht nur Self-Identitaeten, da es generell beim
    Matching/Dedup hilft.

    `is_own_business` markiert eine Self-Identität zusätzlich als eigenes
    Gewerbe/Firma (#1112) -- bewusst additiv *neben* `is_self`, nicht als
    dritter Wert eines Enums, damit die bestehende Richtungs-Ableitung aus
    `is_self` (#1030) unberührt bleibt: `is_self=True`+`is_own_business=True`
    = "meine Firma", `is_self=True`+`is_own_business=False` = "ich privat".
    Signalquelle für `Document.sphere` (geschäftlich vs. privat) -- eine
    fachliche Klassifikation, die bewusst nichts mit dem Sichtbarkeits-/
    Sicherheitsmodell (`Document.visibility`/`departments`) zu tun hat.
    """

    name = models.CharField(max_length=255, unique=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=64, blank=True)
    address = models.TextField(blank=True, default="")
    is_self = models.BooleanField(default=False, db_index=True)
    is_own_business = models.BooleanField(default=False, db_index=True)
    vat_id = models.CharField(max_length=32, blank=True)
    tax_number = models.CharField(max_length=32, blank=True)
    iban = models.CharField(max_length=34, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class LongSummaryMixin(models.Model):
    """Ausfuehrliche Zusammenfassung (#1135): ein zweites, laengeres
    KI-Artefakt neben der bestehenden Kurzzusammenfassung -- Fliesstext
    statt drei Saetzen, auf Knopfdruck erzeugt und dauerhaft gespeichert,
    geteilt zwischen `Document` und `Vorgang` statt zweimal denselben
    Feldsatz zu pflegen.

    Direkt auf dem Besitzer-Model, kein eigenes Run-Model wie
    `VorgangRecommendationRun`: hier gibt es keine strukturierten
    Kind-Zeilen (Empfehlungen mit eigenem Status/Quellen), nur Text plus
    Provenienz -- ein zweites Model waere hier nur Overhead.

    `ai_model`/`ai_model_version` sind dieselbe Herkunftsangabe wie bei
    `Chunk.embedding_model`/`_version`: ohne sie laesst sich spaeter nicht
    beurteilen, wie viel ein alter Text noch wert ist.

    `based_on` haelt fest, worauf die Erzeugung fusste (siehe
    `apps.documents.long_summary` fuer die genaue Struktur je Ebene) --
    Grundlage des Veraltet-Hinweises. `run_started_at` ist reine
    Job-Buchfuehrung fuers Stall-Erkennen (analog
    `recommendations.expire_if_stalled`), bewusst getrennt von
    `updated_at`: dieses Feld ist bei Document/Vorgang bereits mit
    inhaltlichen Aenderungen belegt (Text, Meta, ...), ein Bump durch den
    bloßen Klick auf "erstellen" wuerde die Veraltet-Erkennung verfaelschen
    -- deshalb speichert `long_summary_*` nie `updated_at` mit.
    """

    class LongSummaryStatus(models.TextChoices):
        NONE = "none", "Nicht erstellt"
        RUNNING = "running", "Wird erstellt"
        READY = "ready", "Fertig"
        FAILED = "failed", "Fehlgeschlagen"

    long_summary = models.TextField(blank=True)
    long_summary_status = models.CharField(
        max_length=20, choices=LongSummaryStatus.choices, default=LongSummaryStatus.NONE
    )
    long_summary_generated_at = models.DateTimeField(null=True, blank=True)
    long_summary_based_on = models.JSONField(default=dict, blank=True)
    long_summary_error = models.TextField(blank=True)
    long_summary_ai_model = models.CharField(max_length=100, blank=True)
    long_summary_ai_model_version = models.CharField(max_length=50, blank=True)
    long_summary_run_started_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    @property
    def long_summary_is_running(self):
        return self.long_summary_status == self.LongSummaryStatus.RUNNING

    @property
    def has_long_summary(self):
        """True sobald schon einmal ein Ergebnis eingetroffen ist --
        `long_summary_status` kann waehrenddessen laengst wieder `running`
        sein (erneutes Erzeugen laesst den alten Text bis dahin stehen).
        """
        return self.long_summary_generated_at is not None


class Vorgang(TimeStampedModel, LongSummaryMixin):
    """Aktenvorgang/Angelegenheit -- fachlich kein "Projekt".

    `department` ist eine einzelne, fachliche Zuordnung (welche Abteilung
    diesen Vorgang fuehrt) -- anders als das `departments`-M2M auf
    Document/Task, das eine Sichtbarkeits-*Scope* ist (mehrere Abteilungen
    duerfen mitlesen). Hier geht es nicht um Sichtbarkeit: Vorgang hat
    weiterhin kein `visibility`-Feld und wird nicht nach Abteilung gefiltert.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Offen"
        IN_PROGRESS = "in_progress", "In Bearbeitung"
        CLOSED = "closed", "Abgeschlossen"

    name = models.CharField(max_length=255, unique=True)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL, null=True, blank=True, related_name="vorgaenge"
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "Vorgang"
        verbose_name_plural = "Vorgänge"

    def __str__(self):
        return self.name


class Tag(TimeStampedModel):
    """Freies Schlagwort, optional einer Dimension zugeordnet (z. B.

    Dimension "Thema" mit Tag "Rechnung", Dimension "Priorität" mit Tag
    "Dringend") — erlaubt facettierte Tag-Gruppen statt einer flachen Liste.
    """

    name = models.CharField(max_length=100)
    dimension = models.CharField(max_length=100, blank=True)
    color = models.CharField(max_length=7, blank=True, validators=[_HEX_COLOR_VALIDATOR])

    class Meta:
        ordering = ["dimension", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["name", "dimension"], name="unique_tag_name_dimension"
            ),
        ]

    def __str__(self):
        return f"{self.dimension}:{self.name}" if self.dimension else self.name


class DocumentQuerySet(models.QuerySet):
    def visible_to(self, user):
        """Scope documents by the minimal two-level visibility model.

        Genau zwei Ebenen (siehe Architektur.md, "Sichtbarkeitsmodell"):
        Abteilung sieht alles · privat = nur Besitzer. RLS bewusst noch
        nicht — Durchsetzung läuft rein über diesen Manager/QuerySet. Gilt
        unverändert für Unterdokumente (#1069) -- die Baum-Struktur ändert
        nichts an dieser Query, sie filtert weiter nur nach
        `visibility`/`owner`/`departments`, egal ob der Treffer ein
        Leitdokument oder ein Unterdokument ist.
        """
        if user.is_superuser:
            return self
        return self.filter(
            models.Q(
                visibility=Document.Visibility.DEPARTMENT,
                departments__in=user.departments.all(),
            )
            | models.Q(
                visibility=Document.Visibility.PRIVATE,
                owner=user,
            )
        ).distinct()

    def roots(self):
        """Leitdokumente (kein `parent`) -- was Eingang/Browsing (#1069) als

        eigene Zeile zeigt; Unterdokumente hängen eingeklappt darunter.
        """
        return self.filter(parent__isnull=True)

    def open_visible_to(self, user):
        """Sichtbare, noch offene Dokumente (#1129/#1130) -- gemeinsame Basis

        der Wiedervorlagen-Auswahl in Wiedervorlagen-Ansicht, Nav-Badge
        (`context_processors.due_follow_up_count`) und Dashboard: Termine
        erledigter Dokumente sollen nirgends auftauchen, auch nicht als
        "überfällig" (Begründung siehe `DocumentFollowUpViewTests`, #1129).
        """
        return self.visible_to(user).exclude(action_status=Document.ActionStatus.DONE)

    def children_of(self, document):
        return self.filter(parent=document)

    def create_child(
        self,
        parent,
        *,
        child_role="",
        owner=_UNSET,
        visibility=_UNSET,
        departments=_UNSET,
        **fields,
    ):
        """Create a Unterdokument unter `parent` (#1069).

        Erbt per Default den Scope (`owner`/`visibility`/`departments`) vom
        Leitdokument -- ein Anhang kann inhaltlich keinen anderen
        Sichtbarkeitsbereich haben als sein Anschreiben. Jedes der drei
        Scope-Felder bleibt einzeln überschreibbar für den Einzelfall, der
        einen abweichenden Scope braucht.
        """
        document = self.model(
            parent=parent,
            child_role=child_role,
            owner=parent.owner if owner is _UNSET else owner,
            visibility=parent.visibility if visibility is _UNSET else visibility,
            **fields,
        )
        document.save()
        document.departments.set(
            parent.departments.all() if departments is _UNSET else departments
        )
        return document


class Document(TimeStampedModel, LongSummaryMixin):
    class Visibility(models.TextChoices):
        DEPARTMENT = "department", "Abteilung"
        PRIVATE = "private", "Privat"

    class ProcessingStatus(models.TextChoices):
        """pending -> extracting (#1009, Text-Layer/OCR/Vision-Kaskade) ->
        analyzing (#1020, KI-Analyse auf dem extrahierten Text) ->
        embedding (#1010, Chunking + Embeddings) -> ready, or failed at
        extraction/embedding with `processing_error` set. A fehlschlagende
        KI-Analyse legt die Pipeline NICHT lahm (siehe
        apps.documents.analysis) -- `analyzing` geht immer nach
        `embedding` weiter, ggf. nur ohne Zusammenfassung/Key-Facts/
        Vorschläge.
        """

        PENDING = "pending", "Ausstehend"
        EXTRACTING = "extracting", "Extraktion läuft"
        ANALYZING = "analyzing", "Analyse läuft"
        EMBEDDING = "embedding", "Indizierung läuft"
        READY = "ready", "Bereit"
        FAILED = "failed", "Fehlgeschlagen"

    class ExtractionMethod(models.TextChoices):
        """Which cascade stage (apps.documents.extraction, #1009) actually
        produced `text_content` -- provenance for how much to trust it.
        """

        TEXT_LAYER = "text_layer", "Text-Layer"
        OCR = "ocr", "OCR"
        VISION = "vision", "Vision AI"

    class ContentFormat(models.TextChoices):
        """Ist `text_content` durchgängiger Text oder strukturerhaltendes
        Markdown (#1149)? Bewusst getrennt von `ExtractionMethod`: dessen
        `vision`-Wert kommt auch von der automatischen Kaskade
        (`extraction._extract_page_via_cascade`), die weiterhin flachen
        Text transkribiert -- nur die manuelle KI-Vision-Neuextraktion legt
        echte Markdown-Tabellen an. Downstream-Code (Chunking, Anzeige)
        muss beide Formen kennen, siehe CLAUDE.md.
        """

        TEXT = "text", "Text"
        MARKDOWN = "markdown", "Markdown"

    class VisionReextractionStatus(models.TextChoices):
        """Eigenes Statusfeld fuer die manuelle KI-Vision-Neuextraktion
        (#1143) -- bewusst getrennt von `processing_status`: ein
        Fehlschlag hier darf ein sonst `ready` Dokument nicht als
        `failed` erscheinen lassen (der bisherige Text bleibt ja
        stehen, siehe `apps.documents.extraction.
        reextract_document_with_vision`). Gleiche Bauart wie
        `LongSummaryMixin.LongSummaryStatus`: ein On-Demand-Zusatzlauf,
        kein Pipeline-Schritt.
        """

        NONE = "none", "Nicht ausgeführt"
        RUNNING = "running", "Läuft"
        READY = "ready", "Fertig"
        FAILED = "failed", "Fehlgeschlagen"

    class Source(models.TextChoices):
        """Woher die Datei kam. `generiert_brief` ist der einzige Wert, den
        Findus selbst erzeugt (#1095): ein aus einer Brief-Vorlage
        generiertes, vom Nutzer freigegebenes Schreiben. Bewusst ein
        eigener Wert und kein `upload`: die Herkunft "das haben wir hier
        geschrieben" ist etwas anderes als "jemand hat eine Datei
        hochgeladen" -- danach lässt sich filtern, und die Pipeline
        behandelt es anders (keine Extraktion/KI-Analyse, die Werte sind
        bekannt).
        """

        UPLOAD = "upload", "Upload"
        MAIL = "mail", "E-Mail"
        FOLDER = "folder", "Ordner-Überwachung"
        API = "api", "API"
        GENERATED_LETTER = "generiert_brief", "Generiert (Brief)"

    class Direction(models.TextChoices):
        """Ging das Dokument an mich (Eingang) oder kam es von mir
        (Ausgang)? Bewusst allgemein gehalten (#1030) -- Rechnungen sind
        der Hauptfall, aber auch Briefe/Vertraege sind "an mich/von mir".
        Von der KI-Analyse aus dem erkannten Aussteller/Empfaenger
        gegenueber `Correspondent.is_self` abgeleitet, bleibt aber ein
        normales, vom Nutzer ueberschreibbares Feld.
        """

        EINGANG = "eingang", "Eingang"
        AUSGANG = "ausgang", "Ausgang"
        INTERN = "intern", "Intern"
        UNBEKANNT = "unbekannt", "Unbekannt"

    class Sphere(models.TextChoices):
        """Geschäftlich (Gewerbe) oder privat? (#1112) -- eine *fachliche*
        Klassifikation jedes Dokuments, bewusst orthogonal zum
        Sichtbarkeits-/Sicherheitsmodell (`visibility`/`departments`, "genau
        zwei Ebenen"): geschäftlich-vs-privat sagt nichts darüber aus, wer
        das Dokument sehen darf, und die Abteilung dafür zu missbrauchen
        würde Zugriff mit Semantik vermischen. Von der KI-Analyse aus der
        adressierten Self-Identität vorbelegt (ist die eigene Seite eine
        `Correspondent.is_own_business`-Identität bzw. trägt sie eine
        USt-IdNr -> `geschaeftlich`), bleibt aber ein normales, vom Nutzer
        überschreibbares Feld -- `unbekannt` ist der ehrliche Default, wenn
        sich nichts ableiten lässt.
        """

        GESCHAEFTLICH = "geschaeftlich", "Geschäftlich"
        PRIVAT = "privat", "Privat"
        UNBEKANNT = "unbekannt", "Unbekannt"

    class TaxRelevance(models.TextChoices):
        """Ist dieser **private** Beleg in der Einkommensteuererklärung
        absetzbar? (#1113) -- bewusst *eng* gefasst: gemeint ist
        ausschließlich die private ESt-Absetzbarkeit (Werbungskosten,
        haushaltsnahe Dienstleistungen/Handwerker §35a, Spenden,
        Versicherungs-/Vorsorgebeiträge, außergewöhnliche Belastungen,
        Kinderbetreuung ...), NICHT die betriebliche Absetzbarkeit.

        Ein geschäftlicher (Gewerbe-)Beleg ist zwar als Betriebsausgabe
        immer steuerlich relevant -- aber das ist ein *anderes*, späteres
        Merkmal (eigenes Issue), nicht dieses Feld. Deshalb der eigene Wert
        `NICHT_ZUTREFFEND`: erkennt die Analyse einen geschäftlichen Beleg
        (`Document.sphere == geschaeftlich`), wird er ausdrücklich *nicht*
        fälschlich `ja`, sondern `nicht_zutreffend` -- so bleibt "privat
        absetzbar" sauber von der betrieblichen Relevanz getrennt.

        `VIELLEICHT` ist der ehrliche Hedge für unsichere Fälle (lieber das
        als eine Fehlklassifikation), `UNBEKANNT` der Default vor/ohne
        Analyse. Von der KI vorbelegt, danach nach dem "einmal befuellen,
        nie ungefragt ueberschreiben"-Muster von `direction`/`sphere`
        behandelt -- eine Nutzerkorrektur überlebt jede Re-Analyse. Die
        Einordnung ist eine KI-Einschätzung, keine Steuerberatung.
        """

        JA = "ja", "Ja"
        NEIN = "nein", "Nein"
        VIELLEICHT = "vielleicht", "Vielleicht"
        NICHT_ZUTREFFEND = "nicht_zutreffend", "Nicht zutreffend"
        UNBEKANNT = "unbekannt", "Unbekannt"

    # Sentinel-Filterwert der Dokumentliste (#1113): "privat absetzbar"
    # bündelt Ja UND Vielleicht in einer Auswahl -- der Nutzer will die
    # potenziell absetzbaren Belege gesammelt sehen, nicht zwei Filterläufe
    # fahren. Bewusst *kein* eigener Enum-Wert, sondern nur ein Filter-
    # Kürzel: es ist keine Klassifikation eines Belegs, sondern eine Sicht
    # auf zwei davon.
    TAX_RELEVANCE_DEDUCTIBLE_FILTER = "absetzbar"
    TAX_RELEVANCE_DEDUCTIBLE_VALUES = (TaxRelevance.JA, TaxRelevance.VIELLEICHT)

    class ActionStatus(models.TextChoices):
        """Muss noch jemand etwas mit diesem Dokument tun? (#1057) --

        bewusst getrennt von `ProcessingStatus`: jenes ist die technische
        Pipeline (Extraktion/Analyse/Indizierung), dies hier ist ein
        fachlicher, vom Nutzer gesetzter Haken ("erledigt"/"offen"), den
        auch ein `ready`-Dokument noch braucht.
        """

        NONE = "keine", "Kein Handlungsbedarf"
        OPEN = "offen", "Offen"
        DONE = "erledigt", "Erledigt"

    class Kind(models.TextChoices):
        """Woraus dieses Dokument entstanden ist (#1070). `mail_body` ist ein

        aus dem E-Mail-Text erzeugtes Leitdokument (bereinigter Klartext für
        den Index + generiertes PDF für die Ansicht) -- im Gegensatz zu
        `document`, dem Normalfall einer eingegangenen/hochgeladenen Datei.
        Trägt die "aus Body erzeugt"-Kennzeichnung (Badge im Detail) und
        erlaubt es dem Mail-Ingest, ein bereits erfasstes Body-Leitdokument
        per Message-ID wiederzuerkennen (Dedup), statt es doppelt anzulegen.
        """

        DOCUMENT = "document", "Dokument"
        MAIL_BODY = "mail_body", "Mail-Text"

    class ChildRole(models.TextChoices):
        """Semantik der Kante zu `parent` (#1069) -- hält fest, *warum* ein

        Dokument unter seinem Leitdokument hängt, damit ein automatisch
        erzeugter Mail-Anhang später von einer manuell gesetzten
        Verschachtelung unterscheidbar bleibt. Leer/`""` = keine
        Automatik, jemand hat die Zuordnung von Hand gesetzt.
        """

        MAIL_ATTACHMENT = "mail_attachment", "Mail-Anhang"
        EMBEDDED = "embedded", "Eingebettet"
        NACHTRAG = "nachtrag", "Nachtrag"
        VERSION = "version", "Version"

    title = models.CharField(max_length=255)
    kind = models.CharField(
        max_length=20, choices=Kind.choices, default=Kind.DOCUMENT, db_index=True
    )
    correspondent = models.ForeignKey(
        Correspondent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
    )
    # Leitdokument-Hierarchie (#1069): selbst-referenzierend statt eines
    # generischen Querverweis-Modells, weil sich Mail-Anschreiben+Anhänge,
    # Brief+Anlagen, Vertrag+Nachträge etc. alle mit demselben 1:n-Baum
    # abbilden lassen. CASCADE, nicht SET_NULL/PROTECT: ein Unterdokument
    # ohne sein Leitdokument wäre eine verwaiste Karteikarte ohne erklärten
    # Scope (der ja vom Leitdokument geerbt wird, siehe `create_child`) --
    # und PROTECT würde jedes Löschen eines Leitdokuments blockieren,
    # solange Kinder existieren, ohne dass das einen Vorteil brächte, da
    # `document_delete` ohnehin erst nach einer expliziten Nutzer-
    # Bestätigung feuert.
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
    )
    child_role = models.CharField(
        max_length=20, choices=ChildRole.choices, blank=True, default=""
    )
    vorgaenge = models.ManyToManyField(Vorgang, blank=True, related_name="documents")
    tags = models.ManyToManyField(Tag, blank=True, related_name="documents")

    # Sichtbarkeit: owner + departments (n:n) + visibility-Schalter.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_documents",
    )
    departments = models.ManyToManyField(
        Department, blank=True, related_name="documents"
    )
    visibility = models.CharField(
        max_length=20,
        choices=Visibility.choices,
        default=Visibility.DEPARTMENT,
    )

    source = models.CharField(
        max_length=20, choices=Source.choices, default=Source.UPLOAD
    )
    processing_status = models.CharField(
        max_length=20,
        choices=ProcessingStatus.choices,
        default=ProcessingStatus.PENDING,
    )
    direction = models.CharField(
        max_length=20,
        choices=Direction.choices,
        default=Direction.UNBEKANNT,
        db_index=True,
    )
    action_status = models.CharField(
        max_length=20,
        choices=ActionStatus.choices,
        default=ActionStatus.NONE,
        db_index=True,
    )
    # Sphäre (#1112): geschäftlich (Gewerbe) vs. privat -- ein eigenes,
    # fachliches Feld, NICHT die Abteilung (siehe `Sphere`-Docstring).
    # Von der KI vorbelegt, danach nach dem "einmal befuellen, nie
    # ungefragt ueberschreiben"-Muster von `direction`/`document_date`
    # behandelt: `_apply_analysis` setzt es nur, solange es noch
    # `unbekannt` ist, damit eine erneute Analyse eine bereits gesetzte
    # (KI- oder Nutzer-)Wahl nicht wieder umwirft.
    sphere = models.CharField(
        max_length=20,
        choices=Sphere.choices,
        default=Sphere.UNBEKANNT,
        db_index=True,
    )
    # Private ESt-Absetzbarkeit (#1113): ausschließlich "kann ich diesen
    # *privaten* Beleg in der Einkommensteuererklaerung absetzen?" -- ein
    # eigenes, filter-/auswertbares Feld (nicht nur `key_facts`-JSON), damit
    # sich die absetzbaren Privatbelege spaeter gesammelt bearbeiten/
    # exportieren lassen. Von der KI vorbelegt, danach wie `sphere` nach dem
    # "einmal befuellen, nie ungefragt ueberschreiben"-Muster behandelt
    # (siehe `TaxRelevance`-Docstring). Geschaeftliche Belege werden
    # `nicht_zutreffend`, nie faelschlich `ja` -- betriebliche Absetzbarkeit
    # ist ein separates Merkmal. `tax_relevance_reason` traegt die kurze
    # KI-Begruendung (z. B. "Handwerkerrechnung mit Lohnanteil -> §35a EStG").
    tax_relevance = models.CharField(
        max_length=20,
        choices=TaxRelevance.choices,
        default=TaxRelevance.UNBEKANNT,
        db_index=True,
    )
    tax_relevance_reason = models.TextField(blank=True)

    # Extraktion + Cache.
    text_content = models.TextField(blank=True)
    markdown = models.TextField(blank=True)
    extraction_method = models.CharField(
        max_length=20, choices=ExtractionMethod.choices, blank=True
    )
    content_format = models.CharField(
        max_length=20, choices=ContentFormat.choices, default=ContentFormat.TEXT
    )
    metadata = models.JSONField(default=dict, blank=True)
    processing_error = models.TextField(blank=True)

    # Manuelle KI-Vision-Neuextraktion (#1143): eigene Provenienz- und
    # Job-Felder statt `metadata` -- anders als `extracted_at`/`page_count`
    # (reine Extraktions-Provenienz) braucht dieser Zusatzlauf ein
    # abfragbares Statusfeld fuers Polling/Retry der UI, analog
    # `LongSummaryMixin`. `_run_started_at` ist reine Job-Buchfuehrung
    # fuers Stall-Erkennen, bewusst getrennt von `updated_at` (dasselbe
    # Argument wie beim `LongSummaryMixin`-Docstring). Ein nachfolgender
    # regulaerer Reprocess setzt diese Felder ueber
    # `extraction.extract_document()` wieder zurueck, damit die Anzeige nie
    # einen Vision-Lauf behauptet, dessen Text laengst ueberschrieben ist.
    vision_reextraction_status = models.CharField(
        max_length=20,
        choices=VisionReextractionStatus.choices,
        default=VisionReextractionStatus.NONE,
    )
    vision_reextraction_error = models.TextField(blank=True)
    vision_reextraction_run_started_at = models.DateTimeField(null=True, blank=True)
    vision_reextraction_completed_at = models.DateTimeField(null=True, blank=True)
    vision_reextraction_pages_processed = models.PositiveIntegerField(null=True, blank=True)
    vision_reextraction_pages_total = models.PositiveIntegerField(null=True, blank=True)
    vision_reextraction_truncated = models.BooleanField(default=False)
    # Wie viele der verarbeiteten Seiten technisch fehlgeschlagen sind (#1149,
    # z. B. Provider-Timeout auf einer einzelnen Seite) -- getrennt von einer
    # Seite, die das Modell selbst als "leer/unlesbar" transkribiert: Letzteres
    # ist ein erfolgreiches Ergebnis, Ersteres ein Platzhalter, weil die Seite
    # gar nicht erst beim Modell ankam/beantwortet wurde. Ein Fehlschlag auf
    # *manchen* Seiten entwertet die anderen nicht (siehe
    # `extraction.reextract_document_with_vision`); erst wenn jede Seite
    # fehlschlägt, gilt der gesamte Lauf als gescheitert.
    vision_reextraction_pages_failed = models.PositiveIntegerField(null=True, blank=True)
    # Modell/-version des zuletzt erfolgreichen Laufs (#1149) -- anders als
    # `Chunk.embedding_model`/`_version` gab es dafür bislang kein Feld, obwohl
    # CLAUDE.md ("Hintergrundjobs mit LLM-Aufruf") Herkunft grundsätzlich
    # verlangt. Kommt aus der `VisionResult` der ersten erfolgreichen Seite
    # dieses Laufs, nicht aus der Konfiguration -- so bleibt es auch dann
    # korrekt, wenn ein Provider ein von der Konfiguration abweichendes Modell
    # zurückmeldet.
    vision_reextraction_model = models.CharField(max_length=100, blank=True)
    vision_reextraction_model_version = models.CharField(max_length=100, blank=True)
    # Idempotenz-Merker (#1149): `sha256` der Originaldatei plus
    # `extraction._VISION_MARKDOWN_PROMPT_VERSION`, zusammen der einzige
    # Vergleichswert, der einen erneuten Klick ohne inhaltliche Änderung
    # kostenlos macht (kein Modellaufruf). Bewusst OHNE das verwendete Modell
    # -- eine reine Konfigurationsänderung (Modellwechsel) soll nicht
    # automatisch den ganzen Bestand neu durch Vision schicken, dafür gibt es
    # den expliziten "erneut ausführen"-Weg (`force=True`). Wird NUR nach
    # einem vollständigen Erfolg gesetzt (keine Seitengrenze erreicht, keine
    # Seite technisch fehlgeschlagen) -- ein unvollständiger Lauf bleibt sonst
    # ohne `force` unwiederholbar, weil er sich selbst als "aktuell" ausgibt.
    vision_reextraction_fingerprint = models.CharField(max_length=140, blank=True)
    # Zahlen-Gegenprobe (#1149) gegen den PDF-Text-Layer, wo vorhanden:
    # `performed=False` heißt "kein brauchbarer Text-Layer, Prüfung
    # entfällt", nicht "keine Abweichung gefunden" -- die beiden Fälle sehen
    # in der UI sonst gleich aus. Siehe `apps.documents.number_crosscheck`.
    vision_reextraction_number_check = models.JSONField(default=dict, blank=True)

    # KI-Analyse (#1020, apps.documents.analysis): lesbare Kurz-Zusammenfassung
    # plus strukturierte Key-Facts (Absender/Datum/Typ/Betrag/Frist, jeweils
    # KI-extrahiert -- daher hier statt in `metadata`, das reine
    # Extraktions-Provenienz führt).
    summary = models.TextField(blank=True)
    key_facts = models.JSONField(default=dict, blank=True)

    # Dokumentdatum (#1085): das dem Schriftstueck selbst innewohnende Datum
    # (Rechnungs-/Brief-/Bescheiddatum, ...) -- bewusst ein eigenes Feld statt
    # nur `key_facts["document_date"]`, weil es editierbar sein muss (siehe
    # `document_meta`) und eine Nutzerkorrektur sonst beim naechsten
    # `document_analysis_rerun` wieder von der KI ueberschrieben wuerde.
    # Welche der im Dokument gefundenen Datumsangaben das wird, entscheidet
    # seit #1141 der Code (`apps.documents.document_dates`) nach fester
    # Rangfolge, nicht das Modell. Die Herkunft steht in
    # `metadata["document_date_source"]` und regelt zugleich das
    # Ueberschreiben: `_apply_analysis` fasst nur an, was leer ist oder was
    # ein frueherer Analyse-Lauf selbst gesetzt hat -- eine Handkorrektur
    # (`"manuell"`) und ein Datum ohne Vermerk (EML-`Date`-Header, Bestand)
    # ueberleben jeden Wartungslauf.
    document_date = models.DateField(null=True, blank=True, db_index=True)

    original_file = models.FileField(upload_to="documents/%Y/%m/", blank=True)
    # Vorschaubild der ersten Seite (#1123): beim Ingest gerendert und im
    # selben Object Storage wie das Original abgelegt (nicht in Postgres,
    # siehe Architektur "Gehirn ↔ Regal"). Bewusst ein eigenes FileField
    # statt On-the-fly-Rendering beim ersten Abruf -- die Kachelansicht zeigt
    # 20 Bilder gleichzeitig, 20 synchrone Renderings pro Seitenaufruf waeren
    # der falsche Ort fuer die CPU-Last. Leer, solange kein Thumbnail
    # existiert (nicht renderbarer Typ, Rendering-Fehler oder Bestand ohne
    # Backfill) -- `document_thumbnail` antwortet dann 404, die UI zeigt einen
    # Typ-Platzhalter.
    thumbnail = models.FileField(upload_to="thumbnails/%Y/%m/", blank=True)
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)

    objects = DocumentQuerySet.as_manager()

    # Inline-fähig heißt: der Browser kann es nativ rendern, ohne
    # Konverter (#1036) -- PDF und alle Bildformate; alles andere (docx,
    # xlsx, zip, eml, …) bleibt Download-only.
    INLINE_PREVIEW_MIME_TYPES = {"application/pdf"}

    # Thumbnail-fähig heißt: die Ingest-Pipeline (#1123) kann daraus ein
    # Vorschaubild der ersten Seite rastern -- PDF (pypdfium2) und alle
    # Bildformate (Pillow). Alles andere (docx, xlsx, zip, eml, …) bekommt
    # keins; die UI zeigt dort einen Typ-Platzhalter. Bewusst hier auf dem
    # Model gekapselt (analog `INLINE_PREVIEW_MIME_TYPES`), damit Template,
    # View und Rendering dieselbe Quelle nutzen statt drei Whitelists.
    THUMBNAIL_MIME_TYPES = {"application/pdf"}

    # KI-Vision-neuextrahierbar heißt: die Seiten lassen sich als Bild
    # rendern -- PDF (pdf2image) und alle Bildformate (Pillow), sonst gibt
    # es kein Pixelbild, das ein Vision-Modell lesen könnte (#1143). Eigene
    # Konstante statt Wiederverwendung von `THUMBNAIL_MIME_TYPES`, obwohl
    # beide heute denselben Formatkreis abdecken -- derselbe bewusste
    # Auseinanderhalt wie bei `INLINE_PREVIEW_MIME_TYPES`/
    # `THUMBNAIL_MIME_TYPES`: zwei fachlich unabhängige Fragen, die zufällig
    # dieselbe Antwort haben.
    VISION_REEXTRACTABLE_MIME_TYPES = {"application/pdf"}

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if self.parent_id is not None:
            self._check_no_parent_cycle()
        super().save(*args, **kwargs)

    def _check_no_parent_cycle(self):
        """Walk the `parent` chain before saving (#1069) -- a self-ref FK

        has no built-in cycle protection, and A↝B↝A would turn `subtree()`
        and the Eingang's tree view into an infinite loop.
        """
        ancestor_id = self.parent_id
        visited = set()
        while ancestor_id is not None:
            if self.pk is not None and ancestor_id == self.pk:
                raise ValidationError(
                    "Zyklische Leitdokument-Zuordnung: ein Dokument kann "
                    "nicht (mittelbar) sein eigenes Unterdokument sein."
                )
            if ancestor_id in visited:
                return
            visited.add(ancestor_id)
            ancestor_id = (
                Document.objects.filter(pk=ancestor_id)
                .values_list("parent_id", flat=True)
                .first()
            )

    def subtree(self):
        """All descendants of this document, any depth (#1069) -- "alles

        aus diesem Leitdokument". Iterative Breadth-First-Suche statt
        rekursiver CTE: Anhang-Bäume sind flach, ein künstliches
        Tiefenlimit ist unnötig, aber eine simple Python-Schleife genügt.
        """
        collected_ids = []
        frontier = [self.pk]
        while frontier:
            frontier = list(
                Document.objects.filter(parent_id__in=frontier).values_list(
                    "pk", flat=True
                )
            )
            collected_ids.extend(frontier)
        return Document.objects.filter(pk__in=collected_ids)

    @property
    def display_date(self):
        """The date to show/sort by everywhere (#1085): `document_date` when

        the KI-Analyse found/the user set one, else the Upload-Datum
        (`created_at`) -- never the other way round, `created_at` itself is
        never touched by this fallback.
        """
        return self.document_date or self.created_at.date()

    @property
    def display_date_is_upload_fallback(self):
        """True when `display_date` had to fall back to `created_at` (#1085)

        -- drives the dezent "(Upload)" marker so that fallback stays
        distinguishable from an actually recognised Dokumentdatum.
        """
        return self.document_date is None

    @property
    def billing_period_label(self):
        """"01.11.2023 – 30.11.2023" für ein Dokument mit Abrechnungs- oder

        Leistungszeitraum (#1141), sonst "". Bei einem Kontoauszug ist der
        Zeitraum die aussagekräftigere Angabe als das Einzeldatum, deshalb
        steht er im Detail neben dem Datum. Die Grenzen liegen als ISO-Strings
        in `key_facts` (KI-extrahiert, wie Betrag/Frist); die Formatierung
        gehört hierher und nicht ins Template, damit sie an einer Stelle
        steht -- eine halbe Angabe (nur Beginn oder nur Ende) wird als
        offener Zeitraum gezeigt statt verschwiegen.
        """
        key_facts = self.key_facts if isinstance(self.key_facts, dict) else {}
        start = document_dates.parse_date(key_facts.get("period_start"))
        end = document_dates.parse_date(key_facts.get("period_end"))
        if start is None and end is None:
            return ""
        return " – ".join(
            date_format(value, "d.m.Y") if value else "…" for value in (start, end)
        )

    @property
    def document_date_source_label(self):
        """Woher `document_date` stammt, in Worten (#1141) -- "Ende des

        Abrechnungszeitraums", "Erstell-/Druckdatum (unsichere Quelle)", "Von
        Hand gesetzt", ... Leer für Bestand ohne Herkunftsvermerk. Sichtbar
        im Detail, weil die Quellen unterschiedlich belastbar sind: ein
        Dokument, das nur auf sein Druckdatum zurückfällt, soll das zeigen
        statt so zu tun, als sei das Datum gesichert.
        """
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        return document_dates.SOURCE_LABELS.get(metadata.get("document_date_source"), "")

    @property
    def document_date_was_corrected(self):
        """True, wenn die Plausibilitätsprüfung (#1141) beim letzten

        Analyse-Lauf ein Datum am Upload-Tag zugunsten einer anderen Quelle
        verworfen hat -- Grundlage für den erklärenden Hinweis im Detail.
        """
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        return bool(metadata.get("document_date_upload_conflict"))

    @property
    def display_date_month_label(self):
        """Monat/Jahr-Überschrift für die Timeline-Ansicht (#1087), z. B.

        "August 2026" -- ein einfacher Property statt eines Template-Filters,
        weil Djangos `{% regroup %}`-Tag nur nach einem Attribut gruppieren
        kann, nicht nach einem gefilterten Ausdruck.
        """
        return date_format(self.display_date, "F Y")

    @property
    def is_mail_body(self):
        """True for a Leitdokument erzeugt aus dem E-Mail-Text (#1070) --

        drives the "aus Body erzeugt" badge and keeps the check in one place.
        """
        return self.kind == self.Kind.MAIL_BODY

    @property
    def original_filename(self):
        """The filename as ingested (#1007), not the storage path -- `original_file.name`

        is prefixed with the `upload_to` date path and isn't fit for a
        `Content-Disposition` filename.
        """
        if not self.original_file:
            return ""
        return self.metadata.get("original_filename") or self.original_file.name.rsplit(
            "/", 1
        )[-1]

    @property
    def mime_type(self):
        """The mime type recorded at ingest (#1007); falls back to a

        filename guess for documents ingested before that field existed.
        """
        return (
            self.metadata.get("mime_type")
            or mimetypes.guess_type(self.original_filename)[0]
            or "application/octet-stream"
        )

    @property
    def is_inline_previewable(self):
        """Whitelist check (#1036) for the detail page's Slide-Over --

        only PDF/image get an inline preview; everything else only offers
        the Download button.
        """
        mime = self.mime_type
        return mime in self.INLINE_PREVIEW_MIME_TYPES or mime.startswith("image/")

    @property
    def is_thumbnailable(self):
        """Whitelist check (#1123) whether a first-page Vorschaubild can be
        rendered for this document -- PDF and every image format, nothing
        else. Shared by the ingest step, the backfill command and the
        Kachel-Template so all three agree on "which documents get a
        thumbnail" (the field being empty otherwise means "not renderable"
        *or* "render failed" -- both correctly end up on the placeholder).
        """
        mime = self.mime_type
        return mime in self.THUMBNAIL_MIME_TYPES or mime.startswith("image/")

    @property
    def supports_vision_reextraction(self):
        """Whitelist check (#1143) fuer den "Mit KI-Vision neu
        extrahieren"-Button -- nur Formate, die sich seitenweise als Bild
        rendern lassen (PDF, Bilder); alles andere zeigt den Button gar
        nicht erst.
        """
        mime = self.mime_type
        return mime in self.VISION_REEXTRACTABLE_MIME_TYPES or mime.startswith("image/")

    @property
    def vision_reextraction_is_running(self):
        return self.vision_reextraction_status == self.VisionReextractionStatus.RUNNING

    @property
    def has_tax_assessment(self):
        """True, wenn die KI eine *echte* private ESt-Einschätzung getroffen
        hat (#1113) -- Ja/Nein/Vielleicht. Grenzt die Fälle ab, in denen
        Begründung + Disclaimer sinnvoll sind, von `unbekannt` (keine
        Aussage) und `nicht_zutreffend` (geschäftlich, anderes Thema).
        """
        return self.tax_relevance in (
            self.TaxRelevance.JA,
            self.TaxRelevance.NEIN,
            self.TaxRelevance.VIELLEICHT,
        )

    @classmethod
    def tax_relevance_filter_choices(cls):
        """Die Filter-Auswahl der Dokumentliste (#1113): der Sammelwert
        "privat absetzbar (Ja/Vielleicht)" zuerst, danach die einzelnen
        Feldwerte -- die Meta-Bearbeitung nutzt dagegen `TaxRelevance.choices`
        pur (der Sammelwert ist keine setzbare Klassifikation).
        """
        return [
            (cls.TAX_RELEVANCE_DEDUCTIBLE_FILTER, "Privat absetzbar (Ja/Vielleicht)"),
            *cls.TaxRelevance.choices,
        ]


def follow_up_week_end(today):
    """Sonntag der Woche von `today` (#1129, ISO-Wochenstart Montag) --

    die Wochengrenze fuer die Gruppe "Diese Woche" (ab morgen bis
    einschliesslich diesem Sonntag). Modulfunktion statt einer zweiten Kopie
    der Arithmetik: sowohl die `due_this_week`/`due_later`-Zeitfenster unten
    als auch die Gruppierung der Wiedervorlagen-Ansicht (`views.py`)
    referenzieren dieselbe Regel.
    """
    return today + datetime.timedelta(days=6 - today.weekday())


class DocumentCommentQuerySet(models.QuerySet):
    def due(self, *, on=None):
        """Kommentare mit einer Wiedervorlage, die heute oder frueher liegt

        (#1125) -- unabhaengig von `remind`, treibt allein die
        Hervorhebung im Detail und den Uebersichts-/Filter-Indikator.
        """
        cutoff = on or timezone.localdate()
        return self.filter(follow_up_date__isnull=False, follow_up_date__lte=cutoff)

    def pending_reminders(self, *, on=None):
        """Faellige Wiedervorlagen, die noch nie erinnert haben (#1125) --

        die Grundlage des taeglichen Erinnerungs-Jobs. `reminded_at` wird
        erst nach erfolgreichem Versand gesetzt, damit ein Kommentar genau
        einmal erinnert.
        """
        return self.due(on=on).filter(remind=True, reminded_at__isnull=True)

    def with_follow_up(self):
        """Kommentare mit gesetzter Wiedervorlage, unabhaengig vom Datum

        (#1129) -- gemeinsame Basis der Zeitfenster unten und der
        Wiedervorlagen-Ansicht.
        """
        return self.filter(follow_up_date__isnull=False)

    def overdue(self, *, on=None):
        """Ueberfaellige Wiedervorlagen (#1129) -- eigene Gruppe getrennt von

        `due()` (ueberfaellig *und* heute), das weiterhin unveraendert den
        Karten-/Erinnerungs-Indikator treibt.
        """
        cutoff = on or timezone.localdate()
        return self.with_follow_up().filter(follow_up_date__lt=cutoff)

    def due_today(self, *, on=None):
        """Heute faellige Wiedervorlagen (#1129)."""
        day = on or timezone.localdate()
        return self.with_follow_up().filter(follow_up_date=day)

    def due_this_week(self, *, on=None):
        """Wiedervorlagen ab morgen bis einschliesslich Sonntag dieser Woche

        (#1129, siehe `follow_up_week_end`) -- faellt an einem Sonntag leer
        aus, weil "heute" dann bereits der letzte Tag der Woche ist.
        """
        today = on or timezone.localdate()
        tomorrow = today + datetime.timedelta(days=1)
        week_end = follow_up_week_end(today)
        if tomorrow > week_end:
            return self.none()
        return self.with_follow_up().filter(
            follow_up_date__gte=tomorrow, follow_up_date__lte=week_end
        )

    def due_later(self, *, on=None):
        """Wiedervorlagen nach dieser Woche (#1129)."""
        today = on or timezone.localdate()
        return self.with_follow_up().filter(follow_up_date__gt=follow_up_week_end(today))


class DocumentComment(TimeStampedModel):
    """Ein Eintrag der Kommentar-Chronik eines Dokuments (#1125): freier

    Text mit optionalem Wiedervorlage-Datum und optionaler Erinnerung.

    Ersetzt bewusst kein Statusfeld -- ob an einem Dokument noch etwas
    offen ist, sagt weiterhin allein `Document.action_status` (dieselbe
    Trennung wie `ProcessingStatus` vs. `ActionStatus`, siehe dort). Ein
    Kommentar hat deshalb auch kein eigenes Erledigt-Feld: die Wiedervorlage
    ist ein One-Shot-Ping (erinnert höchstens einmal, `reminded_at`
    dokumentiert das), keine Aufgabe mit eigenem Lebenszyklus -- was nicht
    wichtig genug war, noch einmal nachzuhaken, verschwindet zu Recht
    kommentarlos in der Chronik statt als offener Posten liegen zu bleiben.
    """

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="comments")
    body = models.TextField()
    follow_up_date = models.DateField(null=True, blank=True)
    remind = models.BooleanField(default=False)
    # Nur gesetzt, nachdem der Erinnerungs-Job (apps.documents.reminders)
    # tatsaechlich eine Mail verschickt hat -- der Bewacher gegen
    # Doppelversand bei einem zweiten Lauf desselben oder eines spaeteren
    # Tages.
    reminded_at = models.DateTimeField(null=True, blank=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="document_comments",
    )

    objects = DocumentCommentQuerySet.as_manager()

    class Meta:
        # Journal, keine Terminliste: neueste zuerst nach Anlagedatum, nicht
        # nach `follow_up_date` (siehe Klassendoc).
        ordering = ["-created_at"]

    def __str__(self):
        return self.body[:50]

    @property
    def is_overdue(self):
        return self.follow_up_date is not None and self.follow_up_date < timezone.localdate()

    @property
    def is_due_today(self):
        return self.follow_up_date is not None and self.follow_up_date == timezone.localdate()


class SuggestionStatus(models.TextChoices):
    PENDING = "pending", "Offen"
    ACCEPTED = "accepted", "Angenommen"
    REJECTED = "rejected", "Verworfen"


class SuggestionQuerySet(models.QuerySet):
    def pending(self):
        return self.filter(status=SuggestionStatus.PENDING)


class TagSuggestion(TimeStampedModel):
    """Ein von der KI-Analyse (#1020) vorgeschlagenes Tag -- Prinzip "die KI

    müllt nicht selbstständig zu": erst wenn der Nutzer annimmt, wird ein
    passendes `Tag` gematcht/angelegt und dem Document zugeordnet (siehe
    `apps.documents.views`). Re-Analyse ersetzt nur noch offene (`pending`)
    Vorschläge -- bereits angenommene/verworfene bleiben als Entscheidung
    stehen, statt bei jedem Re-Run erneut aufzutauchen.
    """

    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="tag_suggestions"
    )
    name = models.CharField(max_length=100)
    dimension = models.CharField(max_length=100, blank=True)
    confidence = models.FloatField(default=0.0)
    status = models.CharField(
        max_length=20, choices=SuggestionStatus.choices, default=SuggestionStatus.PENDING
    )

    objects = SuggestionQuerySet.as_manager()

    class Meta:
        ordering = ["-confidence", "name"]

    def __str__(self):
        return f"{self.name} -> Document {self.document_id}"


class VorgangSuggestion(TimeStampedModel):
    """Ein von der KI-Analyse (#1020) vorgeschlagener Vorgang -- analog zu

    `TagSuggestion`, siehe dort für das Vorschlag/Annahme-Prinzip.
    """

    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="vorgang_suggestions"
    )
    name = models.CharField(max_length=255)
    confidence = models.FloatField(default=0.0)
    status = models.CharField(
        max_length=20, choices=SuggestionStatus.choices, default=SuggestionStatus.PENDING
    )

    objects = SuggestionQuerySet.as_manager()

    class Meta:
        ordering = ["-confidence", "name"]

    def __str__(self):
        return f"{self.name} -> Document {self.document_id}"


class Chunk(TimeStampedModel):
    """Ein Text-Abschnitt eines Dokuments samt Embedding.

    `embedding_model` + `embedding_model_version` sind der Lock-in-Hedge
    (siehe Architektur.md, "Lock-in-Hedges"): Vektoren verschiedener
    Modelle/Versionen sind nicht vergleichbar, daher pro Chunk mitführen,
    um Re-Index gezielt fahren zu können.
    """

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="chunks")
    position = models.PositiveIntegerField()
    content = models.TextField()
    embedding = VectorField(dimensions=settings.FINDUS_EMBEDDING_DIMENSIONS)
    embedding_model = models.CharField(max_length=100)
    embedding_model_version = models.CharField(max_length=50)

    class Meta:
        ordering = ["document_id", "position"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "position"], name="unique_chunk_position"
            ),
        ]
        indexes = [
            HnswIndex(
                name="chunk_embedding_hnsw_idx",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
        ]

    def __str__(self):
        return f"Chunk {self.position} of {self.document_id}"


class DocumentLinkQuerySet(models.QuerySet):
    def for_document(self, document):
        """Every link touching `document`, regardless of which side it sits
        on -- the pair is stored canonically (see `DocumentLink`), so a
        caller must never assume "its" document is `document_a`.
        """
        return self.filter(
            models.Q(document_a=document) | models.Q(document_b=document)
        )


class DocumentLink(TimeStampedModel):
    """Ein manuell gesetzter Querverweis zwischen zwei Dokumenten (#1088) --
    "gehört zu", ohne Richtung.

    Ergänzt die automatischen Ähnlichkeits-Treffer
    (`DocumentRetrievalService.similar_documents`) im selben Block des
    Dokument-Details: was die Vektor-Ähnlichkeit nicht findet (anderes
    Thema, andere Sprache, kein Textbezug), trägt der Nutzer hier von Hand
    nach.

    Bewusst *ungerichtet*: "A gehört zu B" heißt immer auch "B gehört zu
    A", und eine zweite Zeile für die Gegenrichtung wäre dieselbe Aussage
    doppelt. Erzwungen wird das über die kanonische Speicherung
    `document_a_id < document_b_id` (`link_documents()` sortiert das Paar)
    -- so deckt die UniqueConstraint beide Richtungen ab, statt A→B und
    B→A als zwei "verschiedene" Links durchzulassen. Dieselbe Bedingung
    schließt den Selbstverweis A↝A gleich mit aus.

    Nicht zu verwechseln mit `Document.parent` (#1069): dort hängt ein
    Unterdokument *in* seinem Leitdokument (geerbter Scope, Kaskaden-Löschung,
    eigener Detail-Block), hier stehen zwei eigenständige Dokumente
    gleichberechtigt nebeneinander.
    """

    document_a = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="links_as_a"
    )
    document_b = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="links_as_b"
    )
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_document_links",
    )

    objects = DocumentLinkQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["document_a", "document_b"], name="unique_document_link"
            ),
            models.CheckConstraint(
                condition=models.Q(document_a__lt=models.F("document_b")),
                name="document_link_canonical_pair",
            ),
        ]

    def __str__(self):
        return f"Link {self.document_a_id} <-> {self.document_b_id}"

    def other_document_id(self, document):
        """The id of the *other* end seen from `document` -- what the detail
        page actually wants to show.
        """
        return (
            self.document_b_id
            if self.document_a_id == document.pk
            else self.document_a_id
        )


def link_documents(first, second, *, created_by=None, note=""):
    """Create (or return) the undirected link between two documents.

    Normalises the pair to `document_a_id < document_b_id` before hitting
    the DB, which is what makes the UniqueConstraint symmetric -- linking
    B to A after A to B is a no-op instead of a second row. Returns
    `(link, created)` like `get_or_create`.
    """
    if first.pk == second.pk:
        raise ValidationError("Ein Dokument kann nicht mit sich selbst verknüpft werden.")
    document_a, document_b = sorted((first, second), key=lambda document: document.pk)
    return DocumentLink.objects.get_or_create(
        document_a=document_a,
        document_b=document_b,
        defaults={"created_by": created_by, "note": note},
    )


def tax_relevance_filter_q(value):
    """Q-Objekt für den Steuerrelevanz-Filter der Dokumentliste (#1113).

    Der Sammelwert `"absetzbar"` (`TAX_RELEVANCE_DEDUCTIBLE_FILTER`) trifft
    Ja UND Vielleicht zusammen -- genau die Belege, die man als ESt-Grundlage
    sammeln will. Jeder andere gültige Feldwert filtert exakt. Ein leerer
    oder unbekannter Wert liefert `None` ("kein Filter"), damit der Aufrufer
    die Query unverändert lässt. An einer Stelle, weil Browse (`views`) und
    semantische Suche (`retrieval`) denselben Filter teilen.
    """
    value = (value or "").strip()
    if not value:
        return None
    if value == Document.TAX_RELEVANCE_DEDUCTIBLE_FILTER:
        return models.Q(tax_relevance__in=Document.TAX_RELEVANCE_DEDUCTIBLE_VALUES)
    if value in Document.TaxRelevance.values:
        return models.Q(tax_relevance=value)
    return None


# Wegwerf-Präfixe vor dem eigentlichen Wert einer Kennung (#1099): "AZ",
# "Aktenzeichen:", "Kd.-Nr.", "IBAN" … Dokumente schreiben dieselbe Nummer
# mal mit, mal ohne Beschriftung ("Az. 123/45" vs. "123/45"), und die KI
# liefert mal so, mal so -- ohne das Abschneiden wären das zwei
# verschiedene Matching-Schlüssel für dieselbe Kennung.
_REFERENCE_LABEL_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"aktenzeichen|az|gesch(?:ae|ä)ftszeichen|gz"
    r"|ihr\s*zeichen|unser\s*zeichen|zeichen"
    r"|kunden(?:nummer|nr)|kd\s*\.?\s*-?\s*nr"
    r"|rechnungs(?:nummer|nr)|re\s*\.?\s*-?\s*nr"
    r"|beleg(?:nummer|nr)|vertrags(?:nummer|nr)|forderungs(?:nummer|nr)"
    r"|iban|nummer|nr"
    # Nach der Beschriftung muss ein Trenner stehen -- sonst würde "AZUBI"
    # zu "UBI" gekürzt, weil "AZ" zufällig am Anfang steht.
    r")(?:\s*[.:\-–]\s*|\s+)",
    re.IGNORECASE,
)


def normalize_reference_value(value: str) -> str:
    """Matching-Schlüssel einer Kennung (#1099): Beschriftung ab, alle
    Leerzeichen raus, Großschreibung.

    Bewusst *typunabhängig* und rein textuell -- eine Kennung ist ein
    Fremdformat (Gericht, Inkassobüro, Stadtwerke), dessen Aufbau Findus
    nicht kennt und nicht validieren kann. Was hier passiert, ist deshalb
    nur das Einebnen der Schreibvarianten, an denen ein exakter Vergleich
    sonst scheitert: "AZ 123/45", "Az. 123 / 45" und "123/45" ergeben
    denselben Schlüssel, "DE02 1203 0000 0000 2020 51" dieselbe IBAN wie
    ohne Leerzeichen. Trennzeichen *innerhalb* des Werts (/, -, .) bleiben
    stehen -- sie gehören zur Kennung, nicht zur Formatierung.

    `value_raw` behält daneben immer die Schreibweise aus dem Dokument:
    angezeigt wird, was dort steht, verglichen wird dieser Schlüssel.
    """
    text = (value or "").strip()
    if not text:
        return ""
    without_prefix = _REFERENCE_LABEL_PREFIX_RE.sub("", text, count=1)
    # Ein Wert, der *nur* aus der Beschriftung besteht, ist keine Kennung --
    # dann lieber das Original behalten als einen leeren Schlüssel.
    if without_prefix.strip():
        text = without_prefix
    return re.sub(r"\s+", "", text).upper()


class DocumentReference(TimeStampedModel):
    """Eine typisierte Kennung/Referenznummer eines Dokuments (#1099):
    Aktenzeichen, Forderungs-/Kunden-/Belegnummer, IBAN, „Ihr/Unser
    Zeichen" …

    Warum eigene Zeilen statt eines Felds an `Document`: ein Schreiben
    trägt regelmäßig *mehrere* Kennungen gleichzeitig (Aktenzeichen **und**
    Forderungsnummer **und** Kundennummer), und jede davon ist für sich ein
    Verknüpfungspunkt. Ein Einzelfeld müsste sich für eine entscheiden.

    Warum überhaupt strukturiert statt „im Embedding mitgewichtet": „AZ
    123/45" geht in einem Vektor im Rauschen unter -- Ähnlichkeit erkennt
    Kennungen schlicht nicht. Ein gemeinsames Aktenzeichen ist dagegen ein
    nahezu sicherer Zusammenhang. Deshalb der exakte Vergleich über
    `(type, value_normalized)` (Index) als bewusst eigenes, *präzises*
    Signal neben der semantischen Ähnlichkeit (#1088) -- beide ergänzen
    sich, keines ersetzt das andere.
    """

    class Type(models.TextChoices):
        """Erweiterbar gehalten: die Liste deckt ab, was in Post von
        Behörden, Inkasso, Versorgern und Rechnungsstellern regelmäßig
        auftaucht; `SONSTIGE` fängt den Rest auf, damit eine unbekannte
        Kennungsart nicht verloren geht, nur weil sie hier noch fehlt.
        """

        AKTENZEICHEN = "aktenzeichen", "Aktenzeichen"
        FORDERUNGSNUMMER = "forderungsnummer", "Forderungsnummer"
        KUNDENNUMMER = "kundennummer", "Kundennummer"
        BELEGNUMMER = "belegnummer", "Belegnummer"
        RECHNUNGSNUMMER = "rechnungsnummer", "Rechnungsnummer"
        VERTRAGSNUMMER = "vertragsnummer", "Vertragsnummer"
        IBAN = "iban", "IBAN"
        IHR_ZEICHEN = "ihr_zeichen", "Ihr Zeichen"
        UNSER_ZEICHEN = "unser_zeichen", "Unser Zeichen"
        SONSTIGE = "sonstige", "Sonstige"

    class Role(models.TextChoices):
        """Wessen Kennung ist das -- unsere oder die der Gegenstelle?

        Löst die im Dokument stehende Beschriftung in eine *absolute*
        Sicht auf: „Ihr Zeichen" in einem eingehenden Brief meint unsere
        Kennung (`deins`), „Unser Zeichen" die des Absenders (`deren`) --
        bei einem Ausgangsschreiben genau andersherum. Ohne diese
        Auflösung stünde in zwei Dokumenten derselben Korrespondenz
        dieselbe Nummer einmal als „Ihr" und einmal als „Unser Zeichen",
        ohne dass irgendwo steht, wem sie gehört.

        Optional: ist die Zuordnung nicht sicher, bleibt das Feld leer
        statt geraten.
        """

        MINE = "deins", "Unsere Kennung"
        THEIRS = "deren", "Kennung der Gegenstelle"

    class Source(models.TextChoices):
        """Woher die Zeile stammt. Trennt die (fehlbare) KI-Extraktion von
        dem, was jemand von Hand nachgetragen oder korrigiert hat -- eine
        erneute Analyse ersetzt nur ihre eigenen Zeilen und fasst manuelle
        nicht an, dasselbe „Nutzerentscheidung schlägt Automatik"-Prinzip
        wie bei `TagSuggestion`/`Document.document_date`.
        """

        AI = "ki", "KI-Analyse"
        MANUAL = "manuell", "Manuell"

    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="references"
    )
    type = models.CharField(max_length=30, choices=Type.choices)
    value_raw = models.CharField(max_length=255)
    value_normalized = models.CharField(max_length=255)
    role = models.CharField(max_length=10, choices=Role.choices, blank=True, default="")
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.AI)

    class Meta:
        ordering = ["type", "value_normalized", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "type", "value_normalized"],
                name="unique_document_reference",
            ),
        ]
        indexes = [
            # Der Index, der den exakten Match trägt: „welche Dokumente
            # haben dieselbe Kennung" ist genau ein Lookup auf diesem Paar.
            models.Index(
                fields=["type", "value_normalized"], name="document_reference_idx"
            ),
        ]
        verbose_name = "Kennung"
        verbose_name_plural = "Kennungen"

    def __str__(self):
        return f"{self.get_type_display()}: {self.value_raw}"

    def save(self, *args, **kwargs):
        """`value_normalized` immer aus `value_raw` ableiten, wenn er fehlt
        -- der Matching-Schlüssel darf nicht davon abhängen, ob ein
        Aufrufer daran gedacht hat, ihn zu setzen.
        """
        if not self.value_normalized:
            self.value_normalized = normalize_reference_value(self.value_raw)
        super().save(*args, **kwargs)


class OwnerReference(TimeStampedModel):
    """Gemeinsame Basis der Kennungen, die einem *Ziel* gehören (#1100):
    dem Vorgang seine fallbezogenen, dem Kontakt seine parteibezogenen.

    `DocumentReference` (#1099) sagt „in diesem Schreiben steht diese
    Nummer" -- hier steht „diese Nummer *ist* die dieses Vorgangs/dieses
    Kontakts". Erst dadurch bekommt eine Kennung ein Zuhause und ein neues
    Dokument ein Ziel: der Abgleich läuft gegen diese Zeilen, nicht mehr
    nur Dokument gegen Dokument.

    Bewusst dieselben `type`-Choices und derselbe `value_normalized`
    -Schlüssel wie am Dokument: der Match ist genau die Gleichheit von
    `(type, value_normalized)` über beide Tabellen hinweg. Zwei
    unterschiedliche Normalisierungen hier und dort hießen: der Abgleich
    findet nichts, ohne dass irgendwo ein Fehler sichtbar würde.

    Keine `role`: ob eine Nummer im Brief als „Ihr" oder „Unser Zeichen"
    stand, ist eine Eigenschaft des einzelnen Schreibens. Der Vorgang hat
    schlicht sein Aktenzeichen.
    """

    class Source(models.TextChoices):
        """Von Hand gepflegt oder aus einem zugeordneten Dokument gelernt.

        Trennt die Nutzerentscheidung von der Automatik -- dasselbe
        Prinzip wie `DocumentReference.Source`, nur mit anderer
        Gegenseite: hier ist die automatische Quelle nicht die KI, sondern
        die Zuordnung eines Dokuments, dessen Kennungen der Vorgang/Kontakt
        damit übernimmt.
        """

        MANUAL = "manuell", "Manuell"
        LEARNED = "gelernt", "Aus Dokument gelernt"

    type = models.CharField(max_length=30, choices=DocumentReference.Type.choices)
    value_raw = models.CharField(max_length=255)
    value_normalized = models.CharField(max_length=255)
    source = models.CharField(
        max_length=10, choices=Source.choices, default=Source.MANUAL
    )
    # Aus welchem Dokument gelernt -- die Herkunft, an der die
    # Sichtbarkeits-Prüfung des Zuordnungs-Vorschlags hängt (siehe
    # `apps.documents.reference_matching`): eine Kennung, die aus einem für
    # mich unsichtbaren Dokument stammt, darf mir keinen Vorschlag
    # erzeugen. `SET_NULL` statt `CASCADE`: löscht jemand das Schreiben,
    # aus dem der Vorgang sein Aktenzeichen gelernt hat, bleibt das
    # Aktenzeichen trotzdem seins -- die Zeile verliert nur ihre Herkunft
    # und zählt danach wie eine von Hand gepflegte.
    learned_from = models.ForeignKey(
        Document,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        abstract = True
        ordering = ["type", "value_normalized", "pk"]

    def __str__(self):
        return f"{self.get_type_display()}: {self.value_raw}"

    def save(self, *args, **kwargs):
        if not self.value_normalized:
            self.value_normalized = normalize_reference_value(self.value_raw)
        super().save(*args, **kwargs)


class VorgangReference(OwnerReference):
    """Eine fallbezogene Kennung dieses Vorgangs (#1100) -- sein
    Aktenzeichen, seine Forderungs-/Vertrags-/Rechnungsnummer.
    """

    vorgang = models.ForeignKey(
        Vorgang, on_delete=models.CASCADE, related_name="references"
    )

    class Meta(OwnerReference.Meta):
        abstract = False
        constraints = [
            models.UniqueConstraint(
                fields=["vorgang", "type", "value_normalized"],
                name="unique_vorgang_reference",
            ),
        ]
        indexes = [
            # Trägt die Vorschlags-Richtung: „welcher Vorgang hat diese
            # Kennung" ist genau ein Lookup auf diesem Paar -- dasselbe
            # Index-Paar wie am Dokument, weil dieselbe Frage gestellt wird.
            models.Index(
                fields=["type", "value_normalized"], name="vorgang_reference_idx"
            ),
        ]
        verbose_name = "Vorgang-Kennung"
        verbose_name_plural = "Vorgang-Kennungen"


class CorrespondentReference(OwnerReference):
    """Eine parteibezogene Kennung dieses Kontakts (#1100) --
    Kundennummer, IBAN.

    Getrennt vom Vorgang, weil eine Kundennummer über Jahre in völlig
    verschiedenen Vorgängen auftaucht: sie sagt „derselbe Kontakt", nicht
    „dieselbe Angelegenheit" (Typ->Scope-Mapping, #1099).
    """

    correspondent = models.ForeignKey(
        Correspondent, on_delete=models.CASCADE, related_name="references"
    )

    class Meta(OwnerReference.Meta):
        abstract = False
        constraints = [
            models.UniqueConstraint(
                fields=["correspondent", "type", "value_normalized"],
                name="unique_correspondent_reference",
            ),
        ]
        indexes = [
            models.Index(
                fields=["type", "value_normalized"], name="correspondent_ref_idx"
            ),
        ]
        verbose_name = "Kontakt-Kennung"
        verbose_name_plural = "Kontakt-Kennungen"


class TaskQuerySet(models.QuerySet):
    def visible_to(self, user):
        """Same two-level visibility model as `DocumentQuerySet.visible_to`

        -- a Task reveals nothing beyond what its linked documents already
        would, so it reuses the identical department/private scoping.
        """
        if user.is_superuser:
            return self
        return self.filter(
            models.Q(
                visibility=Task.Visibility.DEPARTMENT,
                departments__in=user.departments.all(),
            )
            | models.Q(
                visibility=Task.Visibility.PRIVATE,
                owner=user,
            )
        ).distinct()


class Task(TimeStampedModel):
    """A necessity arising from one or more documents (pay an invoice,

    review a utility bill, answer the tax office, ...). Deliberately flat:
    no subtasks, no projects -- `ChecklistItem` covers the "steps within a
    task" need instead.
    """

    class Kind(models.TextChoices):
        PAY = "pay", "Zahlen"
        REVIEW = "review", "Prüfen"
        REPLY = "reply", "Beantworten"
        SUBMIT = "submit", "Einreichen"
        OTHER = "other", "Sonstiges"

    class Status(models.TextChoices):
        OPEN = "open", "Offen"
        DONE = "done", "Erledigt"

    class Visibility(models.TextChoices):
        DEPARTMENT = "department", "Abteilung"
        PRIVATE = "private", "Privat"

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.OTHER)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    due_date = models.DateField(null=True, blank=True)
    done_at = models.DateTimeField(null=True, blank=True)

    documents = models.ManyToManyField(Document, blank=True, related_name="tasks")

    # Sichtbarkeit: owner + departments (n:n) + visibility-Schalter, wie bei Document.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_tasks",
    )
    departments = models.ManyToManyField(Department, blank=True, related_name="tasks")
    visibility = models.CharField(
        max_length=20,
        choices=Visibility.choices,
        default=Visibility.DEPARTMENT,
    )

    objects = TaskQuerySet.as_manager()

    class Meta:
        ordering = ["due_date", "-created_at"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if self.status == self.Status.DONE:
            if self.done_at is None:
                self.done_at = timezone.now()
        else:
            self.done_at = None
        super().save(*args, **kwargs)


class ChecklistItem(TimeStampedModel):
    """A checkable step within a Task -- deliberately not a real subtask

    (no own status/due_date/documents), just an ordered, tickable line.
    """

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="checklist_items")
    text = models.CharField(max_length=255)
    is_done = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["task_id", "order"]

    def __str__(self):
        return self.text


class TaskTemplateQuerySet(models.QuerySet):
    def visible_to(self, user):
        """Same two-level visibility model as `Task.visible_to` (#1037)

        -- a template is only useful to the people who could also see the
        task it would create.
        """
        if user.is_superuser:
            return self
        return self.filter(
            models.Q(
                visibility=TaskTemplate.Visibility.DEPARTMENT,
                departments__in=user.departments.all(),
            )
            | models.Q(
                visibility=TaskTemplate.Visibility.PRIVATE,
                owner=user,
            )
        ).distinct()


class TaskTemplate(TimeStampedModel):
    """A reusable blueprint for recurring `Task`s (e.g. "Umsatzsteuer-

    Voranmeldung", "Monatsabschluss"), #1037. Deliberately separate from
    `Task.kind`: `kind` stays a plain category, the template is its own
    concept holding a checklist and a relative due date -- turning `kind`
    itself into a template would overload a field that other code already
    reads as "just the category". No auto-scheduling here on purpose
    (later issue); creating a `Task` from a template is a manual, one-off
    action (`create_task_from_template`).
    """

    class Visibility(models.TextChoices):
        DEPARTMENT = "department", "Abteilung"
        PRIVATE = "private", "Privat"

    name = models.CharField(max_length=255)
    default_kind = models.CharField(
        max_length=20, choices=Task.Kind.choices, blank=True
    )
    default_title = models.CharField(max_length=255, blank=True)
    default_description = models.TextField(blank=True)
    default_due_offset_days = models.PositiveIntegerField(null=True, blank=True)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_task_templates",
    )
    departments = models.ManyToManyField(
        Department, blank=True, related_name="task_templates"
    )
    visibility = models.CharField(
        max_length=20,
        choices=Visibility.choices,
        default=Visibility.DEPARTMENT,
    )

    objects = TaskTemplateQuerySet.as_manager()

    def __str__(self):
        return self.name


class TaskTemplateItem(models.Model):
    """A checklist line as it will be copied onto tasks created from the

    template -- the blueprint counterpart of `ChecklistItem`.
    """

    template = models.ForeignKey(
        TaskTemplate, on_delete=models.CASCADE, related_name="items"
    )
    text = models.CharField(max_length=255)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["template_id", "order"]

    def __str__(self):
        return self.text


DEFAULT_LETTER_LAYOUT = {
    # Deutscher Geschäftsbrief (DIN-5008-nah) als v1-Grundlayout -- die
    # Werte hier sind das, was jede Vorlage erbt, solange sie nichts
    # überschreibt. Bewusst ein JSON-Feld und keine einzelnen Spalten:
    # das Layout wächst mit dem Renderer (#4b), und jede neue
    # Layout-Option wäre sonst eine weitere Migration.
    "format": "din5008",
    "letterhead": "",
    "date_place": "",
    "closing": "Mit freundlichen Grüßen",
    "show_sender_block": True,
    "show_recipient_block": True,
    "show_date": True,
    "show_subject": True,
}


def default_letter_layout():
    """Callable statt Dict-Literal als `default` -- ein geteiltes Dict wäre
    ein veränderlicher Default, den sich alle Zeilen teilen.
    """
    return dict(DEFAULT_LETTER_LAYOUT)


class LetterTemplateQuerySet(models.QuerySet):
    def visible_to(self, user):
        """Dasselbe zweistufige Sichtbarkeitsmodell wie bei
        `TaskTemplate.visible_to` -- eine Brief-Vorlage trägt Anleitung,
        Signatur und Absenderbezug, gehört also in denselben
        Abteilungs-/Privat-Scope wie alles andere.
        """
        if user.is_superuser:
            return self
        return self.filter(
            models.Q(
                visibility=LetterTemplate.Visibility.DEPARTMENT,
                departments__in=user.departments.all(),
            )
            | models.Q(
                visibility=LetterTemplate.Visibility.PRIVATE,
                owner=user,
            )
        ).distinct()


class LetterTemplate(TimeStampedModel):
    """Vorlage für ein Schreiben (#1094) -- im Kern ein kleiner „Skill":
    **Anleitung** (wie schreibe ich diese Art Brief) + **Daten-Bindungen**
    (welcher Wert kommt woher, siehe `LetterTemplatePlaceholder`) +
    **Layout** (wie das fertige Schreiben aussieht).

    Nicht zu verwechseln mit `TaskTemplate` (#1037): das ist eine
    Aufgaben-Vorlage (Checkliste, Frist-Offset), hier geht es um
    Schriftstücke.

    `instructions` ist bewusst freier Markdown-Text und kein Feld-Baukasten:
    er ist die KI-Anleitung für die Erzeugung in #4b (Ton, Struktur, was
    rein muss und was nicht) -- was sich in Formularfelder pressen ließe,
    wäre genau die Nuance, die einen brauchbaren Brief ausmacht.

    Erzeugt hier noch nichts: Ausgabe/Rendering (Word/PDF) ist #4b.
    """

    class Visibility(models.TextChoices):
        DEPARTMENT = "department", "Abteilung"
        PRIVATE = "private", "Privat"

    # Freitext statt `choices`: die Kategorien im Konzept ("Antwortschreiben",
    # "Widerspruch", "Kündigung", "Anschreiben") sind Beispiele, keine
    # abschließende Liste -- die Vorschlagsliste steht als `datalist` im
    # Formular, ohne eigene Kategorien zu verbieten.
    CATEGORY_SUGGESTIONS = (
        "Antwortschreiben",
        "Widerspruch",
        "Kündigung",
        "Anschreiben",
        "Mahnung",
    )

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=100, blank=True, db_index=True)
    instructions = models.TextField(blank=True)

    layout = models.JSONField(default=default_letter_layout, blank=True)
    signature = models.TextField(blank=True)
    logo = models.ImageField(upload_to="letter_templates/logos/", blank=True)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_letter_templates",
    )
    departments = models.ManyToManyField(
        Department, blank=True, related_name="letter_templates"
    )
    visibility = models.CharField(
        max_length=20,
        choices=Visibility.choices,
        default=Visibility.DEPARTMENT,
    )

    objects = LetterTemplateQuerySet.as_manager()

    class Meta:
        ordering = ["category", "name"]
        verbose_name = "Brief-Vorlage"
        verbose_name_plural = "Brief-Vorlagen"

    def __str__(self):
        return self.name

    def layout_value(self, key):
        """Layout-Wert mit Rückfall auf das Grundlayout -- eine Vorlage, die
        vor einer neuen Layout-Option angelegt wurde, hat deren Schlüssel
        schlicht nicht im JSON.
        """
        return (self.layout or {}).get(key, DEFAULT_LETTER_LAYOUT.get(key))


class LetterTemplatePlaceholder(models.Model):
    """Eine Daten-Bindung: „dieser Platzhalter kommt aus jener Quelle" (#1094).

    Ein eigenes Modell statt einer JSON-Liste am `LetterTemplate`, weil die
    Bindungen sortierbar, einzeln editierbar und -- vor allem -- gegen die
    Quellen-Registry (`apps.documents.letter_bindings`) validierbar sein
    müssen; in einem JSON-Blob wäre jede Quelle ein ungeprüfter String.

    `source` ist genau deshalb auch **ohne** `choices`: die gültigen Werte
    stehen in der Registry, damit eine später ergänzte Quelle (etwa eine
    externe API als weiterer Namespace) keine Migration braucht.
    """

    KEY_VALIDATOR = RegexValidator(
        regex=r"^[a-z][a-z0-9_]*$",
        message=(
            "Schlüssel: nur Kleinbuchstaben, Ziffern und Unterstrich, "
            "beginnend mit einem Buchstaben (z. B. empfaenger_adresse)."
        ),
    )

    template = models.ForeignKey(
        LetterTemplate, on_delete=models.CASCADE, related_name="placeholders"
    )
    key = models.CharField(max_length=64, validators=[KEY_VALIDATOR])
    label = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=100, validators=[validate_source])
    required = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["template_id", "order", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["template", "key"], name="unique_letter_placeholder_key"
            ),
        ]
        verbose_name = "Platzhalter"
        verbose_name_plural = "Platzhalter"

    def __str__(self):
        return f"{{{{{self.key}}}}} <- {self.source}"

    @property
    def source_label(self):
        """„Gruppe · Feld" für die Anzeige -- die Auflösung der Quelle
        gehört der Registry, nicht dem Template.
        """
        return source_label(self.source)

    @property
    def display_label(self):
        return self.label or self.key


class LetterDraftQuerySet(models.QuerySet):
    def visible_to(self, user):
        """Dasselbe zweistufige Sichtbarkeitsmodell wie überall sonst
        (#1052). Ein Entwurf trägt den Brieftext *und* die aus dem
        beantworteten Dokument gezogenen Werte -- er gehört damit in
        denselben Abteilungs-/Privat-Scope wie das Dokument, aus dem er
        entstanden ist.
        """
        if user.is_superuser:
            return self
        return self.filter(
            models.Q(
                visibility=LetterDraft.Visibility.DEPARTMENT,
                departments__in=user.departments.all(),
            )
            | models.Q(
                visibility=LetterDraft.Visibility.PRIVATE,
                owner=user,
            )
        ).distinct()


class LetterDraft(TimeStampedModel):
    """Ein KI-Brief-Entwurf aus einer Brief-Vorlage (#1095) -- Werkbank
    zwischen „Vorlage gewählt" und „Dokument am Vorgang".

    Review-first: hier entsteht *nichts* Endgültiges. Der Entwurf hält den
    generierten Text (editierbar), die daraus gerenderten Dateien (Word als
    editierbarer Master, PDF als Druck-/Sende-Artefakt) und erst nach
    ausdrücklicher Freigabe die Verknüpfung auf das daraus abgelegte
    `Document`. Bis dahin ist der Entwurf kein Dokument, taucht in keiner
    Dokumentliste auf und wird nirgendwohin verschickt (Versand ist #5).

    Warum ein eigenes Modell und nicht gleich ein `Document` im Status
    „Entwurf": ein Dokument ist in Findus überall die abgelegte Wahrheit
    (Suche, Vorgang, Graph, Empfehlungen). Ein halbfertiger Brief, der noch
    dreimal umformuliert wird, hätte in all diesen Ansichten nichts zu
    suchen -- und ein zusätzlicher Entwurfs-Status müsste in jeder einzelnen
    Query mitgedacht werden.

    Snapshot-Prinzip: `layout`, `signature`, `sender_block`,
    `recipient_block` und `letter_date` werden beim Anlegen aus Vorlage und
    Kontakten *kopiert*, nicht bei jedem Rendern neu gelesen. Ein Brief, der
    vor drei Wochen entworfen wurde, darf sich nicht ändern, nur weil
    jemand die Vorlage angefasst oder die Adresse eines Kontakts korrigiert
    hat -- und er muss auch dann noch renderbar sein, wenn die Vorlage
    gelöscht wurde.
    """

    class Visibility(models.TextChoices):
        DEPARTMENT = "department", "Abteilung"
        PRIVATE = "private", "Privat"

    class Status(models.TextChoices):
        RUNNING = "running", "Wird erstellt"
        READY = "ready", "Entwurf bereit"
        FAILED = "failed", "Fehlgeschlagen"
        FINALIZED = "finalized", "Abgelegt"

    template = models.ForeignKey(
        LetterTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="drafts",
    )
    # Name der Vorlage zum Zeitpunkt der Erzeugung -- damit der Entwurf auch
    # nach dem Löschen der Vorlage noch sagen kann, woraus er entstand.
    template_name = models.CharField(max_length=255, blank=True)

    source_document = models.ForeignKey(
        Document,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="letter_drafts",
    )
    vorgang = models.ForeignKey(
        Vorgang,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="letter_drafts",
    )
    recipient = models.ForeignKey(
        Correspondent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="letter_drafts_received",
    )
    sender = models.ForeignKey(
        Correspondent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="letter_drafts_sent",
    )

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.RUNNING
    )
    error = models.TextField(blank=True)

    subject = models.CharField(max_length=255, blank=True)
    body_text = models.TextField(blank=True)
    # Freie Zusatzhinweise des Nutzers für die Generierung (Schwerpunkt,
    # persönlicher Schreibstil) -- Prompt-Material, kein Briefinhalt.
    notes = models.TextField(blank=True)

    letter_date = models.DateField(null=True, blank=True)
    sender_block = models.TextField(blank=True)
    recipient_block = models.TextField(blank=True)
    signature = models.TextField(blank=True)
    layout = models.JSONField(default=dict, blank=True)

    # Was die Bindungen (#1094) ergeben haben, plus die manuell
    # nachgetragenen Werte -- getrennt gehalten: `manual_values` ist
    # Nutzereingabe (überlebt ein „neu generieren"), `placeholder_values`
    # ist das aufgelöste Gesamtergebnis, das in den Prompt ging.
    manual_values = models.JSONField(default=dict, blank=True)
    placeholder_values = models.JSONField(default=dict, blank=True)

    docx_file = models.FileField(upload_to="letters/%Y/%m/", blank=True)
    pdf_file = models.FileField(upload_to="letters/%Y/%m/", blank=True)

    # Erst mit der Freigabe gesetzt: das abgelegte Dokument am Vorgang.
    document = models.OneToOneField(
        Document,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="letter_draft",
    )

    ai_model = models.CharField(max_length=100, blank=True)
    ai_model_version = models.CharField(max_length=50, blank=True)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_letter_drafts",
    )
    departments = models.ManyToManyField(
        Department, blank=True, related_name="letter_drafts"
    )
    visibility = models.CharField(
        max_length=20,
        choices=Visibility.choices,
        default=Visibility.DEPARTMENT,
    )

    objects = LetterDraftQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Brief-Entwurf"
        verbose_name_plural = "Brief-Entwürfe"

    def __str__(self):
        return self.subject or f"Brief-Entwurf {self.pk}"

    @property
    def is_running(self):
        return self.status == self.Status.RUNNING

    @property
    def is_finalized(self):
        return self.status == self.Status.FINALIZED

    @property
    def is_editable(self):
        """Solange nichts abgelegt ist und gerade nichts läuft, gehört der
        Text dem Nutzer -- auch nach einem fehlgeschlagenen KI-Lauf, denn
        dann darf er ihn selbst schreiben, statt in einer Sackgasse zu
        stehen.
        """
        return self.status in (self.Status.READY, self.Status.FAILED)

    @property
    def has_files(self):
        return bool(self.docx_file) and bool(self.pdf_file)

    def layout_value(self, key):
        """Layout-Wert aus dem Snapshot, mit Rückfall auf das Grundlayout --
        derselbe Vertrag wie `LetterTemplate.layout_value`, nur eben gegen
        die eingefrorene Kopie.
        """
        return (self.layout or {}).get(key, DEFAULT_LETTER_LAYOUT.get(key))


class VorgangRecommendationRun(TimeStampedModel):
    """Das gespeicherte Ergebnis *einer* Empfehlungs-Generierung für einen
    Vorgang (#1093): Lage-Einschätzung + die Liste der Empfehlungen.

    Bewusst ein eigenes, kleines Modell statt eines JSON-Felds auf
    `Vorgang`: jede Empfehlung trägt einen eigenen Status
    (offen/übernommen/verworfen), eine n:n-Verknüpfung zu ihren
    Quell-Dokumenten und ggf. die daraus erzeugte `Task` -- das sind echte
    Relationen, die in einem JSON-Blob nur als handgepflegte ID-Listen
    ohne FK-Integrität lägen. Additiv, kein DROP (Lehre #1055).

    `OneToOneField`, nicht FK mit Historie: angezeigt wird ohnehin immer
    nur der aktuelle Stand, und eine wachsende Lauf-Historie wäre Ballast,
    den niemand liest. "Neu generieren" arbeitet deshalb auf derselben
    Zeile weiter -- `status` geht auf `running`, die alten Empfehlungen
    bleiben so lange sichtbar, bis der Worker sie ersetzt (siehe
    `apps.documents.recommendations.generate_vorgang_recommendations`).

    `based_on` hält fest, worauf der Lauf fußte -- die tatsächlich in den
    Prompt gegangenen Dokument-IDs (Provenienz/Limit) *und* den kompletten
    Dokumentbestand des Vorgangs zu dem Zeitpunkt
    (`considered_document_ids`). Der Veraltet-Hinweis vergleicht gegen
    letzteren: käme er gegen die gekürzte Prompt-Liste, wäre ein Lauf mit
    aktivem Limit sofort nach dem Generieren "veraltet".
    """

    class Status(models.TextChoices):
        RUNNING = "running", "Wird erstellt"
        READY = "ready", "Fertig"
        FAILED = "failed", "Fehlgeschlagen"

    vorgang = models.OneToOneField(
        Vorgang, on_delete=models.CASCADE, related_name="recommendation_run"
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.RUNNING
    )
    situation = models.TextField(blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    based_on = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    ai_model = models.CharField(max_length=100, blank=True)
    ai_model_version = models.CharField(max_length=50, blank=True)

    class Meta:
        verbose_name = "Handlungsempfehlungen"
        verbose_name_plural = "Handlungsempfehlungen"

    def __str__(self):
        return f"Empfehlungen fuer Vorgang {self.vorgang_id}"

    @property
    def is_running(self):
        return self.status == self.Status.RUNNING

    @property
    def has_result(self):
        """True sobald überhaupt schon einmal ein Ergebnis eingetroffen ist
        -- `generated_at` ist erst nach dem ersten erfolgreichen Lauf
        gesetzt, `status` kann währenddessen längst wieder `running` sein.
        """
        return self.generated_at is not None


class VorgangRecommendation(TimeStampedModel):
    """Eine einzelne, prüfenswerte Empfehlung aus einem
    `VorgangRecommendationRun` (#1093).

    Immer nur ein Vorschlag: `status` startet auf `open` und wechselt
    ausschliesslich durch eine Nutzeraktion nach `accepted` (dann hängt an
    `task` die daraus erzeugte Aufgabe) oder `dismissed`. Nichts hier legt
    von sich aus eine `Task` an.

    `documents` sind die Quell-Dokumente, auf die sich die Empfehlung
    beruft (Provenienz) -- beim Übernehmen werden genau sie mit der neuen
    Aufgabe verknüpft.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Offen"
        ACCEPTED = "accepted", "Übernommen"
        DISMISSED = "dismissed", "Verworfen"

    class Priority(models.TextChoices):
        HIGH = "hoch", "Hoch"
        MEDIUM = "mittel", "Mittel"
        LOW = "niedrig", "Niedrig"

    run = models.ForeignKey(
        VorgangRecommendationRun, on_delete=models.CASCADE, related_name="recommendations"
    )
    order = models.PositiveIntegerField(default=0)
    title = models.CharField(max_length=255)
    rationale = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True)
    priority = models.CharField(max_length=20, choices=Priority.choices, blank=True)
    documents = models.ManyToManyField(
        Document, blank=True, related_name="vorgang_recommendations"
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN
    )
    task = models.ForeignKey(
        Task,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_recommendations",
    )

    class Meta:
        ordering = ["run_id", "order", "pk"]
        verbose_name = "Handlungsempfehlung"
        verbose_name_plural = "Handlungsempfehlungen"

    def __str__(self):
        return self.title
