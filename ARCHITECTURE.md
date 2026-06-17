# ARCHITECTURE.md

Revenue Manager Agent for a hotel GM. Pipeline:
`Playwright scrape → typed transform → idempotent psycopg load → Postgres → semantic
views → 5 typed tools → Deep Agent → FastAPI gateway (SSE) + chat UI`.

## 1. ETL boundary
- **Extract** (`etl/scrape.py`): Playwright/Chromium drives the SPA, waiting for the
  concrete data selector (not `networkidle`, which never settles) so it never reads a
  "Loading…" placeholder. Pages `/reservations` (**100/page**) via the **"Next →"**
  button, there is no working `?page=` param, collecting all **254** ids; drills into
  each `/reservations/<id>` (retried if it hydrates slowly) for per-night stay rows
  (incl. per-night `financial_status`) + detail-only fields; clicks all **five tabs**
  of the **tabbed** `/reference` (lookups + macro history); reads `dataset_revision`
  from **`/verify`**.
- **Transform** (`etl/transform.py`): pure, typed (pydantic). Enforces grain
  **one row per reservation × stay_date**, coerces types, normalises datetimes to UTC,
  and fails loudly if a reservation's scraped nights ≠ declared `nights`.
- **Load** (`etl/load.py`): **idempotent truncate-and-reload** in FK order, one
  `load_manifest` row per run. `row_hash` uses the same recipe as
  `compute_load_fingerprint.py` so manifest ↔ LOAD_PROOF ↔ `/health` agree.
- **Verify** (`etl/run_etl.py`): writes `SCRAPE_MANIFEST.json` (anchor_date,
  reservation_ids_count + sha256); `LOAD_PROOF.json` via the brief's script;
  reconciled against `/verify` on the scrape day.

## 2. Database and views
Postgres runs on the deployment host (local docker-compose for dev). The brief's
`schema.sql` is kept **byte-identical**; one documented `sql/schema_overrides.sql`
(applied after schema, before views) relaxes **only** the `rate_plan_code` FK, because
the data uses 16 granular rate codes while `rate_plan_lookup` is a fixed 8-row
dimension, so the two cannot coexist under a strict FK (the other three dimension FKs
stay enforced).
`sql/views.sql` sits between tools and raw tables: `vw_stay_night_base` (Posted,
non-cancelled), `vw_segment_stay_night` (adds stay-date-effective `macro_group`), and a
supporting `vw_stay_night_posted` (Posted, keeps cancelled) for the toggle / as-of
paths. Tools never read `reservations_hackathon` directly.

## 3. Tool layer
Five tools (`tools/metrics.py`), each reading the semantic views (never the raw fact
table), no SQL parameter. The OTB tools also read the `room_type_lookup` dimension for
capacity (the occupancy/RevPAR denominator):

| Tool | View(s) | Notes |
|------|---------|-------|
| `get_otb_summary` | `vw_stay_night_posted` (+ `room_type_lookup`) | cancellation toggle; Posted-only; ADR/occupancy/RevPAR |
| `get_segment_mix` | `vw_segment_stay_night` | effective macro group; shares; per-segment ADR |
| `get_pickup_delta` | `vw_stay_night_base` | Europe/London window on `create_datetime`; pickup ADR |
| `get_as_of_otb` | `vw_stay_night_posted` (+ `room_type_lookup`) | point-in-time membership; ADR/occupancy/RevPAR |
| `get_block_vs_transient_mix` | `vw_stay_night_base` | block/transient + top-3 concentration; block/transient ADR |

Defaults (exclude Cancelled + Provisional) are baked in code; arbitrary SQL is not
exposed because correctness (grain, filters, dates) must live in tested code, not in
model-improvised SQL. Grain definitions: `tools/METRIC_DEFINITIONS.md`.

## 4. Deep Agents wiring (`agent/build.py`)

| Building block | Use |
|----------------|-----|
| Tools | Five named tools, no `run_sql` |
| Skills | 8 `SKILL.md` files, filesystem-backed (`FilesystemBackend`), progressive disclosure |
| Subagents | `segment-mix-analyst`, segment/block-mix quarantined, restricted to the 2 mix tools |
| Planning | built-in `write_todos` decomposes multi-part GM questions |
| Memory / filesystem | `InMemorySaver` checkpointer + `InMemoryStore` (multi-turn) + virtual filesystem |
| Human-in-the-loop | `interrupt_on={"get_as_of_otb": True}` (checkpointer required) |
| Model & prompt | Claude via `MODEL_ID` (default `anthropic:claude-sonnet-4-6`); RM persona, §12 answer style |
| MCP (bonus) | the same five tools are also published as an MCP server (`mcp_server`); the deployed agent consumes them over MCP (§9) |

## 5. Skill → tool routing matrix

| Skill | Primary tool(s) | Judgment? |
|-------|-----------------|-----------|
| otb-summary | get_otb_summary | N |
| segment-mix | get_segment_mix | N |
| pickup-pace | get_pickup_delta | Y |
| ota-dependency | get_segment_mix | Y |
| block-concentration | get_block_vs_transient_mix | Y |
| cancellation-risk | get_otb_summary | Y |
| point-in-time-otb | get_as_of_otb | N |
| grain-and-filters | all (guardrail) | guardrail |

OTB questions load `otb-summary`; pace → `pickup-pace`; mix/OTA → `segment-mix` /
`ota-dependency` (often via the subagent); group → `block-concentration`;
"as of" → `point-in-time-otb`. `grain-and-filters` is always consulted.

## 6. Agent tests
- `tests/test_agent.py`: a fake tool-calling model drives the graph, asserts
  `get_as_of_otb` raises an approval **interrupt**, the subagent is restricted to the
  mix tools, skills are filesystem-backed, and checkpointer/store are configured.
  Multi-tool decomposition uses a recorded trace fixture. No live LLM.
- `tests/test_skills.py`: validates pack version, ≥6 skills, ≥3 judgment skills
  (numeric threshold + recommended action regex), tool routing, no-SQL, guardrail -
  all without API calls.

## 7. Deployment topology
Self-hosted on an always-on Linux VM, chosen over a free PaaS tier so the service never
cold-starts during the review window.
- **DB:** Postgres on the host, loaded by the ETL.
- **Agent + UI:** a FastAPI service (`app/server.py`) run by a `systemd` unit (uvicorn),
  behind `nginx` with HTTPS (Let's Encrypt). `POST /chat` and the chat page are basic-auth
  gated; the page renders tool/skill chips live over **SSE**, expandable to each tool's
  inputs, result and latency.
- **MCP server:** under `RM_TOOL_TRANSPORT=mcp` the tool layer runs as a second `systemd`
  unit (`otel-mcp.service`, streamable-http on `127.0.0.1`) that the agent consumes (section 9).
- `GET /health` is unauthenticated so the reviewers' pre-chat check can read it; fields:
  `db_fingerprint` (= `reservation_stay_status_sha256`), `dataset_revision`, `row_hash`,
  `financial_status_posted_only_rows`, computed live and compared to committed `LOAD_PROOF.json`.
- API key and basic-auth credentials live only in the host environment, never in git.

## 8. Out of scope (and why)
- **A managed PaaS (e.g. Render/Neon free tiers)** would serve the streaming UI with
  less setup, but those tiers sleep on idle and cold-start, which risks the "live and
  responsive for ≥7 days" requirement. An always-on self-hosted VM avoids that; the
  trade-off is owning the nginx/systemd/TLS setup directly.
- No daily cron: the dataset is anchor-stable within a day, so the ETL is run-on-demand
  and reproducible for a given anchor date.

## 9. MCP server (bonus)
The five tools are also published as a standalone **MCP server** (`mcp_server/`), and the
deployed agent can consume them over the Model Context Protocol instead of in-process.
Properties:
- **Reuse:** any MCP client (e.g. Claude Desktop) can call the tools as a separate service.
- **Credential isolation:** in the MCP deployment only the server reads `DATABASE_URL`; the
  agent process receives results over the protocol.

The server is generated from `tools.metrics.ALL_TOOLS` via `langchain_mcp_adapters.to_fastmcp`,
so each metric has one definition (in `tools/metrics.py`); grain, default filters, and the
read-only transaction guardrail (`tools/db.py`) are inherited. Tool names are preserved
across the protocol, so the `get_as_of_otb` HITL gate and the five-tool surface are
unchanged. `mcp_server` serves `stdio` (local clients) or `streamable-http`
(remote/production), selected by `--transport` / `MCP_TRANSPORT`.

`build_agent()` defaults to the in-process tools, so the structural tests run without a
server or API key. The deployment sets `RM_TOOL_TRANSPORT=mcp` (and `MCP_SERVER_URL` for
streamable-http); `load_rm_tools_over_mcp()` loads the five tools and verifies the server's
surface is exactly the required names before wiring them. `tests/test_mcp.py` checks the
round-trip over an in-memory transport: the catalog is exactly the five, and a tool called
over MCP returns the same result as the in-process tool. On the VM the server runs as its
own `systemd` unit (`otel-mcp.service`, streamable-http on `127.0.0.1`) and the agent
service connects to it.
