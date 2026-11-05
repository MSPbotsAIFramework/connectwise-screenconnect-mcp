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
"""Async client for ConnectWise ScreenConnect's RESTful API Manager extension.

Covers ``GetSessionsByFilter`` (PRD-21570, read-only session list) and
``GetSessionBySessionID`` / ``SendCommandToSession`` / ``GetSessionDetailsBySessionID``
(PRD-21572, the queue-then-poll remote-command tool).
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any

import httpx

from ._json import error_envelope
from .config import EXTENSION_PATH

logger = logging.getLogger("connectwise_screenconnect_mcp.api")

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_MAX_RETRIES = 2
_MAX_BACKOFF_SECONDS = 20.0
# Whole-call budget including retries; the gateway times a tool call out at 120s.
_CALL_BUDGET_SECONDS = 100.0

# One connection pool for the process. It never holds credentials: the
# CTRLAuthHeader is built per request from the caller's own secret, so
# sharing the pool across tenants is safe (AC5).
_shared_http_client: httpx.AsyncClient | None = None


class _RejectAllCookies(DefaultCookiePolicy):
    """Never store or send a cookie: the HTTP client is shared by every tenant."""

    def set_ok(self, cookie, request) -> bool:  # noqa: ANN001 - stdlib signature
        return False

    def return_ok(self, cookie, request) -> bool:  # noqa: ANN001 - stdlib signature
        return False


def _no_cookies() -> CookieJar:
    # A plain CookieJar: httpx uses it as is (an httpx.Cookies would be copied into a default jar).
    return CookieJar(policy=_RejectAllCookies())


def _get_shared_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None:
        _shared_http_client = httpx.AsyncClient(cookies=_no_cookies(), timeout=_TIMEOUT)
    return _shared_http_client


# status_code -> (error code, retryable). 0 means no HTTP response at all
# (connection error or timeout). The extension itself returns 401/403 when
# CTRLAuthHeader does not match RESTfulAuthenticationSecret, and 404 when the
# extension is not installed/enabled on the instance (AC4).
#
# 408 is not part of the upstream vendor's own status codes: screenconnect_invoke_command
# (PRD-21572) raises ScreenConnectError(408, ...) itself, locally, when polling
# GetSessionDetailsBySessionID never observes a correlated RanCommand event before the
# caller's `timeout` elapses (AC5). Reusing this same status->code table keeps "timeout"
# a structured, distinct error code (ticket's own Error mapping table requires it to be
# distinct from a failure) without adding a second error-envelope code path.
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    0: ("upstream_error", True),
    400: ("invalid_argument", False),
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    408: ("timeout", False),
    429: ("rate_limited", True),
}


def classify_status(status_code: int) -> tuple[str, bool]:
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return "upstream_error", True
    if status_code >= 400:
        return "invalid_argument", False
    return "upstream_error", True


_AUTH_MESSAGES = {401: "unauthorized", 403: "unauthorized"}


class ScreenConnectError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"ScreenConnect API error {status_code}: {message}")

    def to_envelope(self) -> str:
        code, retryable = classify_status(self.status_code)
        return error_envelope(code, self.message, retryable)


@dataclass(frozen=True)
class ScreenConnectCredentials:
    instance_url: str
    auth_secret: str
    origin: str | None = None

    def __repr__(self) -> str:  # never leak the secret into logs or tracebacks
        return f"ScreenConnectCredentials(instance_url={self.instance_url!r}, auth_secret=<redacted>, origin={self.origin!r})"


def build_session_filter(session_type: str, session_group: str) -> str:
    """Build a ScreenConnect SessionFilter expression for a type + group pair.

    [Decision made by DEV01, PRD-21570] GetSessionsByFilter's exact grammar is
    not published on ConnectWise's public reference page (confirmed again at
    dev time: docs.connectwise.com/ScreenConnect_Documentation/Developers/
    RESTful_API_Manager lists the method signature only, no filter-string
    example). ScreenConnect's session objects expose ``SessionType`` and
    ``GroupPath`` (the same properties used by the product's own session
    search/role-restriction filters), so this builds the conjunctive form
    ``SessionType = '<type>' and GroupPath = '<group>'``. Single quotes in the
    group name are escaped by doubling, the convention ScreenConnect's filter
    language shares with SQL-style string literals.
    Not verified against a live instance (no vendor test account, see
    mcp-track.md §2.5 / ac.json TC7) -- if this grammar turns out not to match
    cleanly, the documented fallback is ``GetSessionsByName`` scoped by name
    prefix (ticket's Open Questions); only this function and its tests would
    need to change, not the tool's input/output contract.
    """
    escaped_group = session_group.replace("'", "''")
    return f"SessionType = '{session_type}' and GroupPath = '{escaped_group}'"


def map_session(raw: dict[str, Any]) -> dict[str, Any]:
    """Map one upstream session object to the tool's output shape (AC1).

    [Decision made by DEV01, PRD-21570] The public reference page documents
    GetSessionsByFilter's return category as "List of sessions" but does not
    show the JSON field names. ScreenConnect's session objects are known to
    use PascalCase properties (SessionID, Name, GroupPath, IsConnected,
    GuestInfo); this is the same unverified-pending-a-live-call gap as the
    filter grammar above (ac.json TC7). Falls back to ``None`` for any field
    not present under its expected key, rather than raising, so an unexpected
    shape degrades instead of breaking the whole call.
    """
    return {
        "session_id": raw.get("SessionID", raw.get("SessionId")),
        "name": raw.get("Name"),
        "group_path": raw.get("GroupPath"),
        "is_connected": raw.get("IsConnected"),
        "guest_info": raw.get("GuestInfo"),
    }


def extract_ran_command_output(session: dict[str, Any], after_epoch: float) -> str | None:
    """Find the earliest ``RanCommand`` event after ``after_epoch`` and return its output text.

    [Decision made by DEV01, PRD-21572] Neither the exact JSON field(s) that carry a RanCommand
    event's output text, nor whether GetSessionDetailsBySessionID returns full event history or
    only the latest event, are shown on ConnectWise's public reference page (ticket's own Open
    questions). This assumes PascalCase fields consistent with every other field name already
    confirmed in this repo (SessionID, Name, GroupPath, ...): an ``Events`` list of objects, each
    with ``EventType`` (string, e.g. ``"RanCommand"``, ``"QueuedCommand"``) and ``Time``
    (ISO-8601, UTC) and ``Data`` (the event payload text -- the command's stdout for a
    RanCommand event). Correlation picks the earliest RanCommand event whose ``Time`` is at or
    after ``after_epoch`` (the wall-clock time this invocation's SendCommandToSession call was
    sent), per the Open-questions assumption. Not verified against a live instance (ac.json TC9).
    If the real shape differs, only this function (and the call site in
    ``tools/commands.py::invoke_command``) need to change -- the tool's AC1-AC7 contract does not.
    Returns ``None`` (not an exception) when no matching event is found yet, so the caller can
    keep polling until `timeout`.
    """
    events = session.get("Events")
    if not isinstance(events, list):
        return None
    earliest: tuple[float, str] | None = None
    for event in events:
        if not isinstance(event, dict) or event.get("EventType") != "RanCommand":
            continue
        occurred_at = _parse_event_time(event.get("Time"))
        if occurred_at is None or occurred_at < after_epoch:
            continue
        data = event.get("Data")
        if data is None:
            continue
        if earliest is None or occurred_at < earliest[0]:
            earliest = (occurred_at, str(data))
    return earliest[1] if earliest is not None else None


def _parse_event_time(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


class ScreenConnectClient:
    """Thin wrapper over the RESTful API Manager extension for one caller's credentials."""

    def __init__(self, credentials: ScreenConnectCredentials, http_client: httpx.AsyncClient | None = None):
        self._credentials = credentials
        self._http = http_client
        self.instance_url = credentials.instance_url.rstrip("/")
        self.base_url = f"{self.instance_url}/{EXTENSION_PATH}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "CTRLAuthHeader": self._credentials.auth_secret}
        if self._credentials.origin:
            headers["Origin"] = self._credentials.origin
        return headers

    async def get_sessions_by_filter(self, session_filter: str) -> list[dict[str, Any]]:
        payload = await self._call("GetSessionsByFilter", [session_filter])
        return payload if isinstance(payload, list) else []

    async def get_session_by_id(self, session_id: str) -> dict[str, Any] | None:
        """Resolve one session by id, or ``None`` if the instance has no such session.

        [Decision made by DEV01, PRD-21572] GetSessionBySessionID's "not found" response shape
        is not documented on ConnectWise's public reference page (same gap class as
        ``build_session_filter``/``map_session`` above, PRD-21570). This assumes it mirrors
        ``GetSessionsByFilter``'s empty-result convention -- HTTP 200 with a null/empty JSON
        body for an id that does not exist, not an upstream 4xx. Not verified against a live
        instance (ac.json TC9 covers the sibling live-call gap; this call shares the same
        unverified-contract risk). If the real instance instead answers with a 404, `_call`
        already classifies that generically as "extension not installed" (AC4's mapping), which
        would misreport a plain not-found as an extension problem on the very first live run --
        only this method would need a one-line fix, not the tool's AC1-AC7 contract.
        """
        payload = await self._call("GetSessionBySessionID", [session_id])
        if not isinstance(payload, dict) or not payload:
            return None
        return payload

    async def send_command_to_session(self, session_id: str, script: str) -> None:
        """Queue a PowerShell command on a session (PRD-21572). Not idempotent, not retried.

        Unlike the read calls above, retrying this on a transient 429/5xx/network failure could
        run the command a second time on the remote machine (Tech design, Non-functional
        "Idempotency"). ``allow_retry=False`` disables ``_call``'s generic retry loop for this
        one call; the 401/403/404 error mapping (AC4) still applies unchanged.
        """
        await self._call("SendCommandToSession", [session_id, script], allow_retry=False)

    async def get_session_details(self, session_id: str) -> dict[str, Any]:
        """One poll of a session's current detail/event payload (PRD-21572). Read-only, retried as usual."""
        payload = await self._call("GetSessionDetailsBySessionID", [session_id])
        return payload if isinstance(payload, dict) else {}

    async def _call(self, method_name: str, params: list[Any], *, allow_retry: bool = True) -> Any:
        http = self._http or _get_shared_http_client()
        url = f"{self.base_url}/{method_name}"
        headers = self._headers()
        started = time.monotonic()
        attempt = 0
        while True:
            t0 = time.monotonic()
            resp: httpx.Response | None = None
            error: ScreenConnectError | None = None
            try:
                resp = await http.post(url, json=params, headers=headers)
                status = resp.status_code
            except httpx.TimeoutException:
                status, error = 0, ScreenConnectError(0, f"Timed out calling ScreenConnect instance ({method_name})")
            except httpx.RequestError as e:
                status, error = 0, ScreenConnectError(
                    0, f"ScreenConnect instance unreachable ({method_name}): {type(e).__name__}"
                )

            logger.info(
                "screenconnect_api method=%s status=%s ms=%d attempt=%d host=%s",
                method_name, status, (time.monotonic() - t0) * 1000, attempt, self.instance_url,
            )

            # Read calls (GetSessionsByFilter, GetSessionBySessionID, GetSessionDetailsBySessionID)
            # are idempotent, so retrying on 429/5xx/network failure cannot double-apply anything.
            # `allow_retry=False` (SendCommandToSession, PRD-21572) turns this off: that call is
            # not idempotent, and a retried command would run on the remote machine a second time.
            retryable = allow_retry and (status == 429 or status == 0 or status >= 500)
            if retryable and attempt < _MAX_RETRIES:
                delay = _retry_delay(resp, attempt)
                if time.monotonic() - started + delay < _CALL_BUDGET_SECONDS:
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
            if error is not None:
                raise error
            assert resp is not None
            if resp.status_code in _AUTH_MESSAGES:
                raise ScreenConnectError(
                    resp.status_code, "ScreenConnect rejected the auth secret for this instance"
                )
            if resp.status_code == 404:
                raise ScreenConnectError(
                    404, "RESTful API Manager extension not installed or not enabled on this ScreenConnect instance"
                )
            if resp.status_code >= 400:
                raise ScreenConnectError(resp.status_code, _error_message(resp, method_name))
            if not resp.content or not resp.content.strip():
                return None
            try:
                return resp.json()
            except ValueError:
                return {"raw_response": resp.text[:2000]}


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), _MAX_BACKOFF_SECONDS))
            except ValueError:
                pass
    return min(float(2**attempt), _MAX_BACKOFF_SECONDS)


def _error_message(resp: httpx.Response, method_name: str) -> str:
    fallback = f"ScreenConnect returned HTTP {resp.status_code} for {method_name}"
    try:
        payload = resp.json()
    except ValueError:
        text = resp.text.strip()
        if text.lower().startswith("<"):
            return fallback  # an HTML error page, not useful to the agent
        return f"{fallback}: {text[:300]}" if text else fallback
    if isinstance(payload, dict):
        message = payload.get("Message") or payload.get("message") or payload.get("error")
        if message:
            return f"{fallback}: {str(message)[:500]}"
    return fallback
