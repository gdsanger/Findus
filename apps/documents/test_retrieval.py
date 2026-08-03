from datetime import date, datetime, timezone

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Department
from apps.ai.providers.base import EmbeddingResult

from .models import Chunk, Correspondent, Document, Tag, Vorgang
from .retrieval import DocumentRetrievalService

User = get_user_model()

DIMENSIONS = settings.FINDUS_EMBEDDING_DIMENSIONS


def _one_hot(index: int) -> list[float]:
    vector = [0.0] * DIMENSIONS
    vector[index] = 1.0
    return vector


class _StubEmbeddingProvider:
    """Returns a fixed query vector regardless of input text -- lets

    tests control exactly which chunk should rank first without
    depending on a real provider's embedding output.
    """

    def __init__(self, vector: list[float]):
        self.vector = vector

    def embed(self, texts):
        texts = list(texts)
        return EmbeddingResult(vectors=[self.vector] * len(texts), model="stub", version="1")


def _add_chunk(document: Document, *, position: int, vector: list[float]) -> Chunk:
    return Chunk.objects.create(
        document=document,
        position=position,
        content="chunk text",
        embedding=vector,
        embedding_model="stub",
        embedding_model_version="1",
    )


class DocumentRetrievalSearchTests(TestCase):
    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.query_vector = _one_hot(0)

    def _service(self, user, vector=None):
        provider = _StubEmbeddingProvider(vector if vector is not None else self.query_vector)
        return DocumentRetrievalService(user, embedding_provider=provider)

    def test_ranks_documents_by_best_matching_chunk(self):
        close_doc = Document.objects.create(title="Close", visibility=Document.Visibility.DEPARTMENT)
        close_doc.departments.add(self.dept_a)
        _add_chunk(close_doc, position=0, vector=_one_hot(0))

        far_doc = Document.objects.create(title="Far", visibility=Document.Visibility.DEPARTMENT)
        far_doc.departments.add(self.dept_a)
        _add_chunk(far_doc, position=0, vector=_one_hot(1))

        hits = self._service(self.user_a).search("invoice")

        self.assertEqual([hit.document for hit in hits], [close_doc, far_doc])
        self.assertGreater(hits[0].score, hits[1].score)

    def test_user_without_matching_department_does_not_see_document_via_similarity(self):
        """The core ACL guarantee: a document that is a perfect semantic

        match must still be invisible to a user outside its department --
        similarity ranking must never leak past `visible_to`.
        """
        hidden_doc = Document.objects.create(title="Hidden", visibility=Document.Visibility.DEPARTMENT)
        hidden_doc.departments.add(self.dept_a)
        _add_chunk(hidden_doc, position=0, vector=_one_hot(0))

        visible_doc = Document.objects.create(title="Visible to Bob", visibility=Document.Visibility.DEPARTMENT)
        visible_doc.departments.add(self.dept_b)
        _add_chunk(visible_doc, position=0, vector=_one_hot(1))

        hits = self._service(self.user_b).search("invoice")

        self.assertNotIn(hidden_doc, [hit.document for hit in hits])
        self.assertEqual([hit.document for hit in hits], [visible_doc])

    def test_private_document_only_visible_to_owner(self):
        private_doc = Document.objects.create(
            title="Private",
            visibility=Document.Visibility.PRIVATE,
            owner=self.user_a,
        )
        _add_chunk(private_doc, position=0, vector=_one_hot(0))

        self.assertIn(private_doc, [hit.document for hit in self._service(self.user_a).search("invoice")])
        self.assertNotIn(private_doc, [hit.document for hit in self._service(self.user_b).search("invoice")])

    def test_search_combines_with_structured_filters(self):
        correspondent = Correspondent.objects.create(name="ACME")
        vorgang = Vorgang.objects.create(name="Vorgang X")
        tag = Tag.objects.create(name="invoice")

        matching_doc = Document.objects.create(
            title="Matching",
            visibility=Document.Visibility.DEPARTMENT,
            correspondent=correspondent,
        )
        matching_doc.departments.add(self.dept_a)
        matching_doc.vorgaenge.add(vorgang)
        matching_doc.tags.add(tag)
        _add_chunk(matching_doc, position=0, vector=_one_hot(0))

        other_correspondent_doc = Document.objects.create(
            title="Other correspondent",
            visibility=Document.Visibility.DEPARTMENT,
        )
        other_correspondent_doc.departments.add(self.dept_a)
        _add_chunk(other_correspondent_doc, position=0, vector=_one_hot(0))

        hits = self._service(self.user_a).search("invoice", correspondent=correspondent)

        self.assertEqual([hit.document for hit in hits], [matching_doc])

    def test_search_respects_limit(self):
        for i in range(3):
            doc = Document.objects.create(title=f"Doc {i}", visibility=Document.Visibility.DEPARTMENT)
            doc.departments.add(self.dept_a)
            _add_chunk(doc, position=0, vector=_one_hot(0))

        hits = self._service(self.user_a).search("invoice", limit=2)

        self.assertEqual(len(hits), 2)


class DocumentRetrievalListTests(TestCase):
    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)
        self.service = DocumentRetrievalService(self.user_a)

    def test_list_documents_filters_by_status_and_date(self):
        old_doc = Document.objects.create(
            title="Old",
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.READY,
        )
        old_doc.departments.add(self.dept_a)
        Document.objects.filter(pk=old_doc.pk).update(
            created_at=datetime(2020, 1, 1, tzinfo=timezone.utc)
        )

        pending_doc = Document.objects.create(
            title="Pending",
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.PENDING,
        )
        pending_doc.departments.add(self.dept_a)

        results = list(
            self.service.list_documents(
                status=Document.ProcessingStatus.READY,
                date_from=date(2019, 1, 1),
                date_to=date(2021, 1, 1),
            )
        )

        self.assertEqual(results, [old_doc])

    def test_list_documents_filters_by_direction(self):
        incoming = Document.objects.create(
            title="Eingangsrechnung",
            visibility=Document.Visibility.DEPARTMENT,
            direction=Document.Direction.EINGANG,
        )
        incoming.departments.add(self.dept_a)

        outgoing = Document.objects.create(
            title="Ausgangsrechnung",
            visibility=Document.Visibility.DEPARTMENT,
            direction=Document.Direction.AUSGANG,
        )
        outgoing.departments.add(self.dept_a)

        results = list(self.service.list_documents(direction=Document.Direction.EINGANG))

        self.assertEqual(results, [incoming])

    def test_list_documents_enforces_visibility(self):
        other_dept = Department.objects.create(name="Dept Other")
        other_user = User.objects.create_user(username="carol", password="x")
        other_user.departments.add(other_dept)

        doc = Document.objects.create(title="Dept A doc", visibility=Document.Visibility.DEPARTMENT)
        doc.departments.add(self.dept_a)

        self.assertIn(doc, self.service.list_documents())
        self.assertNotIn(doc, DocumentRetrievalService(other_user).list_documents())
