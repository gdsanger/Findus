"""UI for the Vorgang index/hub (#1040): a directory to browse to a
Vorgang, and a hub page that reuses the existing filtered document list
(`apps.documents.views.filtered_documents` / `_document_list.html`)
scoped to that Vorgang, plus its linked tasks -- deliberately no second
document-list implementation.
"""

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import VorgangForm
from .models import Document, Tag, Task, Vorgang
from .views import (
    DOCUMENTS_PAGE_SIZE,
    PENDING_STATUSES,
    _ingest_uploaded_file,
    _upload_departments_and_visibility,
    _upload_response,
    filtered_documents,
)

VORGANG_SORT_FIELDS = {
    "name": "name",
    "documents": "-document_count",
    "activity": "-last_activity",
}

VORGANG_STATUS_FILTER_CHOICES = [("open", "Offen"), ("closed", "Abgeschlossen")]


@login_required
def vorgang_list(request):
    """Vorgänge-Index (#1040), grouped offen/abgeschlossen (#1084): the
    `status` filter is the coarse open-vs-closed distinction users care
    about here, not the three-way `Vorgang.Status` -- `in_progress` counts
    as "offen" for grouping/filtering purposes, it just keeps its own badge.
    """
    query = request.GET.get("q", "").strip()
    sort = request.GET.get("sort", "name").strip()
    status_filter = request.GET.get("status", "").strip()
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
        vorgaenge = vorgaenge.filter(Q(name__icontains=query) | Q(description__icontains=query))
    if status_filter == "open":
        vorgaenge = vorgaenge.exclude(status=Vorgang.Status.CLOSED)
    elif status_filter == "closed":
        vorgaenge = vorgaenge.filter(status=Vorgang.Status.CLOSED)
    vorgaenge = vorgaenge.order_by(order_by, "name")

    open_vorgaenge = []
    closed_vorgaenge = []
    for vorgang in vorgaenge:
        if vorgang.status == Vorgang.Status.CLOSED:
            closed_vorgaenge.append(vorgang)
        else:
            open_vorgaenge.append(vorgang)

    context = {
        "open_vorgaenge": open_vorgaenge,
        "closed_vorgaenge": closed_vorgaenge,
        "search_query": query,
        "sort": sort,
        "status_choices": VORGANG_STATUS_FILTER_CHOICES,
        "selected": {"status": status_filter},
    }
    return render(request, "documents/vorgaenge/list.html", context)


@login_required
def vorgang_create(request):
    if request.method == "POST":
        form = VorgangForm(request.POST)
        if form.is_valid():
            vorgang = form.save()
            return redirect("documents:vorgang_detail", pk=vorgang.pk)
    else:
        form = VorgangForm()

    return render(request, "documents/vorgaenge/form.html", {"form": form})


@login_required
@require_POST
def vorgang_delete(request, pk):
    """Deletes only the Vorgang, never its Documents (#1050): removing the
    row just drops the rows in the `Document.vorgaenge` M2M through table.
    """
    vorgang = get_object_or_404(Vorgang, pk=pk)
    vorgang.delete()
    return redirect("documents:vorgang_list")


@login_required
def vorgang_detail(request, pk):
    """Kontext-Header (editierbare Vorgangsdaten, #1050) + the reused,
    Vorgang-scoped document list (still further filterable by
    Tag/Status/Richtung) plus the tasks linked to this Vorgang's documents
    (Document:Task n:n).
    """
    vorgang = get_object_or_404(Vorgang, pk=pk)

    if request.method == "POST":
        form = VorgangForm(request.POST, instance=vorgang)
        if form.is_valid():
            form.save()
            return redirect("documents:vorgang_detail", pk=vorgang.pk)
    else:
        form = VorgangForm(instance=vorgang)

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
        "form": form,
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
        "upload_allowed_extensions": settings.FINDUS_INGEST_ALLOWED_EXTENSIONS,
        "upload_max_size_mb": settings.FINDUS_UPLOAD_MAX_SIZE_MB,
    }

    if request.htmx:
        return render(request, "documents/partials/_document_list.html", context)
    return render(request, "documents/vorgaenge/detail.html", context)


@login_required
@require_POST
def vorgang_document_upload(request, pk):
    """Upload straight onto the Vorgang-Hub (#1049): same ingest contract as
    the global upload (`documents:upload`), just with `vorgaenge`
    pre-assigned right after creation -- the KI-Analyse (#1048) only ever
    *suggests* Vorgaenge, never assigns one itself, so this manual
    assignment is never at risk of being overwritten.
    """
    vorgang = get_object_or_404(Vorgang, pk=pk)
    departments, visibility = _upload_departments_and_visibility(request.user)
    uploaded_files = request.FILES.getlist("files")

    results = [
        _ingest_uploaded_file(request.user, departments, visibility, uploaded_file, vorgang=vorgang)
        for uploaded_file in uploaded_files
    ]

    return _upload_response(request, results)
