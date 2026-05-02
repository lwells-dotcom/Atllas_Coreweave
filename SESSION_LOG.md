# Session Log

Chronological record of Atlas development sessions.

---

## 2026-03-28 — Initial Build

### What Atlas Is
Flask web app that lets users upload datacenter cutsheet Excel files and ask an LLM (Claude via Anthropic API) questions about the infrastructure. Answers are grounded strictly in cutsheet data only.

### What We Built

**PostgreSQL Schema (`atlas_schema.sql`):** 9 tables + 2 materialized views. Tables: sites, data_halls, site_vars, cutsheet_uploads, devices, ip_assignments, topology_sections, connections, node_data. Views: optic_inventory, cable_status_summary. IP columns flattened from 20+ Excel columns into ip_assignments rows using Postgres INET type. `cutsheet_uploads` tracks file hashes for versioning and delta detection.

**Data Loader (`atlas_data_loader.py`):** Reads Excel files, detects sheets (CUTSHEET, SITE-HOSTS, SITE-VARS, SITE-IP-DATA, SITE-NODE-DATA), populates Postgres in one transaction. CLI: `python atlas_data_loader.py --file path/to/cutsheet.xlsx --site QCY`

**Docker Stack:** Dockerfile (multi-stage build, non-root user, gunicorn), docker-compose.yml (Postgres 16 + Flask), .dockerignore. Postgres on host port 9000 (5432 occupied), Flask on port 5050. Schema auto-runs on first boot via initdb.d mount.

**LLM Resilience Layer (`llm_resilience.py`):** `@with_retry` tenacity exponential backoff, `@with_timeout` SIGALRM hard ceiling (45s), `@with_fallback` Anthropic → OpenAI chain, `@with_cache` TTL-based cache (300s). All decorators are no-ops if tenacity isn't installed.

**Context Pipeline Fix (`cutsheet_normalizer.py`):** `build_llm_context()` was missing connection status aggregation. Added `connection_status_counts` and `status_by_section` to the context dict. This was the biggest grounding quality fix of the session.

**Knowledge System:** Created `DCT_Scripts/knowledge/` folder structure. `atlas/knowledge.md`, `atlas/hypotheses.md`, `atlas/rules.md`.

### Bugs Fixed
- `DEMO_VERIFY_PIN` defaulted to empty string in docker-compose, overriding Python fallback. Fixed: set real default `${DEMO_VERIFY_PIN:-123456}`
- `signal.SIGALRM` crashed in gunicorn worker threads. Fixed: added `threading.current_thread() is threading.main_thread()` check.
- API key was in `.env.example` but not `.env`. App only reads `.env`.
- `build_llm_context()` didn't aggregate STATUS field, so LLM correctly reported "data not available" even though raw data had it.

### Quincy Cutsheet Stats
~4,300 rows, 53 topology sections. Statuses: LLDP Passed (2953), Cable Is Ran Complete (483), LLDP Failed (255), Human Verified (14).

---

## 2026-04-19 — Ingestion Strictness Hardening (Session 2)

### What We Fixed

**Finding 1 (High):** Missing canonical columns now fail hard — `load_cutsheet()` and `load_site_hosts()` raise ValueError instead of logging a warning.

**Finding 2 (High):** Duplicate source column conflicts are now auditable — `apply_profile()` compares row-by-row when two source columns map to the same Canon target.

**Finding 3 (High):** Section header detection tightened — now uses positive match patterns (TIER, SPINE, LEAF, FDP, CDU, GPU, NVLINK, etc.) in addition to negative match.

**Finding 4 (Medium):** Fuzzy model normalization — `normalize_model()` strips revision/version suffixes (-revB, -v2, -r1) before retrying alias lookup.

**Finding 5 (Medium):** ROW:TYPE separated from ROLE — maps to `Canon.HOST_ROW_TYPE` instead of overloading `Canon.HOST_ROLE`. New `row_type` column added to `host_inventory`.

**Finding 6 (Medium):** Sheet selection schema verification — after heuristic tab selection, loader verifies the picked tab has optic columns AND device/port columns.

**Finding 7 (Low):** Connection uniqueness guard — added unique indexes on `cutsheet_connections`: `(upload_id, cable_id)` for rows with cable IDs, and `(upload_id, a_device, a_port, z_device, z_port)` for rows without. `INSERT` uses `ON CONFLICT DO NOTHING`.

### Query Router Refactor
- Replaced ~90 ordered regex patterns with 12 domain routers in `query_intent.py`.
- New modules: `query_lexicon.py`, `query_extractors.py`, `query_intent.py`, `query_debug.py`.
- `atlas_query_router.py` now a thin facade importing from new modules.
- 86/86 parity test passing against old regex classification.
- `route_question()` now logs classification confidence, domain, and reason.

---

## 2026-04-26 — Next Steps Briefing (After Waves 1-3)

**Last updated:** 2026-04-26. Waves 1-3 of the GitNexus audit attack plan are complete (70+ findings fixed across 15 files). All changes are uncommitted on the working tree.

### Waves 1-3 Summary
- **Wave 1 (Critical):** DB stability (managed_connection rollback), security hardening, SQL routing fixes
- **Wave 2 (High):** Web app route fixes, performance (iterrows removal), routing gaps
- **Wave 3 (Medium):** Cutsheet pipeline cleanup (single status dict, per-site headers), query router refactor (sql_templates.py extraction, formatter registry), new query types (cable_type_summary, data_hall filtering), missing indexes

Key architectural change: `atlas_query_router.py` dropped from 1620 to 923 lines. SQL templates live in `sql_templates.py`. STATUS_NORMALIZATION is now auto-derived from STATUS_MAP (one source of truth).

### What's Next: Multi-File Cutsheet Ingestion

Two Ellendale MASTER cutsheets exist:
- `MASTER-US-CENTRAL-08A-US-LZL01-ELLENDALE.xlsx` (~18.7 MB) — already loaded as site ELD
- `MASTER-US-CENTRAL-08B-US-LZL01-ELLENDALE.xlsx` (~19.8 MB) — **NOT YET LOADED**

**Phase 1 (Parse & Map):** Open 08B in Claude in Excel. Identify sheet/tab names, map columns against `PROFILE_STANDARD_V1`, extract unique STATUS values, extract section headers.

**Phase 2 (Update Preprocessor):** Add any new STATUS_MAP entries to `cutsheet_preprocessor.py`. Add new section headers to `SITE_SECTION_HEADERS["ELD"]`.

**Phase 3 (Load & Verify):** Deploy, load via `python atlas_data_loader.py --file /app/uploads/ELD02.xlsx --site ELD`, verify both uploads coexist.

Key files to read first: `cutsheet_preprocessor.py`, `cutsheet_profiles.py`, `atlas_data_loader.py`, `DEPLOY_READINESS.md`, `ATTACK_PLAN.md`.

### Wave 4 (Deferred — After Cutsheet Integration)
- Q3: Migrate from raw urllib.request to Anthropic SDK (streaming)
- Q1: SSL context caching
- Short-circuit simple queries (skip LLM for count/list)
- M5, V3, V4, N8-N12, B5, B6, G4, G5, F14 — see ATTACK_PLAN.md Wave 4 section

### Deploy Reminder
All Waves 1-3 changes are uncommitted. Before new work: `git diff --stat`, then commit Wave 1-3 work, do a clean deploy + test cycle (see DEPLOY_READINESS.md), then proceed with new cutsheet work.

---

## 2026-04-30 — Optics Inventory, Multi-Site Dashboard, Request Flow Audit

### Round 1: Optics Inventory Integration (4 terminals)

Added real optic inventory (NetBox inventory items) to the dashboard. Previously the dashboard only counted interface ports, not actual physical transceivers. Full implementation prompts in FEATURE_PROMPTS.md.

**Terminal 1 — Schema + Ingest:** New `netbox_optics` table in `atlas_schema.sql`. `_validate_inventory_item_list()` introspection gate runs before every ingest. `_is_optic()` filters by name pattern: sfp, qsfp, osfp, transceiver, xcvr, optic. `optic_count` column added to `netbox_snapshots`.

**Terminal 2 — API Routes:** New `/api/dashboard/optics-inventory` endpoint. Returns per-DH, per-type, per-rack optic breakdowns with utilization percentages. Currently derives optic counts from `netbox_interfaces` type_category — works with existing data before an `inventory_item_list` ingest runs.

**Terminal 3 — Dashboard HTML:** "Installed Optics" KPI tile, port utilization bar, optic types chart (horizontal bar), per-rack density table grouped by DH. Mock data added for standalone preview.

**Terminal 4 — Preview:** `netbox_dashboard_preview.html` updated with optic inventory sections. Cutsheet vs NetBox reconciliation table (mock data, concept only — labeled as preview).

### Round 2: Multi-Site Dashboard (4 terminals)

Transformed the dashboard from Ellendale-only (2 hardcoded sites) to all datacenters (50+ sites) with dynamic discovery and a site dropdown. Full implementation prompts in FEATURE_PROMPTS.md.

**Terminal 1 — Ingest Refactor:** `_discover_sites()` queries `site_list` from NetBox GraphQL. Falls back to `_FALLBACK_SITE_DH_MAP` if discovery fails. Dropped per-DH query loop — now queries at site level (3 GraphQL calls per site). `ThreadPoolExecutor(max_workers=5)` for parallel ingestion. `_close_snapshot()` now writes `site_count`, `sites_failed`, `sites_json`.

**Terminal 2 — API Routes:** `_site_filter()` helper extracts `?site=<slug>`. Every endpoint filters by site via `AND (%s IS NULL OR site = %s)`. New `/api/dashboard/sites` endpoint.

**Terminal 3 — Schema + Dashboard HTML:** 3 new columns on `netbox_snapshots`. Mock data (`window.__MOCK` + override) deleted — real API calls now. Site dropdown in header, `currentSite` variable appended as `?site=` to all API calls. Header: "Ellendale NetBox" → "CoreWeave NetBox".

### Architecture Summary (post Round 2)

```
NetBox GraphQL
    │
    ├─ site_list { name, slug, locations { slug } }     ← discovery (once per cycle)
    └─ per site (parallel, 5 workers):
        ├─ device_list(site.slug)                        ← 1 call
        ├─ interface_list(device.site.slug)              ← 1 call
        └─ inventory_item_list(device.site.slug)         ← 1 call
                                                          = 3 calls per site
    ▼
Postgres (one global snapshot per cycle)
    ├─ netbox_snapshots  (site_count, sites_failed, sites_json)
    ├─ netbox_devices    (site column for filtering)
    ├─ netbox_interfaces (site column for filtering)
    └─ netbox_optics     (site column for filtering)
    ▼
Flask API (all endpoints accept ?site=<slug>)
    ├─ /api/dashboard/sites
    ├─ /api/dashboard/summary
    ├─ /api/dashboard/by-dh
    ├─ /api/dashboard/devices
    ├─ /api/dashboard/optics
    └─ /api/dashboard/optics-inventory
    ▼
Dashboard HTML — Site dropdown → filters all KPIs, charts, tables
```

### Bugs Found During Testing

**BUG 1: Missing DH203/DH204 (GraphQL pagination).** Site-level queries hit NetBox's default pagination limit. us-central-08a has ~7,900 devices but only ~3,914 came back. Fix: revert `_query_site()` to per-location queries using dynamically discovered locations (prompt ready, not yet executed).

**BUG 2: Frontend/backend JSON shape mismatch.** HTML expects `installed_count`, `port_utilization`, `by_rack` from `/api/dashboard/optics`. Current route only returns `overall`, `per_dh`, `categories`. Fix: Glean provided a drop-in replacement for `optics_breakdown()`. See `/uploads/netbox_dashboard_setup.md` section 4.2.

### Round 3: Request Flow Audit (no code written)

Traced `atlas_request_flow.svg` (10-step Postgres Q&A pipeline) against live source code.

**Bugs found:**
- **B1:** `time.monotonic()` / `time.time()` mismatch in `build_postgres_context_for_general()` — `elapsed` is garbage. Fix: change line 209 to `time.monotonic() - t0`.
- **B2:** OpenAI fallback uses `temperature=1` for data-grounded queries (`_call_anthropic()` uses `temperature=0.0`). High hallucination risk if Anthropic API key is exhausted.
- **B3:** `_call_openai()` has no retry logic — any transient 502 is a hard failure.
- **B4:** Dead `None`-check branches in `build_query_params()` — `_build_location_pattern()` docstring says "Returns None for bare rack numbers" but code never returns `None`. Three qtypes have unreachable fallback paths.
- **B5:** `_fmt_device_list()` infers `qtype` by parsing `lines[0]` instead of receiving it as a parameter — breaks silently if the prefix format ever changes.

**Bottlenecks:**
- **P1:** 3 sequential DB round-trips per user request in `build_postgres_context()` (resolve upload_id, execute SQL, fetch site metadata).
- **P2:** `build_postgres_context_for_general()` runs 3 sequential queries against the same table with identical filters.
- **P3:** Claude API call is fully synchronous and blocking — `urllib.request.urlopen()` blocks the Flask worker thread for up to ~135s.

**Design issues:**
- **D1:** Hidden Priority 0 in `_trim_context_for_llm()` — `_active_rack_context` checked BEFORE `_postgres_context`. If both are set, Postgres SQL results are silently discarded. Diagram doesn't show this branch.
- **D2:** `extract_model_status_filter()` re-runs unconditionally (not stored in `QuestionContext`), and also called a third time in `_fmt_model_search()`.
- **D3:** Dead code in `route_optic_intent()` for `cable_type` — `route_cable_type_intent()` runs earlier in `_ROUTER_CHAIN` and already handles all `cable_type` keywords.

### Files Changed / Created (2026-04-30)

**Modified:** `atlas_schema.sql`, `atlas_web_app.py`, `Source_count_Netbox.py`, `docker-compose.yml`, `Dockerfile`, `requirements.txt`, `helm/atlas/files/atlas_schema.sql`

**New:** `netbox_dashboard_ingest.py`, `netbox_dashboard_routes.py`, `netbox_dashboard.html`, `netbox_issues.md`

### Pending (Next Session Priority)
1. Fix `_query_site()` pagination bug — revert to per-location queries with discovered locations (terminal prompt ready in FEATURE_PROMPTS.md)
2. Fix `/api/dashboard/optics` JSON shape — apply Glean's drop-in replacement for `optics_breakdown()`
3. Retest after both fixes: all 4 Ellendale DHs should appear, optics KPI + utilization bar should render
4. Load 08B Ellendale cutsheet (`MASTER-US-CENTRAL-08B`)
5. Wave 4: Anthropic SDK migration, SSL context caching, short-circuit simple queries
6. Chain of custody tool (idea only)
7. Schema upgrade: SERIAL → BIGSERIAL, INTEGER → BIGINT (Glean recommendation, low priority)
