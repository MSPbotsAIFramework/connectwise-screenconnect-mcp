# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Credential middleware: 401 on missing required headers, per-request isolation (TC3, TC6, TC7)."""

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from starlette.testclient import TestClient

from connectwise_screenconnect_mcp.__main__ import build_http_app
from connectwise_screenconnect_mcp.api_client import ScreenConnectClient, ScreenConnectCredentials
from connectwise_screenconnect_mcp.server import (
    GatewayCredentialsMiddleware,
    _credentials_var,
    create_mcp_server,
    get_client_from_context,
)
from connectwise_screenconnect_mcp.tools.commands import invoke_command

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
CREDS = {"X-ScreenConnect-Instance-Url": "https://control.example.com", "X-ScreenConnect-Auth-Secret": "secret-123"}


def _client() -> TestClient:
    return TestClient(build_http_app(create_mcp_server()))


def test_health_is_local():
    with _client() as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "headers,missing",
    [
        ({}, ["X-ScreenConnect-Instance-Url", "X-ScreenConnect-Auth-Secret"]),
        ({"X-ScreenConnect-Instance-Url": "https://control.example.com"}, ["X-ScreenConnect-Auth-Secret"]),
        ({"X-ScreenConnect-Auth-Secret": "secret-123"}, ["X-ScreenConnect-Instance-Url"]),
        ({"X-ScreenConnect-Instance-Url": " ", "X-ScreenConnect-Auth-Secret": "secret-123"}, ["X-ScreenConnect-Instance-Url"]),
    ],
)
def test_missing_required_header_returns_401(headers, missing):
    """TC3 (AC3): a missing required header gets 401 naming exactly that header, no upstream call."""
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | headers)
    assert resp.status_code == 401
    assert resp.json()["required_headers"] == ["X-ScreenConnect-Instance-Url", "X-ScreenConnect-Auth-Secret"]
    for name in missing:
        assert name in resp.json()["message"]


def test_missing_auth_secret_header_returns_401():
    """TC3 (AC3): missing X-ScreenConnect-Auth-Secret specifically -- named exactly, no upstream call."""
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | {"X-ScreenConnect-Instance-Url": "https://control.example.com"})
    assert resp.status_code == 401
    assert resp.json()["message"] == "Missing header(s): X-ScreenConnect-Auth-Secret"


def test_tools_list_over_http():
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS)
    assert resp.status_code == 200
    assert "screenconnect_get_live_data" in resp.text
    assert "screenconnect_invoke_command" in resp.text


def _run_middleware(headers: list[tuple[bytes, bytes]]) -> dict:
    seen: dict = {}

    async def app(scope, receive, send):
        seen["credentials"] = _credentials_var.get()
        seen["client"] = get_client_from_context()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    asyncio.run(GatewayCredentialsMiddleware(app)({"type": "http", "path": "/mcp", "headers": headers}, receive, send))
    return seen


def test_credentials_reach_request_context_and_are_reset():
    seen = _run_middleware(
        [(b"x-screenconnect-instance-url", b" https://control.example.com/ "), (b"x-screenconnect-auth-secret", b" secret-abc ")]
    )
    client = seen["client"]
    assert client.instance_url == "https://control.example.com"
    assert client._headers()["CTRLAuthHeader"] == "secret-abc"
    assert "Origin" not in client._headers()
    assert _credentials_var.get() is None


def test_origin_header_is_optional_and_passed_through():
    seen = _run_middleware(
        [
            (b"x-screenconnect-instance-url", b"https://control.example.com"),
            (b"x-screenconnect-auth-secret", b"secret-abc"),
            (b"x-screenconnect-origin", b"https://documentation.screenconnect.com"),
        ]
    )
    assert seen["client"]._headers()["Origin"] == "https://documentation.screenconnect.com"


def test_concurrent_requests_do_not_share_credentials():
    """TC6 (AC5): two tenants in flight at once never see each other's credentials."""
    observed: list[tuple[str, str]] = []

    async def app(scope, receive, send):
        before = _credentials_var.get().auth_secret
        await asyncio.sleep(0.01)
        observed.append((before, _credentials_var.get().auth_secret))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    middleware = GatewayCredentialsMiddleware(app)

    def scope(secret: bytes) -> dict:
        return {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"x-screenconnect-instance-url", b"https://control.example.com"), (b"x-screenconnect-auth-secret", secret)],
        }

    async def both():
        await asyncio.gather(middleware(scope(b"tenant-a"), receive, send), middleware(scope(b"tenant-b"), receive, send))

    asyncio.run(both())
    assert sorted(observed) == [("tenant-a", "tenant-a"), ("tenant-b", "tenant-b")]


def test_credentials_repr_hides_secret():
    seen = _run_middleware(
        [(b"x-screenconnect-instance-url", b"https://control.example.com"), (b"x-screenconnect-auth-secret", b"very-secret-value")]
    )
    assert "very-secret-value" not in repr(seen["credentials"])


def test_concurrent_requests_do_not_share_credentials_or_session():
    """TC7 (AC6): two tenants calling screenconnect_invoke_command concurrently never cross session/secret.

    Exercises the full validate -> queue -> poll flow (not just the header middleware above) with
    one shared HTTP connection pool, the same production shape (api_client.py's module-level
    pool): each in-flight call's upstream requests must carry only its own credentials and its
    own session id.
    """

    seen: list[tuple[str, str, str]] = []  # (secret, session_id, method)

    def handler(request: httpx.Request) -> httpx.Response:
        secret = request.headers["CTRLAuthHeader"]
        method = request.url.path.rsplit("/", 1)[-1]
        session_id = json.loads(request.content)[0]
        seen.append((secret, session_id, method))
        if method == "GetSessionBySessionID":
            return httpx.Response(200, json={"SessionID": session_id, "GroupPath": "All Machines"})
        if method == "SendCommandToSession":
            return httpx.Response(200, json=None)
        if method == "GetSessionDetailsBySessionID":
            now = datetime.now(timezone.utc).isoformat()
            return httpx.Response(
                200, json={"Events": [{"EventType": "RanCommand", "Time": now, "Data": f"out-{session_id}"}]}
            )
        raise AssertionError(f"unexpected method {method}")

    shared_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def call(secret: str, session_id: str) -> str:
        creds = ScreenConnectCredentials(instance_url="https://control.example.com", auth_secret=secret)
        client = ScreenConnectClient(creds, http_client=shared_http)
        return await invoke_command(client, session_id, "All Machines", "Write-Output hi", timeout=2, poll_interval=0.01)

    async def both():
        return await asyncio.gather(call("tenant-a-secret", "session-a"), call("tenant-b-secret", "session-b"))

    results = asyncio.run(both())
    assert sorted(results) == ["out-session-a", "out-session-b"]
    for secret, session_id, _method in seen:
        expected_session = "session-a" if secret == "tenant-a-secret" else "session-b"
        assert session_id == expected_session, "a request used the other tenant's session id"
