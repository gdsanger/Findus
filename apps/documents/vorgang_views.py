"""UI for the Vorgang index/hub (#1040): a directory to browse to a
Vorgang, and a hub page that reuses the existing filtered document list
(`apps.documents.views.filtered_documents` / `_document_list.html`)
scoped to that Vorgang, plus its linked tasks -- deliberately no second
document-list implementation.
"""

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, render

from .models import Document, Tag, Task, Vorgang
from .views import DOCUMENTS_PAGE_SIZE, PENDING_STATUSES, filtered_documents

VORGANG_SORT_FIELDS = {
    "name": "name",
    "documents": "-document_count",
    "activity": "-last_activity",
}


@login_required
def vorgang_list(request):
    query = request.GET.get("q", "").strip()
    sort = request.GET.get("sort", "name").strip()
    order_by = VORGANG_SORT_FIELDS.get(sort, "name")

    visible_documents = Document.objects.visible_to(request.user)
    vorgaenge = Vorgang.objects.annotate(
        document_count=Count(
            "documents", filter=Q(documents__in=visible_documents), distinct=True
        ),
        last_activity=Max(
            "documents__created_at", filter=Q(documents__in=visible_documents)
        ),
    )
    if query:
        vorgaenge = vorgaenge.filter(name__icontains=query)
    vorgaenge = vorgaenge.order_by(order_by, "name")

    context = {
        "vorgaenge": vorgaenge,
        "search_query": query,
        "sort": sort,
    }
    return render(request, "documents/vorgaenge/list.html", context)


@login_required
def vorgang_detail(request, pk):
    """Kontext-Header + the reused, Vorgang-scoped document list (still
    further filterable by Tag/Status/Richtung) plus the tasks linked to
    this Vorgang's documents (Document:Task n:n).
    """
    vorgang = get_object_or_404(Vorgang, pk=pk)
    visible_documents = Document.objects.visible_to(request.user)

    documents = filtered_documents(request).filter(vorgaenge=vorgang).distinct()
    paginator = Paginator(documents, DOCUMENTS_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    query_without_page = request.GET.copy()
    query_without_page.pop("page", None)

    has_pending = any(
        document.processing_status in PENDING_STATUSES for document in page_obj
    )

    tasks = (
        Task.objects.visible_to(request.user)
        .filter(documents__vorgaenge=vorgang)
        .distinct()
        .prefetch_related("documents")
    )

    context = {
        "vorgang": vorgang,
        "document_count": visible_documents.filter(vorgaenge=vorgang).distinct().count(),
        "open_tasks_count": tasks.filter(status=Task.Status.OPEN).count(),
        "tasks": tasks,
        "page_obj": page_obj,
        "query_without_page": query_without_page.urlencode(),
        "has_pending": has_pending,
        "list_url": request.path,
        "tags": Tag.objects.all(),
        "status_choices": Document.ProcessingStatus.choices,
        "direction_choices": Document.Direction.choices,
        "selected": {
            "tag": request.GET.get("tag", ""),
            "status": request.GET.get("status", ""),
            "direction": request.GET.get("direction", ""),
        },
    }

    if request.htmx:
        return render(request, "documents/partials/_document_list.html", context)
    return render(request, "documents/vorgaenge/detail.html", context)
