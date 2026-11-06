# ConnectWise ScreenConnect MCP Server

An [MCP](https://modelcontextprotocol.io) server for ConnectWise ScreenConnect's **RESTful API
Manager** extension. It lets an AI agent read the live session list for a ScreenConnect session
group (Access sessions), the same data the ScreenConnect host client shows, and run a PowerShell
command on one of those sessions and get its output back.

- Transport: MCP Streamable HTTP at `POST /mcp`, health probe at `GET /health`
- Stateless and multi-tenant: credentials arrive on every request as HTTP headers and are
  never stored; every tool is listed from the first `tools/list`
- Python 3.12, [uv](https://docs.astral.sh/uv/), FastMCP, httpx
- License: Apache-2.0, Copyright 2026 MSPbots.ai

<div align="center">

## MSPbots Connector

**The MCP Gateway built for MSPs: visible, controlled, and your data stays where it belongs.**

[**Get started at connect.mspbots.ai →**](https://connect.mspbots.ai)

</div>

---

MSPbots Connector gives your AI clients one MCP URL to reach your entire MSP stack (RMM, PSA, documentation, security, finance), and gives you control over who can call which tool and a record of everything that was called.

**Hosted service: [connect.mspbots.ai](https://connect.mspbots.ai)** · Free to use.

### Why Connector

![MSPbots Connector](https://stgoss.mspbots.ai/files/mcp/mspbots-mcp.png)

| | |
|---|---|
| **Visible** | Every tool call is logged: who, which vendor, when. Usage dashboard, CSV export, SIEM streaming. |
| **Controlled** | Role-based, per-tool allowlist. Deny by default. |
| **Protected** | Credentials encrypted per record. AI clients never see your vendor passwords or tokens. |
| **MSP-native** | Built for the MSP stack only: RMM, PSA, documentation, security, finance. |

### Quick start

1. **Sign up** at [connect.mspbots.ai](https://connect.mspbots.ai).
2. **Connect your vendors.** Pick from 150+ connectors and authorize each one on the vendor's own page, or bring your own API keys. No extra vendor accounts needed.
3. **Set access.** Create roles and decide which tools each role may call. Nothing is allowed until you allow it.
4. **Add your MCP URL to your AI client.** Copy the URL from your Connector dashboard into Claude, ChatGPT, Cursor, VS Code, Gemini, Codex, or any MCP-capable client.
5. **Ask your AI to do real work**, and watch every call appear in the audit log.

### Features

**Unified gateway**
- One MCP URL for all your AI clients
- 150+ connectors across the MSP stack
- Custom MCP servers supported, free
- Authorization happens on each vendor's own page; the AI never sees passwords or tokens
- Bring your own credentials: use the vendor API keys you already have
- Multiple accounts per app; connect once, reuse across AI clients
- Tool search: finds the right action for the task
- One sign-in; vendor credentials are injected behind the scenes and OAuth tokens refresh automatically

**Visibility**
- Audit log for every tool call (who, which vendor, when)
- Usage dashboard: call volume, top vendors, error rate, per-user activity
- Export audit logs to CSV, or stream to Loki, Graylog, or LogScale (SIEM)
- Optional recording of tool arguments and result summaries (off by default, enabled per organization)

**Control**
- Per-role, per-tool allowlist, deny by default
- owner / admin / member roles
- SCIM provisioning with Entra ID, Okta, JumpCloud, and Google Workspace

**Security**
- Credentials encrypted one record at a time (AES-256-GCM, per-record key)
- AI clients authenticate with OAuth 2.1 + PKCE
- Vendor responses are scanned for prompt-injection patterns and returned as structured data. This is one layer of defense, not a guarantee against every attack.
- Static egress IP for customer firewall and API allowlisting

### Pricing

**Free.**

Links: [connect.mspbots.ai](https://connect.mspbots.ai) · [mspbots.ai](https://mspbots.ai)

---

## Authentication headers

| Header | Required | Type | Default | Allowed values | Description | Example |
|---|---|---|---|---|---|---|
| `X-ScreenConnect-Instance-Url` | Yes | string | none | https URL | Base URL of the customer's ScreenConnect instance. | `https://control.screenconnect.com` |
| `X-ScreenConnect-Auth-Secret` | Yes | string | none | - | Value configured as `RESTfulAuthenticationSecret` on that instance's **RESTful API Manager** extension; sent upstream as the `CTRLAuthHeader` header. | `97a0fe77-dc4a-4f37-a4da-cc12666` |
| `X-ScreenConnect-Origin` | No | string | none | https URL | Only needed if the instance's extension has `RESTfulAllowedOrigin` configured; when absent, no `Origin` header is sent upstream. | `https://documentation.screenconnect.com` |

Where to get these: on the target ScreenConnect instance, an administrator installs/configures the
**RESTful API Manager** extension (ConnectWise Labs) and sets `RESTfulAuthenticationSecret` — that
value is `X-ScreenConnect-Auth-Secret`. If `RESTfulAllowedOrigin` is also set on the extension,
supply the matching value as `X-ScreenConnect-Origin`.

A request to `/mcp` missing `X-ScreenConnect-Instance-Url` or `X-ScreenConnect-Auth-Secret` gets
HTTP 401 naming exactly which header(s) are missing; no request is made to the ScreenConnect
instance in that case.

## Tools

All tools return a JSON string.

| Area | Tools |
|---|---|
| Sessions | `screenconnect_get_live_data` (read-only; **session_type**: `"Access"` only in v1, **session_group**) |
| Sessions | `screenconnect_invoke_command` (write, not idempotent; **session_id**, **session_group**, **script**, *timeout* default 60s) |

`screenconnect_get_live_data` calls the RESTful API Manager extension's `GetSessionsByFilter`
method and returns each session's `session_id`, `name`, `group_path`, `is_connected`, and
`guest_info`. A group with no live sessions returns an empty list, not an error. A `session_group`
that does not exist on the instance also returns an empty list — ScreenConnect's filter has no
separate "group not found" signal.

`screenconnect_invoke_command` runs a PowerShell `script` on the remote machine behind
`session_id` and returns its standard output text. Before sending anything it resolves
`session_id` with `GetSessionBySessionID` and checks it belongs to the supplied `session_group` —
a safety guard against running a command on the wrong machine, since this tool otherwise has no
restriction on what `script` may contain. It then queues the command with `SendCommandToSession`
and polls `GetSessionDetailsBySessionID` until a correlated `RanCommand` event appears or
`timeout` seconds elapse. **Not idempotent**: calling it again re-runs the command, so it is never
retried automatically on a failed send, unlike the read tool above. The command may still finish
on the remote machine after a timeout is reported — the tool only reports that it stopped
waiting.

Errors come back as a JSON envelope instead of an exception:

```json
{"error": {"code": "unauthorized", "message": "ScreenConnect rejected the auth secret for this instance", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`, `invalid_argument`,
`rate_limited`, `upstream_error`, `timeout` (the last one is local to this connector:
`screenconnect_invoke_command` raises it when polling never observes the command's output before
the caller's `timeout`, distinct from an upstream failure). Read calls are retried with backoff on
429, 5xx and network errors; `screenconnect_invoke_command`'s `SendCommandToSession` call is never
retried, since a retry could run the command a second time. `X-ScreenConnect-Auth-Secret` never
appears in an error message or a log line.

## Run locally

```bash
uv sync
uv run python -m connectwise_screenconnect_mcp   # listens on 0.0.0.0:8080
```

or with Docker:

```bash
docker build --platform linux/amd64 -t connectwise-screenconnect-mcp:dev .
docker run --rm -p 8080:8080 connectwise-screenconnect-mcp:dev
```

Configuration (environment variables, all optional): `MCP_HTTP_HOST` (default `0.0.0.0`),
`MCP_HTTP_PORT` (default `8080`), `LOG_LEVEL` (default `INFO`). Credentials are never read
from the environment.

## Testing example

```bash
# list tools
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-ScreenConnect-Instance-Url: https://control.screenconnect.com" \
  -H "X-ScreenConnect-Auth-Secret: your-restful-auth-secret" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# live sessions in one group
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-ScreenConnect-Instance-Url: https://control.screenconnect.com" \
  -H "X-ScreenConnect-Auth-Secret: your-restful-auth-secret" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"screenconnect_get_live_data","arguments":{"session_type":"Access","session_group":"All Machines"}}}'

# run a command on a session and get its output
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-ScreenConnect-Instance-Url: https://control.screenconnect.com" \
  -H "X-ScreenConnect-Auth-Secret: your-restful-auth-secret" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"screenconnect_invoke_command","arguments":{"session_id":"<session-id>","session_group":"All Machines","script":"Write-Output hello","timeout":60}}}'
```

Unit tests (no network access needed):

```bash
uv run pytest
```

## Decisions made by DEV01

First-version scope decisions with no ticket-specific requirement to go on; see the design
documents (`docs/tickets/PRD-21570.md`, `docs/tickets/PRD-21572.md`) for the full reasoning and
alternatives considered:

- Built against the documented RESTful API Manager extension's `GetSessionsByFilter`, not the
  undocumented legacy `PageService.ashx/GetLiveData` endpoint Rewst's flow calls today.
- `session_type` scoped to `"Access"` only for v1 — the ticket's only cited use case is an
  Access-session workflow.
- Credentials read from three headers (`X-ScreenConnect-Instance-Url`,
  `X-ScreenConnect-Auth-Secret`, optional `X-ScreenConnect-Origin`), matching the vendor's own
  `CTRLAuthHeader` + `Origin` model.
- `GetSessionsByFilter`'s exact filter-string grammar is not published on ConnectWise's public
  reference page (re-confirmed at dev time, see `api_client.py::build_session_filter`); v1 builds
  `SessionType = '<type>' and GroupPath = '<group>'`, ScreenConnect's own session properties.
  **Not verified against a live instance** — no vendor test account was available (not a blocker
  per the pipeline's MCP track rules); see `docs/tickets/PRD-21570.ac.json` TC7. If this grammar
  does not match cleanly, the documented fallback is `GetSessionsByName` scoped by name prefix —
  only `build_session_filter` and its tests would need to change.
- The upstream JSON field names for a session object (`SessionID`, `Name`, `GroupPath`,
  `IsConnected`, `GuestInfo`) are an assumption for the same reason (`api_client.py::map_session`),
  also pending a live call.
- `screenconnect_invoke_command` (PRD-21572) uses the same RESTful API Manager extension's
  `SendCommandToSession` to queue a command and polls `GetSessionDetailsBySessionID` for a
  `RanCommand` event, instead of the undocumented legacy `PageService.ashx/AddSessionEvents` +
  `GetSessionDetails` flow Rewst's `cw_control.invoke_command` action calls today — same
  documented-contract preference as the read tool above.
- Added a `session_group` membership check (via `GetSessionBySessionID`) before sending the
  command, even though the vendor's `SendCommandToSession` only takes a session id — the ticket's
  own input shape (`session_group` alongside `session_id`) is treated as a caller-supplied safety
  check against targeting the wrong machine, not just a label, since this tool runs arbitrary
  PowerShell on a remote endpoint.
- The exact JSON field(s) that carry a `RanCommand` event's output text, and whether
  `GetSessionDetailsBySessionID` returns full event history or only the latest event, are not
  published either (`api_client.py::extract_ran_command_output`). v1 assumes an `Events` list of
  `{EventType, Time, Data}` objects and correlates by "earliest `RanCommand` event timestamped at
  or after this call's `SendCommandToSession` request." **Not verified against a live instance**
  (not a blocker; see `docs/tickets/PRD-21572.ac.json` TC9). If this shape differs, only
  `extract_ran_command_output` and its call site need to change — the tool's input/output contract
  does not.
- `screenconnect_invoke_command` is deliberately **not retried** on a failed `SendCommandToSession`
  call (429/5xx/network error): unlike every read call in this connector, retrying could run the
  command a second time on the remote machine.

## License

Apache License 2.0 - see [LICENSE](LICENSE) and [NOTICE](NOTICE).
