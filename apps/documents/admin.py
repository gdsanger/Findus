from django.contrib import admin

from .models import Chunk, Correspondent, Document, DocumentLink, Project, Tag


class ChunkInline(admin.TabularInline):
    """Read-only view of a document's chunks.

    Chunks are produced by the ingest/embedding pipeline (follow-up issue),
    not hand-entered — so the inline has no add permission and never shows
    the `embedding` vector, which has no sane form representation.
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


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "correspondent", "processing_status", "visibility", "created_at")
    list_filter = ("processing_status", "visibility", "departments", "tags")
    search_fields = ("title", "text_content")
    autocomplete_fields = ("correspondent", "owner", "projects", "tags")
    filter_horizontal = ("departments",)
    inlines = [ChunkInline, DocumentLinkInline]


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
