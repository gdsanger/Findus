from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render

from .models import Correspondent, Document, Tag, Vorgang

DOCUMENTS_PAGE_SIZE = 20


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


@login_required
def document_list(request):
    documents = _filtered_documents(request)
    paginator = Paginator(documents, DOCUMENTS_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    query_without_page = request.GET.copy()
    query_without_page.pop("page", None)

    context = {
        "page_obj": page_obj,
        "query_without_page": query_without_page.urlencode(),
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
        return render(request, "documents/partials/_document_list.html", context)
    return render(request, "documents/home.html", context)
