from django.conf import settings
from django.db import models
from django.utils import timezone
from pgvector.django import HnswIndex, VectorField

from apps.accounts.models import Department


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Correspondent(TimeStampedModel):
    """Absender/Empfänger eines Dokuments (z. B. Firma oder Person)."""

    name = models.CharField(max_length=255, unique=True)
    email = models.EmailField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Vorgang(TimeStampedModel):
    """Aktenvorgang/Angelegenheit -- fachlich kein "Projekt"."""

    name = models.CharField(max_length=255, unique=True)

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
        nicht — Durchsetzung läuft rein über diesen Manager/QuerySet.
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


class Document(TimeStampedModel):
    class Visibility(models.TextChoices):
        DEPARTMENT = "department", "Abteilung"
        PRIVATE = "private", "Privat"

    class ProcessingStatus(models.TextChoices):
        """pending -> extracting (#1009, Text-Layer/OCR/Vision-Kaskade) ->
        embedding (#1010, Chunking + Embeddings) -> ready, or failed at
        either stage with `processing_error` set.
        """

        PENDING = "pending", "Ausstehend"
        EXTRACTING = "extracting", "Extraktion läuft"
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

    class Source(models.TextChoices):
        UPLOAD = "upload", "Upload"
        MAIL = "mail", "E-Mail"
        FOLDER = "folder", "Ordner-Überwachung"
        API = "api", "API"

    title = models.CharField(max_length=255)
    correspondent = models.ForeignKey(
        Correspondent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
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

    # Extraktion + Cache.
    text_content = models.TextField(blank=True)
    markdown = models.TextField(blank=True)
    extraction_method = models.CharField(
        max_length=20, choices=ExtractionMethod.choices, blank=True
    )
    metadata = models.JSONField(default=dict, blank=True)
    processing_error = models.TextField(blank=True)

    original_file = models.FileField(upload_to="documents/%Y/%m/", blank=True)
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)

    objects = DocumentQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class DocumentLink(TimeStampedModel):
    class LinkType(models.TextChoices):
        RELATED = "related", "Verwandt"
        REPLY = "reply", "Antwort auf"
        ATTACHMENT = "attachment", "Anhang von"
        SUPERSEDES = "supersedes", "Ersetzt"

    from_document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="links_from"
    )
    to_document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="links_to"
    )
    link_type = models.CharField(
        max_length=20, choices=LinkType.choices, default=LinkType.RELATED
    )
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["from_document", "to_document", "link_type"],
                name="unique_document_link",
            ),
        ]

    def __str__(self):
        return f"{self.from_document_id} -> {self.to_document_id} ({self.link_type})"


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
