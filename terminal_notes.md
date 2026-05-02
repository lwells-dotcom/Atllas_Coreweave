# Terminal Notes

---

## 2026-04-21 — Rack Analyzer Bridge, Model In-Service Counts, Workbook In-Service Sorting

Session context: Focused on the gap between Rack Analyzer and Ask Atlas, then on model-count correctness for queries like `How many SN5610s are in service?`, and finally on the workbook-side `sort by in service` split that was showing a blank left column and pushing everything into `Not In Service`.

**Rack Analyzer → Ask Atlas bridge**
- Confirmed the mismatch was architectural, not hallucination: `/api/buildsheet` read the uploaded workbook directly through `build_sheet_processor.process_rack()`, while `/api/ask` only used normal upload/Postgres/in-memory sheet context.
- Added a bridge in `atlas_web_app.py` so Rack Analyzer caches the last rack result for the authenticated user and `/api/ask` can reuse that cached rack-analysis context when the question matches the same rack and Postgres is empty or weak.
- Updated the Rack Analyzer frontend request to send the bearer token so the rack result can be tied to the same verified user session.
- Added a dedicated Rack Analyzer prompt/context path in `demo_auth_ai.py` so the LLM reads the cached rack result as preformatted grounded text instead of falling back to unrelated sheet context.

**Ask Atlas routing hardening**
- Continued hardening `query_extractors.py` and `query_intent.py` for real phrasing: `dh202 rack 41`, `rack 41 in dh202`, `dh2 rack 041`, plural model forms like `7750-SR-1SEs`.
- Prevented location-like tokens (`dh202`, rack identifiers) from being misread as models/devices.
- Tightened cross-site routing so generic words like `across`, `both`, and `entire cutsheet` do not steal single-site optic/status/device questions.
- Tightened status-router priority so section/model/site-total questions defer to their more specific routers instead of getting swallowed by generic status handling.
- Rebased the 100-question classifier harness on the current public extractor surface.
- Result: `python3 test_classify_100.py` finished at `100/100 correct`.

**Model count semantics**
- Added `extract_model_status_filter()` in `query_extractors.py` and a new `status_count` mode in `atlas_query_router.py`.
- `model_search` now understands model-scoped status phrases: `in service`, `LLDP passed`, `Human Verified`, `Complete`, `Not Run`, `Not Terminated`.
- Added dedicated status-count SQL path reporting unique device locations, hostnames, cutsheet rows, A-side and Z-side row counts.
- Fixed list-mode formatting so `LIMIT 200` is a truncated display cap, not the true total.

**Workbook-side in-service split**
- Root cause: `Define_Optic_Count.py` `sort_by_status=True` only treated `LLDP Passed` as in service.
- Broadened `_is_in_service_status()` to treat the completed/verified family as in service: `LLDP Passed`, `Human Verified`, `Complete`, `Cable Is Ran: Complete`.
- Applied to `count_cutsheet()`, `count_roce()`, `count_devices_cutsheet()`, `count_devices_roce()`.
- Aligned Ask Atlas model-status extraction to the same broader status family.

Validation: `python3 test_classify_100.py` → `100/100 correct`. After fix, `08B` in-service optics: `12433`, not-in-service: `34336`; `SN5610` in-service on `08B`: `811`, on `08A`: `626`.

---

## 2026-04-22 — Step-by-Step Session Log (Waves 1-2)

### atlas_query_router.py — cross_site_models documentation
Added comment above `"cross_site_models"` SQL template: cross-site queries intentionally ignore `upload_id` and join across all active uploads (`cu.is_active = TRUE`). Same comment added to `trend_status` and `trend_section` templates (intentionally include all uploads for historical timeline).

### atlas_web_app.py — Thread safety for shared dicts
- Added `_state_lock = threading.Lock()` after `USER_CONTEXT`, `USER_SITE`, `AUDIT_LOG` declarations.
- Wrapped `_evict_stale_contexts()` and `_audit()` bodies with `with _state_lock:`.
- Narrow locks around all `USER_CONTEXT`/`USER_SITE` reads/writes in route handlers (not held across slow I/O).

### atlas_web_app.py — PIN rate limiting
- Added sliding window rate limiter: 10 attempts per 60s per client IP using `time.monotonic()` and `_state_lock`.
- `_get_client_ip()` reads `X-Forwarded-For`, falls back to `remote_addr`.
- `verify_pin` route: rate limit check fires first (before JSON parse or DB), returns 429 on breach.

### atlas_web_app.py — Security headers middleware
- Added `set_security_headers` `@app.after_request` decorator, matching `demo_web_app.py`.
- Sets `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and `Content-Security-Policy` on every response.

### atlas_web_app.py — upload_count Postgres load moved to background thread
- Extracted `_pg_load_background(save_path, site_code, username)` helper; runs `atlas_data_loader.load_file` in a daemon thread.
- Route returns immediately with `"pg_loaded": "pending"` instead of blocking on DB write.

### atlas_web_app.py — skip build_sheet_context() when Postgres is available
- If `_check_postgres()` is True at upload time, `build_sheet_context()` is skipped entirely; minimal stub `{"files": [...], "ts": ...}` is stored.
- Eliminates the expensive `iterrows()` loops for the common (Postgres-up) case.

### atlas_web_app.py — Monotonic clock for Postgres cache
Changed `_pg_cache["ts"]` timing from `time.time()` to `time.monotonic()`. Matches `atlas_data_loader.check_postgres`.

### demo_web_app.py — Thread safety and monotonic clock
Applied matching fixes: `_state_lock`, wrapped `_check_rate_limit`, `_evict_stale_contexts`, `_audit` bodies, narrow locks in route handlers, `time.monotonic()` for `_check_postgres`.

### atlas_query_router.py — ip_lookup SQL fix
`raw_row` column no longer exists on `cutsheet_connections` (migrated to `cutsheet_raw_rows`). Updated `ip_lookup` template: aliased `cutsheet_connections` as `cc`, added `LEFT JOIN cutsheet_raw_rows rr ON rr.connection_id = cc.id`, updated `raw_row` reference to `rr.raw_row`.

### atlas_query_router.py — ip_lookup format block
Now renders `a_device:a_port -> z_device:z_port [status]`. Appends up to 3 shortest `key:value` pairs from `raw_row` whose values contain a token from the question (≥4 chars).

### diagnose_live.py — Schema State section
- Added `"cutsheet_raw_rows"` to `required_tables` list.
- Added post-loop `warn()` check: if `raw_row` column still exists on `cutsheet_connections`, flags migration may not have completed.
- Removed dead code pattern (line 144): `cur.execute()` inside a list comprehension. Replaced with plain `cur.execute()` + `fetchall()`.

### 2026-04-22 12:33 — diagnose_live.py verification (omc team)
Ran `/omc team 1:claude`. Both fixes already applied; no code changes needed.

### demo_auth_ai.py — System prompt rework
Rewrote all three system prompts in `_build_grounded_messages` (POSTGRES, RACK_ANALYZER, default paths). Removed forced "Always respond with exactly two sections: Summary and Key Findings" structure. New prompts: answer directly in first sentence, tables only for 5+ item comparisons. Updated user message prefix from "Include brief evidence references" to "Cite specific counts or values from the data when relevant."

### demo_auth_ai.py — Context compression (in-memory path)
- `_build_legacy_trimmed_context`: capped optic_locations to top 10 per type, added `other_locations_count`, stripped `evidence` arrays, reduced `max_locations` from 20 to 10.
- `_trim_context_for_llm`: progressive token budget enforcement — drops `top_locations` first, then `optic_locations` detail, then `device_model_summary` locations. Logs warning when trimming fires.
- `_build_normalized_context`: connection list capped at 500 with truncation note.
- Target: in-memory path under ~6k tokens for Ellendale-sized sheets (was ~19k).

### atlas_postgres_context.py — Optic summary query fix
Replaced the `UNION ALL` subquery in `build_postgres_context_for_general` with a single-pass `COALESCE(NULLIF(a_optic, ''), NULLIF(z_optic, ''))` query. Parameter tuple reduced from 6 elements to 3.

### Define_Optic_Count.py + atlas_web_app.py — Single-pass sheet parse
Added `count_and_build_context(files)` to `Define_Optic_Count.py`: single-pass function that parses xlsx once. Eliminates the double-parse where `count_all_files_gui` and `build_sheet_context` each opened the file independently. Expected ~40-50% upload time reduction.

### docker-compose.yml — Hardening changes
1. `DEMO_VERIFY_PIN` changed from `:-123456` default to `:?err` — `docker compose up` now fails fast if not set in `.env`
2. `restart: unless-stopped` added to web service
3. `shm_size: 256mb` added to db service
4. `env_file: .env` block removed from web service — eliminates the precedence footgun

### .dockerignore created
New file at `Optic_Count/.dockerignore` excluding `.env`, `__pycache__`, `uploads/`, `*.xlsx`, `*.csv`, test files, `diagnose_*.py`, `knowledge/`, `helm/`, `*.md` from Docker build context.

### .env — DB_PASSWORD alignment
Renamed `POSTGRES_PASSWORD` to `DB_PASSWORD` so docker-compose.yml picks it up. Compose uses `${DB_PASSWORD:-atlas}` but `.env` had the wrong var name, causing both services to silently use default password `atlas` instead of `atlas_dev`.

### helm/atlas/values.yaml — web.replicaCount set to 1
Changed from `2` to `1`. Documents ReadWriteMany + session affinity path for future multi-replica scaling.

### helm/atlas/templates/web-deployment.yaml — securityContext added
Added container `securityContext` after `imagePullPolicy`: `runAsNonRoot: true`, `runAsUser/Group: 1000`, `readOnlyRootFilesystem: false` (gunicorn tmp + uploads writes), `allowPrivilegeEscalation: false`.

### helm/atlas/templates/web-deployment.yaml — wait-for-postgres retry limit
Added `retries` / `max_retries=30` counter to init container shell script. After 30 attempts (~60s at 2s sleep), script exits 1 → pod enters `CrashLoopBackOff`. Log line shows `($retries/$max_retries)` on each wait iteration.

### helm/atlas/templates/web-service.yaml — Session affinity
Added `sessionAffinity: ClientIP` with `timeoutSeconds: 3600`. No-op at 1 replica but future-proofs for scale-out.

### helm/atlas/values.yaml — demoVerifyPin
Changed from hardcoded `"123456"` to `""` with `required "secrets.demoVerifyPin must be set"` validator in `secret.yaml`.

### helm/atlas/templates/postgres-statefulset.yaml — volumes block
Removed outer `{{- if .Values.schemaInit.enabled }}` / `{{- end }}` that wrapped the entire `volumes:` block. `volumes:` now always renders; only the `schema-init` configMap entry inside is conditional.

### atlas_schema.sql + helm/atlas/files/atlas_schema.sql — Documentation cleanup
- Second `cutsheet_raw_rows` block: replaced comment explaining duplication is intentional and `IF NOT EXISTS` makes it safe.
- `ALTER TABLE host_inventory`: comment clarifies kept for manual upgrades against existing databases.
- Helm copy brought in sync: added `row_type TEXT` column to `host_inventory` CREATE TABLE.

### Dockerfile — gunicorn worker model
Changed from `--workers 4` to `--workers 1 --threads 4`. Single worker keeps `USER_CONTEXT`/`USER_SITE` in one process; `_state_lock` protects concurrent thread access. Atlas is I/O bound so threads give equivalent throughput. **Closes gunicorn worker isolation issue.**

### Test results
25/30 tests pass. All routing, classification, and SQL logic tests clean. 5 errors are pre-existing:
- `test_rack_context_bridge`: Flask not installed in test runner's Python
- `test_build_sheet_processor`: stale test referencing renamed/moved function
- `test_define_optic_count_in_service` (3 errors): tests reference deleted API

---

## 2026-04-22 — Cutsheet Data Quality Analysis & Preprocessing Plan

### Optic undercount root cause — 11,284 rows with mismatched A/Z optics
COALESCE(a_optic, z_optic) picks A-side first, drops Z-side on mismatched rows. 23% of the sheet has different optic types on each side. Biggest pair: OSFP-800G-2DR4 (A) / QSFP112-400G-DR4 (Z) at 10,872 rows. QSFP112-400G-DR4 is Z-side only. Fix: count each side independently.

### STATUS column — 165 junk values are section headers
~200 rows have section labels (e.g. "CON-01 Grid C1", "DH202 :: C2") in the STATUS column. All have empty optic data. 6 real statuses cover 55,983 rows. Claude in Excel generated STATUS_MAP (171 mappings), OPTIC_SUMMARY (A:24,451 + Z:24,416 = 48,867 total), and CUTSHEET_CLEAN (headers stripped, canonical statuses).

### Next: cutsheet_preprocessor.py
Automated normalization at upload time. Applies STATUS_MAP, strips section headers, counts A/Z optics independently. Source-agnostic. Fixes optic undercount at data layer, not SQL. Full plan in CUTSHEET_CLEANUP_PLAN.md.

---

## 2026-04-29 — Optic/Device Count Bug Fixes (Waves 1-3 Follow-On)

Three files changed this session:

### OpticType.py — case-insensitive model matching
`compare_name()` changed from exact string equality to `.casefold()` on both sides. Canonical name stored as `.upper()` in `put_optic_in_list()`. Prevents `SN2201` and `sn2201` from being counted as separate models.

### Define_Optic_Count.py — multiple fixes
1. **Min-length guard** in `put_optic_in_list()`: drops entries shorter than 2 chars — killed the `v:1` junk entry
2. **Uppercase canonical storage**: `OpticType(optic_type_input.upper(), 1)` — consistent display names
3. **Sort by volume descending** in `create_side_by_side_string()`
4. **Column name constants**: `_SITE_HOSTS_HOSTNAME_COLS`, `_SITE_HOSTS_MODEL_COLS`, `_SITE_HOSTS_STATUS_COLS`, `_CUTSHEET_A_DEVICE_COLS`, `_CUTSHEET_Z_DEVICE_COLS`
5. **`_first_col()` helper**: returns first candidate column present in df
6. **`_read_site_hosts_tab()` helper**: finds and normalizes the SITE-HOSTS tab from xlsx
7. **SITE-HOSTS pass** at end of `count_devices_cutsheet()`: reads devices not present in CUTSHEET connections (e.g. GPU-H200-04 with 480 nodes had blank STATUS). ~3,935 compute node rows affected.

Root cause of missing GPU-H200-04: devices with no CUTSHEET connections (not yet cabled) were completely invisible. Networking gear appeared fine because they show up in CUTSHEET Z-side connections.

### sql_templates.py — breakout deduplication in DB queries
Added `ROW_NUMBER() OVER (PARTITION BY ...)` dedup CTEs to `optic_count` and `cross_site_optics` queries. Partitions by `(upload_id, a_loc_cab_ru, a_port)` for breakout rows. Fixes NVIDIA QSFPDD-400G-DR4 showing 276 not-in-service in DB vs 78 in program (4x fan-out inflation from breakout rows).

### Docker / deployment notes
- Running locally with `docker compose up -d --build` (not Kind/Kubernetes) — Kind/Helm guide in DEPLOY_READINESS.md is for the Kubernetes path
- Quick rebuild: `docker compose up -d --build web` (skips db, preserves pgdata volume)
- Upload hang diagnosis: `docker compose logs -f web` + `docker compose exec db psql -U atlas -d atlas -c "SELECT pid, state, wait_event_type, wait_event, left(query,80) as query FROM pg_stat_activity WHERE state != 'idle';"`
- Materialized view refresh (`refresh_atlas_views()`) is blocking — takes ~3 min on full dataset. Future fix: `REFRESH MATERIALIZED VIEW CONCURRENTLY` (requires unique index on each view).

### UNKNOWN status values observed (need STATUS_MAP entries)
From Orangeburg upload: `'DATA HALL 1'`, `'DATA HALL 2'`, `'DATA HALL 3 & 4'`, `'R163 Net + Con'`, `'UFM Patch'`, `'lll'`

---

## 2026-04-29 — Dashboard Fixes, NetBox SSL, Role Backfill Index

### sql_templates.py — cross_site_optics per-side fix
Replaced `COALESCE(a_optic, z_optic)` single-pass approach with separate `a_deduped` / `z_deduped` CTEs. Mixed-optic cables (A=OSFP-800G, Z=QSFP112-400G) now count once per side.

### Define_Optic_Count.py — _is_in_service_status() / _is_lldp_passed() delegation
Both functions now import `classify_status`, `COMPLETE`, `HUMAN_VERIFIED`, `LLDP_PASSED` from `cutsheet_preprocessor` and delegate entirely to `STATUS_MAP`. Hardcoded string sets removed. `_IN_SERVICE_CANONICAL = {COMPLETE, HUMAN_VERIFIED, LLDP_PASSED}` constant added.

### Source_count_Netbox.py — SSL context + 401 handling
`_graphql_request()`: added `ssl.create_default_context(cafile=certifi.where())` — was using system certs (fails in Docker). `_test_netbox_reachable()`: now handles HTTP 401 alongside 403.

### netbox_dashboard_routes.py — started_at in sql_recent
Added `started_at` to `sql_recent` SELECT. Running snapshots with null `finished_at` now show start time instead of `—`.

### netbox_dashboard.html — three display fixes
1. DH card label "Optics" → "Interfaces" (was showing ALL non-virtual interface count, not optical-only)
2. KPI "Ingest Status" pill: now reads `recent_snapshots[0].status` — shows green OK, yellow RUNNING, red FAILED
3. `started_at` fallback now works

### atlas_schema.sql + live DB — role backfill functional indexes
Root cause of 5+ minute upload hang: `backfill_device_roles()` joins on `LOWER(TRIM(cc.a_device)) = LOWER(TRIM(hi.hostname))` — expression wrapping prevents any B-tree index from being used, forcing full cross-scan.

Fix — three functional expression indexes:
```sql
CREATE INDEX idx_cc_a_device_lower ON cutsheet_connections (upload_id, LOWER(TRIM(a_device)));
CREATE INDEX idx_cc_z_device_lower ON cutsheet_connections (upload_id, LOWER(TRIM(z_device)));
CREATE INDEX idx_hi_hostname_lower ON host_inventory (upload_id, LOWER(TRIM(hostname)));
```
Query plan after: Nested Loop + Memoize + Index Scan on `idx_hi_hostname_lower`. Sub-second per pass.

### Rack analyzer accuracy — verified correct
DH2 Rack 167 (Orangeburg): QSFPDD-400G-DR4: 352 = 288 (A-side) + 64 (Z-side). Counts accurate.

### Dashboard scheduler — already wired
`_start_netbox_scheduler()` in `atlas_web_app.py` fires `_run_netbox_ingest_safe()` via APScheduler every 15 min.

### Pending
- Load 08B (Ellendale Phoenix): re-upload `MASTER-US-CENTRAL-08B-US-LZL01-ELLENDALE.xlsx` — role backfill indexes now in place
- Wave 4 (LLM perf): Anthropic SDK migration, SSL context caching, short-circuit simple queries, V16+V17 security hardening
