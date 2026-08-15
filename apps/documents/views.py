import datetime
import logging
import mimetypes

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import DateField, Exists, OuterRef, Prefetch
from django.db.models.functions import Coalesce, TruncDate
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from apps.ingest.service import ingest_file

from .analysis import analyze_and_finalize
from .comment_views import document_comments_context
from .forms import TaskForm
from .models import (
    Correspondent,
    Document,
    DocumentComment,
    DocumentLink,
    DocumentReference,
    SuggestionStatus,
    Tag,
    Task,
    Vorgang,
    link_documents,
    normalize_reference_value,
    tax_relevance_filter_q,
)
from .reference_matching import (
    REFERENCE_OWNERS,
    assign_suggested,
    assignment_suggestions,
    learn_references_from_document,
)
from .references import set_reference, shared_reference_groups
from .retrieval import DocumentRetrievalService
from .task_views import task_departments_and_visibility

FOLLOW_UP_FILTER_CHOICES = [
    ("due", "Nur fällige"),
]

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

    Structured browsing shows only Leitdokumente (`.roots()`, #1069) --
    Unterdokumente hängen eingeklappt darunter (see `_document_list.html`)
    statt als eigene Top-Level-Zeile aufzutauchen. Semantic search
    (`_search_hits`) intentionally does *not* use this: a hit inside a
    Unterdokument must surface on its own there.
    """
    documents = (
        Document.objects.visible_to(request.user)
        .roots()
        .select_related("correspondent")
        .prefetch_related(
            "vorgaenge",
            "tags",
            Prefetch(
                "children",
                queryset=Document.objects.visible_to(request.user).select_related(
                    "correspondent"
                ),
            ),
        )
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

    sphere = request.GET.get("sphere", "").strip()
    if sphere:
        documents = documents.filter(sphere=sphere)

    tax_q = tax_relevance_filter_q(request.GET.get("tax_relevance", ""))
    if tax_q is not None:
        documents = documents.filter(tax_q)

    action_status = request.GET.get("action_status", "").strip()
    if action_status:
        documents = documents.filter(action_status=action_status)

    today = timezone.localdate()
    follow_up = request.GET.get("follow_up", "").strip()
    if follow_up == "due":
        documents = documents.filter(
            comments__follow_up_date__isnull=False, comments__follow_up_date__lte=today
        )

    if vorgang_id or tag_id or follow_up == "due":
        documents = documents.distinct()

    # Dezenter Indikator fuer die Kachelansicht (#1125): unabhaengig vom
    # `follow_up`-Filter annotiert, damit die Kachel auch dann zeigt "hier
    # steht was an", wenn der Filter selbst nicht aktiv ist. `Exists` statt
    # eines zweiten `filter()`/`distinct()` -- vermeidet, dass ein Dokument
    # mit mehreren faelligen Kommentaren mehrfach in der Seite auftaucht.
    documents = documents.annotate(
        has_due_comment=Exists(
            DocumentComment.objects.filter(
                document_id=OuterRef("pk"),
                follow_up_date__isnull=False,
                follow_up_date__lte=today,
            )
        )
    )

    # Default-Sortierung nach Dokumentdatum, nicht Upload-Datum (#1085):
    # `document_date` (KI-erkannt/user-korrigiert) absteigend, mit
    # Upload-Datum (`created_at`) als Fallback fuer Dokumente ohne
    # erkanntes Datum -- als DB-seitiges `Coalesce` annotiert, damit
    # Fallback-Dokumente an ihrer chronologisch richtigen Stelle
    # einsortiert werden statt pauschal an den Rand zu rutschen.
    # `-created_at` bricht Gleichstaende (gleicher Tag) deterministisch.
    documents = documents.annotate(
        effective_date=Coalesce(
            "document_date", TruncDate("created_at"), output_field=DateField()
        )
    ).order_by("-effective_date", "-created_at")

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
        sphere=request.GET.get("sphere", "").strip() or None,
        tax_relevance=request.GET.get("tax_relevance", "").strip() or None,
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
        "sphere_choices": Document.Sphere.choices,
        "tax_relevance_filter_choices": Document.tax_relevance_filter_choices(),
        "action_status_choices": Document.ActionStatus.choices,
        "follow_up_choices": FOLLOW_UP_FILTER_CHOICES,
        "selected": {
            "correspondent": request.GET.get("correspondent", ""),
            "vorgang": request.GET.get("vorgang", ""),
            "tag": request.GET.get("tag", ""),
            "status": request.GET.get("status", ""),
            "direction": request.GET.get("direction", ""),
            "sphere": request.GET.get("sphere", ""),
            "tax_relevance": request.GET.get("tax_relevance", ""),
            "action_status": request.GET.get("action_status", ""),
            "follow_up": request.GET.get("follow_up", ""),
            # Kachel/Grid ist der Default (#1124); "" = Liste, "timeline" =
            # Zeitleiste (#1087). Der Wert steuert nur die Darstellung im
            # gemeinsamen Listen-Partial, nicht das Filtern/Sortieren.
            "view": request.GET.get("view", "grid").strip(),
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
        .select_related("correspondent", "parent")
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


def _children_context(user, document):
    visible_documents = Document.objects.visible_to(user)
    return {
        "document": document,
        "children": document.children.filter(pk__in=visible_documents).select_related(
            "correspondent"
        ),
    }


def _links_context(user, document, *, link_error=None):
    """Der "Verknüpfungen"-Tab des Details (#1126): manuell gesetzte
    Querverweise (`DocumentLink`, #1088) dieses Dokuments -- ein- und
    ausgehend -- plus die (gescopte) Auswahl zum Anlegen eines neuen.

    Läuft durch `visible_to`: die Querverweise über das `in_bulk` auf
    `visible_documents` -- ein Link auf ein Dokument, das der Nutzer nicht
    sehen darf, verschwindet damit still, statt dessen Titel zu leaken.

    Bewusst getrennt von `_related_context` (#1126): die Verknüpfungen
    werden direkt mitgerendert, die Ähnlichkeitssuche erst beim Öffnen
    ihres Tabs nachgeladen -- die kNN-Query gehört nicht in jeden
    Detailaufruf.
    """
    visible_documents = Document.objects.visible_to(user).select_related("correspondent")

    links = list(DocumentLink.objects.for_document(document))
    linked_by_id = visible_documents.in_bulk(
        [link.other_document_id(document) for link in links]
    )
    linked_documents = [
        {"link": link, "document": linked_by_id[link.other_document_id(document)]}
        for link in links
        if link.other_document_id(document) in linked_by_id
    ]

    return {
        "document": document,
        "linked_documents": linked_documents,
        "link_candidates": visible_documents.exclude(
            pk__in=[document.pk, *linked_by_id]
        ).order_by("-created_at")[: settings.FINDUS_DOCUMENT_LINK_PICKER_LIMIT],
        "link_error": link_error,
    }


def _related_context(user, document):
    """Der "Ähnliche Dokumente"-Tab des Details (#1088/#1126): die
    automatischen Ähnlichkeits-Treffer aus dem `DocumentRetrievalService`.

    Läuft durch `visible_to` (der Service scopet selbst). Bereits manuell
    verknüpfte Dokumente fliegen aus der Ähnlichkeitsliste, damit derselbe
    Treffer nicht zugleich unter "Verknüpfungen" und "Ähnliche" steht.
    """
    linked_ids = [
        link.other_document_id(document)
        for link in DocumentLink.objects.for_document(document)
    ]

    similar_hits = DocumentRetrievalService(user).similar_documents(
        document, exclude_ids=linked_ids
    )

    return {
        "document": document,
        "similar_hits": similar_hits,
        # Ohne Chunks gibt es kein Embedding und damit keine Ähnlichkeit --
        # das ist ein anderer Zustand als "nichts über dem Schwellwert" und
        # verdient im Template einen eigenen Hinweis.
        "document_is_indexed": document.chunks.exists(),
        "similarity_min_score_percent": round(
            settings.FINDUS_SIMILAR_DOCUMENTS_MIN_SCORE * 100
        ),
    }


@login_required
def document_links(request, pk):
    """Render the manual-links block on its own (#1126).

    Swap-Ziel nach dem Anlegen/Löschen eines Querverweises und -- weil die
    Verknüpfungen in einem eigenen Tab neben den Ähnlichen stehen --
    zusätzlich per `findus:links-refresh` nachgeladen, wenn ein Detach
    (#1111) einen Soft-Link anlegt, während der Tab gerade nicht sichtbar
    ist.
    """
    document = _visible_document(request.user, pk)
    return render(
        request,
        "documents/partials/_detail_links.html",
        _links_context(request.user, document),
    )


@login_required
def document_related(request, pk):
    """Render the related-documents block on its own (#1088).

    Das Detail lädt den Block per HTMX erst beim Öffnen des Ähnlichkeits-Tabs
    nach (#1126), statt ihn synchron mitzurendern: die kNN-Query ist zwar
    billig, aber sie ist Beiwerk -- Titel, Zusammenfassung und Original
    sollen nicht auf sie warten. Derselbe Endpunkt ist auch das Swap-Ziel
    nach dem Anlegen/Löschen eines Querverweises (via `findus:related-refresh`).
    """
    document = _visible_document(request.user, pk)
    return render(
        request,
        "documents/partials/_detail_related.html",
        _related_context(request.user, document),
    )


@login_required
@require_POST
def document_link_create(request, pk):
    """Verknüpfe dieses Dokument mit einem zweiten (#1088, "gehört zu").

    Das Ziel wird erneut durch `visible_to` gezogen, nicht nur aus dem POST
    übernommen: die Auswahlliste ist zwar schon gescoped, aber eine
    handgeschriebene ID darf keinen Verweis auf ein fremdes Dokument
    anlegen können (und über den Block dessen Titel zurückspielen).
    """
    document = _visible_document(request.user, pk)
    target_id = request.POST.get("target", "").strip()
    target = (
        Document.objects.visible_to(request.user).filter(pk=target_id).first()
        if target_id.isdigit()
        else None
    )

    link_error = None
    if target is None:
        link_error = "Bitte ein Dokument zum Verknüpfen auswählen."
    elif target.pk == document.pk:
        link_error = "Ein Dokument kann nicht mit sich selbst verknüpft werden."
    else:
        link_documents(
            document,
            target,
            created_by=request.user,
            note=request.POST.get("note", "").strip()[:255],
        )

    response = render(
        request,
        "documents/partials/_detail_links.html",
        _links_context(request.user, document, link_error=link_error),
    )
    # Der Ähnlichkeits-Tab hängt an einem eigenen Target und weiß nichts vom
    # Swap hier -- ein neu verknüpftes Dokument muss dort aus der
    # Trefferliste verschwinden (#1126).
    response["HX-Trigger"] = "findus:related-refresh"
    return response


@login_required
@require_POST
def document_link_delete(request, pk, link_id):
    """Remove a manual Querverweis (#1088) -- gescoped über `for_document`,
    damit die ID aus der URL nur einen Link lösen kann, der dieses (für den
    Nutzer sichtbare) Dokument auch wirklich betrifft.
    """
    document = _visible_document(request.user, pk)
    link = get_object_or_404(DocumentLink.objects.for_document(document), pk=link_id)
    link.delete()
    response = render(
        request,
        "documents/partials/_detail_links.html",
        _links_context(request.user, document),
    )
    # Ein gelöster Querverweis kann als Ähnlichkeits-Treffer wieder auftauchen
    # -- der Ähnlichkeits-Tab muss sich also neu laden (#1126).
    response["HX-Trigger"] = "findus:related-refresh"
    return response


def _references_context(user, document, *, reference_error=None):
    """Der Kennungen-Block des Details (#1099): die typisierten Kennungen
    dieses Dokuments (KI-extrahiert und von Hand gepflegt, gemeinsam
    editierbar) plus die je Kennung gruppierten Dokumente, die exakt
    dieselbe tragen.

    Beides in *einem* Kontext/Partial, weil das eine das andere erzeugt:
    wer eine Kennung korrigiert, muss die daraus folgenden Verknüpfungen
    sofort sehen -- eine getrennt nachzuladende Trefferliste stünde nach
    jeder Korrektur veraltet daneben. Aus demselben Grund stehen seit
    #1100 auch die Zuordnungs-Vorschläge hier: eine korrigierte Kennung
    kann genau den Vorschlag erzeugen (oder erledigen), auf den es
    ankommt.
    """
    return {
        "document": document,
        "references": list(document.references.all()),
        "reference_groups": shared_reference_groups(user, document),
        "reference_suggestions": assignment_suggestions(user, document),
        "reference_type_choices": DocumentReference.Type.choices,
        "reference_role_choices": DocumentReference.Role.choices,
        "reference_error": reference_error,
    }


@login_required
def document_references(request, pk):
    """Render the Kennungen block on its own (#1099) -- Swap-Ziel nach
    jedem Anlegen/Ändern/Löschen einer Kennung.
    """
    document = _visible_document(request.user, pk)
    return render(
        request,
        "documents/partials/_detail_references.html",
        _references_context(request.user, document),
    )


@login_required
@require_POST
def document_reference_create(request, pk):
    """Kennung von Hand nachtragen (#1099).

    Die Extraktion ist nicht perfekt -- eine übersehene oder falsch
    gelesene Nummer muss nachtragbar sein, sonst hängt der exakte Match an
    der Tagesform des Modells. Angelegt wird als `Source.MANUAL`, damit
    ein späterer Analyse-Lauf die Zeile nicht wieder wegräumt.
    """
    document = _visible_document(request.user, pk)
    reference = set_reference(
        document,
        reference_type=request.POST.get("type", ""),
        value=request.POST.get("value", ""),
        role=request.POST.get("role", ""),
    )
    if reference is not None:
        learn_references_from_document(document)
    reference_error = None if reference is not None else "Bitte eine Kennung eingeben."
    return render(
        request,
        "documents/partials/_detail_references_and_groups.html",
        _references_context(request.user, document, reference_error=reference_error),
    )


@login_required
@require_POST
def document_reference_update(request, pk, reference_id):
    """Eine Kennung korrigieren (#1099) -- Typ, Wert und Rolle.

    Umgesetzt als Löschen+Neuanlegen über `set_reference()` statt als
    In-Place-Edit: der korrigierte Wert kann auf eine bereits vorhandene
    Kennung desselben Dokuments fallen (z. B. beim Tippfehler-Fix), und
    ein blindes `save()` liefe dann in die UniqueConstraint. So endet
    derselbe Fall in genau einer Zeile.

    Ein leerer Wert ändert nichts, statt die Zeile stillschweigend zu
    entfernen -- Löschen ist ein eigener, bestätigter Knopf, kein
    Nebeneffekt eines versehentlich geleerten Feldes.

    Die korrigierte Zeile gilt danach als manuell gepflegt, auch wenn sie
    aus der KI-Analyse stammte -- eine Korrektur, die der nächste
    Analyse-Lauf überschreibt, wäre keine.
    """
    document = _visible_document(request.user, pk)
    reference = get_object_or_404(document.references, pk=reference_id)
    reference_error = None
    if normalize_reference_value(request.POST.get("value", "")):
        reference.delete()
        set_reference(
            document,
            reference_type=request.POST.get("type", reference.type),
            value=request.POST.get("value", ""),
            role=request.POST.get("role", ""),
        )
        # Die korrigierte Schreibweise ist die, gegen die künftig gematcht
        # wird -- ein bereits zugeordneter Vorgang/Kontakt muss sie
        # übernehmen (#1100). Die alte, falsche Zeile bleibt am Ziel
        # stehen: sie wurde einmal bewusst gelernt und lässt sich dort
        # entfernen, aber nicht von hier aus erraten.
        learn_references_from_document(document)
    else:
        reference_error = "Bitte eine Kennung eingeben."
    return render(
        request,
        "documents/partials/_detail_references_and_groups.html",
        _references_context(request.user, document, reference_error=reference_error),
    )


@login_required
@require_POST
def document_reference_delete(request, pk, reference_id):
    """Eine Kennung löschen (#1099) -- gescoped über die Reverse-Relation,
    damit eine ID aus der URL nur eine Kennung dieses (sichtbaren)
    Dokuments treffen kann.
    """
    document = _visible_document(request.user, pk)
    reference = get_object_or_404(document.references, pk=reference_id)
    reference.delete()
    return render(
        request,
        "documents/partials/_detail_references_and_groups.html",
        _references_context(request.user, document),
    )


@login_required
@require_POST
def document_reference_assign(request, pk, scope, target_id):
    """Einen Zuordnungs-Vorschlag (#1100) mit einem Klick annehmen.

    Zugeordnet wird nur, was auch wirklich vorgeschlagen war: der Ziel-PK
    aus der URL muss in `assignment_suggestions()` auftauchen. Das ist
    keine Rechteprüfung -- Zuordnen darf jeder über die Zuordnungs-Maske
    --, sondern die Zusicherung, dass dieser Endpunkt genau das tut, was
    daneben steht. Ein Vorschlag, der inzwischen weg ist (Kennung
    korrigiert, Kontakt gesetzt, Vorgang schon zugeordnet), führt zu einem
    aktualisierten Block statt zu einer stillen Zuordnung.

    Neben dem Kennungen-Block wird die Zuordnungs-Anzeige (`#document-meta`)
    per Out-of-Band-Swap mitgetauscht: die Zuordnung ist der eigentliche
    Effekt des Klicks und darf nicht bis zum nächsten Seitenaufbau
    unsichtbar bleiben.
    """
    if scope not in REFERENCE_OWNERS:
        raise Http404(f"Unbekannter Kennungs-Scope: {scope}")

    document = _visible_document(request.user, pk)
    suggestion = next(
        (
            candidate
            for candidate in assignment_suggestions(request.user, document)
            if candidate["scope"] == scope and candidate["target"].pk == target_id
        ),
        None,
    )
    if suggestion is not None:
        assign_suggested(document, suggestion)
        document = _visible_document(request.user, pk)

    return render(
        request,
        "documents/partials/_detail_references_assigned.html",
        {
            **_references_context(request.user, document),
            "action_status_choices": Document.ActionStatus.choices,
        },
    )


@login_required
def document_detail(request, pk):
    document = _visible_document(request.user, pk)
    visible_documents = Document.objects.visible_to(request.user)

    context = {
        **_document_tasks_context(request.user, document),
        **document_comments_context(request.user, document),
        **_analysis_status_context(document),
        **_children_context(request.user, document),
        **_references_context(request.user, document),
        # Verknüpfungen direkt mitrendern (#1126); der Ähnlichkeits-Tab
        # lädt seinen Inhalt dagegen erst beim Öffnen nach.
        **_links_context(request.user, document),
        # Direktzugriff auf ein Unterdokument zeigt den Elternkontext
        # (#1069) -- aber nur, wenn das Leitdokument auch sichtbar ist:
        # der Scope eines Kindes ist überschreibbar, ein sichtbares Kind
        # mit unsichtbarem Elter darf dessen Titel nicht leaken.
        "parent_visible": (
            document.parent_id is not None
            and visible_documents.filter(pk=document.parent_id).exists()
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
    inline PDF preview (#1036) from embedding this route in an iframe.
    `@xframe_options_sameorigin` relaxes the header to `SAMEORIGIN` for *this
    endpoint only*, so our own app can embed it while DENY stays in force
    everywhere else (no site-wide clickjacking weakening).
    """
    document = _visible_document(request.user, pk)
    if not document.original_file:
        raise Http404("Kein Original vorhanden.")
    return _stream_original(document, as_attachment=True)


@login_required
@xframe_options_sameorigin
def document_original_preview(request, pk):
    """Inline variant of the same auth-gated stream (#1036), embedded by the

    detail page's fest eingebettete Vorschau (#1126) as an `<iframe>`/`<img>`
    source. Gated by the same `mime_type` whitelist as
    `Document.is_inline_previewable` so a format the browser can't render
    natively never reaches this view -- the template only offers the
    Download-Platzhalter for those.

    This is the route the inline `<iframe>` in `detail.html` actually points
    at, so it -- not `document_original_download` -- needs
    `@xframe_options_sameorigin` to relax the global `DENY` from
    `XFrameOptionsMiddleware` for PDFs. `<img>` previews are unaffected by
    X-Frame-Options, but the decorator is harmless for them too.
    """
    document = _visible_document(request.user, pk)
    if not document.original_file or not document.is_inline_previewable:
        raise Http404("Keine Vorschau verfügbar.")
    return _stream_original(document, as_attachment=False)


@login_required
def document_thumbnail(request, pk):
    """Serve the first-page Vorschaubild (#1123) through the same
    `visible_to` scoping as every other document view -- the storage
    backend's own URL is a public S3/MinIO link that bypasses the ACL, so
    the thumbnail (like the original, #1024) must only ever be reached
    through this auth-gated route, never linked directly.

    A missing thumbnail (not renderable, render failed, or a Bestand doc not
    yet backfilled) is a 404, not an error -- the Kachel-Template falls back
    to a type placeholder. Scoping via `_visible_document` means a foreign
    document is a 404 too, not a 403, so the endpoint never leaks that a pk
    exists. `Cache-Control: private` because the image is per-document and
    unveraenderlich, but must not land in a shared/proxy cache given the
    auth gate.
    """
    document = _visible_document(request.user, pk)
    if not document.thumbnail:
        raise Http404("Kein Thumbnail vorhanden.")
    content_type = mimetypes.guess_type(document.thumbnail.name)[0] or "image/webp"
    response = FileResponse(
        document.thumbnail.open("rb"),
        content_type=content_type,
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = (
        f"private, max-age={settings.FINDUS_THUMBNAIL_CACHE_MAX_AGE}"
    )
    return response


@login_required
@require_POST
def document_delete(request, pk):
    """Delete a document (#1022) from the list or the detail page, guarded

    by the same `visible_to` scope (department/owner) as every other
    document view -- confirmation happens client-side before the POST is
    ever sent. Chunks, tag/Vorgang suggestions, Unterdokumente (#1069) and
    the Task m2m rows all cascade via FK `on_delete=CASCADE`/m2m cleanup;
    only the object-storage files need an explicit delete, since Django
    never removes FileField contents on its own -- both the `original_file`
    and the `thumbnail` (#1123). Deleting a Leitdokument takes its whole
    subtree with it, so every descendant's files need the same treatment,
    not just this document's own.
    """
    document = _visible_document(request.user, pk)
    for doc in [document, *document.subtree()]:
        if doc.original_file:
            doc.original_file.delete(save=False)
        if doc.thumbnail:
            doc.thumbnail.delete(save=False)
    document.delete()
    return redirect("documents:home")


@login_required
@require_POST
def document_child_delete(request, pk, child_id):
    """Delete a single Unterdokument from the parent's children list (#1080)

    -- mail attachments bring a lot of noise (signature logos etc.) that's
    worth discarding individually without losing the rest of the mail.
    Same delete mechanics as `document_delete` (#1022), just scoped to one
    child (plus its own subtree, in case it has children of its own)
    instead of the whole tree, and re-rendered as the HTMX partial instead
    of a redirect so the list updates without a full page reload.

    The child is looked up through `visible_to` again, not just the parent
    -- a child's scope can be overridden independently of its parent
    (`Document.create_child`), so parent visibility alone isn't sufficient
    proof the user may act on this specific child.
    """
    document = _visible_document(request.user, pk)
    child = get_object_or_404(
        Document.objects.visible_to(request.user),
        pk=child_id,
        parent_id=document.pk,
    )
    for doc in [child, *child.subtree()]:
        if doc.original_file:
            doc.original_file.delete(save=False)
        if doc.thumbnail:
            doc.thumbnail.delete(save=False)
    child.delete()
    return render(
        request,
        "documents/partials/_detail_children.html",
        _children_context(request.user, document),
    )


@login_required
@require_POST
def document_child_detach(request, pk, child_id):
    """Löse ein Unterdokument von seinem Leitdokument (#1111).

    Der häufige Fall aus dem Mail-Ingest (#1069/#1070): belangloses
    Anschreiben, wichtiger Anhang. Der Anhang hängt als Unterdokument am
    Mail-Text, taucht damit weder im Eingang noch im Browsing auf
    (`DocumentQuerySet.roots()`) und war bisher nur *löschbar* (#1080) --
    Trennen macht ihn stattdessen zum eigenständigen Leitdokument, ohne
    dass ein Byte verloren geht: `parent`/`child_role` fallen weg, Original,
    Extraktion, Kennungen und Chunks bleiben unangetastet.

    Kein Migrationsbedarf: `parent` ist bereits nullable (#1069).

    Der Scope wird bei `create_child` *kopiert*, nicht zur Laufzeit vom
    Elter geerbt -- ein getrenntes Kind behält also seinen eigenen gültigen
    Sichtbarkeitsbereich. Nur für den Fall, dass er (z. B. durch eine
    Alt-Zuordnung von Hand) leer ist, werden `owner`/`departments` vorher
    vom bisherigen Leitdokument übernommen: sonst entstünde genau die
    verwaiste Karteikarte ohne erklärten Scope, vor der der Kommentar an
    `Document.parent` warnt -- und die nach dem Trennen niemand mehr sähe.

    Die harte Eltern-Kante wird zum Soft-Link herabgestuft statt ersatzlos
    gekappt (`link_documents`, #1088): der inhaltliche Bezug Anhang ↔
    ursprüngliche Mail bleibt auf beiden Detailseiten sichtbar.

    Beide Seiten laufen wie bei `document_child_delete` durch `visible_to`
    -- die Sichtbarkeit des Elters allein ist kein Beleg dafür, dass der
    Nutzer dieses Kind anfassen darf.
    """
    document = _visible_document(request.user, pk)
    child = get_object_or_404(
        Document.objects.visible_to(request.user),
        pk=child_id,
        parent_id=document.pk,
    )

    if child.owner_id is None:
        child.owner_id = document.owner_id
    if not child.departments.exists():
        child.departments.set(document.departments.all())

    former_role = child.get_child_role_display() if child.child_role else ""
    child.parent = None
    child.child_role = ""
    child.save()

    link_documents(
        child,
        document,
        created_by=request.user,
        note=f"vormals {former_role or 'Unterdokument'}",
    )

    # Aus der Leitdokument-Ansicht heraus wird nur die Unterdokumente-Liste
    # neu gerendert (HTMX). Aus dem Unterdokument selbst kommt ein normaler
    # POST -- danach zurück auf dessen eigene Detailseite, die jetzt ohne
    # Breadcrumb als Leitdokument rendert.
    if request.htmx:
        response = render(
            request,
            "documents/partials/_detail_children.html",
            _children_context(request.user, document),
        )
        # Der Verknüpfungs- und der Ähnlichkeits-Tab hängen an eigenen
        # Targets und wissen nichts vom Swap hier -- der neue Soft-Link
        # (#1088) erscheint sonst erst beim nächsten Seitenaufbau, und das
        # getrennte Kind taucht in beiden Blöcken erst dann auf (#1126).
        response["HX-Trigger"] = "findus:links-refresh, findus:related-refresh"
        return response
    return redirect("documents:detail", pk=child.pk)


def _meta_edit_context(document, quick_create_error=None):
    return {
        "document": document,
        "all_correspondents": Correspondent.objects.all(),
        "all_vorgaenge": Vorgang.objects.all(),
        "all_tags": Tag.objects.all(),
        "direction_choices": Document.Direction.choices,
        "sphere_choices": Document.Sphere.choices,
        "tax_relevance_choices": Document.TaxRelevance.choices,
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
        # Sphäre (#1112) / private ESt-Absetzbarkeit (#1113): ein manuelles
        # Speichern ist die Nutzerentscheidung, die jede Re-Analyse ab jetzt
        # respektiert -- deshalb weiter unten auch die "noch nicht
        # bestaetigt"-Kennzeichen (`*_source`) aus `metadata` entfernen,
        # damit die KI-Badges im Detail verschwinden.
        sphere = request.POST.get("sphere", "").strip()
        if sphere in Document.Sphere.values:
            document.sphere = sphere
        # Aendert sich die Steuerrelevanz gegenueber dem gespeicherten Wert,
        # wird die KI-Begruendung verworfen -- sie begruendete die alte,
        # jetzt ueberstimmte Einschaetzung.
        tax_relevance = request.POST.get("tax_relevance", "").strip()
        if tax_relevance in Document.TaxRelevance.values:
            if tax_relevance != document.tax_relevance:
                document.tax_relevance_reason = ""
            document.tax_relevance = tax_relevance
        if document.metadata.get("sphere_source") or document.metadata.get(
            "tax_relevance_source"
        ):
            metadata = dict(document.metadata)
            metadata.pop("sphere_source", None)
            metadata.pop("tax_relevance_source", None)
            document.metadata = metadata
        document_date = request.POST.get("document_date", "").strip()
        if not document_date:
            document.document_date = None
        else:
            try:
                document.document_date = datetime.date.fromisoformat(document_date)
            except ValueError:
                pass
        document.save(
            update_fields=[
                "correspondent",
                "direction",
                "sphere",
                "tax_relevance",
                "tax_relevance_reason",
                "document_date",
                "metadata",
                "updated_at",
            ]
        )
        document.vorgaenge.set(request.POST.getlist("vorgaenge"))
        document.tags.set(request.POST.getlist("tags"))
        # Zuordnen heißt: "diese Nummern gehören hierher" (#1100). Eine
        # entfernte Zuordnung nimmt die gelernte Kennung *nicht* wieder
        # mit -- der Vorgang hat sein Aktenzeichen dann trotzdem, und was
        # er nicht behalten soll, wird an seinem Hub entfernt.
        learn_references_from_document(document)
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

    A blank name used to be silently dropped, which looked from the user's
    side like the button did nothing (#1064) -- it now reports back a
    visible error next to the field it belongs to instead.
    """
    if kind not in _QUICK_CREATE_KINDS:
        raise Http404(f"Unbekannte Zuordnungsart: {kind}")

    document = _visible_document(request.user, pk)
    # Each quick-create block carries a kind-specific field name
    # (`correspondent_name`/`vorgang_name`/`tag_name`) rather than a shared
    # `name`. All three blocks live inside the same <form>, which HTMX
    # serialises in full on every POST -- with a shared `name` the two empty
    # blocks collided with the filled one and Django's QueryDict.get() picked
    # the last (empty) value, so the typed name never reached the server (#1064).
    name = request.POST.get(f"{kind}_name", "").strip()
    dimension = request.POST.get(f"{kind}_dimension", "").strip()
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
            dimension = _truncated(Tag, "dimension", dimension)
            name = _truncated(Tag, "name", name)
            tag, _created = Tag.objects.get_or_create(name=name, dimension=dimension)
            document.tags.add(tag)
        if kind in ("correspondent", "vorgang"):
            learn_references_from_document(document)
    else:
        quick_create_error = {
            "kind": kind,
            "message": "Bitte einen Namen eingeben.",
            "name": name,
            "dimension": dimension,
        }

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
    learn_references_from_document(document)
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
