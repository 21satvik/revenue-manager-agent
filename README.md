# Revenue Manager Agent, solution

A Revenue Manager Agent for a hotel GM, built on **LangChain Deep Agents** over a
**Postgres** database populated by an **ETL scrape** of the challenge data site.
This repository is the solution to the Otel AI build challenge. `schema.sql` at the
repo root is the challenge's schema, kept byte-identical, with one documented foreign-key
relaxation isolated in `sql/schema_overrides.sql`.

```
ETL (Playwright → typed transform → idempotent load)
  → Postgres → semantic views → 5 typed tools
  → Deep Agent (skills · segment subagent · HITL on get_as_of_otb · memory)
  → FastAPI gateway (basic-auth chat · open /health · SSE tool/skill streaming) + chat UI
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design and the skill→tool
routing matrix, and [ATTESTATION.md](ATTESTATION.md) for the Phase 0 comprehension.

## Layout
| Path | What |
|------|------|
| `etl/` | `scrape.py` (Playwright) · `transform.py` (typed, grain) · `load.py` (idempotent) · `run_etl.py` |
| `sql/views.sql` | semantic views (`vw_stay_night_base`, `vw_segment_stay_night`, `vw_stay_night_posted`) |
| `tools/` | `metrics.py` (the 5 tools) · `db.py` (read-only access) · `METRIC_DEFINITIONS.md` |
| `skills/` | 8 `SKILL.md` skills + `CHALLENGE_SKILL.md` (pack `otel-rm-v2`) |
| `agent/` | `build.py` (`create_deep_agent` wiring) · `subagents.py` · `prompt.py` |
| `mcp_server/` | publishes the 5 tools over MCP (bonus); the deployed agent consumes them over the protocol |
| `app/` | `server.py` (FastAPI) · `static/index.html` (chat UI) |
| `tests/` | `test_etl.py` · `test_tools.py` · `test_skills.py` · `test_agent.py` · `test_mcp.py` + synthetic fixture |

## Run it end-to-end

Prereqs: [uv](https://docs.astral.sh/uv/), Docker (for local Postgres), and a
Chromium for Playwright. From the repo root:

```bash
uv sync --extra dev
uv run playwright install chromium

# 1. Local Postgres (mounts ./schema.sql) + FK override + semantic views
docker compose up -d
export DATABASE_URL=postgresql://hackathon:hackathon@localhost:5432/hotel_hackathon
uv run python scripts/apply_views.py   # applies sql/schema_overrides.sql then sql/views.sql

# 2. ETL: scrape the data site, load, write the scrape manifest
uv run python -m etl.run_etl

# 3. Load proof (brief's script) + reconcile with /verify on the same day
uv run python scripts/compute_load_fingerprint.py \
  --database-url "$DATABASE_URL" \
  --manifest etl/SCRAPE_MANIFEST.json --output etl/LOAD_PROOF.json

# 4. Tests
uv run pytest

# 5. Serve the agent (needs ANTHROPIC_API_KEY + BASIC_AUTH_* set)
uv run uvicorn app.server:app --host 0.0.0.0 --port 8000
```

`etl/SCRAPE_MANIFEST.json` and `etl/LOAD_PROOF.json` are produced by step 2-3 and
committed after a real load (they are anchor-dated; reconcile with `/verify` on the
scrape day).

## Tests
`uv run pytest` runs all suites against an isolated `*_test` database (derived from
`DATABASE_URL` and auto-created, or set `TEST_DATABASE_URL`), so it never disturbs
the load from steps 2-3:
- `test_etl.py` / `test_tools.py`, against the database. They load a deterministic
  synthetic fixture (`tests/fixture_data.py`) engineered to exercise every published
  scenario, so they pass before a live scrape and against a real load alike. The
  manifest reconciliation also re-checks `SCRAPE_MANIFEST.json` against the working
  load when one is present, and skips when it is not.
- `test_skills.py` / `test_agent.py`, structural / graph-introspection with a fake
  injected model. No LLM API calls.
- `test_mcp.py`, loads the tools over an in-memory MCP transport and checks a tool
  round-trips to the same result as the in-process call. No network or API calls.

## MCP (bonus)
The same five tools are also published as a standalone **MCP server** so any MCP client
can reuse them, and the deployed agent consumes them over the protocol (see
[ARCHITECTURE.md](ARCHITECTURE.md) §9). One definition of each tool: the server is
generated from `tools.metrics.ALL_TOOLS`, so grain, filters, and the read-only guardrail
are inherited and no SQL is exposed.

```bash
# Run the server for a local client (Claude Desktop): stdio transport
uv run python -m mcp_server --transport stdio

# Or as a network service (production): streamable-HTTP on 127.0.0.1:9000/mcp
uv run python -m mcp_server --transport streamable-http
```

Claude Desktop (`claude_desktop_config.json`), pointing at the local server:

```json
{
  "mcpServers": {
    "otel-revenue-rm": {
      "command": "uv",
      "args": ["run", "python", "-m", "mcp_server", "--transport", "stdio"],
      "env": { "DATABASE_URL": "postgresql://hackathon:hackathon@localhost:5432/hotel_hackathon" }
    }
  }
}
```

The agent uses in-process tools by default (so tests and local runs need no server). The
deployment opts into MCP with `RM_TOOL_TRANSPORT=mcp` (and `MCP_SERVER_URL=...` for the
streamable-HTTP server); only the MCP server then holds `DATABASE_URL`.

## Deployment
A FastAPI service (uvicorn) behind nginx with HTTPS, reading a hosted Postgres loaded by
the ETL. `POST /chat` and the chat page are HTTP basic-auth gated; `GET /health` is left
unauthenticated so the reviewers' pre-chat proof check can read it. `GET /health` returns
`db_fingerprint`, `dataset_revision`, `row_hash`, and `financial_status_posted_only_rows`,
computed live and compared to the committed `LOAD_PROOF.json`; `POST /chat` streams tool and
skill events over SSE and the static page renders them live. Under `RM_TOOL_TRANSPORT=mcp`
the tool layer runs as a second service (`mcp_server`) the agent consumes over the protocol
(see [ARCHITECTURE.md](ARCHITECTURE.md) section 9). The model API key and basic-auth
credentials live only in the deployment environment, never committed.
