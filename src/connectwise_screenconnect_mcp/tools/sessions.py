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
"""screenconnect_get_live_data: live session list for a session group (PRD-21570)."""

from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import ScreenConnectClient, build_session_filter, map_session
from ._common import READ_ONLY, ClientFactory, run_tool

# v1 only supports Access sessions (Non-goals): the ticket's only cited use
# case (Rewst's remote-PowerShell flow) is an Access-session workflow. Passing
# the vendor's own string label here (rather than its internal enum ordinal)
# sidesteps the Open-questions ordinal-confirmation problem entirely.
SessionType = Literal["Access"]
SessionGroup = Annotated[
    str,
    Field(description="Session group/folder name as shown in the ScreenConnect session list, e.g. 'All Machines'.", min_length=1),
]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def screenconnect_get_live_data(session_type: SessionType, session_group: SessionGroup) -> str:
        """Get the live session list for a ScreenConnect session group (Access sessions only)."""

        async def action(client: ScreenConnectClient) -> list[dict]:
            session_filter = build_session_filter(session_type, session_group)
            raw_sessions = await client.get_sessions_by_filter(session_filter)
            return [map_session(raw) for raw in raw_sessions]

        return await run_tool("screenconnect_get_live_data", client_factory, action)
