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
"""screenconnect_invoke_command: run a PowerShell command on a session and return its stdout (PRD-21572)."""

import asyncio
import time
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from ..api_client import ScreenConnectClient, ScreenConnectError, extract_ran_command_output
from ._common import ClientFactory, run_tool

# This tool runs an arbitrary remote command: it is not read-only, and it is not safe to
# retry blindly (Tech design, Non-functional "Idempotency" -- a retry re-runs the command).
# destructiveHint signals the agent this call has a real-world side effect a repeat call
# cannot undo (vendor-mcp-development SOP §2.7: write/delete tools are destructiveHint=True).
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

SessionId = Annotated[
    str, Field(description="The target session's id, e.g. as returned by screenconnect_get_live_data.", min_length=1)
]
SessionGroup = Annotated[
    str,
    Field(
        description=(
            "Session group/folder name the session is expected to belong to. Checked as a safety "
            "guard before any command is sent -- the call is refused if the session is in a "
            "different group or does not exist."
        ),
        min_length=1,
    ),
]
Script = Annotated[str, Field(description="PowerShell command text to run on the session's remote machine.", min_length=1)]
Timeout = Annotated[
    int, Field(description="Seconds to wait for the command's output before giving up with a timeout error.", ge=1, le=600)
]

_DEFAULT_TIMEOUT_SECONDS = 60
_POLL_INTERVAL_SECONDS = 2.0


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=WRITE)
    async def screenconnect_invoke_command(
        session_id: SessionId,
        session_group: SessionGroup,
        script: Script,
        timeout: Timeout = _DEFAULT_TIMEOUT_SECONDS,
    ) -> str:
        """Run a PowerShell command on a ScreenConnect session's remote machine and return its stdout."""

        async def action(client: ScreenConnectClient) -> str:
            return await invoke_command(client, session_id, session_group, script, timeout)

        return await run_tool("screenconnect_invoke_command", client_factory, action)


async def invoke_command(
    client: ScreenConnectClient,
    session_id: str,
    session_group: str,
    script: str,
    timeout: float,
    *,
    poll_interval: float = _POLL_INTERVAL_SECONDS,
) -> str:
    """The tool's validate -> queue -> poll flow (AC1-AC2, AC5, AC7).

    Split out of the ``@mcp.tool`` closure above so tests can call it directly against a
    mocked ``ScreenConnectClient`` without going through FastMCP's call_tool plumbing, the
    same shape ``tools/sessions.py``'s sibling tool keeps its ``action`` inline because it has
    no polling loop to isolate.
    """
    session = await client.get_session_by_id(session_id)
    if session is None:
        # AC7: no such session at all.
        raise ScreenConnectError(404, f"ScreenConnect session '{session_id}' was not found on this instance.")

    actual_group = session.get("GroupPath")
    if actual_group != session_group:
        # AC2: exists, but not in the caller-supplied session_group -- the safety check this
        # ticket added beyond what the vendor API gives for free (Tech design, Data & API contract).
        raise ScreenConnectError(
            400,
            f"Session '{session_id}' belongs to group '{actual_group}', not the expected group "
            f"'{session_group}'; refusing to send a command.",
        )

    sent_at = time.time()
    await client.send_command_to_session(session_id, script)

    deadline = time.monotonic() + timeout
    while True:
        details = await client.get_session_details(session_id)
        output = extract_ran_command_output(details, sent_at)
        if output is not None:
            # AC1
            return output
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # AC5: queued fine, but no RanCommand event observed in time -- distinct from a
            # failure, since the command may still complete later on the remote machine.
            raise ScreenConnectError(
                408, f"Timed out after {timeout}s waiting for '{session_id}' to report the command's output."
            )
        await asyncio.sleep(min(poll_interval, remaining))
