import datetime
import logging
import mimetypes

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, DateField, Exists, OuterRef, Prefetch, Q
from django.db.models.functions import Coalesce, TruncDate
from django.http import FileResponse, Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from apps.ingest.service import ingest_eml_file, ingest_file, sniff_mime_type

from .analysis import analyze_and_finalize
from .comment_views import document_comments_context
from .document_dates import MANUAL_SOURCE as MANUAL_DATE_SOURCE
from .extraction import (
    expire_vision_reextraction_if_stalled,
    start_vision_reextraction,
    vision_markdown_fingerprint,
)
from .forms import DocumentDateInlineForm
from .long_summary import (
    document_long_summary_is_stale,
    expire_document_long_summary_if_stalled,
    start_document_long_summary,
)
from .models import (
    Correspondent,
    Document,
    DocumentComment,
    DocumentLink,
    DocumentReference,
    SuggestionStatus,
    Tag,
    Vorgang,
    follow_up_week_end,
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


def _combinable_document_filters(request, documents):
    """Apply the combinable Absender/Vorgang/Tag/Status/Richtung/Sphäre/

    Steuerlich/Erledigung filters (#1014) on top of whatever base queryset
    the caller already scoped -- filters narrow, they never widen. Shared
    between `filtered_documents` (Kachel/Liste/Timeline) and
    `_followup_entries` (Wiedervorlagen-Ansicht, #1129), which starts from a
    differently scoped `Document`-Queryset but needs the exact same
    narrowing.

    Returns `(documents, vorgang_id, tag_id)` -- the two ids are handed back
    because `filtered_documents` needs them again to decide whether a
    `.distinct()` is required for its M2M-join fan-out.
    """
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

    return documents, vorgang_id, tag_id


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

    documents, vorgang_id, tag_id = _combinable_document_filters(request, documents)

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

    if request.GET.get("status", "").strip() == Document.ProcessingStatus.READY:
        # Sicht "Zu pruefen" (#1153): aeltestes zuerst, weil das am
        # laengsten liegende Dokument am ehesten vergessen wird -- eine
        # bewusste Ausnahme von der sonst hier geltenden
        # Dokumentdatum-absteigend-Sortierung, an genau dieser einen Stelle
        # (CLAUDE.md, "Zustand & Fachlogik": Datumslogik lebt an einer
        # Stelle, nicht parallel in Template/Job). Dieselbe Sortierung wie
        # `DocumentQuerySet.needs_review()`, hier aber nicht darueber
        # aufgerufen, weil die kombinierbaren Filter oben schon angewendet
        # sind und `needs_review()` selbst noch den Status filtern wuerde.
        #
        # `open_suggestion_count` (nur hier annotiert, nicht in jeder
        # Ansicht): die eigentlichen Pruefkandidaten sind Dokumente mit noch
        # nicht angenommenen/verworfenen Tag-/Vorgangsvorschlaegen. Beide
        # Counts brauchen `distinct=True` -- zwei separate `Count()` ueber
        # zwei verschiedene Reverse-FKs in derselben `annotate()` joinen
        # sonst kreuzweise (Tag-Vorschlaege x Vorgangs-Vorschlaege) und
        # blaehen beide Zahlen auf.
        documents = documents.annotate(
            open_suggestion_count=(
                Count(
                    "tag_suggestions",
                    filter=Q(tag_suggestions__status=SuggestionStatus.PENDING),
                    distinct=True,
                )
                + Count(
                    "vorgang_suggestions",
                    filter=Q(vorgang_suggestions__status=SuggestionStatus.PENDING),
                    distinct=True,
                )
            )
        ).order_by("created_at")
    else:
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


def _followup_entries(request):
    """`DocumentComment`-Queryset für `view=wiedervorlagen` (#1129): gelistet

    wird der einzelne Wiedervorlage-Eintrag, nicht das Dokument -- ein
    Dokument mit zwei Terminen taucht zweimal auf.

    Ausgeschlossen bleiben Wiedervorlagen erledigter Dokumente
    (`action_status=erledigt`): Kommentare kennen keinen eigenen
    Erledigt-Status (#1125, siehe `DocumentComment`-Klassendoc), das
    Dokument abzuhaken ist die einzige Geste, die die Liste aufräumt --
    sonst bliebe jede je gesetzte Wiedervorlage für immer "überfällig"
    stehen. `keine`/`offen` bleiben beide sichtbar: nur ein explizites
    "erledigt" räumt auf.

    Dieselben kombinierbaren Filter wie `filtered_documents` schränken ein,
    welche Dokumente berücksichtigt werden; der `follow_up`("fällig")-Filter
    ist hier redundant (die Gruppierung zeigt bereits alles) und wird
    ignoriert.
    """
    documents = Document.objects.open_visible_to(request.user)
    documents, _vorgang_id, _tag_id = _combinable_document_filters(request, documents)

    return (
        DocumentComment.objects.with_follow_up()
        .filter(document__in=documents)
        .select_related("document", "document__correspondent")
        .order_by("follow_up_date", "created_at")
    )


FOLLOW_UP_GROUP_LABELS = (
    ("overdue", "Überfällig"),
    ("today", "Heute"),
    ("this_week", "Diese Woche"),
    ("later", "Später"),
)


def _grouped_follow_up_entries(entries):
    """Bucket an already-fetched, ascending-by-`follow_up_date` page of

    Wiedervorlage-Einträge into Überfällig/Heute/Diese Woche/Später (#1129)
    -- computed here, not as Datumsarithmetik in the template, and against
    the same week boundary (`follow_up_week_end`) the `due_this_week`/
    `due_later` queryset filters use, so the rule lives in exactly one
    place. Empty groups are dropped: in dieser Ansicht ist die Liste selbst
    der Inhalt, nicht ein fester Rahmen wie bei den Detail-Tabs (#1126).
    """
    today = timezone.localdate()
    week_end = follow_up_week_end(today)
    buckets = {key: [] for key, _label in FOLLOW_UP_GROUP_LABELS}

    for entry in entries:
        if entry.follow_up_date < today:
            buckets["overdue"].append(entry)
        elif entry.follow_up_date == today:
            buckets["today"].append(entry)
        elif entry.follow_up_date <= week_end:
            buckets["this_week"].append(entry)
        else:
            buckets["later"].append(entry)

    return [
        {"key": key, "label": label, "entries": buckets[key]}
        for key, label in FOLLOW_UP_GROUP_LABELS
        if buckets[key]
    ]


@login_required
def document_list(request):
    query = request.GET.get("q", "").strip()
    # Kachel (Default)/Liste/Timeline (#1087/#1124) oder Wiedervorlagen
    # (#1129) -- eine Suche gewinnt immer: eine Ranking-Ansicht und eine
    # Terminliste lassen sich nicht sinnvoll übereinanderlegen, `q` schaltet
    # deshalb auf die Trefferliste zurück, auch wenn `view=wiedervorlagen`
    # in der URL steht (siehe `_document_followups.html`).
    view = request.GET.get("view", "grid").strip()
    is_followup_view = not query and view == "wiedervorlagen"

    if query:
        results = _search_hits(request, query)
        result_partial = "documents/partials/_search_results.html"
    elif is_followup_view:
        results = _followup_entries(request)
        result_partial = "documents/partials/_document_followups.html"
    else:
        results = filtered_documents(request)
        result_partial = "documents/partials/_document_list.html"

    paginator = Paginator(results, DOCUMENTS_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    query_without_page = request.GET.copy()
    query_without_page.pop("page", None)

    # Search hits (query set) wrap Document in a SearchHit, and the
    # Wiedervorlagen-Ansicht lists DocumentComment, not Document -- polling
    # only applies to the plain Document list, so this is only ever computed
    # for that branch.
    has_pending = not query and not is_followup_view and any(
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
        # Home ist die einzige Seite, die die Wiedervorlagen-Ansicht (#1129)
        # rendern kann -- nur hier gehört ihr Umschalter hin.
        "show_followup_view": True,
        "correspondents": Correspondent.objects.all(),
        "vorgaenge": Vorgang.objects.all(),
        "tags": Tag.objects.all(),
        "status_choices": Document.ProcessingStatus.choices,
        "direction_choices": Document.Direction.choices,
        "sphere_choices": Document.Sphere.choices,
        "tax_relevance_filter_choices": Document.tax_relevance_filter_choices(),
        "action_status_choices": Document.ActionStatus.choices,
        "follow_up_choices": FOLLOW_UP_FILTER_CHOICES,
        "follow_up_groups": _grouped_follow_up_entries(page_obj) if is_followup_view else [],
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
            # Zeitleiste (#1087), "wiedervorlagen" = Terminliste (#1129). Der
            # Wert steuert nur die Darstellung im gemeinsamen Listen-Partial,
            # nicht das Filtern/Sortieren.
            "view": view,
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
        mime_type = sniff_mime_type(
            uploaded_file, filename=uploaded_file.name, content_type=uploaded_file.content_type or ""
        )
        if mime_type == "message/rfc822":
            # `.eml` (#1133): Mail als Leitdokument, Anhaenge als
            # Unterdokumente -- derselbe Ingest-Kontrakt, nur ein anderer
            # Einstiegspunkt als ein gewoehnlicher Upload.
            result = ingest_eml_file(
                uploaded_file,
                filename=uploaded_file.name,
                source=Document.Source.UPLOAD,
                department=departments[0] if departments else None,
                owner=user,
                visibility=visibility,
                correspondent=correspondent,
            )
        else:
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


def _content_context(document, *, truncated=True):
    """Der "Inhalt"-Tab des Details (#1142): der extrahierte Rohtext plus
    Herkunft (Extraktionsmethode/-zeitpunkt) und Zeichenanzahl -- die
    schnellste Diagnose fuer eine verstuemmelte OCR/Text-Layer-Extraktion,
    siehe CLAUDE.md "Dokumentdatum" fuer ein Beispiel derselben
    Fehlerklasse (nur dass hier die Ursache direkt sichtbar wird statt nur
    ihre Folgen).

    Die Zeichenanzahl bleibt immer die volle Laenge, auch wenn `content_text`
    fuer die Erstanzeige gekuerzt ist (`truncated=True`, Default) -- ein
    paar hundert Zeichen bei einem mehrseitigen Dokument sind bereits der
    Befund. `document_content_full` ruft mit `truncated=False`, um denselben
    Endpunkt fuer "vollstaendig anzeigen" und "Text kopieren" zu bedienen.

    Fuer `content_format == "markdown"` (#1149) wird nie gekuerzt: eine
    Tabelle, die mitten in einer Zeile abgeschnitten wird, rendert als
    kaputtes Markdown statt als das, was das Feature eigentlich zeigen
    soll. Ein mehrseitiges Vision-Ergebnis ist damit zwar ein groesserer
    Erstladeaufwand, aber immer noch derselbe Text, der ohnehin schon
    vollstaendig in der Datenbank steht.
    """
    text = document.text_content
    char_count = len(text)
    preview_limit = settings.FINDUS_DOCUMENT_CONTENT_PREVIEW_CHARS
    is_truncated = (
        truncated
        and char_count > preview_limit
        and document.content_format != Document.ContentFormat.MARKDOWN
    )

    extracted_at_raw = document.metadata.get("extracted_at")
    extracted_at = None
    if extracted_at_raw:
        try:
            extracted_at = datetime.datetime.fromisoformat(extracted_at_raw)
        except (TypeError, ValueError):
            extracted_at = None

    return {
        "document": document,
        "content_text": text[:preview_limit] if is_truncated else text,
        "content_char_count": char_count,
        "content_truncated": is_truncated,
        "content_extracted_at": extracted_at,
    }


@login_required
def document_content_full(request, pk):
    """Laedt den vollstaendigen Rohtext nach (#1142), dasselbe Muster wie
    `document_related`: die Erstanzeige im Detail bringt nur die gekuerzte
    Fassung, dieser Endpunkt liefert den Rest. Bedient sowohl den
    "vollstaendig anzeigen"-Button (deklaratives `hx-get`) als auch den
    "Text kopieren"-Button bei gekuerzter Anzeige (per JS ueber denselben
    Endpunkt nachgeladen, siehe `detail.html`) -- kein zweiter Mechanismus
    fuers Nachladen.
    """
    document = _visible_document(request.user, pk)
    return render(
        request,
        "documents/partials/_detail_content_body.html",
        _content_context(document, truncated=False),
    )


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
            # Der Out-of-Band-Swap rendert den Zuordnungs-Block mit -- der ist
            # seit dem Inline-Umbau direkt bearbeitbar und braucht deshalb
            # auch hier die Auswahllisten.
            **_meta_context(document),
        },
    )


@login_required
def document_detail(request, pk):
    document = _visible_document(request.user, pk)
    visible_documents = Document.objects.visible_to(request.user)

    context = {
        "document": document,
        **document_comments_context(request.user, document),
        **_content_context(document),
        **_analysis_status_context(document),
        **_long_summary_context(document),
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
        **_meta_context(document),
    }
    return render(request, "documents/detail.html", context)


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


def _meta_context(document, quick_create_error=None):
    """Context for `_detail_meta.html` -- the Zuordnung block on the detail
    page.

    Since the block is directly editable (no "Zuordnung bearbeiten" toggle
    and no second edit partial anymore), the display context *is* the edit
    context: every render needs the option lists and the current selection.
    """
    return {
        "document": document,
        "all_correspondents": Correspondent.objects.all(),
        "direction_choices": Document.Direction.choices,
        "sphere_choices": Document.Sphere.choices,
        "tax_relevance_choices": Document.TaxRelevance.choices,
        "action_status_choices": Document.ActionStatus.choices,
        "selected_correspondent_id": document.correspondent_id,
        "vorgaenge_items": [
            {"id": vorgang.id, "label": vorgang.name, "dimension": ""}
            for vorgang in document.vorgaenge.all()
        ],
        "tags_items": [
            {"id": tag.id, "label": str(tag), "dimension": tag.dimension}
            for tag in document.tags.all()
        ],
        "quick_create_error": quick_create_error,
    }


_META_SEARCH_KINDS = {"vorgang", "tag"}
_META_SEARCH_POOL_LIMIT = 200
_META_SEARCH_RESULTS_LIMIT = 10


def _parse_tag_query(raw):
    """"Dimension:Name" -> (dimension, name) -- ohne Doppelpunkt keine
    Dimension. Ersetzt das frühere separate Dimensions-Feld der
    Tag-Schnellanlage (#1136).
    """
    if ":" in raw:
        dimension, _, name = raw.partition(":")
        return dimension.strip(), name.strip()
    return "", raw.strip()


@login_required
def document_meta_search(request, pk, kind):
    """Trefferliste der Token-Eingabe für Vorgänge/Tags (#1136).

    Rein lesend -- schreibt nichts, im Unterschied zu `document_meta`
    (Auswahl übernehmen) und `document_meta_quick_create` (Neuanlage).
    Bereits zugeordnete Einträge fallen raus, damit dieselbe Zuordnung nicht
    zweimal auswählbar ist. Ein "neu anlegen"-Eintrag kommt nur dazu, wenn
    die Eingabe keinen exakten Treffer hat -- sonst könnte man scheinbar
    einen zweiten, in Wahrheit identischen Eintrag anlegen.
    """
    if kind not in _META_SEARCH_KINDS:
        raise Http404(f"Unbekannte Zuordnungsart: {kind}")

    document = _visible_document(request.user, pk)
    query = request.GET.get("q", "").strip()
    query_lower = query.lower()

    results = []
    exact_match = False
    if query:
        if kind == "vorgang":
            candidates = list(
                Vorgang.objects.exclude(
                    id__in=document.vorgaenge.values_list("id", flat=True)
                ).filter(name__icontains=query)[:_META_SEARCH_POOL_LIMIT]
            )
            candidates.sort(key=lambda v: not v.name.lower().startswith(query_lower))
            results = [
                {"id": v.id, "label": v.name, "dimension": ""}
                for v in candidates[:_META_SEARCH_RESULTS_LIMIT]
            ]
            exact_match = Vorgang.objects.filter(name__iexact=query).exists()
        else:
            candidates = list(
                Tag.objects.exclude(
                    id__in=document.tags.values_list("id", flat=True)
                ).filter(Q(name__icontains=query) | Q(dimension__icontains=query))[
                    :_META_SEARCH_POOL_LIMIT
                ]
            )
            candidates.sort(
                key=lambda t: not (
                    t.name.lower().startswith(query_lower)
                    or t.dimension.lower().startswith(query_lower)
                )
            )
            results = [
                {"id": t.id, "label": str(t), "dimension": t.dimension}
                for t in candidates[:_META_SEARCH_RESULTS_LIMIT]
            ]
            dimension, name = _parse_tag_query(query)
            exact_match = bool(name) and Tag.objects.filter(
                name__iexact=name, dimension__iexact=dimension
            ).exists()

    return render(
        request,
        "documents/partials/_meta_search_results.html",
        {
            "query": query,
            "results": results,
            "show_create": bool(query) and not exact_match,
        },
    )


@login_required
@require_POST
def document_action_status(request, pk):
    """Set `Document.action_status` (#1057) to any of its values --

    a dedicated single-field endpoint rather than routing through
    `document_meta`, because the control renders as a badge+dropdown pair,
    far away from the Zuordnung block. Reserved for the detail view's
    fine-grained control; the overview's one-click toggle is the separate
    `document_action_status_toggle` below (#1137).
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


@login_required
@require_POST
def document_action_status_toggle(request, pk):
    """One-click Erledigung toggle for the overview views (#1137).

    Flips between `erledigt` and `offen` from the *stored* value, never
    from a posted target state -- two fast clicks would otherwise race each
    other into an unpredictable result. A document sitting on the third
    value (`keine`) counts as "not done" and also flips to `erledigt`; the
    finer-grained states stay reserved for `document_action_status` in the
    detail view.
    """
    document = _visible_document(request.user, pk)
    document.action_status = (
        Document.ActionStatus.OPEN
        if document.action_status == Document.ActionStatus.DONE
        else Document.ActionStatus.DONE
    )
    document.save(update_fields=["action_status", "updated_at"])
    # `swap_key` unterscheidet mehrere Vorkommen desselben Dokuments in der
    # Wiedervorlagen-Ansicht (#1139, siehe _action_status_toggle.html) und
    # muss unverändert in die neue `id`/`hx-target` zurückwandern, sonst
    # verliert der Eintrag beim ersten Klick seine eindeutige Kennung.
    swap_key = request.GET.get("swap_key", "").strip()
    context = {"document": document}
    if swap_key.isdigit():
        context["swap_key"] = swap_key
    return render(
        request,
        "documents/partials/_action_status_toggle.html",
        context,
    )


@login_required
@require_POST
def document_processing_status_toggle(request, pk):
    """One-click "Zu prüfen" ↔ "Geprüft"-Umschalter für die Übersichten
    (#1153) -- analog `document_action_status_toggle` (#1137): der neue
    Zustand wird aus dem *gespeicherten* Wert abgeleitet, nie aus einem
    mitgeschickten Wunschzustand, sonst kippt er bei zwei schnellen Klicks
    unkontrolliert.

    Nur `ready`/`reviewed` sind hier ein gültiger Ausgangspunkt (siehe
    `_processing_status_toggle.html`, das für jeden anderen Status gar
    keinen Button rendert) -- ein Dokument in einem anderen Status bleibt
    unangetastet, der Endpunkt rendert dann nur die reine Statusanzeige.
    `reviewed_at`/`_by` werden beim Zurücknehmen geleert (CLAUDE.md,
    "Zustand & Fachlogik": die Zustandsfrage beantwortet ausschließlich
    `processing_status`, nicht "ist ein Zeitstempel gesetzt?" -- ein
    stehen gelassener alter Zeitstempel wäre trotzdem irreführend).
    """
    document = _visible_document(request.user, pk)
    if document.processing_status == Document.ProcessingStatus.REVIEWED:
        document.processing_status = Document.ProcessingStatus.READY
        document.processing_reviewed_at = None
        document.processing_reviewed_by = None
        document.save(
            update_fields=[
                "processing_status",
                "processing_reviewed_at",
                "processing_reviewed_by",
                "updated_at",
            ]
        )
    elif document.processing_status == Document.ProcessingStatus.READY:
        document.processing_status = Document.ProcessingStatus.REVIEWED
        document.processing_reviewed_at = timezone.now()
        document.processing_reviewed_by = request.user
        document.save(
            update_fields=[
                "processing_status",
                "processing_reviewed_at",
                "processing_reviewed_by",
                "updated_at",
            ]
        )
    swap_key = request.GET.get("swap_key", "").strip()
    context = {"document": document}
    if swap_key.isdigit():
        context["swap_key"] = swap_key
    return render(
        request,
        "documents/partials/_processing_status_toggle.html",
        context,
    )


def _next_document_to_review(user):
    """Nächstes Dokument der Sicht "Zu prüfen" (#1153) -- dieselbe Auswahl-
    und Sortierlogik wie die Liste selbst (`DocumentQuerySet.
    needs_review()`), damit "das nächste" nach einem Klick auf "Geprüft"
    auch wirklich das nächste in der angezeigten Reihenfolge ist.
    """
    return Document.objects.visible_to(user).roots().needs_review().first()


def _redirect_to_next_review_or_queue(request):
    next_document = _next_document_to_review(request.user)
    if next_document is not None:
        return redirect("documents:detail", pk=next_document.pk)
    # Keine offenen Dokumente mehr (#1153): zurück auf die gefilterte
    # Liste -- die zeigt bereits "Keine Dokumente für die gewählten
    # Filter" (siehe `_document_list.html`), eine eigene Meldung dafür
    # wäre ein zweiter Weg zum selben Text.
    return redirect(f"{reverse('documents:home')}?status=ready")


@login_required
@require_POST
def document_review_toggle(request, pk):
    """"Geprüft"-Knopf auf der Detailseite (#1153): setzt den Status und
    springt direkt zum nächsten zu prüfenden Dokument -- ohne diesen
    Sprung landet man nach jedem Dokument wieder in der Liste und sucht die
    Stelle erneut. Dieselbe Ableitung aus dem gespeicherten Wert wie
    `document_processing_status_toggle`, hier aber mit Redirect statt
    Partial-Rerender, weil sich nach dem Klick ein ganz anderes Dokument
    zeigt -- kein htmx-Swap, echte Navigation (siehe `child_detach` für
    dasselbe Muster).

    Ein Dokument, das nicht `ready`/`reviewed` ist, bleibt unangetastet und
    landet einfach wieder auf sich selbst -- "Geprüft" ist nur ab `ready`
    erreichbar (CLAUDE.md).
    """
    document = _visible_document(request.user, pk)
    if document.processing_status == Document.ProcessingStatus.REVIEWED:
        document.processing_status = Document.ProcessingStatus.READY
        document.processing_reviewed_at = None
        document.processing_reviewed_by = None
        document.save(
            update_fields=[
                "processing_status",
                "processing_reviewed_at",
                "processing_reviewed_by",
                "updated_at",
            ]
        )
        return redirect("documents:detail", pk=document.pk)
    if document.processing_status == Document.ProcessingStatus.READY:
        document.processing_status = Document.ProcessingStatus.REVIEWED
        document.processing_reviewed_at = timezone.now()
        document.processing_reviewed_by = request.user
        document.save(
            update_fields=[
                "processing_status",
                "processing_reviewed_at",
                "processing_reviewed_by",
                "updated_at",
            ]
        )
        return _redirect_to_next_review_or_queue(request)
    return redirect("documents:detail", pk=document.pk)


def _vision_reextraction_up_to_date(document):
    """True, wenn ein erneuter Klick auf "Mit KI-Vision neu extrahieren"
    (ohne `force`) keinen Modellaufruf ausloesen wuerde (#1149) -- dieselbe
    Idempotenz-Pruefung wie in `extraction.reextract_document_with_vision`,
    hier nur fuers Button-Label/den versteckten `force`-Wert, nicht fuer
    die eigentliche Entscheidung (die trifft der Service selbst noch
    einmal).
    """
    return bool(document.sha256) and document.vision_reextraction_fingerprint == (
        vision_markdown_fingerprint(document)
    )


def _analysis_status_context(document):
    """Kontext des Panels "Analyse erneut ausfuehren"/"Neu verarbeiten"/"Mit
    KI-Vision neu extrahieren" -- prueft nebenbei auf einen
    haengengebliebenen Vision-Neuextraktions-Job (#1143, analog
    `_long_summary_context`/`expire_document_long_summary_if_stalled`),
    bevor das Panel gerendert wird.
    """
    expire_vision_reextraction_if_stalled(document)
    return {
        "document": document,
        "pending_statuses": PENDING_STATUSES,
        "vision_reextract_page_limit": settings.FINDUS_VISION_REEXTRACT_MAX_PAGES,
        "vision_reextraction_up_to_date": _vision_reextraction_up_to_date(document),
    }


def _long_summary_context(document):
    """Kontext des Panels "Ausfuehrliche Zusammenfassung" (#1135) --
    prueft nebenbei auf einen haengengebliebenen Job (analog
    `vorgang_views._recommendations_context`/`expire_if_stalled`), bevor
    der Veraltet-Hinweis berechnet wird.
    """
    expire_document_long_summary_if_stalled(document)
    return {
        "document": document,
        "long_summary_stale": document_long_summary_is_stale(document),
    }


@login_required
def document_long_summary_status(request, pk):
    """Poll-/Anzeige-Ziel des Panels "Ausfuehrliche Zusammenfassung"
    (#1135) -- solange `long_summary_status` `running` ist, holt sich das
    eingeschwenkte Fragment selbst wieder ab, analog
    `document_analysis_status`/`vorgang_views.vorgang_recommendations`.
    """
    document = _visible_document(request.user, pk)
    return render(
        request,
        "documents/partials/_detail_long_summary.html",
        _long_summary_context(document),
    )


@login_required
@require_POST
def document_long_summary_generate(request, pk):
    """"Ausfuehrliche Zusammenfassung erstellen"/"erneut erzeugen" -- laeuft
    async ueber den Django-Q-Worker, ein `generate()`-Call pro Klick.

    `long_summary_status` flippt hier synchron auf `running`
    (`start_document_long_summary`), damit das Panel den Spinner ab dem
    Klick zeigt statt bis zum Anlaufen des Workers noch den alten Stand zu
    behaupten.
    """
    document = _visible_document(request.user, pk)
    start_document_long_summary(document)

    from django_q.tasks import async_task

    from .tasks import (
        generate_document_long_summary_hook,
        generate_document_long_summary_task,
    )

    async_task(
        generate_document_long_summary_task,
        document.id,
        timeout=settings.FINDUS_LONG_SUMMARY_TASK_TIMEOUT_SECONDS,
        hook=generate_document_long_summary_hook,
    )

    return render(
        request,
        "documents/partials/_detail_long_summary.html",
        _long_summary_context(document),
    )


@login_required
def document_analysis_status(request, pk):
    """Poll target for the detail page's "Analyse erneut ausfuehren"/"Neu
    verarbeiten"/"Mit KI-Vision neu extrahieren" controls (#1063, #1143):
    while `processing_status` is still pending, or the vision
    re-extraction is `running`, the swapped-in partial keeps polling
    itself via `hx-trigger="every ...s"`. Once a poll observes both are
    terminal, it sends `HX-Refresh` instead of just re-rendering the small
    status fragment, so the rest of the page (summary, key facts,
    suggestions, the "Inhalt" tab) catches up too -- reusing the full-page
    render rather than duplicating every affected partial's logic here.
    """
    document = _visible_document(request.user, pk)
    response = render(
        request,
        "documents/partials/_detail_analysis_status.html",
        _analysis_status_context(document),
    )
    still_pending = (
        document.processing_status in PENDING_STATUSES or document.vision_reextraction_is_running
    )
    if not still_pending:
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
@require_POST
def document_vision_reextract(request, pk):
    """Loest die manuelle KI-Vision-Neuextraktion aus (#1143) -- Text-Layer/
    OCR liefern bei manchen Vorlagen (schlechte Scans, Handschrift,
    mehrspaltige Layouts) unvollstaendigen oder verstuemmelten Text; ein
    bildfaehiges Modell liest solche Seiten oft zuverlaessiger. Bewusst
    ausgeloest statt automatisch -- teurer und langsamer als die normale
    Kaskade (CLAUDE.md, "KI-Funktionen").

    `Document.supports_vision_reextraction` gated denselben Formatkreis wie
    der Button im Template (nur PDF/Bild); ein direkter POST auf ein
    anderes Format bekommt trotzdem eine klare Antwort statt eines 404,
    das faelschlich "Dokument existiert nicht" suggerieren wuerde --
    anders als eine fehlende Sichtbarkeit, die *soll* wie "existiert nicht"
    aussehen (`_visible_document`).

    `vision_reextraction_status` flippt synchron auf `running`
    (`start_vision_reextraction`), damit die UI ab dem Klick den Spinner
    zeigt -- der bisherige Text bleibt bis zu einem erfolgreichen Ergebnis
    unangetastet.

    Ist die Datei seit dem letzten vollstaendig erfolgreichen Lauf
    unveraendert (#1149, `_vision_reextraction_up_to_date`) und kommt
    `force` nicht mit im POST, wird gar kein Task eingereiht -- das Template
    setzt `force=1` dann selbst, sobald es genau diesen Zustand anzeigt
    (siehe `_detail_analysis_status.html`), ein Klick ohne `force` bedeutet
    also "es gibt noch etwas zu tun". Kein Job, kein Spinner, keine
    Wartezeit fuer einen Klick, der ohnehin nichts veraendert haette.
    """
    document = _visible_document(request.user, pk)
    if not document.supports_vision_reextraction:
        return HttpResponseBadRequest(
            "KI-Vision-Neuextraktion ist fuer dieses Dateiformat nicht verfuegbar."
        )

    force = request.POST.get("force") == "1"
    if not force and _vision_reextraction_up_to_date(document):
        return render(
            request,
            "documents/partials/_detail_analysis_status.html",
            _analysis_status_context(document),
        )

    start_vision_reextraction(document)

    from django_q.tasks import async_task

    from .tasks import reextract_document_with_vision_hook, reextract_document_with_vision_task

    async_task(
        reextract_document_with_vision_task,
        document.id,
        force,
        timeout=settings.FINDUS_VISION_REEXTRACT_TASK_TIMEOUT_SECONDS,
        hook=reextract_document_with_vision_hook,
    )

    return render(
        request,
        "documents/partials/_detail_analysis_status.html",
        _analysis_status_context(document),
    )


_META_FIELDS = (
    "correspondent",
    "direction",
    "sphere",
    "tax_relevance",
    "document_date",
    "vorgaenge",
    "tags",
)


def _requested_meta_fields(post):
    """Which Zuordnungs-Felder this POST wants to change.

    Every inline control sends a `field` marker alongside its value, so a
    single-field save touches exactly that field and leaves the rest alone.
    The marker isn't cosmetic: an emptied multi-select (Vorgänge/Tags)
    submits nothing at all, so without it "alles abgewählt" and "Feld gar
    nicht mitgeschickt" would look identical on the server.

    Without any marker the request is read the old way -- whatever field
    names are present get written -- which keeps a plain full form POST
    (no HTMX/JS) working.
    """
    marked = {field for field in post.getlist("field") if field in _META_FIELDS}
    if marked:
        return marked
    return {field for field in _META_FIELDS if field in post}


def _drop_metadata_source(document, key):
    """Remove a `*_source` provenance marker (#1112/#1113) from `metadata` in
    place and report which model field that dirtied, so the caller can pass it
    to `save(update_fields=...)`.
    """
    if not document.metadata.get(key):
        return []
    metadata = dict(document.metadata)
    metadata.pop(key, None)
    document.metadata = metadata
    return ["metadata"]


def _set_metadata_source(document, key, value):
    """Gegenstueck zu `_drop_metadata_source`: einen Herkunftsvermerk in
    `metadata` setzen und melden, welches Model-Feld das dirty macht.
    """
    if document.metadata.get(key) == value:
        return []
    document.metadata = {**document.metadata, key: value}
    return ["metadata"]


def _submitted_ids(post, name):
    """IDs of a submitted multi-select -- an empty selection means "keine",
    not "unverändert" (the `field` marker already told us the field is meant).
    """
    return [value for value in post.getlist(name) if value.isdigit()]


def _apply_meta_fields(document, post, fields):
    """Apply exactly `fields` from `post` onto `document` -- the shared save
    logic behind both the per-field auto-save (`document_meta`, one field
    per request via the `field` marker) and the full-form "Speichern und
    als geprüft markieren" (`document_meta_review`, #1153, every field at
    once). Extracted so the two endpoints can't drift apart.
    """
    changed = []
    if "correspondent" in fields:
        correspondent_id = post.get("correspondent", "").strip()
        document.correspondent = (
            get_object_or_404(Correspondent, pk=correspondent_id)
            if correspondent_id.isdigit()
            else None
        )
        changed.append("correspondent")
    if "direction" in fields:
        direction = post.get("direction", "").strip()
        if direction in Document.Direction.values:
            document.direction = direction
            changed.append("direction")
    # Sphäre (#1112) / private ESt-Absetzbarkeit (#1113): ein manuelles
    # Speichern ist die Nutzerentscheidung, die jede Re-Analyse ab jetzt
    # respektiert -- deshalb jeweils auch das "noch nicht
    # bestaetigt"-Kennzeichen (`*_source`) aus `metadata` entfernen,
    # damit das KI-Badge am bestätigten Feld verschwindet.
    if "sphere" in fields:
        sphere = post.get("sphere", "").strip()
        if sphere in Document.Sphere.values:
            document.sphere = sphere
            changed.append("sphere")
            changed.extend(_drop_metadata_source(document, "sphere_source"))
    # Aendert sich die Steuerrelevanz gegenueber dem gespeicherten Wert,
    # wird die KI-Begruendung verworfen -- sie begruendete die alte,
    # jetzt ueberstimmte Einschaetzung.
    if "tax_relevance" in fields:
        tax_relevance = post.get("tax_relevance", "").strip()
        if tax_relevance in Document.TaxRelevance.values:
            if tax_relevance != document.tax_relevance:
                document.tax_relevance_reason = ""
                changed.append("tax_relevance_reason")
            document.tax_relevance = tax_relevance
            changed.append("tax_relevance")
            changed.extend(
                _drop_metadata_source(document, "tax_relevance_source")
            )
    # Dokumentdatum (#1141): ein von Hand gesetztes Datum wird als
    # `"manuell"` vermerkt und ist damit vor jeder erneuten KI-Analyse
    # geschuetzt -- das ist der Vermerk, an dem `_apply_analysis` die
    # Nutzerentscheidung erkennt. Ein geleertes Feld nimmt den Vermerk
    # wieder mit: "kein Datum" ist keine zu schuetzende Korrektur,
    # sondern die Freigabe, es neu bestimmen zu lassen. Der Hinweis auf
    # eine frueher eingegriffene Plausibilitaetspruefung faellt in beiden
    # Faellen weg, der Nutzer hat gerade selbst entschieden.
    if "document_date" in fields:
        document_date = post.get("document_date", "").strip()
        if not document_date:
            document.document_date = None
            changed.append("document_date")
            changed.extend(_drop_metadata_source(document, "document_date_source"))
        else:
            try:
                document.document_date = datetime.date.fromisoformat(document_date)
            except ValueError:
                pass
            else:
                changed.append("document_date")
                changed.extend(
                    _set_metadata_source(
                        document, "document_date_source", MANUAL_DATE_SOURCE
                    )
                )
        changed.extend(
            _drop_metadata_source(document, "document_date_upload_conflict")
        )
    if changed:
        document.save(update_fields=[*dict.fromkeys(changed), "updated_at"])
    if "vorgaenge" in fields:
        document.vorgaenge.set(_submitted_ids(post, "vorgaenge"))
    if "tags" in fields:
        document.tags.set(_submitted_ids(post, "tags"))
    # Zuordnen heißt: "diese Nummern gehören hierher" (#1100). Eine
    # entfernte Zuordnung nimmt die gelernte Kennung *nicht* wieder
    # mit -- der Vorgang hat sein Aktenzeichen dann trotzdem, und was
    # er nicht behalten soll, wird an seinem Hub entfernt. Nur die
    # Zuordnungsfelder lösen das aus; ein geändertes Datum lernt nichts.
    if fields & {"correspondent", "vorgaenge"}:
        learn_references_from_document(document)
    return changed


@login_required
def document_meta(request, pk):
    """Display partial for `#document-meta` -- also the save target: a POST
    here sets the submitted Zuordnungs-Felder, then re-renders the block.
    """
    document = _visible_document(request.user, pk)
    if request.method == "POST":
        _apply_meta_fields(document, request.POST, _requested_meta_fields(request.POST))
    return _render_meta(request, document)


@login_required
@require_POST
def document_meta_review(request, pk):
    """"Speichern und als geprüft markieren" im Zuordnungsformular (#1153):
    anders als `document_meta` (ein Feld pro Request, über den `field`-
    Marker) ist dies ein echtes Full-Form-Submit -- der übliche Fall ist
    "Vorgang und Tags vergeben, dann bestätigen" in einem Schritt. Alle
    `_META_FIELDS` gelten deshalb unbedingt als angefasst, nicht nur die
    markierten; eine geleerte Vorgänge-/Tags-Auswahl wird dabei korrekt als
    "keine" übernommen (`_submitted_ids` liefert `[]`, wenn kein Chip mehr
    im Formular steht).

    Setzt danach `reviewed` nur von `ready` aus (CLAUDE.md, "Geprüft ist
    nur ab ready erreichbar") und springt wie `document_review_toggle` zum
    nächsten zu prüfenden Dokument.
    """
    document = _visible_document(request.user, pk)
    _apply_meta_fields(document, request.POST, set(_META_FIELDS))
    if document.processing_status == Document.ProcessingStatus.READY:
        document.processing_status = Document.ProcessingStatus.REVIEWED
        document.processing_reviewed_at = timezone.now()
        document.processing_reviewed_by = request.user
        document.save(
            update_fields=[
                "processing_status",
                "processing_reviewed_at",
                "processing_reviewed_by",
                "updated_at",
            ]
        )
    return _redirect_to_next_review_or_queue(request)


@login_required
@require_POST
def document_date_inline(request, pk):
    """Inline-Korrektur von `Document.document_date` direkt in Timeline,

    Liste und Kachel (#1140) -- Klick aufs Datum statt Umweg über das
    Detail. Teilt die Schutzlogik mit `document_meta` (#1141): ein hier
    gesetztes Datum wird als "manuell" vermerkt (`_set_metadata_source`)
    und übersteht damit jede erneute KI-Analyse. Anders als `document_meta`
    verwirft dies eine unparsbare Eingabe nicht still -- ohne Detailseite
    drumherum bräuchte der Nutzer sonst einen zweiten Blick, um zu merken,
    dass nichts gespeichert wurde. Das Formular bleibt deshalb im
    Bearbeitungszustand stehen und zeigt den Fehler direkt am Feld.
    """
    document = _visible_document(request.user, pk)
    form = DocumentDateInlineForm(request.POST)
    if form.is_valid():
        document.document_date = form.cleaned_data["document_date"]
        changed = ["document_date"]
        changed.extend(
            _set_metadata_source(document, "document_date_source", MANUAL_DATE_SOURCE)
        )
        changed.extend(_drop_metadata_source(document, "document_date_upload_conflict"))
        document.save(update_fields=[*dict.fromkeys(changed), "updated_at"])
        context = {"document": document}
    else:
        context = {
            "document": document,
            "error": "Ungültiges Datum. Format: TT.MM.JJJJ oder JJJJ-MM-TT.",
            "input_value": request.POST.get("document_date", ""),
        }
    return render(request, "documents/partials/_document_date_inline.html", context)


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

    Answers with the Zuordnungs-Block itself, so the new Kontakt/Vorgang/Tag
    is immediately selected in its dropdown -- unless the request came from
    the Vorgang/Tag token input (#1136, `chip=1`), which asks for just the
    new chip back instead, so an unconfirmed search text in the other field
    doesn't get wiped by a full-block rerender.
    """
    if kind not in _QUICK_CREATE_KINDS:
        raise Http404(f"Unbekannte Zuordnungsart: {kind}")

    document = _visible_document(request.user, pk)
    # Each quick-create block carries a kind-specific field name
    # (`correspondent_name`/`vorgang_name`/`tag_name`) rather than a shared
    # `name` -- a leftover safeguard from the days of one big <form> around
    # all three blocks, where a shared `name` made the empty blocks shadow
    # the filled one and the typed name never reached the server (#1064).
    name = request.POST.get(f"{kind}_name", "").strip()
    dimension = request.POST.get(f"{kind}_dimension", "").strip()
    quick_create_error = None
    created_object = None
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
            created_object = vorgang
        elif kind == "tag":
            # Die Token-Eingabe (#1136) schickt Dimension+Name kombiniert als
            # "Dimension:Name" in `tag_name` -- ein separat mitgeschicktes
            # `tag_dimension` (ältere Zwei-Felder-Schnellanlage) geht vor.
            if not dimension:
                dimension, name = _parse_tag_query(name)
            dimension = _truncated(Tag, "dimension", dimension)
            name = _truncated(Tag, "name", name)
            tag, _created = Tag.objects.get_or_create(name=name, dimension=dimension)
            document.tags.add(tag)
            created_object = tag
        if kind in ("correspondent", "vorgang"):
            learn_references_from_document(document)
    else:
        quick_create_error = {
            "kind": kind,
            "message": "Bitte einen Namen eingeben.",
            "name": name,
            "dimension": dimension,
        }

    if request.POST.get("chip") == "1" and created_object is not None:
        return _render_token_chip(request, document, kind, created_object)

    return _render_meta(request, document, quick_create_error=quick_create_error)


def _render_token_chip(request, document, kind, obj):
    """Antwort der Token-Neuanlage (#1136): nur der neue Chip statt des
    ganzen Zuordnungs-Blocks (siehe `document_meta_quick_create`). Vorgänge
    stehen zusätzlich im Schnellzugriff über der Seite (#1098) -- der zieht
    per Out-of-Band-Swap mit, Tags stehen dort nicht.
    """
    return render(
        request,
        "documents/partials/_meta_token_chip_response.html",
        {
            "document": document,
            "field_name": "vorgaenge" if kind == "vorgang" else "tags",
            "object_id": obj.id,
            "object_label": obj.name if kind == "vorgang" else str(obj),
            "dimension": obj.dimension if kind == "tag" else "",
            "is_tag": kind == "tag",
            "hub_links_oob": kind == "vorgang",
        },
    )


def _render_meta(request, document, quick_create_error=None):
    return render(
        request,
        "documents/partials/_detail_meta_response.html",
        _meta_context(document, quick_create_error=quick_create_error),
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
