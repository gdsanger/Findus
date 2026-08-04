from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from apps.mcp.auth import TokenAuthMiddleware, get_mcp_user

User = get_user_model()


def _protected_app():
    async def endpoint(request):
        return PlainTextResponse("ok")

    return TokenAuthMiddleware(Starlette(routes=[Route("/sse", endpoint)]))


@override_settings(MCP_TOKEN="s3cr3t-token")
class TokenAuthMiddlewareTests(SimpleTestCase):
    """Covers the #1052 baseline: the SSE endpoint requires a token, passed

    in the query string because Claude Desktop can't send a custom
    `Authorization` header over SSE -- a bearer header is accepted too, but
    only as a fallback, never the only way in.
    """

    def setUp(self):
        self.client = TestClient(_protected_app())

    def test_missing_token_is_rejected(self):
        response = self.client.get("/sse")
        self.assertEqual(response.status_code, 401)

    def test_wrong_query_string_token_is_rejected(self):
        response = self.client.get("/sse", params={"token": "wrong"})
        self.assertEqual(response.status_code, 401)

    def test_valid_query_string_token_connects(self):
        response = self.client.get("/sse", params={"token": "s3cr3t-token"})
        self.assertEqual(response.status_code, 200)

    def test_valid_bearer_header_connects(self):
        response = self.client.get("/sse", headers={"Authorization": "Bearer s3cr3t-token"})
        self.assertEqual(response.status_code, 200)


@override_settings(MCP_TOKEN="")
class TokenAuthMiddlewareNoTokenConfiguredTests(SimpleTestCase):
    def test_empty_expected_token_rejects_everything(self):
        """An unset `MCP_TOKEN` must fail closed, not open -- a candidate

        can never equal an empty expected token, so an operator who forgot
        to set the env var gets a service that refuses all requests
        instead of one that silently accepts none.
        """
        client = TestClient(_protected_app())
        response = client.get("/sse", params={"token": ""})
        self.assertEqual(response.status_code, 401)


class GetMcpUserTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="mcp-runner", password="x")

    @override_settings(MCP_USER_USERNAME="mcp-runner")
    def test_returns_the_configured_user(self):
        self.assertEqual(get_mcp_user(), self.user)

    @override_settings(MCP_USER_USERNAME="")
    def test_raises_without_a_configured_username(self):
        with self.assertRaises(RuntimeError):
            get_mcp_user()
