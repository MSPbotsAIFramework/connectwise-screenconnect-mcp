# PRD-21570 · Code review

Status: frozen @ 5ed43f3

## Round 1 · 2026-11-05 · reviewed dev/PRD-21570 @ 5ed43f3 (base main @ 410e37a)

Verdict: approved

### Findings

None.

### Verification performed

- `uv sync && uv run pytest -q` on `5ed43f3`: 38 passed, 1 warning (pytest-asyncio upstream deprecation notice, library-level), matching the `[DEV]` stub's claim.
- Chinese-character scan (`grep -rnP '[\x{4e00}-\x{9fff}]' --exclude-dir=.git --exclude-dir=.venv .`): no output.
- `gitleaks` is not installed on this reviewing machine; not independently re-run. Self-test report claims `gitleaks detect --source . --no-git` -> "no leaks found"; `.gitignore` / `.dockerignore` exclude `.env*`, and no credential literals were found anywhere in the diff during manual review.
- README `## MSPbots Connector` section (lines 13-83) diffed byte-for-byte against `vendor-mcp-development/reference/readme-connector-section.md`: exact match.
- Credential handling: `Settings` (`config.py`) has no credential fields; `ScreenConnectCredentials.__repr__` redacts the secret (tested); `api_client.py`'s only log line (`logger.info("screenconnect_api method=%s status=%s ms=%d attempt=%d host=%s", ...)`) logs method/status/latency/host only, never headers; error messages for 401/403/404/timeout never include the secret value (tested in `test_get_live_data_upstream_401_maps_to_auth_rejected`).
- Per-request isolation: credentials and request-id are `contextvars.ContextVar`, set in `GatewayCredentialsMiddleware.__call__` and reset in `finally`; no module-level mutable credential state. `_get_shared_http_client()` holds a single process-wide `httpx.AsyncClient` but the `CTRLAuthHeader` is built per call from the caller's own credentials (`ScreenConnectClient._headers()`), so sharing the connection pool across tenants does not leak credentials (AC5, tested by `test_concurrent_requests_do_not_share_credentials` and `test_get_live_data_via_run_tool_wraps_errors_as_envelope_not_exception`).
- Error envelope: every tool error returns `{"error": {"code", "message", "retryable"}}` via `run_tool`/`error_envelope`; `ScreenConnectError` never escapes as a raw exception from the tool layer (`tools/_common.py::run_tool` catches `ScreenConnectError`, `ValueError`, and `Exception`).
- Scope: diff `410e37a..5ed43f3` only touches this ticket's new connector scaffold and the one tool (`screenconnect_get_live_data`); no changes outside what the tech design's change list names. Sibling ticket PRD-21572's branch (`dev/PRD-21572`) is docs-only so far and does not intersect this diff.
- Tests match the ac.json: TC1-TC6 automated and passing (`tests/test_tools.py`, `tests/test_middleware.py`); TC7 correctly left `untested` pending a real ScreenConnect instance per `mcp-track.md` §2.5 (not a blocker).
- Checklist (`vendor-mcp-development-sop.md` §14): credential-header-only config, 401 + required_headers list on missing header, request-level isolation with `finally` reset, DNS-rebinding protection disabled, MCP lifespan wired to the outer Starlette app, structured error envelope with fixed code vocabulary, outbound connect/read timeouts set, bounded retry with backoff respecting `Retry-After` and a <120s call budget, shared HTTP client reused, multi-stage Dockerfile with non-root `production` stage and `curl` installed for the healthcheck, `EXPOSE 8080` + default `MCP_HTTP_PORT`/`MCP_HTTP_HOST` + `HEALTHCHECK`, `uv.lock` committed, README credential table + MSPbots Connector section before it, repository all-English, single tool ≤20 limit with `screenconnect_`-prefixed name, description budgets tested, `instructions` present and budget-tested, `dump_json_capped` caps output at 20,000 chars, `READ_ONLY` tool annotations set — all satisfied.

No must-fix or suggestion-level findings. Design's two Open Questions (filter-string grammar, upstream field names) are correctly carried forward as `ac.json` TC7 / README "Decisions made by DEV01" rather than silently assumed; this is documentation, not a code defect, and does not block merge per `mcp-track.md` §2.5 (no vendor test account is not a blocker).
