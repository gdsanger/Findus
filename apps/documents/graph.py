"""Nachbarschafts-Daten für den semantischen Fokus-Graphen (#1091).

Der Graph geht bewusst *nicht* vom Gesamtbestand aus ("Hairball"), sondern
von einem Fokus-Knoten: dieses Modul liefert zu genau einem Knoten
(Dokument/Vorgang/Tag/Kontakt) dessen direkte Nachbarschaft -- ein Hop,
gedeckelt, in einer festen Zahl von Queries. Aufklappen im Frontend heißt
schlicht: derselbe Aufruf noch einmal, mit dem angeklickten Knoten als
Fokus. Damit bleibt die Datenmenge pro Request konstant, egal wie groß das
Archiv ist.

Drei Kantensorten, im Frontend optisch getrennt:

* **Struktur** -- die gepflegten Zuordnungen (Dokument→Kontakt/Vorgang/Tag)
  plus die manuellen Querverweise zwischen Dokumenten (`DocumentLink`,
  #1088).
* **Kennung** -- Dokument↔Dokument über eine gemeinsame, exakt gleiche
  Referenznummer (#1099, `apps.documents.references`). Bewusst als
  eigene, *starke* Kante: das ist kein Ähnlichkeitswert, sondern
  Gleichheit -- zwei Schreiben mit demselben Aktenzeichen gehören
  zusammen, und das soll im Graphen anders aussehen als "liegt nah
  beieinander".
* **Ähnlichkeit** -- Dokument↔Dokument aus der Embedding-kNN. Die läuft
  über dieselbe `DocumentRetrievalService.similar_documents()` wie der
  "Ähnliche Dokumente"-Block des Details (#1088), inklusive dessen
  Schwellwert und Cap -- kein zweiter Ähnlichkeitsbegriff im System.

Sichtbarkeit: Dokument-Knoten und alles, was an ihnen hängt, laufen
ausnahmslos durch `Document.objects.visible_to(user)` bzw. (für die
Ähnlichkeit) durch den `DocumentRetrievalService`, der seinerseits nur so
scopen kann. Vorgang/Tag/Kontakt haben -- wie im Rest der App (siehe
`vorgang_list`/`tag_list`/`correspondent_list`) -- kein eigenes
Sichtbarkeitsmodell; sie tauchen hier aber nur auf, wenn sie an einem
sichtbaren Dokument hängen oder selbst der gewählte Fokus sind.
"""

from __future__ import annotations

from django.conf import settings
from django.db.models import DateField, Q
from django.db.models.functions import Coalesce, TruncDate
from django.urls import reverse

from .models import Correspondent, Document, DocumentLink, Tag, Vorgang
from .references import shared_reference_matches
from .retrieval import DocumentRetrievalService

DOCUMENT = "document"
VORGANG = "vorgang"
TAG = "tag"
CORRESPONDENT = "correspondent"

# Reihenfolge = Reihenfolge der Filter-Checkboxen in der Toolbar.
NODE_TYPE_LABELS = {
    DOCUMENT: "Dokument",
    VORGANG: "Vorgang",
    TAG: "Tag",
    CORRESPONDENT: "Kontakt",
}

STRUCTURE = "structure"
REFERENCE = "reference"
SIMILARITY = "similarity"


def node_id(node_type: str, pk: int) -> str:
    """Stabile Knoten-ID über Typgrenzen hinweg -- Dokument 7 und Tag 7
    sind zwei verschiedene Knoten, eine reine PK reichte als ID im
    Cytoscape-Graphen also nicht.
    """
    return f"{node_type}:{pk}"


def _capped(queryset, limit: int) -> tuple[list, bool]:
    """Hole `limit + 1` Zeilen, gib `limit` zurück und melde, ob es mehr
    gab -- ein zusätzliches `COUNT(*)` je Kantengruppe nur für die
    "gekürzt"-Anzeige wäre die teurere Variante derselben Auskunft.
    """
    rows = list(queryset[: limit + 1])
    return rows[:limit], len(rows) > limit


def _by_display_date(documents):
    """Neueste zuerst nach Dokumentdatum mit Upload-Datum als Fallback --
    dieselbe `Coalesce`-Sortierung wie `filtered_documents` (#1085), damit
    der Cap pro Expansion dieselben "obersten" Dokumente behält, die auch
    eine Liste zeigen würde.
    """
    return documents.annotate(
        effective_date=Coalesce(
            "document_date", TruncDate("created_at"), output_field=DateField()
        )
    ).order_by("-effective_date", "-created_at")


class GraphService:
    """Fokus-Knoten rein, Nachbarschaft raus -- eine Instanz pro Request/User."""

    def __init__(self, user):
        self.user = user
        self.limit = settings.FINDUS_GRAPH_NEIGHBOR_LIMIT

    # -- Auflösung des Fokus ------------------------------------------------

    def visible_documents(self):
        return Document.objects.visible_to(self.user).select_related("correspondent")

    def resolve(self, node_type: str, pk: int):
        """Das Objekt hinter einer Knoten-ID, oder None.

        Dokumente laufen durch `visible_to` -- eine geratene ID darf über
        den Graphen weder Titel noch Nachbarschaft eines fremden Dokuments
        preisgeben.
        """
        if node_type == DOCUMENT:
            return self.visible_documents().filter(pk=pk).first()
        if node_type == VORGANG:
            return Vorgang.objects.filter(pk=pk).first()
        if node_type == TAG:
            return Tag.objects.filter(pk=pk).first()
        if node_type == CORRESPONDENT:
            return Correspondent.objects.filter(pk=pk).first()
        return None

    # -- Nachbarschaft ------------------------------------------------------

    def neighborhood(self, obj, *, include_similarity: bool = True) -> dict:
        """Ein Hop um `obj` herum: Knoten (inkl. `obj` selbst) und Kanten.

        `include_similarity` schaltet nur die Ähnlichkeitskanten zu bzw. ab
        -- ist der Toggle im Frontend aus, wird die kNN-Query gar nicht
        erst gestellt, statt sie zu rechnen und das Ergebnis wegzublenden.
        """
        if isinstance(obj, Document):
            return self._document_neighborhood(obj, include_similarity)
        if isinstance(obj, Vorgang):
            documents = self.visible_documents().filter(vorgaenge=obj)
            edge_label = NODE_TYPE_LABELS[VORGANG]
        elif isinstance(obj, Tag):
            documents = self.visible_documents().filter(tags=obj)
            edge_label = NODE_TYPE_LABELS[TAG]
        elif isinstance(obj, Correspondent):
            documents = self.visible_documents().filter(correspondent=obj)
            edge_label = NODE_TYPE_LABELS[CORRESPONDENT]
        else:
            raise TypeError(f"Kein Graph-Knotentyp: {type(obj)!r}")
        return self._document_list_neighborhood(node_payload(obj), documents, edge_label)

    def _document_neighborhood(self, document: Document, include_similarity: bool) -> dict:
        focus = node_payload(document)
        nodes = [focus]
        edges = []
        truncated = False

        if document.correspondent_id is not None:
            nodes.append(_correspondent_node(document.correspondent))
            edges.append(
                _structure_edge(
                    focus["id"],
                    node_id(CORRESPONDENT, document.correspondent_id),
                    "Kontakt",
                )
            )

        vorgaenge, more = _capped(document.vorgaenge.all(), self.limit)
        truncated = truncated or more
        for vorgang in vorgaenge:
            nodes.append(_vorgang_node(vorgang))
            edges.append(
                _structure_edge(focus["id"], node_id(VORGANG, vorgang.pk), "Vorgang")
            )

        tags, more = _capped(document.tags.all(), self.limit)
        truncated = truncated or more
        for tag in tags:
            nodes.append(_tag_node(tag))
            edges.append(_structure_edge(focus["id"], node_id(TAG, tag.pk), "Tag"))

        linked_ids, more = self._append_document_links(document, focus, nodes, edges)
        truncated = truncated or more

        reference_ids, more = self._append_reference_edges(
            document, nodes, edges, exclude_ids=linked_ids
        )
        truncated = truncated or more

        if include_similarity:
            # Ausgeschlossen wird, was schon als Querverweis oder gemeinsame
            # Kennung dransteht: dieselbe Beziehung zweimal (durchgezogen
            # *und* gestrichelt) zwischen denselben zwei Knoten wäre nur
            # Rauschen -- die härtere Aussage gewinnt.
            hits = DocumentRetrievalService(self.user).similar_documents(
                document, exclude_ids=[*linked_ids, *reference_ids]
            )
            for hit in hits:
                nodes.append(_document_node(hit.document))
                edges.append(
                    _similarity_edge(
                        focus["id"], node_id(DOCUMENT, hit.document.pk), hit.score
                    )
                )

        return _payload(focus, nodes, edges, truncated)

    def _append_document_links(self, document, focus, nodes, edges):
        """Manuelle Querverweise (#1088) als Struktur-Kanten Dokument↔Dokument.

        Die Gegenseite wird DB-seitig gegen `visible_to` gefiltert, nicht
        erst in Python: ein Verweis auf ein nicht sichtbares Dokument darf
        weder dessen Titel liefern noch (als stiller Ausfall) den Cap
        aufbrauchen.
        """
        visible_pks = self.visible_documents().values("pk")
        link_rows = DocumentLink.objects.filter(
            Q(document_a=document, document_b__in=visible_pks)
            | Q(document_b=document, document_a__in=visible_pks)
        ).select_related("document_a__correspondent", "document_b__correspondent")

        links, more = _capped(link_rows, self.limit)
        linked_ids = []
        for link in links:
            other = (
                link.document_b if link.document_a_id == document.pk else link.document_a
            )
            linked_ids.append(other.pk)
            nodes.append(_document_node(other))
            # Endpunkte in der kanonischen Reihenfolge des Links (a < b, siehe
            # `DocumentLink`), nicht "Fokus zuerst": ein ungerichteter Verweis
            # muss dieselbe Kanten-ID ergeben, egal von welchem der beiden
            # Enden aus jemand aufklappt -- sonst hinge er nach dem Aufklappen
            # beider Seiten doppelt im Graphen.
            edges.append(
                _structure_edge(
                    node_id(DOCUMENT, link.document_a_id),
                    node_id(DOCUMENT, link.document_b_id),
                    "Querverweis",
                )
            )
        return linked_ids, more

    def _append_reference_edges(self, document, nodes, edges, *, exclude_ids):
        """Gemeinsame Kennungen (#1099) als starke Dokument↔Dokument-Kanten.

        Die Gegenseite kommt aus `shared_reference_matches()` und ist
        damit schon durch `visible_to` gescoped. Paare, die ohnehin einen
        manuellen Querverweis haben, bleiben draußen -- "gehört zusammen"
        steht dort bereits, eine zweite Kante dazwischen wäre dieselbe
        Aussage doppelt.

        Beschriftet mit Art *und* Wert der Kennung ("Aktenzeichen 123/45"):
        die Kante ist nur so viel wert wie ihre Begründung, und die soll
        man am Graphen ablesen können, ohne beide Dokumente zu öffnen.
        """
        excluded = set(exclude_ids)
        matches = [
            (other, reference)
            for other, reference in shared_reference_matches(self.user, document)
            if other.pk not in excluded
        ]
        rows, more = matches[: self.limit], len(matches) > self.limit

        reference_ids = []
        for other, reference in rows:
            reference_ids.append(other.pk)
            nodes.append(_document_node(other))
            edges.append(
                _reference_edge(
                    node_id(DOCUMENT, document.pk),
                    node_id(DOCUMENT, other.pk),
                    f"{reference.get_type_display()} {reference.value_raw}",
                )
            )
        return reference_ids, more

    def _document_list_neighborhood(self, focus, documents, edge_label) -> dict:
        """Nachbarschaft von Vorgang/Tag/Kontakt: die zugeordneten Dokumente.

        Keine Ähnlichkeitskanten auf dieser Ebene -- die hängen am
        einzelnen Dokument und kämen sonst als N kNN-Queries pro Expansion
        zurück. Wer sie sehen will, klappt das Dokument auf.
        """
        rows, truncated = _capped(_by_display_date(documents), self.limit)
        nodes = [focus]
        edges = []
        for document in rows:
            nodes.append(_document_node(document))
            edges.append(
                _structure_edge(
                    node_id(DOCUMENT, document.pk), focus["id"], edge_label
                )
            )
        return _payload(focus, nodes, edges, truncated)


def _payload(focus, nodes, edges, truncated) -> dict:
    """Knoten nach ID deduplizieren -- dieselbe Nachbarschaft kann einen
    Knoten über zwei Wege enthalten (z. B. ein Dokument als Querverweis
    *und* als Ähnlichkeitstreffer). Das Frontend mergt ohnehin, aber ein
    Payload mit doppelten IDs wäre schon hier falsch.
    """
    nodes_by_id = {node["id"]: node for node in nodes}
    edges_by_id = {edge["id"]: edge for edge in edges}
    return {
        "focus": focus,
        "nodes": list(nodes_by_id.values()),
        "edges": list(edges_by_id.values()),
        "truncated": truncated,
        "limit": settings.FINDUS_GRAPH_NEIGHBOR_LIMIT,
    }


def node_payload(obj) -> dict:
    """Ein Modell -> die Knoten-Darstellung im Graphen (ID, Typ, Label, URL,
    Tooltip-Zeilen). Auch die Fokus-Suche rendert ihre Treffer daraus,
    damit Trefferliste und Graph denselben Knoten gleich beschriften.
    """
    if isinstance(obj, Document):
        return _document_node(obj)
    if isinstance(obj, Vorgang):
        return _vorgang_node(obj)
    if isinstance(obj, Tag):
        return _tag_node(obj)
    if isinstance(obj, Correspondent):
        return _correspondent_node(obj)
    raise TypeError(f"Kein Graph-Knotentyp: {type(obj)!r}")


def _document_node(document: Document) -> dict:
    meta = [NODE_TYPE_LABELS[DOCUMENT], _formatted_date(document)]
    if document.correspondent_id is not None:
        meta.append(document.correspondent.name)
    return {
        "id": node_id(DOCUMENT, document.pk),
        "type": DOCUMENT,
        "pk": document.pk,
        "label": document.title,
        "url": reverse("documents:detail", args=[document.pk]),
        "meta": meta,
    }


def _formatted_date(document: Document) -> str:
    stamp = document.display_date.strftime("%d.%m.%Y")
    return f"{stamp} (Upload)" if document.display_date_is_upload_fallback else stamp


def _vorgang_node(vorgang: Vorgang) -> dict:
    return {
        "id": node_id(VORGANG, vorgang.pk),
        "type": VORGANG,
        "pk": vorgang.pk,
        "label": vorgang.name,
        "url": reverse("documents:vorgang_detail", args=[vorgang.pk]),
        "meta": [NODE_TYPE_LABELS[VORGANG], vorgang.get_status_display()],
    }


def _tag_node(tag: Tag) -> dict:
    return {
        "id": node_id(TAG, tag.pk),
        "type": TAG,
        "pk": tag.pk,
        "label": tag.name,
        "url": reverse("documents:tag_detail", args=[tag.pk]),
        "meta": [NODE_TYPE_LABELS[TAG], tag.dimension or "ohne Dimension"],
    }


def _correspondent_node(correspondent: Correspondent) -> dict:
    meta = [NODE_TYPE_LABELS[CORRESPONDENT]]
    if correspondent.is_self:
        meta.append("Das bin ich")
    return {
        "id": node_id(CORRESPONDENT, correspondent.pk),
        "type": CORRESPONDENT,
        "pk": correspondent.pk,
        "label": correspondent.name,
        "url": reverse("documents:correspondent_detail", args=[correspondent.pk]),
        "meta": meta,
    }


def _structure_edge(source: str, target: str, label: str) -> dict:
    return {
        "id": f"{STRUCTURE}|{source}|{target}",
        "source": source,
        "target": target,
        "kind": STRUCTURE,
        "label": label,
    }


def _reference_edge(source: str, target: str, label: str) -> dict:
    """Ungerichtet wie die Ähnlichkeitskante, deshalb dieselbe kanonisch
    sortierte ID: "A und B teilen Kennung X" ist von beiden Enden aus
    dieselbe Aussage und darf beim Aufklappen beider Seiten nicht doppelt
    im Graphen landen.

    Die ID trägt bewusst *nicht* die Kennung selbst: teilen zwei Dokumente
    mehrere Kennungen, ist das eine Verbindung mit mehreren Gründen, keine
    mehrfache Verbindung (`shared_reference_matches` liefert deshalb je
    Gegenstelle nur ein Paar).
    """
    first, second = sorted((source, target))
    return {
        "id": f"{REFERENCE}|{first}|{second}",
        "source": first,
        "target": second,
        "kind": REFERENCE,
        "label": label,
    }


def _similarity_edge(source: str, target: str, score: float) -> dict:
    """Ungerichtet, deshalb kanonisch sortierte ID: A→B und B→A sind
    dieselbe Ähnlichkeit und dürfen beim Aufklappen beider Enden nicht als
    zwei Kanten im Graphen landen.
    """
    first, second = sorted((source, target))
    return {
        "id": f"{SIMILARITY}|{first}|{second}",
        "source": first,
        "target": second,
        "kind": SIMILARITY,
        "label": f"{round(score * 100)} %",
        "score": round(score, 4),
    }
