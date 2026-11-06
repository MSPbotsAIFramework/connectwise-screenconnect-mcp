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
"""tools/list snapshot, error-envelope mapping, and the tools' own behavior (TC1, TC2, TC4-TC6, TC8)."""

import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
import pytest

from connectwise_screenconnect_mcp._json import MAX_CHARS, dump_json_capped
from connectwise_screenconnect_mcp.api_client import (
    ScreenConnectClient,
    ScreenConnectCredentials,
    ScreenConnectError,
    build_session_filter,
    extract_ran_command_output,
    map_session,
)
from connectwise_screenconnect_mcp.server import INSTRUCTIONS, create_mcp_server
from connectwise_screenconnect_mcp.tools.commands import invoke_command

EXPECTED_TOOLS: dict[str, set[str]] = {
    "screenconnect_get_live_data": {"session_type", "session_group"},
    "screenconnect_invoke_command": {"session_id", "session_group", "script"},
}
READ_ONLY_TOOLS = {"screenconnect_get_live_data"}
WRITE_TOOLS = {"screenconnect_invoke_command"}


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(create_mcp_server().list_tools())}


def test_tool_names_are_stable(tools):
    # Tool names are a public contract (SOP §13): changing this is a breaking change.
    assert set(tools) == set(EXPECTED_TOOLS)


def test_tool_count_within_budget(tools):
    # SOP §2: a single service should expose at most 20 tools.
    assert len(tools) <= 20


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_required_params(tools, name):
    assert set(tools[name].inputSchema.get("required", [])) == EXPECTED_TOOLS[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_description_budget(tools, name):
    description = (tools[name].description or "").strip()
    assert description, f"{name} has no description"
    assert len(description) <= 500
    assert len(description.splitlines()[0]) <= 100


@pytest.mark.parametrize("name", sorted(READ_ONLY_TOOLS))
def test_annotations_are_read_only(tools, name):
    annotations = tools[name].annotations
    assert annotations is not None
    assert annotations.readOnlyHint is True
    assert not annotations.destructiveHint


@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
def test_annotations_are_destructive_write(tools, name):
    # SOP §2.6/§2.7: a write tool that is not safe to retry must say so (Tech design,
    # Non-functional "Idempotency": retrying screenconnect_invoke_command re-runs the command).
    annotations = tools[name].annotations
    assert annotations is not None
    assert annotations.readOnlyHint is False
    assert annotations.destructiveHint is True
    assert annotations.idempotentHint is False


def test_instructions_budget():
    assert len(INSTRUCTIONS) <= 1500


def test_tool_names_prefixed(tools):
    assert all(name.startswith("screenconnect_") for name in tools)


def test_session_type_rejects_anything_but_access(tools):
    # v1 scope (Non-goals): only "Access" is accepted. A single-value Literal
    # renders as a JSON Schema "const", not "enum".
    assert tools["screenconnect_get_live_data"].inputSchema["properties"]["session_type"]["const"] == "Access"


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (400, "invalid_argument", False),
        (401, "unauthorized", False),
        (403, "unauthorized", False),
        (404, "not_found", False),
        (429, "rate_limited", True),
        (500, "upstream_error", True),
        (0, "upstream_error", True),
        (408, "timeout", False),
    ],
)
def test_status_to_code_mapping(status, code, retryable):
    envelope = json.loads(ScreenConnectError(status, "boom").to_envelope())
    assert envelope == {"error": {"code": code, "message": "boom", "retryable": retryable}}


def test_invalid_session_type_returns_invalid_argument():
    content, _ = asyncio.run(
        create_mcp_server().call_tool("screenconnect_get_live_data", {"session_type": "Support", "session_group": "x"})
    )
    assert json.loads(content[0].text)["error"]["code"] == "invalid_argument"


def test_tool_without_credentials_returns_not_configured():
    content, _ = asyncio.run(create_mcp_server().call_tool("screenconnect_get_live_data", {"session_type": "Access", "session_group": "All Machines"}))
    assert json.loads(content[0].text)["error"]["code"] == "not_configured"


def test_invoke_command_without_credentials_returns_not_configured():
    content, _ = asyncio.run(
        create_mcp_server().call_tool(
            "screenconnect_invoke_command", {"session_id": "s1", "session_group": "All Machines", "script": "Write-Output hi"}
        )
    )
    assert json.loads(content[0].text)["error"]["code"] == "not_configured"


def test_credentials_repr_redacts_secret():
    creds = ScreenConnectCredentials(instance_url="https://control.example.com", auth_secret="very-secret-value")
    assert "very-secret-value" not in repr(creds)
    assert "redacted" in repr(creds)


def test_dump_json_capped_truncates_large_list():
    rows = [{"id": i, **{f"f{j}": f"value-{j:04d}" for j in range(120)}} for i in range(25)]
    data = {"sessions": rows}
    text = dump_json_capped(data)
    assert len(text) <= MAX_CHARS


# --- build_session_filter (the one Open-questions assumption dev had to resolve, see api_client.py) ---


def test_build_session_filter_combines_type_and_group():
    assert build_session_filter("Access", "All Machines") == "SessionType = 'Access' and GroupPath = 'All Machines'"


def test_build_session_filter_escapes_single_quotes_in_group_name():
    assert build_session_filter("Access", "O'Brien's Laptops") == "SessionType = 'Access' and GroupPath = 'O''Brien''s Laptops'"


# --- map_session (the other unverified-pending-a-real-account assumption, see api_client.py) ---


def test_map_session_extracts_expected_fields():
    raw = {"SessionID": "abc-123", "Name": "DESKTOP-1", "GroupPath": "All Machines", "IsConnected": True, "GuestInfo": "jdoe"}
    assert map_session(raw) == {
        "session_id": "abc-123",
        "name": "DESKTOP-1",
        "group_path": "All Machines",
        "is_connected": True,
        "guest_info": "jdoe",
    }


def test_map_session_degrades_on_unexpected_shape():
    assert map_session({}) == {"session_id": None, "name": None, "group_path": None, "is_connected": None, "guest_info": None}


# --- screenconnect_get_live_data end to end against a mocked ScreenConnect instance ---

CREDS = ScreenConnectCredentials(instance_url="https://control.example.com", auth_secret="secret-123")


def _mock_client(handler) -> ScreenConnectClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ScreenConnectClient(CREDS, http_client=http)


def test_get_live_data_returns_session_list():
    """TC1 (AC1): a group with active sessions returns the mapped session list."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/GetSessionsByFilter")
        assert request.headers["CTRLAuthHeader"] == "secret-123"
        assert json.loads(request.content) == ["SessionType = 'Access' and GroupPath = 'All Machines'"]
        return httpx.Response(
            200,
            json=[{"SessionID": "s1", "Name": "DESKTOP-1", "GroupPath": "All Machines", "IsConnected": True, "GuestInfo": None}],
        )

    client = _mock_client(handler)
    sessions = asyncio.run(client.get_sessions_by_filter(build_session_filter("Access", "All Machines")))
    assert [map_session(s) for s in sessions] == [
        {"session_id": "s1", "name": "DESKTOP-1", "group_path": "All Machines", "is_connected": True, "guest_info": None}
    ]


def test_get_live_data_empty_group_returns_empty_list():
    """TC2 (AC2): zero active sessions returns an empty list, not an error."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = _mock_client(handler)
    sessions = asyncio.run(client.get_sessions_by_filter(build_session_filter("Access", "Empty Group")))
    assert sessions == []


def test_get_live_data_upstream_401_maps_to_auth_rejected():
    """TC4 (AC4): upstream auth rejection never echoes the secret."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    client = _mock_client(handler)
    with pytest.raises(ScreenConnectError) as exc_info:
        asyncio.run(client.get_sessions_by_filter("SessionType = 'Access'"))
    assert exc_info.value.status_code == 401
    assert "secret-123" not in exc_info.value.message
    envelope = json.loads(exc_info.value.to_envelope())
    assert envelope == {"error": {"code": "unauthorized", "message": "ScreenConnect rejected the auth secret for this instance", "retryable": False}}


def test_get_live_data_upstream_404_maps_to_extension_not_installed():
    """TC5 (AC4): extension-not-found is a distinct error from TC4's auth-rejected."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    client = _mock_client(handler)
    with pytest.raises(ScreenConnectError) as exc_info:
        asyncio.run(client.get_sessions_by_filter("SessionType = 'Access'"))
    assert exc_info.value.status_code == 404
    envelope = json.loads(exc_info.value.to_envelope())
    assert envelope["error"]["code"] == "not_found"
    assert envelope["error"]["message"] != "ScreenConnect rejected the auth secret for this instance"
    assert "extension" in envelope["error"]["message"].lower()


def test_get_live_data_via_run_tool_wraps_errors_as_envelope_not_exception():
    """The MCP tool layer (run_tool) never lets ScreenConnectError escape as an exception."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    from connectwise_screenconnect_mcp.tools._common import run_tool

    client = _mock_client(handler)

    async def action(c: ScreenConnectClient):
        return await c.get_sessions_by_filter("SessionType = 'Access'")

    result = asyncio.run(run_tool("screenconnect_get_live_data", lambda: client, action))
    assert json.loads(result)["error"]["code"] == "unauthorized"


# --- screenconnect_invoke_command end to end against a mocked ScreenConnect instance ---


def _method(request: httpx.Request) -> str:
    return request.url.path.rsplit("/", 1)[-1]


def test_invoke_command_returns_stdout_on_ran_command_event():
    """TC1 (AC1): queues the command, polls, and returns the RanCommand event's output text."""

    details_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        method = _method(request)
        if method == "GetSessionBySessionID":
            assert json.loads(request.content) == ["s1"]
            return httpx.Response(200, json={"SessionID": "s1", "GroupPath": "All Machines"})
        if method == "SendCommandToSession":
            assert json.loads(request.content) == ["s1", "Write-Output hi"]
            assert request.headers["CTRLAuthHeader"] == "secret-123"
            return httpx.Response(200, json=None)
        if method == "GetSessionDetailsBySessionID":
            details_calls["n"] += 1
            if details_calls["n"] == 1:
                return httpx.Response(200, json={"Events": []})
            now = datetime.now(timezone.utc).isoformat()
            return httpx.Response(200, json={"Events": [{"EventType": "RanCommand", "Time": now, "Data": "hi\n"}]})
        raise AssertionError(f"unexpected method {method}")

    client = _mock_client(handler)
    output = asyncio.run(invoke_command(client, "s1", "All Machines", "Write-Output hi", timeout=2, poll_interval=0.01))
    assert output == "hi\n"
    assert details_calls["n"] == 2  # exercised the polling loop, not just the first attempt


def test_invoke_command_group_mismatch_blocks_command():
    """TC2 (AC2): session exists but in a different group -- no command sent."""

    sent = {"called": False}

    def handler(request: httpx.Request) -> httpx.Response:
        method = _method(request)
        if method == "GetSessionBySessionID":
            return httpx.Response(200, json={"SessionID": "s1", "GroupPath": "Other Group"})
        if method == "SendCommandToSession":
            sent["called"] = True
            return httpx.Response(200, json=None)
        raise AssertionError(f"unexpected method {method}")

    client = _mock_client(handler)
    with pytest.raises(ScreenConnectError) as exc_info:
        asyncio.run(invoke_command(client, "s1", "All Machines", "Write-Output hi", timeout=2, poll_interval=0.01))
    assert sent["called"] is False
    envelope = json.loads(exc_info.value.to_envelope())
    assert envelope["error"]["code"] == "invalid_argument"
    assert "Other Group" in envelope["error"]["message"]
    assert "All Machines" in envelope["error"]["message"]


def test_invoke_command_unknown_session_returns_not_found():
    """TC8 (AC7): session_id that does not exist at all -- no command sent."""

    sent = {"called": False}

    def handler(request: httpx.Request) -> httpx.Response:
        method = _method(request)
        if method == "GetSessionBySessionID":
            return httpx.Response(200, json=None)
        if method == "SendCommandToSession":
            sent["called"] = True
            return httpx.Response(200, json=None)
        raise AssertionError(f"unexpected method {method}")

    client = _mock_client(handler)
    with pytest.raises(ScreenConnectError) as exc_info:
        asyncio.run(invoke_command(client, "missing", "All Machines", "Write-Output hi", timeout=2, poll_interval=0.01))
    assert sent["called"] is False
    assert exc_info.value.status_code == 404
    envelope = json.loads(exc_info.value.to_envelope())
    assert envelope["error"]["code"] == "not_found"


def test_invoke_command_upstream_401_maps_to_auth_rejected():
    """TC4 (AC4): the SendCommandToSession call itself can be rejected; secret never leaks."""

    def handler(request: httpx.Request) -> httpx.Response:
        method = _method(request)
        if method == "GetSessionBySessionID":
            return httpx.Response(200, json={"SessionID": "s1", "GroupPath": "All Machines"})
        if method == "SendCommandToSession":
            return httpx.Response(401, text="Unauthorized")
        raise AssertionError(f"unexpected method {method}")

    client = _mock_client(handler)
    with pytest.raises(ScreenConnectError) as exc_info:
        asyncio.run(invoke_command(client, "s1", "All Machines", "Write-Output hi", timeout=2, poll_interval=0.01))
    assert exc_info.value.status_code == 401
    assert "secret-123" not in exc_info.value.message
    envelope = json.loads(exc_info.value.to_envelope())
    assert envelope == {
        "error": {"code": "unauthorized", "message": "ScreenConnect rejected the auth secret for this instance", "retryable": False}
    }


def test_invoke_command_upstream_404_maps_to_extension_not_installed():
    """TC5 (AC4): extension-not-found is distinct from TC4's auth-rejected error."""

    def handler(request: httpx.Request) -> httpx.Response:
        method = _method(request)
        if method == "GetSessionBySessionID":
            return httpx.Response(200, json={"SessionID": "s1", "GroupPath": "All Machines"})
        if method == "SendCommandToSession":
            return httpx.Response(404, text="Not Found")
        raise AssertionError(f"unexpected method {method}")

    client = _mock_client(handler)
    with pytest.raises(ScreenConnectError) as exc_info:
        asyncio.run(invoke_command(client, "s1", "All Machines", "Write-Output hi", timeout=2, poll_interval=0.01))
    assert exc_info.value.status_code == 404
    envelope = json.loads(exc_info.value.to_envelope())
    assert envelope["error"]["code"] == "not_found"
    assert envelope["error"]["message"] != "ScreenConnect rejected the auth secret for this instance"
    assert "extension" in envelope["error"]["message"].lower()


def test_invoke_command_times_out_waiting_for_ran_command():
    """TC6 (AC5): queued fine, but no RanCommand event ever appears -- distinct timeout error."""

    def handler(request: httpx.Request) -> httpx.Response:
        method = _method(request)
        if method == "GetSessionBySessionID":
            return httpx.Response(200, json={"SessionID": "s1", "GroupPath": "All Machines"})
        if method == "SendCommandToSession":
            return httpx.Response(200, json=None)
        if method == "GetSessionDetailsBySessionID":
            return httpx.Response(200, json={"Events": []})
        raise AssertionError(f"unexpected method {method}")

    client = _mock_client(handler)
    with pytest.raises(ScreenConnectError) as exc_info:
        asyncio.run(invoke_command(client, "s1", "All Machines", "Write-Output hi", timeout=0.2, poll_interval=0.05))
    assert exc_info.value.status_code == 408
    envelope = json.loads(exc_info.value.to_envelope())
    assert envelope["error"]["code"] == "timeout"
    assert envelope["error"]["retryable"] is False
    assert "fail" not in envelope["error"]["message"].lower()  # distinct wording from a failure


def test_invoke_command_does_not_retry_send_command_on_5xx():
    """SendCommandToSession must never be retried: a retry on a transient 5xx could re-run the command."""

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        method = _method(request)
        if method == "GetSessionBySessionID":
            return httpx.Response(200, json={"SessionID": "s1", "GroupPath": "All Machines"})
        if method == "SendCommandToSession":
            attempts["n"] += 1
            return httpx.Response(503, text="Service Unavailable")
        raise AssertionError(f"unexpected method {method}")

    client = _mock_client(handler)
    with pytest.raises(ScreenConnectError):
        asyncio.run(invoke_command(client, "s1", "All Machines", "Write-Output hi", timeout=2, poll_interval=0.01))
    assert attempts["n"] == 1


def test_invoke_command_via_run_tool_wraps_errors_as_envelope_not_exception():
    """The MCP tool layer (run_tool) never lets a ScreenConnectError from invoke_command escape as an exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        if _method(request) == "GetSessionBySessionID":
            return httpx.Response(200, json=None)
        raise AssertionError("SendCommandToSession must not be reached for an unknown session")

    from connectwise_screenconnect_mcp.tools._common import run_tool

    client = _mock_client(handler)

    async def action(c: ScreenConnectClient):
        return await invoke_command(c, "missing", "All Machines", "Write-Output hi", timeout=2, poll_interval=0.01)

    result = asyncio.run(run_tool("screenconnect_invoke_command", lambda: client, action))
    assert json.loads(result)["error"]["code"] == "not_found"


# --- extract_ran_command_output (the other Open-questions assumption, see api_client.py) ---


def test_extract_ran_command_output_picks_earliest_matching_event_after_cutoff():
    after = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    session = {
        "Events": [
            {"EventType": "QueuedCommand", "Time": "2026-01-01T00:00:05Z", "Data": "ignored"},
            {"EventType": "RanCommand", "Time": "2025-01-01T00:00:00Z", "Data": "too-early"},
            {"EventType": "RanCommand", "Time": "2026-01-01T00:00:10Z", "Data": "second"},
            {"EventType": "RanCommand", "Time": "2026-01-01T00:00:02Z", "Data": "first"},
        ]
    }
    assert extract_ran_command_output(session, after) == "first"


def test_extract_ran_command_output_returns_none_when_no_event_yet():
    assert extract_ran_command_output({"Events": []}, time.time()) is None
    assert extract_ran_command_output({}, time.time()) is None
