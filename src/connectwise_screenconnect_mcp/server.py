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
"""FastMCP factory and the per-request credential middleware."""

import contextvars
import uuid
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ._arguments import EnvelopeFastMCP
from .api_client import ScreenConnectClient, ScreenConnectCredentials
from .config import Settings

HEADER_INSTANCE_URL = "X-ScreenConnect-Instance-Url"
HEADER_AUTH_SECRET = "X-ScreenConnect-Auth-Secret"
HEADER_ORIGIN = "X-ScreenConnect-Origin"
REQUIRED_HEADERS = [HEADER_INSTANCE_URL, HEADER_AUTH_SECRET]

# Per-request isolation: the middleware sets these for the duration of one
# HTTP request and resets them in `finally`. Nothing credential-related is
# ever stored at module level (AC5).
_credentials_var: contextvars.ContextVar[ScreenConnectCredentials | None] = contextvars.ContextVar(
    "screenconnect_credentials", default=None
)
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("screenconnect_request_id", default="-")

INSTRUCTIONS = (
    "ConnectWise ScreenConnect is a remote support/access product. This connector covers its "
    "documented RESTful API Manager extension.\n"
    "- screenconnect_get_live_data -> the live Access-session list for a session group "
    "(the same session list the ScreenConnect host client shows), via GetSessionsByFilter.\n"
    "- screenconnect_invoke_command -> run a PowerShell command on one session's remote "
    "machine and return its stdout (queues via SendCommandToSession, polls "
    "GetSessionDetailsBySessionID for the output). Not idempotent: a retry re-runs the command.\n"
    "Every instance needs the RESTful API Manager extension installed and configured "
    "(RESTfulAuthenticationSecret set) -- a missing/unreachable extension returns a "
    "structured error distinct from a rejected secret."
)


def get_client_from_context() -> ScreenConnectClient | None:
    """The client for the current request, or None outside a request."""
    credentials = _credentials_var.get()
    if credentials is None:
        return None
    return ScreenConnectClient(credentials)


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


class GatewayCredentialsMiddleware:
    """Reads the X-ScreenConnect-* headers on /mcp requests; 401 when a required one is missing."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        instance_url = _clean(request.headers.get(HEADER_INSTANCE_URL))
        auth_secret = _clean(request.headers.get(HEADER_AUTH_SECRET))
        origin = _clean(request.headers.get(HEADER_ORIGIN))

        values = {HEADER_INSTANCE_URL: instance_url, HEADER_AUTH_SECRET: auth_secret}
        missing = [h for h in REQUIRED_HEADERS if not values[h]]
        if missing:
            await JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": f"Missing header(s): {', '.join(missing)}",
                    "required_headers": REQUIRED_HEADERS,
                },
                status_code=401,
            )(scope, receive, send)
            return

        request_id = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex[:12]
        creds = ScreenConnectCredentials(instance_url=instance_url, auth_secret=auth_secret, origin=origin)
        creds_token = _credentials_var.set(creds)
        rid_token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials_var.reset(creds_token)
            request_id_var.reset(rid_token)


def create_mcp_server(settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server and register every tool."""
    mcp = EnvelopeFastMCP(
        name="connectwise-screenconnect-mcp",
        instructions=INSTRUCTIONS,
        # The service sits behind the gateway, so Host is never localhost;
        # DNS-rebinding protection would reject those requests with 421.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
    )

    client_factory: Callable[[], ScreenConnectClient | None] = get_client_from_context

    from .tools import commands, sessions

    sessions.register(mcp, client_factory)
    commands.register(mcp, client_factory)

    return mcp
