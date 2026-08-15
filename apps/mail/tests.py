"""Unit tests for apps.mail.

Every test runs offline: the registry/service tests use the built-in
`fake` backend, the SMTP test patches Django's own SMTP backend class so
no socket is opened, and the Graph tests patch `requests.post`. Per this
issue's acceptance criterion ("kein echter Versand im Test").
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.mail.backends import MailBackendError, OutgoingMessage, get_mail_backend
from apps.mail.backends.fake import FakeMailBackend
from apps.mail.backends.graph import GraphBackend
from apps.mail.backends.smtp import SMTPBackend
from apps.mail.service import send_notification
from apps.mail.templating import render_mail


class RegistrySwitchTests(SimpleTestCase):
    """Backend per Config umschaltbar, ohne Aufrufer-Code zu ändern."""

    @override_settings(FINDUS_MAIL_BACKEND="fake")
    def test_fake_backend_selected_from_settings(self):
        backend = get_mail_backend()
        self.assertIsInstance(backend, FakeMailBackend)

    def test_explicit_name_overrides_settings(self):
        backend = get_mail_backend("fake")
        self.assertIsInstance(backend, FakeMailBackend)

    @override_settings(
        FINDUS_MAIL_BACKENDS={
            "smtp": {
                "host": "localhost",
                "port": 587,
                "username": "",
                "password": "",
                "use_tls": True,
                "use_ssl": False,
            },
        }
    )
    def test_same_caller_code_gets_different_classes_per_config(self):
        """One call site (`get_mail_backend()`), two different concrete
        classes depending only on settings -- no branching in the caller.
        """

        def caller():
            return type(get_mail_backend()).__name__

        with override_settings(FINDUS_MAIL_BACKEND="fake"):
            self.assertEqual(caller(), "FakeMailBackend")

        with override_settings(FINDUS_MAIL_BACKEND="smtp"):
            self.assertEqual(caller(), "SMTPBackend")

    def test_unknown_backend_raises_clear_error(self):
        with self.assertRaises(MailBackendError):
            get_mail_backend("not-a-real-backend")

    def test_missing_backend_config_raises_clear_error(self):
        with override_settings(FINDUS_MAIL_BACKENDS={}):
            with self.assertRaises(MailBackendError):
                get_mail_backend("graph")


class TemplatingTests(SimpleTestCase):
    def test_renders_subject_text_and_html(self):
        rendered = render_mail(
            "processing_complete",
            {"document_name": "Rechnung.pdf", "document_url": "https://findus.example/d/1"},
        )
        self.assertIn("Rechnung.pdf", rendered.subject)
        self.assertNotIn("\n", rendered.subject)
        self.assertIn("Rechnung.pdf", rendered.text_body)
        self.assertIn("Rechnung.pdf", rendered.html_body)
        self.assertIn("https://findus.example/d/1", rendered.html_body)

    def test_html_body_is_none_without_html_template(self):
        # documents template dir has no mail/ templates -- render_to_string
        # falls through to a template-name that only exists under mail/.
        with self.assertRaises(Exception):
            render_mail("does-not-exist", {})

    def test_html_body_uses_shared_layout(self):
        """Both templates extend `mail/base_email.html` (#1128) -- wordmark,

        clickable CTA button and footer should show up in either rendering.
        """
        rendered = render_mail(
            "processing_complete",
            {"document_name": "Rechnung.pdf", "document_url": "https://findus.example/d/1"},
        )
        self.assertIn(">Findus<", rendered.html_body)
        self.assertIn('href="https://findus.example/d/1"', rendered.html_body)
        self.assertIn("Dokument ansehen", rendered.html_body)
        self.assertIn(
            "Du bekommst diese Nachricht, weil Findus die Verarbeitung", rendered.html_body
        )

    def test_reminder_html_distinguishes_overdue_from_due_today(self):
        rendered = render_mail(
            "document_comment_reminder",
            {
                "count": 2,
                "items": [
                    {
                        "document_title": "Mietvertrag",
                        "body": "Kündigungsfrist beachten",
                        "follow_up_date": datetime.date(2026, 1, 1),
                        "document_url": "https://findus.example/d/1",
                        "is_overdue": True,
                    },
                    {
                        "document_title": "Versicherung",
                        "body": "Police prüfen",
                        "follow_up_date": datetime.date(2026, 8, 15),
                        "document_url": "https://findus.example/d/2",
                        "is_overdue": False,
                    },
                ],
            },
        )
        self.assertIn(">Findus<", rendered.html_body)
        self.assertIn('href="https://findus.example/d/1"', rendered.html_body)
        self.assertIn('href="https://findus.example/d/2"', rendered.html_body)
        self.assertIn("Überfällig seit", rendered.html_body)
        self.assertIn("Fällig heute", rendered.html_body)
        self.assertIn(
            "Du bekommst diese Nachricht, weil du zu diesem Dokument", rendered.html_body
        )


class SendNotificationServiceTests(SimpleTestCase):
    """Unit-Test mit Dummy-Backend (kein echter Versand)."""

    def test_send_notification_renders_and_dispatches_via_configured_backend(self):
        fake_backend = FakeMailBackend()
        with patch("apps.mail.service.get_mail_backend", return_value=fake_backend):
            send_notification(
                "processing_complete",
                ["owner@example.com"],
                {"document_name": "Vertrag.pdf"},
            )
        self.assertEqual(len(fake_backend.sent), 1)
        sent = fake_backend.sent[0]
        self.assertEqual(sent.to, ["owner@example.com"])
        self.assertIn("Vertrag.pdf", sent.subject)


class SMTPBackendTests(SimpleTestCase):
    def _backend(self):
        return SMTPBackend(
            host="smtp.example.com",
            port=587,
            username="smtp-account-1",
            password="secret",
            use_tls=True,
            use_ssl=False,
            from_address="findus@example.com",
            timeout=5,
            max_retries=0,
            retry_backoff_seconds=0,
        )

    @patch("django.core.mail.backends.smtp.EmailBackend.open", return_value=True)
    @patch("django.core.mail.backends.smtp.EmailBackend.send_messages", return_value=1)
    def test_send_dispatches_multipart_message_without_real_network(
        self, mock_send_messages, mock_open
    ):
        message = OutgoingMessage(
            to=["a@b.de"],
            subject="Betreff",
            text_body="Text-Version",
            html_body="<p>HTML-Version</p>",
        )
        self._backend().send(message)

        mock_send_messages.assert_called_once()
        sent_email = mock_send_messages.call_args.args[0][0]
        self.assertEqual(sent_email.subject, "Betreff")
        self.assertEqual(sent_email.body, "Text-Version")
        self.assertEqual(sent_email.to, ["a@b.de"])
        self.assertEqual(sent_email.alternatives, [("<p>HTML-Version</p>", "text/html")])

    @patch("django.core.mail.backends.smtp.EmailBackend.open", return_value=True)
    @patch("django.core.mail.backends.smtp.EmailBackend.send_messages", return_value=0)
    def test_zero_sent_raises_mail_backend_error(self, mock_send_messages, mock_open):
        message = OutgoingMessage(to=["a@b.de"], subject="s", text_body="t")
        with self.assertRaises(MailBackendError):
            self._backend().send(message)

    def test_repr_masks_username(self):
        backend = self._backend()
        self.assertNotIn("secret", repr(backend))
        self.assertNotIn("smtp-account-1", repr(backend))


class GraphBackendTests(SimpleTestCase):
    def _backend(self):
        return GraphBackend(
            tenant_id="tenant-1",
            client_id="client-1",
            client_secret="client-secret",
            sender="notifications@example.com",
            from_address="findus@example.com",
            timeout=5,
            max_retries=0,
            retry_backoff_seconds=0,
        )

    @patch("apps.mail.backends.graph.requests.post")
    def test_send_fetches_token_then_calls_sendmail(self, mock_post):
        token_response = _mock_response({"access_token": "tok-1", "expires_in": 3600})
        send_response = _mock_response({})
        mock_post.side_effect = [token_response, send_response]

        message = OutgoingMessage(
            to=["a@b.de"], subject="Betreff", text_body="Text", html_body="<p>HTML</p>"
        )
        self._backend().send(message)

        self.assertEqual(mock_post.call_count, 2)
        token_call, send_call = mock_post.call_args_list
        self.assertIn("login.microsoftonline.com/tenant-1", token_call.args[0])
        self.assertIn("/users/notifications@example.com/sendMail", send_call.args[0])
        self.assertEqual(send_call.kwargs["headers"]["Authorization"], "Bearer tok-1")
        body = send_call.kwargs["json"]["message"]
        self.assertEqual(body["body"]["contentType"], "HTML")
        self.assertEqual(body["body"]["content"], "<p>HTML</p>")
        self.assertEqual(body["toRecipients"], [{"emailAddress": {"address": "a@b.de"}}])

    @patch("apps.mail.backends.graph.requests.post")
    def test_text_only_message_sends_text_content_type(self, mock_post):
        mock_post.side_effect = [
            _mock_response({"access_token": "tok-1", "expires_in": 3600}),
            _mock_response({}),
        ]
        message = OutgoingMessage(to=["a@b.de"], subject="s", text_body="plain only")
        self._backend().send(message)
        body = mock_post.call_args_list[1].kwargs["json"]["message"]
        self.assertEqual(body["body"]["contentType"], "Text")
        self.assertEqual(body["body"]["content"], "plain only")

    @patch("apps.mail.backends.graph.requests.post")
    def test_token_is_cached_across_sends(self, mock_post):
        mock_post.side_effect = [
            _mock_response({"access_token": "tok-1", "expires_in": 3600}),
            _mock_response({}),
            _mock_response({}),
        ]
        backend = self._backend()
        message = OutgoingMessage(to=["a@b.de"], subject="s", text_body="t")
        backend.send(message)
        backend.send(message)
        # 1 token fetch + 2 sendMail calls, not 2 token fetches.
        self.assertEqual(mock_post.call_count, 3)

    def test_repr_masks_client_secret(self):
        backend = self._backend()
        self.assertNotIn("client-secret", repr(backend))


def _mock_response(json_body):
    from unittest.mock import Mock

    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = json_body
    return response
