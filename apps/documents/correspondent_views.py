"""UI for the Absender/Kontakt index/hub (#1041): a directory to browse to
a Correspondent, and a hub page that reuses the existing filtered document
list (`apps.documents.views.filtered_documents` / `_document_list.html`)
scoped to that Correspondent -- deliberately no second document-list
implementation.
"""

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import CorrespondentForm
from .models import Correspondent, Document, Tag, Task, Vorgang
from .views import (
    DOCUMENTS_PAGE_SIZE,
    PENDING_STATUSES,
    _ingest_uploaded_file,
    _upload_departments_and_visibility,
    _upload_response,
    filtered_documents,
)

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
def correspondent_create(request):
    if request.method == "POST":
        form = CorrespondentForm(request.POST)
        if form.is_valid():
            correspondent = form.save()
            return redirect("documents:correspondent_detail", pk=correspondent.pk)
    else:
        form = CorrespondentForm()

    return render(request, "documents/correspondents/form.html", {"form": form})


@login_required
@require_POST
def correspondent_delete(request, pk):
    """Deletes only the Correspondent, never its Documents (#1050): the FK
    on `Document.correspondent` is `on_delete=SET_NULL`, so this just drops
    the assignment.
    """
    correspondent = get_object_or_404(Correspondent, pk=pk)
    correspondent.delete()
    return redirect("documents:correspondent_list")


@login_required
def correspondent_detail(request, pk):
    """Kontext-Header (editierbare Kontaktdaten + Eingang/Ausgang-Kennzahlen,
    #1050) plus the reused, Correspondent-scoped document list (still
    further filterable by Tag/Status/Richtung/Vorgang).
    """
    correspondent = get_object_or_404(Correspondent, pk=pk)

    if request.method == "POST":
        form = CorrespondentForm(request.POST, instance=correspondent)
        if form.is_valid():
            form.save()
            return redirect("documents:correspondent_detail", pk=correspondent.pk)
    else:
        form = CorrespondentForm(instance=correspondent)

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
        "form": form,
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
            "view": request.GET.get("view", "").strip(),
        },
        "upload_allowed_extensions": settings.FINDUS_INGEST_ALLOWED_EXTENSIONS,
        "upload_max_size_mb": settings.FINDUS_UPLOAD_MAX_SIZE_MB,
    }

    if request.htmx:
        return render(request, "documents/partials/_document_list.html", context)
    return render(request, "documents/correspondents/detail.html", context)


@login_required
@require_POST
def correspondent_document_upload(request, pk):
    """Upload straight onto the Kontakt-Hub (#1049): same ingest contract as
    the global upload (`documents:upload`), just with `correspondent`
    pre-assigned -- the KI-Analyse (#1048) never overwrites an already-set
    `Document.correspondent`, so this manual assignment sticks.
    """
    correspondent = get_object_or_404(Correspondent, pk=pk)
    departments, visibility = _upload_departments_and_visibility(request.user)
    uploaded_files = request.FILES.getlist("files")

    results = [
        _ingest_uploaded_file(
            request.user, departments, visibility, uploaded_file, correspondent=correspondent
        )
        for uploaded_file in uploaded_files
    ]

    return _upload_response(request, results)
