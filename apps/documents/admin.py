from django.contrib import admin

from .models import (
    ChecklistItem,
    Chunk,
    Correspondent,
    Document,
    DocumentLink,
    Project,
    Tag,
    Task,
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


class DocumentLinkInline(admin.TabularInline):
    model = DocumentLink
    fk_name = "from_document"
    autocomplete_fields = ("to_document",)
    extra = 0


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


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "correspondent",
        "processing_status",
        "extraction_method",
        "visibility",
        "created_at",
    )
    list_filter = ("processing_status", "extraction_method", "visibility", "departments", "tags")
    search_fields = ("title", "text_content")
    autocomplete_fields = ("correspondent", "owner", "projects", "tags")
    filter_horizontal = ("departments",)
    readonly_fields = ("processing_error", "extraction_method")
    inlines = [ChunkInline, DocumentLinkInline, DocumentTaskInline]


@admin.register(Correspondent)
class CorrespondentAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "created_at")
    search_fields = ("name", "email")


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")
    search_fields = ("name",)


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
