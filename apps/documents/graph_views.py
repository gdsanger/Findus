"""UI und JSON-Endpunkte für den Fokus-Graphen (#1091).

Drei Views, klare Arbeitsteilung: `graph` rendert nur die Seite (Toolbar,
Legende, leere Zeichenfläche) und reicht den Fokus aus der URL an das
Frontend durch; `graph_neighbors` liefert die eigentlichen Daten als JSON
-- immer nur *eine* Nachbarschaft, nie den ganzen Graphen, siehe
`apps.documents.graph`; `graph_search` ist die HTMX-Trefferliste des
Suchfelds, mit der man den Fokus wählt.

Die Aufteilung "Seite server-gerendert, Daten per JSON" ist hier die
Ausnahme vom sonstigen HTMX-Muster: Cytoscape hält seinen Zustand
(Positionen, Zoom, bereits aufgeklappte Knoten) im Browser, ein
HTML-Swap würde ihn bei jeder Expansion wegwerfen.
"""

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.shortcuts import render

from . import graph
from .graph import GraphService
from .models import Correspondent, Tag, Vorgang

SEARCH_RESULTS_PER_TYPE = 6


def _resolve_focus(request):
    """Fokus-Knoten aus `?type=&id=`, oder None.

    None ist ein normaler Zustand (Direktaufruf von `/graph` ohne
    Parameter): die Seite zeigt dann das Suchfeld statt eines Graphen.
    """
    node_type = request.GET.get("type", "").strip()
    node_pk = request.GET.get("id", "").strip()
    if node_type not in graph.NODE_TYPE_LABELS or not node_pk.isdigit():
        return None
    return GraphService(request.user).resolve(node_type, int(node_pk))


@login_required
def graph_view(request):
    """`/graph` -- Fokus-Graph-Seite (#1091).

    Rendert bewusst noch keine Knoten: die Nachbarschaft holt das Frontend
    beim Laden über `graph_neighbors`, damit Erst-Aufbau und jede spätere
    Expansion durch denselben Code laufen.
    """
    focus = _resolve_focus(request)
    focus_requested = bool(request.GET.get("type") or request.GET.get("id"))

    context = {
        "focus_node": graph.node_payload(focus) if focus is not None else None,
        # Fokus angefragt, aber nicht auflösbar (gelöscht, oder für diesen
        # Nutzer nicht sichtbar) -- das verdient einen Hinweis statt einer
        # kommentarlos leeren Zeichenfläche.
        "focus_missing": focus is None and focus_requested,
        "node_type_labels": graph.NODE_TYPE_LABELS,
        "similarity_enabled": request.GET.get("similarity", "1") != "0",
        "similarity_min_score_percent": round(
            settings.FINDUS_SIMILAR_DOCUMENTS_MIN_SCORE * 100
        ),
        "neighbor_limit": settings.FINDUS_GRAPH_NEIGHBOR_LIMIT,
    }
    return render(request, "documents/graph.html", context)


@login_required
def graph_neighbors(request):
    """JSON-Nachbarschaft eines Knotens (`?type=&id=&similarity=`).

    404 statt einer leeren Antwort, wenn der Knoten nicht existiert oder
    nicht sichtbar ist -- "gibt es nicht" und "leere Nachbarschaft" sind
    zwei verschiedene Aussagen, und nur die erste darf der Grund sein, dass
    ein fremdes Dokument hier nichts liefert.
    """
    node_type = request.GET.get("type", "").strip()
    node_pk = request.GET.get("id", "").strip()
    if node_type not in graph.NODE_TYPE_LABELS or not node_pk.isdigit():
        raise Http404("Unbekannter Knoten.")

    service = GraphService(request.user)
    obj = service.resolve(node_type, int(node_pk))
    if obj is None:
        raise Http404("Knoten nicht gefunden.")

    return JsonResponse(
        service.neighborhood(
            obj, include_similarity=request.GET.get("similarity", "1") != "0"
        )
    )


@login_required
def graph_search(request):
    """HTMX-Trefferliste zur Wahl des Fokus-Knotens.

    Sucht über alle vier Knotentypen gleichzeitig, je Typ gedeckelt --
    wer "Miete" eingibt, meint mal den Vorgang, mal das Tag, mal das
    Dokument, und soll nicht vorher einen Typ auswählen müssen.
    """
    query = request.GET.get("q", "").strip()
    results = []
    if query:
        service = GraphService(request.user)
        visible_documents = service.visible_documents()
        results = [
            *[
                graph.node_payload(document)
                for document in visible_documents.filter(title__icontains=query)[
                    :SEARCH_RESULTS_PER_TYPE
                ]
            ],
            *[
                graph.node_payload(vorgang)
                for vorgang in _with_visible_documents(
                    Vorgang.objects.filter(name__icontains=query), visible_documents
                )[:SEARCH_RESULTS_PER_TYPE]
            ],
            *[
                graph.node_payload(tag)
                for tag in _with_visible_documents(
                    Tag.objects.filter(
                        Q(name__icontains=query) | Q(dimension__icontains=query)
                    ),
                    visible_documents,
                )[:SEARCH_RESULTS_PER_TYPE]
            ],
            *[
                graph.node_payload(correspondent)
                for correspondent in _with_visible_documents(
                    Correspondent.objects.filter(name__icontains=query),
                    visible_documents,
                )[:SEARCH_RESULTS_PER_TYPE]
            ],
        ]

    return render(
        request,
        "documents/partials/_graph_search_results.html",
        {"results": results, "search_query": query},
    )


def _with_visible_documents(queryset, visible_documents):
    """Nur Vorgänge/Tags/Kontakte anbieten, an denen mindestens ein für
    diesen Nutzer sichtbares Dokument hängt.

    Ein Fokus-Knoten ohne sichtbare Nachbarn führt zu einem Graphen aus
    genau einem Punkt -- als Suchtreffer ist er damit wertlos, und die
    Zuordnung selbst ist nichts, was jemand hier erfahren müsste.
    """
    # Der Join auf `documents` vervielfacht die Zeilen (ein Treffer je
    # zugeordnetem Dokument); das `annotate` erzeugt das GROUP BY, das sie
    # wieder zu einer Zeile je Vorgang/Tag/Kontakt faltet -- und liefert
    # die Zahl gleich als Zusatzinfo für die Trefferliste mit.
    return queryset.filter(documents__in=visible_documents).annotate(
        visible_document_count=Count("documents", distinct=True)
    )
