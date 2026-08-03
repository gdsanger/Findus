"""UI for the Absender/Kontakt index/hub (#1041): a directory to browse to
a Correspondent, and a hub page that reuses the existing filtered document
list (`apps.documents.views.filtered_documents` / `_document_list.html`)
scoped to that Correspondent -- deliberately no second document-list
implementation.
"""

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, render

from .models import Correspondent, Document, Tag, Task, Vorgang
from .views import DOCUMENTS_PAGE_SIZE, PENDING_STATUSES, filtered_documents

CORRESPONDENT_SORT_FIELDS = {
    "name": "name",
    "documents": "-document_count",
    "activity": "-last_activity",
}


@login_required
def correspondent_list(request):
    query = request.GET.get("q", "").strip()
    sort = request.GET.get("sort", "name").strip()
    order_by = CORRESPONDENT_SORT_FIELDS.get(sort, "name")

    visible_documents = Document.objects.visible_to(request.user)
    correspondents = Correspondent.objects.annotate(
        document_count=Count(
            "documents", filter=Q(documents__in=visible_documents), distinct=True
        ),
        eingang_count=Count(
            "documents",
            filter=Q(documents__in=visible_documents, documents__direction=Document.Direction.EINGANG),
            distinct=True,
        ),
        ausgang_count=Count(
            "documents",
            filter=Q(documents__in=visible_documents, documents__direction=Document.Direction.AUSGANG),
            distinct=True,
        ),
        last_activity=Max(
            "documents__created_at", filter=Q(documents__in=visible_documents)
        ),
    )
    if query:
        correspondents = correspondents.filter(name__icontains=query)
    correspondents = correspondents.order_by(order_by, "name")

    context = {
        "correspondents": correspondents,
        "search_query": query,
        "sort": sort,
    }
    return render(request, "documents/correspondents/list.html", context)


@login_required
def correspondent_detail(request, pk):
    """Kontext-Header (Kontaktdaten + Eingang/Ausgang-Kennzahlen) plus the
    reused, Correspondent-scoped document list (still further filterable
    by Tag/Status/Richtung/Vorgang).
    """
    correspondent = get_object_or_404(Correspondent, pk=pk)
    visible_documents = Document.objects.visible_to(request.user).filter(correspondent=correspondent)

    documents = filtered_documents(request).filter(correspondent=correspondent)
    paginator = Paginator(documents, DOCUMENTS_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    query_without_page = request.GET.copy()
    query_without_page.pop("page", None)

    has_pending = any(
        document.processing_status in PENDING_STATUSES for document in page_obj
    )

    open_tasks_count = (
        Task.objects.visible_to(request.user)
        .filter(status=Task.Status.OPEN, documents__correspondent=correspondent)
        .distinct()
        .count()
    )

    context = {
        "correspondent": correspondent,
        "document_count": visible_documents.count(),
        "eingang_count": visible_documents.filter(direction=Document.Direction.EINGANG).count(),
        "ausgang_count": visible_documents.filter(direction=Document.Direction.AUSGANG).count(),
        "open_tasks_count": open_tasks_count,
        "page_obj": page_obj,
        "query_without_page": query_without_page.urlencode(),
        "has_pending": has_pending,
        "list_url": request.path,
        "show_search": False,
        "show_correspondent": False,
        "show_vorgang_filter": True,
        "vorgaenge": Vorgang.objects.all(),
        "tags": Tag.objects.all(),
        "status_choices": Document.ProcessingStatus.choices,
        "direction_choices": Document.Direction.choices,
        "selected": {
            "vorgang": request.GET.get("vorgang", ""),
            "tag": request.GET.get("tag", ""),
            "status": request.GET.get("status", ""),
            "direction": request.GET.get("direction", ""),
        },
    }

    if request.htmx:
        return render(request, "documents/partials/_document_list.html", context)
    return render(request, "documents/correspondents/detail.html", context)
