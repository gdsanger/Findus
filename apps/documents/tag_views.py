"""UI for the Tag index/hub (#1051): a directory grouped by dimension, and
a hub page that reuses the existing filtered document list
(`apps.documents.views.filtered_documents` / `_document_list.html`) scoped
to that Tag -- deliberately no second document-list implementation. Moves
Tag CRUD out of the shared Stammdaten page (#1021), which is now empty and
removed along with it.
"""

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import TagForm
from .models import Correspondent, Document, Tag, Vorgang
from .views import DOCUMENTS_PAGE_SIZE, PENDING_STATUSES, filtered_documents


@login_required
def tag_list(request):
    query = request.GET.get("q", "").strip()

    visible_documents = Document.objects.visible_to(request.user)
    tags = Tag.objects.annotate(
        document_count=Count("documents", filter=Q(documents__in=visible_documents), distinct=True)
    )
    if query:
        tags = tags.filter(Q(name__icontains=query) | Q(dimension__icontains=query))
    tags = tags.order_by("dimension", "name")

    groups = []
    current_dimension = None
    for tag in tags:
        if tag.dimension != current_dimension or not groups:
            groups.append({"dimension": tag.dimension, "tags": []})
            current_dimension = tag.dimension
        groups[-1]["tags"].append(tag)

    context = {"groups": groups, "search_query": query}
    return render(request, "documents/tags/list.html", context)


@login_required
def tag_create(request):
    if request.method == "POST":
        form = TagForm(request.POST)
        if form.is_valid():
            tag = form.save()
            return redirect("documents:tag_detail", pk=tag.pk)
    else:
        form = TagForm()

    return render(request, "documents/tags/form.html", {"form": form})


@login_required
@require_POST
def tag_delete(request, pk):
    """Deletes only the Tag, never its Documents (#1051): removing the row
    just drops the rows in the `Document.tags` M2M through table.
    """
    tag = get_object_or_404(Tag, pk=pk)
    tag.delete()
    return redirect("documents:tag_list")


@login_required
def tag_detail(request, pk):
    """Kontext-Header (editierbare Name/Dimension/Farbe) + the reused,
    Tag-scoped document list (still further filterable by Kontakt/Vorgang/
    Status/Richtung).
    """
    tag = get_object_or_404(Tag, pk=pk)

    if request.method == "POST":
        form = TagForm(request.POST, instance=tag)
        if form.is_valid():
            form.save()
            return redirect("documents:tag_detail", pk=tag.pk)
    else:
        form = TagForm(instance=tag)

    visible_documents = Document.objects.visible_to(request.user).filter(tags=tag)

    documents = filtered_documents(request).filter(tags=tag).distinct()
    paginator = Paginator(documents, DOCUMENTS_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    query_without_page = request.GET.copy()
    query_without_page.pop("page", None)

    has_pending = any(
        document.processing_status in PENDING_STATUSES for document in page_obj
    )

    context = {
        "tag": tag,
        "form": form,
        "document_count": visible_documents.distinct().count(),
        "page_obj": page_obj,
        "query_without_page": query_without_page.urlencode(),
        "has_pending": has_pending,
        "list_url": request.path,
        "show_search": False,
        "show_correspondent": True,
        "show_vorgang_filter": True,
        "show_tag_filter": False,
        # Die Wiedervorlagen-Ansicht (#1129) gibt es nur auf Home: sie listet
        # DocumentComment statt Document und hat hier keine Entsprechung. Der
        # Umschalter wird deshalb ausgeblendet statt einen Knopf anzubieten,
        # der still auf die Tabelle zurückfällt (CLAUDE.md, "toter Button").
        "show_followup_view": False,
        "correspondents": Correspondent.objects.all(),
        "vorgaenge": Vorgang.objects.all(),
        "status_choices": Document.ProcessingStatus.choices,
        "direction_choices": Document.Direction.choices,
        "sphere_choices": Document.Sphere.choices,
        "tax_relevance_filter_choices": Document.tax_relevance_filter_choices(),
        "selected": {
            "correspondent": request.GET.get("correspondent", ""),
            "vorgang": request.GET.get("vorgang", ""),
            "status": request.GET.get("status", ""),
            "direction": request.GET.get("direction", ""),
            "sphere": request.GET.get("sphere", ""),
            "tax_relevance": request.GET.get("tax_relevance", ""),
            # Kachel ist der Default -- derselbe wie auf Home (#1124), damit
            # die Ansicht beim Wechsel Home <-> Hub nicht springt. "" =
            # Liste, "timeline" = Zeitleiste (#1087).
            "view": request.GET.get("view", "grid").strip(),
        },
    }

    if request.htmx:
        return render(request, "documents/partials/_document_list.html", context)
    return render(request, "documents/tags/detail.html", context)
