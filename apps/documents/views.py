from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render

from .models import Correspondent, Document, Tag, Task, Vorgang
from .retrieval import DocumentRetrievalService

DOCUMENTS_PAGE_SIZE = 20
SEARCH_RESULTS_LIMIT = 50


def _filtered_documents(request):
    """Apply the combinable Absender/Vorgang/Tag/Status filters (#1014) on
    top of the visibility scope -- filters narrow what's already visible,
    they never widen it.
    """
    documents = (
        Document.objects.visible_to(request.user)
        .select_related("correspondent")
        .prefetch_related("vorgaenge", "tags")
    )

    correspondent_id = request.GET.get("correspondent", "").strip()
    if correspondent_id:
        documents = documents.filter(correspondent_id=correspondent_id)

    vorgang_id = request.GET.get("vorgang", "").strip()
    if vorgang_id:
        documents = documents.filter(vorgaenge__id=vorgang_id)

    tag_id = request.GET.get("tag", "").strip()
    if tag_id:
        documents = documents.filter(tags__id=tag_id)

    status = request.GET.get("status", "").strip()
    if status:
        documents = documents.filter(processing_status=status)

    if vorgang_id or tag_id:
        documents = documents.distinct()

    return documents


def _search_hits(request, query):
    """Rank visible documents for `query` through the retrieval service
    (#1005) -- semantic search never touches Document/Chunk directly, and
    it applies the same combinable Absender/Vorgang/Tag/Status filters as
    structured browsing above.
    """
    service = DocumentRetrievalService(request.user)
    tag_id = request.GET.get("tag", "").strip()

    return service.search(
        query,
        limit=SEARCH_RESULTS_LIMIT,
        correspondent=request.GET.get("correspondent", "").strip() or None,
        vorgang=request.GET.get("vorgang", "").strip() or None,
        tags=[tag_id] if tag_id else None,
        status=request.GET.get("status", "").strip() or None,
    )


@login_required
def document_list(request):
    query = request.GET.get("q", "").strip()

    if query:
        results = _search_hits(request, query)
        result_partial = "documents/partials/_search_results.html"
    else:
        results = _filtered_documents(request)
        result_partial = "documents/partials/_document_list.html"

    paginator = Paginator(results, DOCUMENTS_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    query_without_page = request.GET.copy()
    query_without_page.pop("page", None)

    context = {
        "page_obj": page_obj,
        "query_without_page": query_without_page.urlencode(),
        "result_partial": result_partial,
        "search_query": query,
        "correspondents": Correspondent.objects.all(),
        "vorgaenge": Vorgang.objects.all(),
        "tags": Tag.objects.all(),
        "status_choices": Document.ProcessingStatus.choices,
        "selected": {
            "correspondent": request.GET.get("correspondent", ""),
            "vorgang": request.GET.get("vorgang", ""),
            "tag": request.GET.get("tag", ""),
            "status": request.GET.get("status", ""),
        },
    }

    if request.htmx:
        return render(request, result_partial, context)
    return render(request, "documents/home.html", context)


def _visible_document(user, pk):
    return get_object_or_404(
        Document.objects.visible_to(user)
        .select_related("correspondent")
        .prefetch_related("vorgaenge", "tags"),
        pk=pk,
    )


@login_required
def document_detail(request, pk):
    document = _visible_document(request.user, pk)
    visible_documents = Document.objects.visible_to(request.user)

    context = {
        "document": document,
        "tasks": Task.objects.visible_to(request.user)
        .filter(documents=document)
        .prefetch_related("checklist_items"),
        "outgoing_links": document.links_from.select_related("to_document").filter(
            to_document__in=visible_documents
        ),
        "incoming_links": document.links_to.select_related("from_document").filter(
            from_document__in=visible_documents
        ),
    }
    return render(request, "documents/detail.html", context)


@login_required
def document_meta_edit(request, pk):
    """Render the editable Vorgang/Tag assignment form (#1016, HTMX
    nice-to-have) -- swapped into `#document-meta` in place of the
    read-only view.
    """
    document = _visible_document(request.user, pk)
    context = {
        "document": document,
        "all_vorgaenge": Vorgang.objects.all(),
        "all_tags": Tag.objects.all(),
        "selected_vorgang_ids": set(document.vorgaenge.values_list("id", flat=True)),
        "selected_tag_ids": set(document.tags.values_list("id", flat=True)),
    }
    return render(request, "documents/partials/_detail_meta_edit.html", context)


@login_required
def document_meta(request, pk):
    """Display partial for `#document-meta` -- also the save target: a POST
    here sets Vorgänge/Tags, then re-renders the same read-only view.
    """
    document = _visible_document(request.user, pk)
    if request.method == "POST":
        document.vorgaenge.set(request.POST.getlist("vorgaenge"))
        document.tags.set(request.POST.getlist("tags"))
    return render(request, "documents/partials/_detail_meta.html", {"document": document})
