"""UI for the Vorgang index/hub (#1040): a directory to browse to a
Vorgang, and a hub page that reuses the existing filtered document list
(`apps.documents.views.filtered_documents` / `_document_list.html`)
scoped to that Vorgang -- deliberately no second document-list
implementation.

Since #1093 the hub also carries the "Handlungsempfehlungen" panel: an
on-demand KI-Beurteilung of the whole Vorgang (see
`apps.documents.recommendations`), whose single-recommendation actions
("als Aufgabe übernehmen"/"verwerfen") live here as well.
"""

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import VorgangForm
from .models import Document, Tag, Task, Vorgang, VorgangRecommendation
from .recommendations import is_stale_for, start_recommendation_run
from .reference_views import owner_references_context
from .task_views import task_departments_and_visibility
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
    """Editierbare Vorgangsdaten (#1050) + the reused, Vorgang-scoped
    document list (still further filterable by Tag/Status/Richtung).

    Kein "Verknüpfte Aufgaben"-Block mehr: Aufgaben sind auf dem Rückzug
    (siehe CLAUDE.md), neue Funktionalität hängt an Dokument-Kommentaren.
    Eine aus einer Handlungsempfehlung übernommene Aufgabe bleibt über den
    "Aufgabe öffnen"-Link im Empfehlungs-Panel erreichbar.
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

    context = {
        "vorgang": vorgang,
        "form": form,
        "document_count": visible_documents.filter(vorgaenge=vorgang).distinct().count(),
        "page_obj": page_obj,
        "query_without_page": query_without_page.urlencode(),
        "has_pending": has_pending,
        "list_url": request.path,
        "tags": Tag.objects.all(),
        "status_choices": Document.ProcessingStatus.choices,
        "direction_choices": Document.Direction.choices,
        "sphere_choices": Document.Sphere.choices,
        "tax_relevance_filter_choices": Document.tax_relevance_filter_choices(),
        "selected": {
            "tag": request.GET.get("tag", ""),
            "status": request.GET.get("status", ""),
            "direction": request.GET.get("direction", ""),
            "sphere": request.GET.get("sphere", ""),
            "tax_relevance": request.GET.get("tax_relevance", ""),
            "view": request.GET.get("view", "timeline").strip(),
        },
        "upload_allowed_extensions": settings.FINDUS_INGEST_ALLOWED_EXTENSIONS,
        "upload_max_size_mb": settings.FINDUS_UPLOAD_MAX_SIZE_MB,
    }

    if request.htmx:
        return render(request, "documents/partials/_document_list.html", context)

    context.update(_recommendations_context(request.user, vorgang))
    context.update(owner_references_context(vorgang))
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


# -- Handlungsempfehlungen (#1093) -----------------------------------------


def _basis_is_visible(user, run):
    """Darf `user` das gespeicherte Ergebnis ueberhaupt sehen? (#1052)

    Der Lauf haengt am Vorgang, nicht am Nutzer -- er wird also auch dem
    Kollegen angezeigt, der den Hub spaeter oeffnet. Lage-Einschaetzung und
    Begruendungen sind aber aus den Zusammenfassungen der Datenbasis
    destilliert: waere darunter ein Dokument, das dieser Nutzer nicht
    oeffnen darf, wuerde der Fliesstext genau das preisgeben, was das
    Sichtbarkeitsmodell verhindern soll. Ein reines Filtern der
    Quellen-Links reicht dafuer nicht -- der Text bliebe stehen.

    In der Praxis (eine Abteilung, ein Bestand) trifft das nie zu; wenn
    doch, bleibt der Weg offen, sich die Empfehlungen selbst auf der
    eigenen, engeren Datenbasis zu generieren.
    """
    basis_ids = set(run.based_on.get("document_ids") or [])
    if not basis_ids:
        return True
    visible_count = (
        Document.objects.visible_to(user).filter(pk__in=basis_ids).distinct().count()
    )
    return visible_count == len(basis_ids)


def _recommendations_context(user, vorgang):
    """Panel-Kontext: der (einzige) Lauf des Vorgangs plus je Empfehlung
    ihre *sichtbaren* Quell-Dokumente.

    Die Quellen werden hier noch einmal gegen `visible_to` gefiltert, nicht
    nur bei der Generierung: ein Dokument kann nach dem Lauf privat
    gestellt oder einer anderen Abteilung zugeordnet worden sein, und dann
    darf sein Titel hier nicht weiter auftauchen.
    """
    # `getattr` mit Default: der OneToOne-Zugriff wirft
    # `RelatedObjectDoesNotExist` (ein `AttributeError`), solange fuer
    # diesen Vorgang noch nie generiert wurde.
    run = getattr(vorgang, "recommendation_run", None)
    recommendations = []
    restricted = run is not None and not _basis_is_visible(user, run)

    if run is not None and not restricted:
        visible_ids = set(
            Document.objects.visible_to(user)
            .filter(vorgang_recommendations__run=run)
            .values_list("pk", flat=True)
        )
        recommendations = [
            {
                "item": recommendation,
                "sources": [
                    document
                    for document in recommendation.documents.all()
                    if document.pk in visible_ids
                ],
            }
            for recommendation in run.recommendations.prefetch_related("documents")
        ]

    return {
        "vorgang": vorgang,
        "recommendation_run": run,
        "recommendations": recommendations,
        "recommendations_restricted": restricted,
        "recommendations_stale": is_stale_for(run, user),
    }


def _render_recommendations(request, vorgang):
    return render(
        request,
        "documents/partials/_vorgang_recommendations.html",
        _recommendations_context(request.user, vorgang),
    )


@login_required
def vorgang_recommendations(request, pk):
    """Poll-/Anzeige-Ziel des Empfehlungs-Panels (#1093): solange der Lauf
    `running` ist, holt sich das eingeschwenkte Fragment selbst wieder ab
    (`hx-trigger="every ...s"`), analog zu `document_analysis_status`
    (#1063). Ein Seiten-Refresh am Ende braucht es hier nicht -- alles,
    was sich aendert, steht in diesem Panel.
    """
    vorgang = get_object_or_404(Vorgang, pk=pk)
    return _render_recommendations(request, vorgang)


@login_required
@require_POST
def vorgang_recommendations_generate(request, pk):
    """"Empfehlungen generieren"/"neu generieren" -- laeuft async ueber den
    Django-Q-Worker, genau ein `generate()`-Call pro Klick.

    `request.user.pk` geht mit in den Job: die Datenbasis ist
    `visible_to`-gescoped und der Worker hat keinen Request, aus dem er
    das ableiten koennte.
    """
    vorgang = get_object_or_404(Vorgang, pk=pk)
    start_recommendation_run(vorgang)

    from django_q.tasks import async_task

    from .tasks import generate_vorgang_recommendations_task

    async_task(generate_vorgang_recommendations_task, vorgang.pk, request.user.pk)

    return _render_recommendations(request, vorgang)


def _recommendation_task_description(recommendation, vorgang):
    """Begruendung + Vorgang-Kontext als Aufgaben-Beschreibung.

    Der Disclaimer wandert bewusst mit in die Aufgabe: sie wird spaeter
    ohne das Panel drumherum gelesen, und dann darf ihr Ursprung als
    *pruefenswerter* KI-Vorschlag nicht verloren gegangen sein.
    """
    parts = []
    if recommendation.rationale.strip():
        parts.append(recommendation.rationale.strip())
    parts.append(
        f"Aus KI-Handlungsempfehlung zum Vorgang „{vorgang.name}“ – "
        "bitte fachlich prüfen, keine Rechts-/Steuerberatung."
    )
    return "\n\n".join(parts)


@login_required
@require_POST
def vorgang_recommendation_accept(request, pk, recommendation_id):
    """1-Klick "als Aufgabe übernehmen" (#1093 -> #1012): Titel =
    Empfehlung, Beschreibung = Begruendung, `due_date` = vorgeschlagene
    Frist, verknuepft mit den Quell-Dokumenten.

    Verknuepft werden nur die fuer *diesen* Nutzer sichtbaren Quellen
    (#1052) -- dieselbe Regel wie `task_views._set_task_documents`: eine
    Aufgabe darf kein Dokument anhaengen, das ihr Ersteller nicht sehen
    darf. Der Hub selbst listet keine Aufgaben mehr; erreichbar bleibt die
    neue Aufgabe ueber den "Aufgabe oeffnen"-Link, den das Panel nach der
    Uebernahme direkt an der Empfehlung zeigt.

    Nur eine offene Empfehlung ist uebernehmbar -- ein zweiter Klick (oder
    ein zweiter Tab) darf nicht dieselbe Aufgabe ein zweites Mal anlegen.
    """
    vorgang = get_object_or_404(Vorgang, pk=pk)
    recommendation = get_object_or_404(
        VorgangRecommendation, pk=recommendation_id, run__vorgang=vorgang
    )
    if not _basis_is_visible(request.user, recommendation.run):
        raise Http404

    if recommendation.status == VorgangRecommendation.Status.OPEN:
        departments, visibility = task_departments_and_visibility(request.user)
        task = Task.objects.create(
            title=recommendation.title[:255],
            description=_recommendation_task_description(recommendation, vorgang),
            due_date=recommendation.due_date,
            owner=request.user,
            visibility=visibility,
        )
        task.departments.set(departments)
        task.documents.set(
            Document.objects.visible_to(request.user).filter(
                vorgang_recommendations=recommendation
            )
        )
        recommendation.task = task
        recommendation.status = VorgangRecommendation.Status.ACCEPTED
        recommendation.save(update_fields=["task", "status", "updated_at"])

    return _render_recommendations(request, vorgang)


@login_required
@require_POST
def vorgang_recommendation_dismiss(request, pk, recommendation_id):
    """"Verwerfen": die Empfehlung bleibt als getroffene Entscheidung
    stehen (im Panel abgeblendet), statt zu verschwinden -- sonst waere
    beim naechsten Blick nicht mehr erkennbar, dass sie schon beurteilt
    wurde.
    """
    vorgang = get_object_or_404(Vorgang, pk=pk)
    recommendation = get_object_or_404(
        VorgangRecommendation, pk=recommendation_id, run__vorgang=vorgang
    )
    if not _basis_is_visible(request.user, recommendation.run):
        raise Http404

    if recommendation.status == VorgangRecommendation.Status.OPEN:
        recommendation.status = VorgangRecommendation.Status.DISMISSED
        recommendation.save(update_fields=["status", "updated_at"])

    return _render_recommendations(request, vorgang)
