"""Fokus-Graph (#1091): Sichtbarkeits-Scoping, Caps und Kantensorten.

Der Schwerpunkt liegt auf dem JSON-Endpunkt -- er ist die Stelle, an der
ein Fehler ein fremdes Dokument preisgäbe oder aus einer Nachbarschaft
einen Full-Table-Scan machte. Die Seite selbst wird nur darauf geprüft,
dass sie den Fokus korrekt durchreicht; das Zeichnen macht Cytoscape.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.models import Department
from apps.ai.providers.base import EmbeddingResult

from .models import Chunk, Correspondent, Document, Tag, Vorgang, link_documents

User = get_user_model()

DIMENSIONS = settings.FINDUS_EMBEDDING_DIMENSIONS


def _one_hot(index: int) -> list[float]:
    vector = [0.0] * DIMENSIONS
    vector[index] = 1.0
    return vector


def _add_chunk(document, *, position=0, vector=None):
    return Chunk.objects.create(
        document=document,
        position=position,
        content="chunk text",
        embedding=vector if vector is not None else _one_hot(0),
        embedding_model="stub",
        embedding_model_version="1",
    )


class _StubEmbeddingProvider:
    def embed(self, texts):
        texts = list(texts)
        return EmbeddingResult(vectors=[_one_hot(0)] * len(texts), model="stub", version="1")


class GraphViewTests(TestCase):
    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.correspondent = Correspondent.objects.create(name="Stadtwerke")
        self.vorgang = Vorgang.objects.create(name="Hausanschluss")
        self.tag = Tag.objects.create(name="Rechnung", dimension="Thema")

        self.document = self._document("Abschlagsrechnung", self.dept_a)
        self.document.correspondent = self.correspondent
        self.document.save()
        self.document.vorgaenge.add(self.vorgang)
        self.document.tags.add(self.tag)

        self.foreign_document = self._document("Fremde Abteilung", self.dept_b)
        self.foreign_document.correspondent = self.correspondent
        self.foreign_document.save()
        self.foreign_document.vorgaenge.add(self.vorgang)

        self.client.force_login(self.user_a)

    def _document(self, title, department, **fields):
        document = Document.objects.create(
            title=title, visibility=Document.Visibility.DEPARTMENT, **fields
        )
        document.departments.add(department)
        return document

    def _neighbors(self, node_type, pk, **params):
        response = self.client.get(
            reverse("documents:graph_neighbors"),
            {"type": node_type, "id": pk, "similarity": "0", **params},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    # -- Seite ---------------------------------------------------------------

    def test_graph_page_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("documents:graph"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_graph_page_without_focus_shows_search_prompt(self):
        response = self.client.get(reverse("documents:graph"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["focus_node"])
        self.assertFalse(response.context["focus_missing"])

    def test_graph_page_passes_focus_to_the_frontend(self):
        response = self.client.get(
            reverse("documents:graph"), {"type": "document", "id": self.document.pk}
        )
        self.assertEqual(
            response.context["focus_node"]["id"], f"document:{self.document.pk}"
        )
        self.assertContains(response, f'data-focus-node="document:{self.document.pk}"')

    def test_graph_page_ships_the_vendored_assets_and_controls(self):
        """Cytoscape ist vendored (kein CDN, kein Build) -- die Seite muss es
        samt Toolbar tatsächlich einbinden, sonst bleibt die Fläche leer.
        """
        response = self.client.get(reverse("documents:graph"))

        self.assertContains(response, "vendor/cytoscape/cytoscape.min.js")
        self.assertContains(response, "js/findus-graph.js")
        self.assertContains(response, 'id="graph-canvas"')
        self.assertContains(response, "data-graph-similarity")
        for node_type in ("document", "vorgang", "tag", "correspondent"):
            self.assertContains(response, f'data-graph-node-filter="{node_type}"')

    def test_hubs_link_into_the_graph(self):
        """"Im Graph anzeigen" von Dokument-Detail, Vorgangs- und Kontakt-Hub."""
        graph_url = reverse("documents:graph")
        for url, expected in [
            (
                reverse("documents:detail", args=[self.document.pk]),
                f"{graph_url}?type=document&amp;id={self.document.pk}",
            ),
            (
                reverse("documents:vorgang_detail", args=[self.vorgang.pk]),
                f"{graph_url}?type=vorgang&amp;id={self.vorgang.pk}",
            ),
            (
                reverse("documents:correspondent_detail", args=[self.correspondent.pk]),
                f"{graph_url}?type=correspondent&amp;id={self.correspondent.pk}",
            ),
        ]:
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), expected)

    def test_graph_page_reports_a_focus_the_user_may_not_see(self):
        """Ein nicht sichtbares Dokument darf keinen Fokus ergeben -- und die
        Seite muss das sagen, statt eine leere Fläche zu zeigen.
        """
        response = self.client.get(
            reverse("documents:graph"),
            {"type": "document", "id": self.foreign_document.pk},
        )
        self.assertIsNone(response.context["focus_node"])
        self.assertTrue(response.context["focus_missing"])

    # -- Nachbarschaft: Struktur ---------------------------------------------

    def test_document_neighborhood_contains_structure_edges(self):
        payload = self._neighbors("document", self.document.pk)

        node_ids = {node["id"] for node in payload["nodes"]}
        self.assertEqual(
            node_ids,
            {
                f"document:{self.document.pk}",
                f"correspondent:{self.correspondent.pk}",
                f"vorgang:{self.vorgang.pk}",
                f"tag:{self.tag.pk}",
            },
        )
        self.assertTrue(all(edge["kind"] == "structure" for edge in payload["edges"]))
        self.assertEqual(
            {edge["label"] for edge in payload["edges"]}, {"Kontakt", "Vorgang", "Tag"}
        )
        self.assertFalse(payload["truncated"])

    def test_vorgang_neighborhood_only_lists_visible_documents(self):
        payload = self._neighbors("vorgang", self.vorgang.pk)

        node_ids = {node["id"] for node in payload["nodes"]}
        self.assertIn(f"document:{self.document.pk}", node_ids)
        self.assertNotIn(f"document:{self.foreign_document.pk}", node_ids)

    def test_correspondent_neighborhood_only_lists_visible_documents(self):
        payload = self._neighbors("correspondent", self.correspondent.pk)

        node_ids = {node["id"] for node in payload["nodes"]}
        self.assertIn(f"document:{self.document.pk}", node_ids)
        self.assertNotIn(f"document:{self.foreign_document.pk}", node_ids)

    def test_neighbors_of_an_invisible_document_are_404(self):
        response = self.client.get(
            reverse("documents:graph_neighbors"),
            {"type": "document", "id": self.foreign_document.pk},
        )
        self.assertEqual(response.status_code, 404)

    def test_unknown_node_type_is_404(self):
        response = self.client.get(
            reverse("documents:graph_neighbors"), {"type": "chunk", "id": "1"}
        )
        self.assertEqual(response.status_code, 404)

    def test_document_links_become_structure_edges(self):
        partner = self._document("Zahlungserinnerung", self.dept_a)
        link_documents(self.document, partner, created_by=self.user_a)

        payload = self._neighbors("document", self.document.pk)

        querverweise = [
            edge for edge in payload["edges"] if edge["label"] == "Querverweis"
        ]
        self.assertEqual(len(querverweise), 1)
        self.assertEqual(querverweise[0]["kind"], "structure")
        self.assertIn(
            f"document:{partner.pk}",
            {querverweise[0]["source"], querverweise[0]["target"]},
        )

    def test_a_querverweis_has_the_same_edge_id_from_both_ends(self):
        """Ungerichtet heißt: wer beide Enden aufklappt, bekommt eine Kante,
        nicht zwei parallele.
        """
        partner = self._document("Zahlungserinnerung", self.dept_a)
        link_documents(self.document, partner, created_by=self.user_a)

        from_here = self._neighbors("document", self.document.pk)
        from_there = self._neighbors("document", partner.pk)

        def link_edge_ids(payload):
            return {
                edge["id"] for edge in payload["edges"] if edge["label"] == "Querverweis"
            }

        self.assertEqual(link_edge_ids(from_here), link_edge_ids(from_there))

    def test_document_links_to_invisible_documents_are_dropped(self):
        link_documents(self.document, self.foreign_document, created_by=self.user_a)

        payload = self._neighbors("document", self.document.pk)

        node_ids = {node["id"] for node in payload["nodes"]}
        self.assertNotIn(f"document:{self.foreign_document.pk}", node_ids)

    def test_expansion_is_capped_and_reports_truncation(self):
        limit = settings.FINDUS_GRAPH_NEIGHBOR_LIMIT
        for index in range(limit + 3):
            document = self._document(f"Beleg {index}", self.dept_a)
            document.tags.add(self.tag)

        payload = self._neighbors("tag", self.tag.pk)

        document_nodes = [
            node for node in payload["nodes"] if node["type"] == "document"
        ]
        self.assertEqual(len(document_nodes), limit)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["limit"], limit)

    def test_neighborhood_query_count_is_independent_of_neighbor_count(self):
        """Kein N+1: die Nachbarschaft eines Vorgangs kostet dieselbe Zahl
        Queries, egal ob drei oder dreißig Dokumente dranhängen.
        """
        for index in range(3):
            document = self._document(f"Klein {index}", self.dept_a)
            document.vorgaenge.add(self.vorgang)
        with CaptureQueriesContext(connection) as small:
            self._neighbors("vorgang", self.vorgang.pk)

        for index in range(15):
            document = self._document(f"Gross {index}", self.dept_a)
            document.vorgaenge.add(self.vorgang)
        with CaptureQueriesContext(connection) as large:
            self._neighbors("vorgang", self.vorgang.pk)

        self.assertEqual(len(large.captured_queries), len(small.captured_queries))

    # -- Nachbarschaft: Ähnlichkeit ------------------------------------------

    def test_similarity_edges_are_returned_and_switchable(self):
        similar = self._document("Fast dasselbe", self.dept_a)
        _add_chunk(self.document, vector=_one_hot(0))
        _add_chunk(similar, vector=_one_hot(0))

        with_similarity = self._neighbors(
            "document", self.document.pk, similarity="1"
        )
        similarity_edges = [
            edge for edge in with_similarity["edges"] if edge["kind"] == "similarity"
        ]
        self.assertEqual(len(similarity_edges), 1)
        self.assertIn(
            f"document:{similar.pk}",
            {similarity_edges[0]["source"], similarity_edges[0]["target"]},
        )
        self.assertGreaterEqual(similarity_edges[0]["score"], 0.99)

        without = self._neighbors("document", self.document.pk, similarity="0")
        self.assertEqual(
            [edge for edge in without["edges"] if edge["kind"] == "similarity"], []
        )

    def test_similarity_never_crosses_the_visibility_boundary(self):
        _add_chunk(self.document, vector=_one_hot(0))
        _add_chunk(self.foreign_document, vector=_one_hot(0))

        payload = self._neighbors("document", self.document.pk, similarity="1")

        node_ids = {node["id"] for node in payload["nodes"]}
        self.assertNotIn(f"document:{self.foreign_document.pk}", node_ids)

    def test_manually_linked_documents_are_not_also_similarity_edges(self):
        """Dieselbe Beziehung nicht zweimal: was als Querverweis dransteht,
        taucht nicht zusätzlich als gestrichelte Ähnlichkeitskante auf.
        """
        partner = self._document("Doppelt", self.dept_a)
        _add_chunk(self.document, vector=_one_hot(0))
        _add_chunk(partner, vector=_one_hot(0))
        link_documents(self.document, partner, created_by=self.user_a)

        payload = self._neighbors("document", self.document.pk, similarity="1")

        self.assertEqual(
            [edge for edge in payload["edges"] if edge["kind"] == "similarity"], []
        )


class GraphSearchTests(TestCase):
    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")
        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)
        self.client.force_login(self.user_a)

    def _document(self, title, department):
        document = Document.objects.create(
            title=title, visibility=Document.Visibility.DEPARTMENT
        )
        document.departments.add(department)
        return document

    def test_search_finds_all_four_node_types(self):
        document = self._document("Mietvertrag", self.dept_a)
        vorgang = Vorgang.objects.create(name="Miete Wohnung")
        tag = Tag.objects.create(name="Miete")
        correspondent = Correspondent.objects.create(name="Mieterverein")
        document.vorgaenge.add(vorgang)
        document.tags.add(tag)
        document.correspondent = correspondent
        document.save()

        response = self.client.get(reverse("documents:graph_search"), {"q": "Miet"})

        self.assertEqual(response.status_code, 200)
        found = {node["id"] for node in response.context["results"]}
        self.assertEqual(
            found,
            {
                f"document:{document.pk}",
                f"vorgang:{vorgang.pk}",
                f"tag:{tag.pk}",
                f"correspondent:{correspondent.pk}",
            },
        )

    def test_search_hides_documents_of_other_departments(self):
        self._document("Mietvertrag fremd", self.dept_b)

        response = self.client.get(reverse("documents:graph_search"), {"q": "Miet"})

        self.assertEqual(response.context["results"], [])

    def test_search_skips_vorgaenge_without_visible_documents(self):
        """Ein Vorgang, dessen Dokumente alle fremd sind, ergäbe einen Graphen
        aus einem einzelnen Punkt -- als Startpunkt ist er wertlos.
        """
        vorgang = Vorgang.objects.create(name="Mietsache")
        foreign = self._document("Fremd", self.dept_b)
        foreign.vorgaenge.add(vorgang)

        response = self.client.get(reverse("documents:graph_search"), {"q": "Miet"})

        self.assertEqual(response.context["results"], [])

    def test_empty_query_returns_no_results(self):
        self._document("Mietvertrag", self.dept_a)

        response = self.client.get(reverse("documents:graph_search"), {"q": "  "})

        self.assertEqual(response.context["results"], [])
