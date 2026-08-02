from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render

from .models import Correspondent, Document, Tag, Vorgang
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


@login_required
def document_detail(request, pk):
    document = get_object_or_404(
        Document.objects.visible_to(request.user)
        .select_related("correspondent")
        .prefetch_related("vorgaenge", "tags"),
        pk=pk,
    )
    return render(request, "documents/detail.html", {"document": document})
