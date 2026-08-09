from django.contrib import admin

from .models import (
    ChecklistItem,
    Chunk,
    Correspondent,
    Document,
    DocumentLink,
    LetterDraft,
    LetterTemplate,
    LetterTemplatePlaceholder,
    Tag,
    TagSuggestion,
    Task,
    TaskTemplate,
    TaskTemplateItem,
    Vorgang,
    VorgangRecommendation,
    VorgangRecommendationRun,
    VorgangSuggestion,
)


class ChunkInline(admin.TabularInline):
    """Read-only view of a document's chunks.

    Chunks are produced by the chunking/embedding pipeline
    (`apps.documents.processing`), not hand-entered — so the inline has no
    add permission and never shows the `embedding` vector, which has no
    sane form representation.
    """

    model = Chunk
    fields = ("position", "content", "embedding_model", "embedding_model_version")
    readonly_fields = fields
    extra = 0
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


class DocumentTaskInline(admin.TabularInline):
    """Shows which Tasks (#1012) are linked to this document.

    Uses the auto-created n:n through table directly since `tasks` is only
    a reverse accessor on Document (the field lives on `Task.documents`).
    """

    model = Task.documents.through
    fk_name = "document"
    autocomplete_fields = ("task",)
    extra = 0
    verbose_name = "Verknüpfte Aufgabe"
    verbose_name_plural = "Verknüpfte Aufgaben"


class TagSuggestionInline(admin.TabularInline):
    """Read-only view of the KI-Analyse's tag suggestions (#1020) -- accept

    /reject happens in the document detail UI, not the admin, so no add
    permission here either.
    """

    model = TagSuggestion
    fields = ("name", "dimension", "confidence", "status")
    readonly_fields = fields
    extra = 0

    def has_add_permission(self, request, obj=None):
        return False


class VorgangSuggestionInline(admin.TabularInline):
    model = VorgangSuggestion
    fields = ("name", "confidence", "status")
    readonly_fields = fields
    extra = 0

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "parent",
        "child_role",
        "correspondent",
        "direction",
        "action_status",
        "processing_status",
        "extraction_method",
        "visibility",
        "created_at",
    )
    list_filter = (
        "direction",
        "action_status",
        "processing_status",
        "extraction_method",
        "visibility",
        "departments",
        "tags",
    )
    search_fields = ("title", "text_content")
    autocomplete_fields = ("correspondent", "owner", "parent", "vorgaenge", "tags")
    filter_horizontal = ("departments",)
    readonly_fields = ("processing_error", "extraction_method")
    inlines = [ChunkInline, DocumentTaskInline, TagSuggestionInline, VorgangSuggestionInline]


@admin.register(DocumentLink)
class DocumentLinkAdmin(admin.ModelAdmin):
    """Manuelle Querverweise (#1088) -- angelegt werden sie im
    Dokument-Detail; hier stehen sie nur zum Nachsehen/Aufräumen.

    Beim Anlegen von Hand gilt dieselbe kanonische Ordnung wie in
    `link_documents()` (`document_a` vor `document_b`, nach ID) -- sonst
    weist der DB-Check-Constraint die Zeile ab.
    """

    list_display = ("document_a", "document_b", "note", "created_by", "created_at")
    search_fields = ("document_a__title", "document_b__title", "note")
    autocomplete_fields = ("document_a", "document_b", "created_by")


@admin.register(Correspondent)
class CorrespondentAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "is_self", "vat_id", "iban", "created_at")
    list_filter = ("is_self",)
    search_fields = ("name", "email", "vat_id", "tax_number", "iban")


@admin.register(Vorgang)
class VorgangAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "department", "created_at")
    list_filter = ("status", "department")
    search_fields = ("name",)


class VorgangRecommendationInline(admin.TabularInline):
    """Read-only view of the generated Handlungsempfehlungen (#1093).

    Übernehmen/Verwerfen happens on the Vorgang-Hub, not here -- and
    nothing may hand-create a recommendation, since a row without a real
    generation behind it would carry provenance it never had.
    """

    model = VorgangRecommendation
    fields = ("order", "title", "due_date", "priority", "status", "task")
    readonly_fields = fields
    extra = 0

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(VorgangRecommendationRun)
class VorgangRecommendationRunAdmin(admin.ModelAdmin):
    list_display = ("vorgang", "status", "generated_at", "ai_model")
    list_filter = ("status",)
    search_fields = ("vorgang__name", "situation")
    readonly_fields = ("based_on", "error", "ai_model", "ai_model_version")
    inlines = [VorgangRecommendationInline]


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name", "dimension", "created_at")
    list_filter = ("dimension",)
    search_fields = ("name", "dimension")


class ChecklistItemInline(admin.TabularInline):
    model = ChecklistItem
    fields = ("text", "is_done", "order")
    extra = 0


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "status", "due_date")
    list_filter = ("status", "kind", "due_date")
    search_fields = ("title", "description")
    autocomplete_fields = ("owner", "documents")
    filter_horizontal = ("departments",)
    readonly_fields = ("done_at",)
    inlines = [ChecklistItemInline]


class TaskTemplateItemInline(admin.TabularInline):
    """Sortable checklist blueprint (#1037) -- `order` drives the position

    used when copied onto a task's `ChecklistItem`s.
    """

    model = TaskTemplateItem
    fields = ("text", "order")
    extra = 0


class LetterTemplatePlaceholderInline(admin.TabularInline):
    """Die Daten-Bindungen einer Brief-Vorlage (#1094).

    `source` wird vom Model-Validator gegen die Quellen-Registry
    (`apps.documents.letter_bindings`) geprüft -- auch hier im Admin, wo
    das Feld als freies Textfeld erscheint, weil die gültigen Werte
    bewusst nicht als `choices` am Model stehen.
    """

    model = LetterTemplatePlaceholder
    fields = ("order", "key", "label", "source", "required")
    extra = 0


@admin.register(LetterTemplate)
class LetterTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "visibility", "updated_at")
    list_filter = ("category", "visibility", "departments")
    search_fields = ("name", "description", "category", "instructions")
    autocomplete_fields = ("owner",)
    filter_horizontal = ("departments",)
    inlines = [LetterTemplatePlaceholderInline]


@admin.register(LetterDraft)
class LetterDraftAdmin(admin.ModelAdmin):
    """Brief-Entwürfe (#1095) -- zum Nachsehen, nicht zum Schreiben.

    Der Entwurfsweg ist der Review-Ablauf in der App (generieren, prüfen,
    freigeben); hier soll nur nachvollziehbar sein, was ein Lauf ergeben
    hat. Deshalb sind Status, Rendering-Ergebnis und das abgelegte
    Dokument schreibgeschützt -- ein im Admin auf „abgelegt" gedrehter
    Status hätte kein Dokument hinter sich.
    """

    list_display = ("__str__", "template_name", "status", "recipient", "vorgang", "created_at")
    list_filter = ("status", "visibility")
    search_fields = ("subject", "body_text", "template_name")
    autocomplete_fields = ("owner",)
    filter_horizontal = ("departments",)
    readonly_fields = (
        "status",
        "error",
        "placeholder_values",
        "docx_file",
        "pdf_file",
        "document",
        "ai_model",
        "ai_model_version",
    )


@admin.register(TaskTemplate)
class TaskTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "default_kind", "default_due_offset_days", "visibility")
    list_filter = ("default_kind", "visibility")
    search_fields = ("name", "default_title", "default_description")
    autocomplete_fields = ("owner",)
    filter_horizontal = ("departments",)
    inlines = [TaskTemplateItemInline]
