"""Vertragstests fuer die Invarianten aus `CLAUDE.md`.

Bewusst getrennt von den fachlichen Testmodulen (`test_views.py`,
`test_analysis.py`, ...): hier wird Architektur geprueft, nicht Verhalten.
Jeder Test ist nach der Regel benannt, die er absichert, nicht nach der
Funktion, die er aufruft -- ein fehlschlagender Test soll sagen, welche
Entscheidung gerade verletzt wird. Wer einen dieser Tests bewusst aendert,
aendert `CLAUDE.md` im selben PR (siehe dort, "Pflegeregel").
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.template.loader import get_template
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Department
from apps.ai.providers.fake import FakeEmbeddingProvider, FakeGenerationProvider

from . import urls as documents_urls
from .analysis import analyze_document
from .models import Chunk, Document, DocumentComment, Vorgang
from .processing import process_document
from .recommendations import generate_vorgang_recommendations
from .thumbnails import generate_thumbnail_for_document

User = get_user_model()

# Eigenes Temp-Verzeichnis statt des echten MEDIA_ROOT (gleiches Muster wie
# test_thumbnails.py) -- sonst schreiben diese Tests echte Dateien ins
# Repo-Arbeitsverzeichnis.
_TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="findus-contracts-media-")
_LOCAL_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


class CommentsAreNotEmbeddedTests(TestCase):
    """Kommentare sind Notizen ueber ein Dokument, nicht dessen Inhalt --

    in den Chunks wuerden sie die semantische Suche ueber den
    Dokumentbestand verwaessern (#1125, CLAUDE.md "Pipelines & Services").
    """

    def test_comments_are_not_embedded(self):
        document = Document.objects.create(
            title="Rechnung.pdf",
            text_content="Rechnungsinhalt " * 20,
        )
        DocumentComment.objects.create(
            document=document,
            body="Streng vertrauliche Notiz, die niemals in einem Chunk landen darf.",
        )

        process_document(
            document.id,
            embedding_provider=FakeEmbeddingProvider(
                dimensions=settings.FINDUS_EMBEDDING_DIMENSIONS
            ),
        )

        contents = list(
            Chunk.objects.filter(document=document).values_list("content", flat=True)
        )
        self.assertTrue(contents, "process_document sollte Chunks fuer text_content anlegen")
        self.assertFalse(
            any("Streng vertrauliche Notiz" in content for content in contents),
            "Kommentartext ist in einem Chunk gelandet -- Kommentare duerfen nicht embedded werden",
        )


class CommentsAreUsedAsVorgangRecommendationContextTests(TestCase):
    """Die zweite Haelfte der Kommentar-Regel (siehe oben): nicht embedded,

    aber sehr wohl als Prompt-Kontext fuer die Handlungsempfehlungen eines
    Vorgangs -- als eigener, benannter Abschnitt, nicht vermischt mit den
    Dokumentbloecken (#1132, CLAUDE.md "Pipelines & Services").
    """

    def test_comment_reaches_the_recommendation_prompt_as_its_own_section(self):
        department = Department.objects.create(name="Buchhaltung")
        user = User.objects.create_user(username="alice", password="x")
        user.departments.add(department)

        vorgang = Vorgang.objects.create(name="Forderung Acme")
        document = Document.objects.create(
            title="Rechnung", summary="Kurz.", visibility=Document.Visibility.DEPARTMENT
        )
        document.departments.add(department)
        document.vorgaenge.add(vorgang)
        DocumentComment.objects.create(
            document=document, body="Bereits vollstaendig beglichen."
        )

        provider = FakeGenerationProvider(
            reply=json.dumps({"lage": "Erledigt.", "empfehlungen": []})
        )
        generate_vorgang_recommendations(vorgang.pk, user.pk, generation_provider=provider)

        prompt = provider.calls[0][-1].content
        self.assertIn(
            "Notizen des Nutzers zu diesen Dokumenten",
            prompt,
            "Kommentare muessen als eigener, benannter Abschnitt im Prompt stehen",
        )
        self.assertIn("Bereits vollstaendig beglichen.", prompt)


class AllDocumentEndpointsScopeByVisibilityTests(TestCase):
    """Jeder dokumentbezogene Endpunkt muss ueber

    `Document.objects.visible_to(user)` laufen: ein fremdes Dokument ist ein
    404, nie ein 403 oder gar sichtbarer Inhalt (CLAUDE.md "Sichtbarkeit &
    Auslieferung"). Laeuft generisch ueber `documents/urls.py`, damit eine
    neue, nicht gescopte View auffaellt, ohne dass jemand aktiv daran denken
    muss, sie hier nachzutragen.
    """

    # Werte fuer Extra-Pfadparameter, die eine View selbst vor dem
    # Sichtbarkeits-Check auswertet (`scope`/`kind`) -- muessen gueltig
    # sein, sonst wird der Test durch einen fruehen, unabhaengigen 404
    # bestehen, ohne die Sichtbarkeits-Pruefung ueberhaupt zu erreichen.
    _STRING_PARAM_VALUES = {"scope": "vorgang", "kind": "correspondent"}
    _NONEXISTENT_ID = 999999

    def setUp(self):
        self.dept = Department.objects.create(name="Eigene Abteilung")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept)

        foreign_dept = Department.objects.create(name="Fremde Abteilung")
        self.foreign_document = Document.objects.create(
            title="Fremdes Dokument", visibility=Document.Visibility.DEPARTMENT
        )
        self.foreign_document.departments.add(foreign_dept)

        self.client.force_login(self.user)

    def _document_scoped_patterns(self):
        for pattern in documents_urls.urlpatterns:
            route = str(pattern.pattern)
            if route.startswith("documents/<int:pk>"):
                yield pattern

    def _kwargs_for(self, pattern):
        kwargs = {}
        for name, converter in pattern.pattern.converters.items():
            if name == "pk":
                kwargs[name] = self.foreign_document.pk
            elif type(converter).__name__ == "IntConverter":
                kwargs[name] = self._NONEXISTENT_ID
            else:
                kwargs[name] = self._STRING_PARAM_VALUES.get(name, "x")
        return kwargs

    def test_all_document_endpoints_scope_by_visibility(self):
        patterns = list(self._document_scoped_patterns())
        self.assertGreater(
            len(patterns), 0, "Erwartet mindestens die bekannten documents/<pk>/...-Routen"
        )

        failures = []
        for pattern in patterns:
            url = reverse(f"documents:{pattern.name}", kwargs=self._kwargs_for(pattern))

            response = self.client.get(url)
            if response.status_code == 405:
                response = self.client.post(url)

            if response.status_code != 404:
                failures.append(
                    f"{pattern.name} ({url}): erwartet 404 fuer fremdes Dokument, "
                    f"bekam {response.status_code}"
                )

        self.assertEqual(
            failures,
            [],
            "Endpunkt(e) ohne visible_to-Scoping fuer ein fremdes Dokument:\n"
            + "\n".join(failures),
        )


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=_TEST_MEDIA_ROOT)
class OriginalAndThumbnailAreNeverPublicUrlsTests(TestCase):
    """Original und Thumbnail werden ausschliesslich ueber den

    auth-gestuetzten Stream ausgeliefert, nie ueber eine direkte
    Storage-/MEDIA-URL (CLAUDE.md "Sichtbarkeit & Auslieferung") -- die
    Storage-Backend-URL ist ein oeffentlicher S3-/MinIO-Link, der die ACL
    vollstaendig umgeht.
    """

    def setUp(self):
        self.dept = Department.objects.create(name="Abteilung")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept)
        self.client.force_login(self.user)

        # application/zip ist weder inline-previewable noch ein Bildformat
        # (siehe Document.is_inline_previewable) -- die Vorschau-Vorlage
        # rendert damit sowohl das Thumbnail-<img> als auch den
        # Original-Download-Link, ohne eine echte PDF/Bild-Datei zu brauchen.
        self.document = Document.objects.create(
            title="Archiv.zip",
            metadata={"mime_type": "application/zip", "original_filename": "Archiv.zip"},
        )
        self.document.departments.add(self.dept)
        self.document.original_file.save("Archiv.zip", _dummy_file(b"PK\x03\x04dummy"))
        self.document.thumbnail.save("Archiv.webp", _dummy_file(b"dummy-thumbnail"))

    def test_original_and_thumbnail_are_never_public_urls(self):
        response = self.client.get(
            reverse("documents:detail", kwargs={"pk": self.document.pk})
        )

        self.assertEqual(response.status_code, 200)
        markup = response.content.decode()

        self.assertNotIn(settings.MEDIA_URL, markup)
        self.assertNotIn("minio", markup.lower())

        self.assertIn(
            reverse("documents:original_download", kwargs={"pk": self.document.pk}), markup
        )
        self.assertIn(
            reverse("documents:thumbnail", kwargs={"pk": self.document.pk}), markup
        )


class FailedAnalysisDoesNotFailDocumentTests(TestCase):
    """Eine fehlschlagende KI-Analyse legt die Pipeline nicht lahm -- sie

    loggt und laesst `processing_status` unangetastet statt ihn auf
    `failed` zu setzen (CLAUDE.md "Pipelines & Services").
    """

    def test_failed_analysis_does_not_fail_document(self):
        class _RaisingProvider:
            def generate(self, messages, *, stream=False):
                raise RuntimeError("provider down")

        document = Document.objects.create(
            title="doc.pdf",
            text_content="Inhalt",
            processing_status=Document.ProcessingStatus.ANALYZING,
        )

        result = analyze_document(document.id, generation_provider=_RaisingProvider())

        self.assertNotEqual(result.processing_status, Document.ProcessingStatus.FAILED)
        self.assertIn("analysis_error", result.metadata)


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=_TEST_MEDIA_ROOT)
class FailedThumbnailDoesNotFailDocumentTests(TestCase):
    """Ein fehlschlagendes Thumbnail-Rendering legt die Pipeline nicht lahm

    -- gleiches Prinzip wie bei der KI-Analyse: loggen und weiterlaufen,
    statt `processing_status` auf `failed` zu setzen (CLAUDE.md "Pipelines &
    Services").
    """

    def test_failed_thumbnail_does_not_fail_document(self):
        document = Document.objects.create(
            title="Rechnung.pdf",
            metadata={"mime_type": "application/pdf", "original_filename": "Rechnung.pdf"},
            processing_status=Document.ProcessingStatus.READY,
        )
        document.original_file.save("Rechnung.pdf", _dummy_file(b"%PDF-1.4 dummy"))

        with patch(
            "apps.documents.thumbnails.generate_thumbnail",
            side_effect=RuntimeError("render kaputt"),
        ):
            generate_thumbnail_for_document(document.id)

        document.refresh_from_db()
        self.assertNotEqual(document.processing_status, Document.ProcessingStatus.FAILED)
        self.assertEqual(document.processing_status, Document.ProcessingStatus.READY)


class NoSecondDoneStateOnCommentsTests(TestCase):
    """`Document.action_status` ist die einzige Zustandsquelle fuer "hier

    ist noch etwas zu tun" (CLAUDE.md "Zustand & Fachlogik") --
    Strukturtest gegen eine versehentliche Wiedereinfuehrung eines eigenen
    Erledigt-Felds an `DocumentComment`.
    """

    _FORBIDDEN_FIELD_NAMES = {"done", "is_done", "completed", "resolved", "status", "erledigt"}

    def test_no_second_done_state_on_comments(self):
        field_names = {field.name for field in DocumentComment._meta.get_fields()}

        overlap = field_names & self._FORBIDDEN_FIELD_NAMES
        self.assertFalse(
            overlap,
            f"DocumentComment traegt ein eigenes Erledigt-Feld ({overlap}) -- "
            "der Zustand gehoert ausschliesslich auf Document.action_status.",
        )


class SwapPartialsCarryNoOwnFrameTests(SimpleTestCase):
    """Ein HTMX-Swap-Partial rahmt sich nicht selbst ein (CLAUDE.md

    "UI-Konventionen"). `_document_list.html` ist Antwort auf den
    Pending-Poller, der sich per `hx-swap="outerHTML"` gegen genau diese
    Antwort austauscht -- ein Card-Rahmen *im* Partial landete damit bei
    jedem Lauf eine Ebene tiefer in der vorigen Card. Dazu kommt: der
    Rahmen muesste in beiden Zweigen (Treffer/leer) auf- und zugehen, sonst
    stehen im leeren Zweig zwei herrenlose `</div>`.

    Der Rahmen gehoert deshalb in die einbindende Seite, um
    `#document-list-region` herum -- dort gilt er zugleich fuer die beiden
    anderen Partials, die in derselben Region landen koennen
    (`_document_followups.html`, `_search_results.html`).
    """

    _SWAP_PARTIALS = (
        "documents/partials/_document_list.html",
        "documents/partials/_document_followups.html",
        "documents/partials/_search_results.html",
    )

    _PAGES_WITH_A_LIST_REGION = (
        "documents/home.html",
        "documents/vorgaenge/detail.html",
        "documents/correspondents/detail.html",
        "documents/tags/detail.html",
    )

    def _source_of(self, template_name: str) -> str:
        return Path(get_template(template_name).origin.name).read_text(encoding="utf-8")

    def test_swap_partials_carry_no_own_frame(self):
        for template_name in self._SWAP_PARTIALS:
            with self.subTest(template=template_name):
                self.assertNotIn(
                    'class="card"',
                    self._source_of(template_name),
                    f"{template_name} rahmt sich selbst ein -- der Card-Rahmen "
                    "gehoert um #document-list-region in die einbindende Seite.",
                )

    def test_every_list_region_is_framed_by_its_page(self):
        for template_name in self._PAGES_WITH_A_LIST_REGION:
            with self.subTest(template=template_name):
                self.assertIn(
                    'id="document-list-region"',
                    self._source_of(template_name),
                    f"{template_name} bindet die Dokumentliste ein, ohne die "
                    "vereinbarte Swap-Region #document-list-region zu setzen.",
                )


class HubColumnsStackBeforeMdTests(SimpleTestCase):
    """Die Spalten der Hub-Seiten stapeln unterhalb `md` (CLAUDE.md,

    "UI-Konventionen"). Ein nacktes `col-3`/`col-9` gilt in Bootstrap ab
    dem kleinsten Breakpoint und bleibt damit auch auf dem Telefon stehen:
    die Stammdatenspalte ist dort ~90 px breit, ihre Eingabefelder laufen
    unter ihre `min-width` und sind unbedienbar. Deshalb die vollstaendige
    Leiter je Spalte -- `col-12 col-md-4 col-lg-3` neben
    `col-12 col-md-8 col-lg-9` --, mit dem Drittel im `md`-Bereich, weil die
    Felder bei 3/12 auf ihre Mindestbreite von 10rem zusammenfallen.

    Geprueft wird die Quelle statt des gerenderten Ergebnisses: die Regel
    haengt an genau einer Klassenliste je Spalte, und der Test soll auch
    dann anschlagen, wenn ein kuenftiger Hub dazukommt, den niemand rendert.
    """

    _HUB_PAGES = (
        "documents/vorgaenge/detail.html",
        "documents/correspondents/detail.html",
        "documents/tags/detail.html",
    )

    _EXPECTED_COLUMNS = ('"col-12 col-md-4 col-lg-3"', '"col-12 col-md-8 col-lg-9"')

    # Fasst jede Bootstrap-Spaltenklasse, deren Breite schon unterhalb `md`
    # greift (`col-3`, `col-sm-9`, ...) -- genau die Schreibweise, die den
    # Hub am Telefon zerlegt. `col-12` ist erlaubt: volle Breite ist der
    # gewuenschte Zustand.
    _COLUMN_BEFORE_MD = re.compile(r'\bcol(?:-sm)?-(?!12\b)\d+\b')

    def _source_of(self, template_name: str) -> str:
        return Path(get_template(template_name).origin.name).read_text(encoding="utf-8")

    def test_hub_columns_carry_the_full_breakpoint_ladder(self):
        for template_name in self._HUB_PAGES:
            source = self._source_of(template_name)
            for expected in self._EXPECTED_COLUMNS:
                with self.subTest(template=template_name, columns=expected):
                    self.assertIn(
                        expected,
                        source,
                        f"{template_name} weicht von der gemeinsamen "
                        f"Spaltenleiter {expected} ab -- die Hubs teilen ein "
                        "Layout, ein Alleingang laesst es beim Wechsel "
                        "springen.",
                    )

    def test_no_hub_column_is_narrow_before_md(self):
        for template_name in self._HUB_PAGES:
            with self.subTest(template=template_name):
                found = self._COLUMN_BEFORE_MD.findall(self._source_of(template_name))
                self.assertEqual(
                    found,
                    [],
                    f"{template_name} setzt {found} -- diese Breite gilt schon "
                    "unterhalb `md` und laesst die Stammdatenspalte am Telefon "
                    "auf einem Bruchteil der Breite stehen.",
                )


class DocumentListPagesShareOneViewDefaultTests(SimpleTestCase):
    """Alle Seiten mit Dokumentliste haben denselben Ansicht-Default:

    Kachel (CLAUDE.md, "UI-Konventionen"). Home, Vorgang-, Kontakt- und
    Tag-Hub teilen sich `_filter_bar.html` und `_document_list.html` -- ein
    abweichender Default in einer der Views laesst die Ansicht beim Wechsel
    Home <-> Hub springen, obwohl der Umschalter unveraendert dasteht (genau
    das war der Zustand, als nur Home auf Kachel umgestellt wurde und die
    Hubs auf "timeline" stehen blieben).

    Geprueft wird die Quelle statt des gerenderten Ergebnisses: der Default
    ist eine Entscheidung *einer Zeile je View*, und der Test soll auch dann
    anschlagen, wenn eine kuenftige Seite dazukommt, die niemand rendert.
    """

    _VIEW_MODULES = (
        "apps/documents/views.py",
        "apps/documents/vorgang_views.py",
        "apps/documents/correspondent_views.py",
        "apps/documents/tag_views.py",
    )

    def test_every_document_list_page_defaults_to_the_grid_view(self):
        pattern = re.compile(r'GET\.get\(\s*"view",\s*"([^"]*)"')
        for module_path in self._VIEW_MODULES:
            with self.subTest(module=module_path):
                source = (Path(settings.BASE_DIR) / module_path).read_text(encoding="utf-8")
                defaults = pattern.findall(source)
                self.assertTrue(
                    defaults,
                    f"{module_path} liest keinen `view`-Default mehr -- der "
                    "gemeinsame Ansicht-Vertrag haengt an dieser Zeile.",
                )
                for default in defaults:
                    self.assertEqual(
                        default,
                        "grid",
                        f"{module_path} faellt auf '{default}' statt auf die "
                        "Kachelansicht zurueck -- alle Seiten mit "
                        "Dokumentliste teilen einen Default.",
                    )


class QClusterRetryOutlivesTimeoutTests(SimpleTestCase):
    """`Q_CLUSTER["retry"]` muss groesser sein als jeder Task-Timeout im
    Cluster -- der globale `Q_CLUSTER["timeout"]` *und* jeder per-Task-
    Override wie `FINDUS_VORGANG_RECOMMENDATION_TASK_TIMEOUT_SECONDS`
    (#1134). Ist `retry` kleiner, stellt Django-Q den Task erneut in die
    Warteschlange, waehrend der erste Versuch noch laeuft -- doppelte
    Ausfuehrung, doppelte Token-Kosten, im schlimmsten Fall eine
    Endlosschleife. Siehe der Kommentar an `Q_CLUSTER` in
    `config/settings/base.py`.
    """

    def test_retry_exceeds_the_default_cluster_timeout(self):
        self.assertGreater(settings.Q_CLUSTER["retry"], settings.Q_CLUSTER["timeout"])

    def test_retry_exceeds_the_recommendation_task_timeout_override(self):
        self.assertGreater(
            settings.Q_CLUSTER["retry"],
            settings.FINDUS_VORGANG_RECOMMENDATION_TASK_TIMEOUT_SECONDS,
        )

    def test_poll_timeout_exceeds_the_recommendation_task_timeout(self):
        """Die Polling-Obergrenze im Panel darf nicht knapper sein als der
        Task-Timeout selbst, sonst gibt die UI einen noch regulaer
        laufenden Job faelschlich als haengengeblieben aus.
        """
        self.assertGreater(
            settings.FINDUS_VORGANG_RECOMMENDATION_POLL_TIMEOUT_SECONDS,
            settings.FINDUS_VORGANG_RECOMMENDATION_TASK_TIMEOUT_SECONDS,
        )

    def test_retry_exceeds_the_long_summary_task_timeout_overrides(self):
        """Dieselbe Invariante wie oben, fuer die ausfuehrliche
        Zusammenfassung (#1135) -- Dokument- *und* Vorgang-Ebene haben je
        einen eigenen Task-Timeout-Override.
        """
        self.assertGreater(
            settings.Q_CLUSTER["retry"], settings.FINDUS_LONG_SUMMARY_TASK_TIMEOUT_SECONDS
        )
        self.assertGreater(
            settings.Q_CLUSTER["retry"],
            settings.FINDUS_VORGANG_LONG_SUMMARY_TASK_TIMEOUT_SECONDS,
        )

    def test_poll_timeout_exceeds_the_long_summary_task_timeout(self):
        self.assertGreater(
            settings.FINDUS_LONG_SUMMARY_POLL_TIMEOUT_SECONDS,
            settings.FINDUS_LONG_SUMMARY_TASK_TIMEOUT_SECONDS,
        )
        self.assertGreater(
            settings.FINDUS_VORGANG_LONG_SUMMARY_POLL_TIMEOUT_SECONDS,
            settings.FINDUS_VORGANG_LONG_SUMMARY_TASK_TIMEOUT_SECONDS,
        )


class ActionStatusToggleTargetsExistTests(TestCase):
    """Der Erledigt-Schalter (#1137) crashte in den Übersichten, weil sein

    `hx-target`/`hx-sync` auf Elemente zeigte, die im gerenderten Markup gar
    nicht existierten -- HTMX bricht dann schon vor dem Request mit einem
    `TypeError` ab, nicht erst am Server (#1139). Geprueft wird das
    gerenderte Markup aller vier Ansichten: zu jedem Schalter mit
    `hx-target="#…"` muss im selben Markup ein Element mit genau dieser
    `id` existieren, und keine dieser IDs darf auf einen leeren Wert enden
    -- das waere das Symptom einer falsch benannten Kontextvariable im
    gemeinsamen Partial (`_action_status_toggle.html`).
    """

    # Nur die Ziele des Erledigt-Schalters (`document-status-…`) -- die
    # Seite hat weitere `hx-target`-Attribute (Filterformular, Upload,
    # Pagination), die mit diesem Ticket nichts zu tun haben.
    _TARGET_RE = re.compile(r'hx-target="#(document-status-[^"]*)"')

    def setUp(self):
        self.dept = Department.objects.create(name="Dept")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept)

        self.doc = Document.objects.create(
            title="Rechnung Acme", visibility=Document.Visibility.DEPARTMENT
        )
        self.doc.departments.add(self.dept)

        # Ein Dokument mit zwei Wiedervorlagen erscheint in dieser Ansicht
        # zweimal (#1129) -- `document.pk` allein waere dort keine
        # eindeutige Kennung fuer den Schalter.
        today = timezone.localdate()
        DocumentComment.objects.create(
            document=self.doc, body="Erster Termin", follow_up_date=today
        )
        DocumentComment.objects.create(
            document=self.doc, body="Zweiter Termin", follow_up_date=today
        )

        self.client.force_login(self.user)

    def _assert_targets_resolve(self, html):
        targets = self._TARGET_RE.findall(html)
        self.assertTrue(targets, "Kein hx-target auf dem Erledigt-Schalter gefunden.")
        for target_id in targets:
            with self.subTest(target=target_id):
                self.assertTrue(
                    target_id and not target_id.endswith("-"),
                    f'hx-target="#{target_id}" ist leer oder endet ohne Kennung -- '
                    "der Erledigt-Schalter zeigt vermutlich auf eine nicht "
                    "gesetzte Kontextvariable.",
                )
                self.assertIn(
                    f'id="{target_id}"',
                    html,
                    f'Kein Element mit id="{target_id}" im Markup -- der '
                    "hx-target des Erledigt-Schalters laeuft ins Leere.",
                )

    def test_grid_view_targets_exist(self):
        response = self.client.get(reverse("documents:home"), {"view": "grid"})
        self._assert_targets_resolve(response.content.decode())

    def test_table_view_targets_exist(self):
        response = self.client.get(reverse("documents:home"), {"view": ""})
        self._assert_targets_resolve(response.content.decode())

    def test_timeline_view_targets_exist(self):
        response = self.client.get(reverse("documents:home"), {"view": "timeline"})
        self._assert_targets_resolve(response.content.decode())

    def test_wiedervorlagen_view_targets_are_unique_per_entry(self):
        response = self.client.get(reverse("documents:home"), {"view": "wiedervorlagen"})
        html = response.content.decode()
        self._assert_targets_resolve(html)

        targets = self._TARGET_RE.findall(html)
        self.assertEqual(
            len(targets),
            2,
            "Erwartet: zwei Erledigt-Schalter fuer die zwei Wiedervorlagen "
            "desselben Dokuments.",
        )
        self.assertEqual(
            len(set(targets)),
            2,
            "Zwei Wiedervorlagen desselben Dokuments teilen sich denselben "
            "hx-target -- ein Klick traefe damit den falschen Eintrag.",
        )


def _dummy_file(data: bytes) -> ContentFile:
    return ContentFile(data)
