# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:


## Commands

All commands run from `DCT_Scripts/Optic_Count` unless noted.

**Run the app locally (Docker):**
```bash
cd Optic_Count
cp .env.example .env   # fill in secrets first
docker compose up
```
Web UI: http://localhost:5050 — Postgres: localhost:9000 (mapped from container 5432).

**Run without Docker (dev mode):**
```bash
cd Optic_Count
python3.11 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt   # same stack as the container
# optional: pytest, graphifyy, etc.
pip install -r requirements-dev.txt
python atlas_web_app.py
```
Use **`requirements.txt`** for parity with Docker; use **`requirements-dev.txt`** only on the host for tests and tooling.

**Run a single test file:**
```bash
python test_router_priority_regressions.py
python test_classify_100.py          # classification report (not unittest)
python test_model_search_semantics.py
python test_location_rack_routing.py
```
Tests mock psycopg2 at import time — no running Postgres needed for routing/classification tests.

**Run all unittest-based tests:**
```bash
python -m pytest test_*.py -v
# or individually:
python -m unittest test_router_priority_regressions.py
```

**Diagnose a live question against the router:**
```bash
cd Optic_Count
python diagnose_model_route.py
python query_debug.py
```

## Architecture

Atlas is a **grounded LLM Q&A platform** for datacenter cutsheet data. Questions are answered by routing them to parameterized SQL templates — no LLM-generated SQL anywhere.

### Two data paths

| Path | Entry point | When used |
|---|---|---|
| In-memory (pandas) | `cutsheet_normalizer.py` → `demo_auth_ai.py` | Small sites, quick demo |
| Postgres | `atlas_data_loader.py` → `atlas_query_router.py` | Production, multi-upload, scaling |

The Postgres path is the scaling solution. The in-memory path works up to ~4,300-row cutsheets (Quincy scale) but hits token limits beyond that.

### Query pipeline (Postgres path)

```
User question
  → query_intent.py        classify_question() / classify_with_context()
                           builds QuestionContext (extractors run once)
  → query_extractors.py    device/location/model/optic/role/side/IP extractors
  → query_lexicon.py       frozenset keyword dictionaries (single source of truth)
  → atlas_query_router.py  selects SQL template, executes, formats result
  → atlas_postgres_context.py  builds LLM context dict with token estimates
  → demo_auth_ai.py        sends to Anthropic API (claude-sonnet-4-6 primary)
```

`atlas_query_router.py` owns ~27 question types (`QUESTION_TYPES` list). All SQL templates are parameterized and support `upload_id` scoping — `None` = full site scope, integer = specific upload snapshot.

### LLM context pipeline (in-memory path)

```
Excel cutsheet
  → cutsheet_profiles.py   canonicalize columns, normalize models/status
  → cutsheet_normalizer.py build Device Inventory + Connection Table
  → demo_auth_ai.py        build_llm_context() → Anthropic API
```

`cutsheet_profiles.py:Canon` is the **single source of truth** for all column names used downstream. If a data field isn't aggregated into `build_llm_context()`, the LLM will correctly report "data not available" — always verify new fields propagate the full pipeline.

### Auth flow

`demo_auth_ai.py` handles auth end-to-end:
1. User submits PIN → `verify_demo_pin()` → HMAC token (15-min TTL)
2. Subsequent requests carry `Authorization: Bearer <token>`
3. `DEMO_TOKEN_SECRET` must be set; raises at call time (not import) if missing

### Schema (Postgres)

Core tables: `sites` → `cutsheet_uploads` (soft-delete via `is_active`) → `cutsheet_connections` + `cutsheet_raw_rows` + `host_inventory`. All connections carry `upload_id` for versioned snapshots. Materialized views are refreshed after each ingest.

### Web app

`atlas_web_app.py` — Flask, port 5050. Server-side session store (`USER_CONTEXT` dict, 2-hour TTL). SSE endpoint for NetBox streaming. Upload dir: `ATLAS_UPLOAD_DIR` env var (default `./uploads`).

`demo_web_app.py` is the older standalone demo — still contains the `/api/health` route that the Helm liveness probe expects (see deploy readiness notes).

### Helm / Kubernetes

Chart at `Optic_Count/helm/atlas/`. Targets Kind cluster (`values.yaml`). Production overrides go in `values-local.yaml`. Known blockers tracked in `../DEPLOY_READINESS.md`.

## Confirmed operational rules

- `signal.SIGALRM` only works in the main thread. Any decorator using it must check `threading.current_thread() is threading.main_thread()` before arming.
- `docker-compose` env var defaults (e.g. `DEMO_VERIFY_PIN: ${DEMO_VERIFY_PIN:-}`) set an empty string that **overrides** Python-level `os.getenv("KEY", "fallback")` defaults. Always set a real default in compose when Python has a fallback.
- `.env.example` is a template; the app reads `.env` only. New devs must `cp .env.example .env`.
- Port mapping: `DB_PORT` 9000 → container 5432 (Postgres). `WEB_PORT` 5050 (Flask UI).
- Multi-stage Docker builds preferred for new Dockerfiles.
- Before writing code, check `knowledge/Index.md` for confirmed rules and open hypotheses in the relevant domain.
- When a hypothesis is confirmed 5+ times, promote it to a rule. When a rule is contradicted, demote it to a hypothesis.
- `cutsheet_raw_rows` is now a required table. `ip_lookup` JOINs to it. If `raw_row` column still exists on `cutsheet_connections`, the schema migration has not completed.
- All shared in-memory dicts (`USER_CONTEXT`, `USER_SITE`, `AUDIT_LOG`, `_RATE_LIMIT_STORE`) must be accessed under `_state_lock` in both `atlas_web_app.py` and `demo_web_app.py`.
- Use `time.monotonic()` (not `time.time()`) for all cache TTL checks. `time.time()` can jump during NTP sync.
