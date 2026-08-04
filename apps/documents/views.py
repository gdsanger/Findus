import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from apps.ingest.service import ingest_file

from .analysis import analyze_and_finalize
from .forms import TaskForm
from .models import Correspondent, Document, SuggestionStatus, Tag, Task, Vorgang
from .retrieval import DocumentRetrievalService
from .task_views import task_departments_and_visibility

logger = logging.getLogger(__name__)

DOCUMENTS_PAGE_SIZE = 20
SEARCH_RESULTS_LIMIT = 50

PENDING_STATUSES = {
    Document.ProcessingStatus.PENDING,
    Document.ProcessingStatus.EXTRACTING,
    Document.ProcessingStatus.ANALYZING,
    Document.ProcessingStatus.EMBEDDING,
}


def filtered_documents(request):
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

    direction = request.GET.get("direction", "").strip()
    if direction:
        documents = documents.filter(direction=direction)

    action_status = request.GET.get("action_status", "").strip()
    if action_status:
        documents = documents.filter(action_status=action_status)

    if vorgang_id or tag_id:
        documents = documents.distinct()

    return documents


def _search_hits(request, query):
    """Rank visible documents for `query` through the retrieval service
    (#1005) -- semantic search never touches Document/Chunk directly, and
    it applies the same combinable Absender/Vorgang/Tag/Status/Richtung
    filters as structured browsing above.
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
        direction=request.GET.get("direction", "").strip() or None,
        action_status=request.GET.get("action_status", "").strip() or None,
    )


@login_required
def document_list(request):
    query = request.GET.get("q", "").strip()

    if query:
        results = _search_hits(request, query)
        result_partial = "documents/partials/_search_results.html"
    else:
        results = filtered_documents(request)
        result_partial = "documents/partials/_document_list.html"

    paginator = Paginator(results, DOCUMENTS_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    query_without_page = request.GET.copy()
    query_without_page.pop("page", None)

    # Search hits (query set) wrap Document in a SearchHit, not a bare
    # Document -- polling only applies to the plain list, so this is only
    # ever computed for that branch.
    has_pending = not query and any(
        document.processing_status in PENDING_STATUSES for document in page_obj
    )

    context = {
        "page_obj": page_obj,
        "query_without_page": query_without_page.urlencode(),
        "result_partial": result_partial,
        "search_query": query,
        "has_pending": has_pending,
        "list_url": request.path,
        "show_search": True,
        "show_correspondent": True,
        "show_vorgang_filter": True,
        "correspondents": Correspondent.objects.all(),
        "vorgaenge": Vorgang.objects.all(),
        "tags": Tag.objects.all(),
        "status_choices": Document.ProcessingStatus.choices,
        "direction_choices": Document.Direction.choices,
        "action_status_choices": Document.ActionStatus.choices,
        "selected": {
            "correspondent": request.GET.get("correspondent", ""),
            "vorgang": request.GET.get("vorgang", ""),
            "tag": request.GET.get("tag", ""),
            "status": request.GET.get("status", ""),
            "direction": request.GET.get("direction", ""),
            "action_status": request.GET.get("action_status", ""),
        },
        "upload_allowed_extensions": settings.FINDUS_INGEST_ALLOWED_EXTENSIONS,
        "upload_max_size_mb": settings.FINDUS_UPLOAD_MAX_SIZE_MB,
    }

    if request.htmx:
        return render(request, result_partial, context)
    return render(request, "documents/home.html", context)


def _upload_departments_and_visibility(user):
    """A department-less user can still upload -- it just lands `private`
    (owner-only) instead of unscoped, since `Document.visibility` has no
    "nobody" option.
    """
    departments = list(user.departments.all())
    if departments:
        return departments, Document.Visibility.DEPARTMENT
    return departments, Document.Visibility.PRIVATE


def _ingest_uploaded_file(user, departments, visibility, uploaded_file, *, correspondent=None, vorgang=None):
    """`correspondent`/`vorgang` (#1049) pre-assign a newly created Document
    to the Kontakt/Vorgang Hub it was dropped on -- same as a folder/mail
    connector's provenance, just from the Hub context instead of a resolved
    sender. Only applied to a newly created Document, mirroring how
    `ingest_file` already treats `correspondent` and `department` on a
    duplicate hit (the existing document's assignment is left alone).
    """
    extension = uploaded_file.name.rsplit(".", 1)[-1].lower() if "." in uploaded_file.name else ""
    allowed_extensions = {ext.lower() for ext in settings.FINDUS_INGEST_ALLOWED_EXTENSIONS}
    if allowed_extensions and extension not in allowed_extensions:
        return {
            "filename": uploaded_file.name,
            "status": "error",
            "message": f"Dateityp „{extension or '?'}“ wird nicht unterstützt.",
        }

    max_size_bytes = settings.FINDUS_UPLOAD_MAX_SIZE_MB * 1024 * 1024
    if uploaded_file.size > max_size_bytes:
        return {
            "filename": uploaded_file.name,
            "status": "error",
            "message": f"Datei zu groß (max. {settings.FINDUS_UPLOAD_MAX_SIZE_MB:g} MB).",
        }

    try:
        result = ingest_file(
            uploaded_file,
            filename=uploaded_file.name,
            source=Document.Source.UPLOAD,
            department=departments[0] if departments else None,
            owner=user,
            visibility=visibility,
            content_type=uploaded_file.content_type or "",
            correspondent=correspondent,
        )
    except Exception:
        logger.exception("Upload: Ingest fehlgeschlagen für %s", uploaded_file.name)
        return {
            "filename": uploaded_file.name,
            "status": "error",
            "message": "Verarbeitung fehlgeschlagen.",
        }

    if result.created and len(departments) > 1:
        # `ingest_file` only takes a single department (the folder/mail
        # connectors have exactly one); a multi-department user's document
        # still needs to be visible to all of them, so add the rest here.
        result.document.departments.add(*departments[1:])

    if result.created and vorgang is not None:
        result.document.vorgaenge.add(vorgang)

    if result.duplicate:
        return {
            "filename": uploaded_file.name,
            "status": "duplicate",
            "message": "Bereits vorhanden (Duplikat erkannt).",
        }
    return {"filename": uploaded_file.name, "status": "ok", "document": result.document}


def _upload_response(request, results):
    response = render(request, "documents/partials/_upload_feedback.html", {"results": results})
    if results:
        response["HX-Trigger"] = "findus:documents-changed"
    return response


@login_required
@require_POST
def document_upload(request):
    """Upload documents from the browser through the ingest contract
    (#1007) -- same dedup/storage/visibility/enqueue path as the folder and
    mail connectors (`apps.ingest.service.ingest_file`), just fed by an
    HTMX multipart POST instead of a watched folder/mailbox.
    """
    departments, visibility = _upload_departments_and_visibility(request.user)
    uploaded_files = request.FILES.getlist("files")

    results = [
        _ingest_uploaded_file(request.user, departments, visibility, uploaded_file)
        for uploaded_file in uploaded_files
    ]

    return _upload_response(request, results)


def _visible_document(user, pk):
    return get_object_or_404(
        Document.objects.visible_to(user)
        .select_related("correspondent")
        .prefetch_related("vorgaenge", "tags"),
        pk=pk,
    )


def _document_tasks_context(user, document, quick_create_form=None):
    return {
        "document": document,
        "tasks": Task.objects.visible_to(user)
        .filter(documents=document)
        .prefetch_related("checklist_items"),
        "task_kind_choices": Task.Kind.choices,
        "quick_create_form": quick_create_form,
    }


@login_required
def document_detail(request, pk):
    document = _visible_document(request.user, pk)
    visible_documents = Document.objects.visible_to(request.user)

    context = {
        **_document_tasks_context(request.user, document),
        **_analysis_status_context(document),
        "outgoing_links": document.links_from.select_related("to_document").filter(
            to_document__in=visible_documents
        ),
        "incoming_links": document.links_to.select_related("from_document").filter(
            from_document__in=visible_documents
        ),
        "action_status_choices": Document.ActionStatus.choices,
    }
    return render(request, "documents/detail.html", context)


@login_required
@require_POST
def document_task_create(request, pk):
    """Create a Task straight from the document detail page (#1023) --

    same quick-create-without-context-switch principle as
    `document_meta_quick_create`, just producing a `Task` linked to this
    document instead of assigning an existing Absender/Vorgang/Tag.

    Validated through the same `TaskForm` as the full `/tasks/create/` flow
    (#1045) -- building the `Task` straight from raw POST data let an
    unparsable `due_date` reach `DateField.to_python` unvalidated at
    `.save()` time and raise an uncaught `ValidationError` (500), instead of
    a clean inline error next to the quick-create fields.
    """
    document = _visible_document(request.user, pk)
    data = request.POST.copy()
    data.setdefault("status", Task.Status.OPEN)
    form = TaskForm(data)
    if form.is_valid():
        task = form.save(commit=False)
        task.owner = request.user
        departments, visibility = task_departments_and_visibility(request.user)
        task.visibility = visibility
        task.save()
        task.departments.set(departments)
        task.documents.add(document)
        form = None

    return render(
        request,
        "documents/partials/_detail_tasks.html",
        _document_tasks_context(request.user, document, quick_create_form=form),
    )


def _stream_original(document, *, as_attachment):
    response = FileResponse(
        document.original_file.open("rb"),
        content_type=document.mime_type,
        as_attachment=as_attachment,
        filename=document.original_filename,
    )
    # Browsers must trust our Content-Type, not sniff the bytes -- relevant
    # both for the inline preview (never execute an uploaded file as HTML/JS)
    # and the download (never second-guess the declared type).
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@xframe_options_sameorigin
def document_original_download(request, pk):
    """Stream the original file through the same `visible_to` scoping as the
    detail page (#1024) -- the storage backend's own URL is a public S3/
    MinIO link that bypasses ACL entirely, so the original must never be
    linked directly, only served through this auth-gated view.

    The global `XFrameOptionsMiddleware` defaults to `DENY`, which blocks the
    inline PDF preview (#1036) from embedding this route in the slide-over's
    iframe. `@xframe_options_sameorigin` relaxes the header to `SAMEORIGIN`
    for *this endpoint only*, so our own app can embed it while DENY stays in
    force everywhere else (no site-wide clickjacking weakening).
    """
    document = _visible_document(request.user, pk)
    if not document.original_file:
        raise Http404("Kein Original vorhanden.")
    return _stream_original(document, as_attachment=True)


@login_required
@xframe_options_sameorigin
def document_original_preview(request, pk):
    """Inline variant of the same auth-gated stream (#1036), embedded by the

    detail page's Slide-Over as an `<iframe>`/`<img>` source. Gated by the
    same `mime_type` whitelist as `Document.is_inline_previewable` so a
    format the browser can't render natively never reaches this view --
    the template only offers the Download button for those.

    This is the route the Slide-Over's iframe actually points at (see
    `_detail_original_preview.html`), so it -- not `document_original_download`
    -- needs `@xframe_options_sameorigin` to relax the global `DENY` from
    `XFrameOptionsMiddleware` for PDFs. `<img>` previews are unaffected by
    X-Frame-Options, but the decorator is harmless for them too.
    """
    document = _visible_document(request.user, pk)
    if not document.original_file or not document.is_inline_previewable:
        raise Http404("Keine Vorschau verfügbar.")
    return _stream_original(document, as_attachment=False)


@login_required
def document_original_preview_panel(request, pk):
    """Render the Slide-Over markup for `document_original_preview` (#1036).

    Fetched via HTMX from the detail page's trigger button so opening the
    preview never causes a full page reload -- the swapped-in markup is
    then shown as a Bootstrap Offcanvas by `detail.html`'s `htmx:afterSwap`
    handler. Re-checks `is_inline_previewable` even though the trigger
    button only renders for previewable documents, since this view is
    reachable directly by URL too.
    """
    document = _visible_document(request.user, pk)
    if not document.original_file or not document.is_inline_previewable:
        raise Http404("Keine Vorschau verfügbar.")
    return render(
        request,
        "documents/partials/_detail_original_preview.html",
        {"document": document},
    )


@login_required
@require_POST
def document_delete(request, pk):
    """Delete a document (#1022) from the list or the detail page, guarded

    by the same `visible_to` scope (department/owner) as every other
    document view -- confirmation happens client-side before the POST is
    ever sent. Chunks, tag/Vorgang suggestions, DocumentLinks and the Task
    m2m rows all cascade via FK `on_delete=CASCADE`/m2m cleanup; only the
    object-storage original needs an explicit delete, since Django never
    removes FileField contents on its own.
    """
    document = _visible_document(request.user, pk)
    if document.original_file:
        document.original_file.delete(save=False)
    document.delete()
    return redirect("documents:home")


def _meta_edit_context(document, quick_create_error=None):
    return {
        "document": document,
        "all_correspondents": Correspondent.objects.all(),
        "all_vorgaenge": Vorgang.objects.all(),
        "all_tags": Tag.objects.all(),
        "direction_choices": Document.Direction.choices,
        "selected_correspondent_id": document.correspondent_id,
        "selected_vorgang_ids": set(document.vorgaenge.values_list("id", flat=True)),
        "selected_tag_ids": set(document.tags.values_list("id", flat=True)),
        "quick_create_error": quick_create_error,
    }


@login_required
def document_meta_edit(request, pk):
    """Render the editable Absender/Vorgang/Tag assignment form (#1016,
    #1021) -- swapped into `#document-meta` in place of the read-only view.
    """
    document = _visible_document(request.user, pk)
    return render(request, "documents/partials/_detail_meta_edit.html", _meta_edit_context(document))


@login_required
@require_POST
def document_action_status(request, pk):
    """Set `Document.action_status` (#1057) from the list row or detail --

    a dedicated single-field endpoint rather than routing through
    `document_meta`, since the badge+dropdown control needs to save on
    every change without the "Zuordnung bearbeiten" edit form being open.
    """
    document = _visible_document(request.user, pk)
    action_status = request.POST.get("action_status", "").strip()
    if action_status in Document.ActionStatus.values:
        document.action_status = action_status
        document.save(update_fields=["action_status", "updated_at"])
    return render(
        request,
        "documents/partials/_action_status_control.html",
        {"document": document, "action_status_choices": Document.ActionStatus.choices},
    )


def _analysis_status_context(document):
    return {"document": document, "pending_statuses": PENDING_STATUSES}


@login_required
def document_analysis_status(request, pk):
    """Poll target for the detail page's "Analyse erneut ausfuehren"/"Neu
    verarbeiten" controls (#1063): while `processing_status` is still
    pending, the swapped-in partial keeps polling itself via
    `hx-trigger="every ...s"`. Once a poll observes a terminal status, it
    sends `HX-Refresh` instead of just re-rendering the small status
    fragment, so the rest of the page (summary, key facts, suggestions)
    catches up too -- reusing the full-page render rather than
    duplicating every affected partial's logic here.
    """
    document = _visible_document(request.user, pk)
    response = render(
        request,
        "documents/partials/_detail_analysis_status.html",
        _analysis_status_context(document),
    )
    if document.processing_status not in PENDING_STATUSES:
        response["HX-Refresh"] = "true"
    return response


@login_required
@require_POST
def document_analysis_rerun(request, pk):
    """Re-run just the KI-Analyse (#1020) for this document from the detail
    page, as a manual fallback for the rare case it didn't come out right
    (the JSON-robustness fix #1028 lowers how often that happens, this is
    the escape hatch). Queued on the Django-Q worker exactly like
    `manage.py analyze_documents --queue` (`analyze_and_finalize` is the
    same function, so there is no second copy of the finalize-to-a-
    terminal-status logic to keep in sync -- see #1029/#1035).

    `processing_status` flips to `analyzing` here, synchronously, rather
    than waiting for the worker to pick the task up: that's what makes
    the status partial's polling condition true immediately, so the UI
    shows progress from the moment the button is clicked instead of
    still showing the old (possibly `failed`) status for a beat.
    """
    document = _visible_document(request.user, pk)
    document.processing_status = Document.ProcessingStatus.ANALYZING
    document.save(update_fields=["processing_status", "updated_at"])

    from django_q.tasks import async_task

    async_task(analyze_and_finalize, document.id)

    return render(
        request,
        "documents/partials/_detail_analysis_status.html",
        _analysis_status_context(document),
    )


@login_required
@require_POST
def document_reprocess(request, pk):
    """Re-run the whole pipeline (extraction -> KI-Analyse -> embedding,
    #1009/#1020/#1010) for this document from the detail page -- most
    useful for a `failed` document, e.g. one that tripped the NUL-byte
    extraction bug (#1061) and can now go through cleanly. Queues the
    same `extract_document_task` the ingest flow enqueues for a brand new
    upload (`apps.ingest.service._enqueue_processing`), so reprocessing
    isn't a separate pipeline implementation.
    """
    document = _visible_document(request.user, pk)
    document.processing_status = Document.ProcessingStatus.PENDING
    document.processing_error = ""
    document.save(update_fields=["processing_status", "processing_error", "updated_at"])

    from django_q.tasks import async_task

    from .tasks import extract_document_task

    async_task(extract_document_task, document.id)

    return render(
        request,
        "documents/partials/_detail_analysis_status.html",
        _analysis_status_context(document),
    )


@login_required
def document_meta(request, pk):
    """Display partial for `#document-meta` -- also the save target: a POST
    here sets Absender/Vorgänge/Tags, then re-renders the same read-only
    view.
    """
    document = _visible_document(request.user, pk)
    if request.method == "POST":
        correspondent_id = request.POST.get("correspondent", "").strip()
        document.correspondent = (
            get_object_or_404(Correspondent, pk=correspondent_id)
            if correspondent_id.isdigit()
            else None
        )
        direction = request.POST.get("direction", "").strip()
        if direction in Document.Direction.values:
            document.direction = direction
        document.save(update_fields=["correspondent", "direction", "updated_at"])
        document.vorgaenge.set(request.POST.getlist("vorgaenge"))
        document.tags.set(request.POST.getlist("tags"))
    return _render_meta(request, document)


_QUICK_CREATE_KINDS = {"correspondent", "vorgang", "tag"}


def _truncated(model, field_name, value):
    """Clamp free-typed quick-create input to the target field's
    `max_length` -- unlike the Stammdaten forms (ModelForm, full validation),
    this endpoint has no form to reject an overlong value with, and Postgres
    would otherwise raise a hard `DataError` on save.
    """
    max_length = model._meta.get_field(field_name).max_length
    return value[:max_length]


@login_required
@require_POST
def document_meta_quick_create(request, pk, kind):
    """Create+assign a new Absender/Vorgang/Tag straight from the Zuordnung
    edit form (#1021), without a context switch to the Stammdaten pages --
    same match/create-then-assign principle as the KI-suggestion accept
    views, just for a value the user typed themselves instead of one the KI
    proposed.

    The three quick-create inputs used to all share `name="name"`. They sit
    inside the outer Zuordnung <form>, and htmx includes a POST's "related
    form" values with priority over hx-include -- so every "+ Anlegen" click
    submitted all three name fields under the same key, and Django's
    QueryDict.get() returned the *last* one in DOM order (the Tag field)
    regardless of which button was actually clicked. A typed Kontakt name was
    silently replaced by whatever (usually nothing) sat in the Tag field,
    which read from the user's side as "I typed a name and it still says
    it's required" (#1064). Each input now has a kind-prefixed name so they
    can never collide.
    """
    if kind not in _QUICK_CREATE_KINDS:
        raise Http404(f"Unbekannte Zuordnungsart: {kind}")

    document = _visible_document(request.user, pk)
    name = request.POST.get(f"{kind}_name", "").strip()
    quick_create_error = None
    if name:
        if kind == "correspondent":
            name = _truncated(Correspondent, "name", name)
            correspondent, _created = Correspondent.objects.get_or_create(name=name)
            document.correspondent = correspondent
            document.save(update_fields=["correspondent", "updated_at"])
        elif kind == "vorgang":
            name = _truncated(Vorgang, "name", name)
            vorgang, _created = Vorgang.objects.get_or_create(name=name)
            document.vorgaenge.add(vorgang)
        elif kind == "tag":
            dimension = _truncated(Tag, "dimension", request.POST.get("tag_dimension", "").strip())
            name = _truncated(Tag, "name", name)
            tag, _created = Tag.objects.get_or_create(name=name, dimension=dimension)
            document.tags.add(tag)
    else:
        quick_create_error = {"kind": kind, "message": "Bitte einen Namen eingeben."}

    return render(
        request,
        "documents/partials/_detail_meta_edit.html",
        _meta_edit_context(document, quick_create_error=quick_create_error),
    )


def _render_meta(request, document):
    return render(
        request,
        "documents/partials/_detail_meta.html",
        {"document": document, "action_status_choices": Document.ActionStatus.choices},
    )


@login_required
@require_POST
def document_tag_suggestion_accept(request, pk, suggestion_id):
    """Accept a KI-Analyse tag suggestion (#1020): match/create the real

    `Tag` by name+dimension and assign it -- the suggestion only ever
    becomes a real tag through this explicit user action, never on its
    own.
    """
    document = _visible_document(request.user, pk)
    suggestion = get_object_or_404(document.tag_suggestions, pk=suggestion_id)
    tag, _ = Tag.objects.get_or_create(name=suggestion.name, dimension=suggestion.dimension)
    document.tags.add(tag)
    suggestion.status = SuggestionStatus.ACCEPTED
    suggestion.save(update_fields=["status", "updated_at"])
    return _render_meta(request, document)


@login_required
@require_POST
def document_tag_suggestion_reject(request, pk, suggestion_id):
    document = _visible_document(request.user, pk)
    suggestion = get_object_or_404(document.tag_suggestions, pk=suggestion_id)
    suggestion.status = SuggestionStatus.REJECTED
    suggestion.save(update_fields=["status", "updated_at"])
    return _render_meta(request, document)


@login_required
@require_POST
def document_vorgang_suggestion_accept(request, pk, suggestion_id):
    """Accept a KI-Analyse Vorgang suggestion (#1020) -- same

    match/create-then-assign principle as `document_tag_suggestion_accept`.
    """
    document = _visible_document(request.user, pk)
    suggestion = get_object_or_404(document.vorgang_suggestions, pk=suggestion_id)
    vorgang, _ = Vorgang.objects.get_or_create(name=suggestion.name)
    document.vorgaenge.add(vorgang)
    suggestion.status = SuggestionStatus.ACCEPTED
    suggestion.save(update_fields=["status", "updated_at"])
    return _render_meta(request, document)


@login_required
@require_POST
def document_vorgang_suggestion_reject(request, pk, suggestion_id):
    document = _visible_document(request.user, pk)
    suggestion = get_object_or_404(document.vorgang_suggestions, pk=suggestion_id)
    suggestion.status = SuggestionStatus.REJECTED
    suggestion.save(update_fields=["status", "updated_at"])
    return _render_meta(request, document)
