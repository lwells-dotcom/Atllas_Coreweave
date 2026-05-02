# Feature Build Prompts

Terminal prompts for multi-agent feature implementation sessions. Each part has 4 terminals with non-overlapping file ownership that can run in parallel.

---

# Part 1: Optics Inventory Integration

**Date:** 2026-04-30
**Goal:** Add real optic inventory (NetBox inventory items) to the NetBox dashboard. Previously the dashboard only showed interface ports — not actual physical transceivers.
**Status:** Implemented. Two bugs found during testing (see SESSION_LOG.md → 2026-04-30 Bugs section).

---

## Shared Contracts

### New Table: `netbox_optics`

```sql
CREATE TABLE IF NOT EXISTS netbox_optics (
    id              SERIAL PRIMARY KEY,
    snapshot_id     INTEGER NOT NULL REFERENCES netbox_snapshots(id) ON DELETE CASCADE,
    site            TEXT NOT NULL,
    location_slug   TEXT NOT NULL,
    rack            TEXT,
    device_name     TEXT,
    device_status   TEXT,
    item_name       TEXT,
    part_id         TEXT,
    serial          TEXT,
    description     TEXT,
    role            TEXT
);
CREATE INDEX IF NOT EXISTS idx_netbox_optics_snapshot ON netbox_optics(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_netbox_optics_dh ON netbox_optics(snapshot_id, location_slug);
CREATE INDEX IF NOT EXISTS idx_netbox_optics_item ON netbox_optics(snapshot_id, item_name);
CREATE INDEX IF NOT EXISTS idx_netbox_optics_rack ON netbox_optics(snapshot_id, location_slug, rack);
```

### Updated `netbox_snapshots` column
```sql
ALTER TABLE netbox_snapshots ADD COLUMN IF NOT EXISTS optic_count INTEGER DEFAULT 0;
```

### New API endpoint: `/api/dashboard/optics-inventory`
```json
{
  "ok": true,
  "snapshot_id": 42,
  "total_optics": 3847,
  "by_dh": [{"dh": "data-hall-201", "site": "us-central-08b", "optic_count": 921, "rack_count": 28}],
  "by_type": [{"item_name": "QSFP-DD-400G-SR4", "n": 812}],
  "by_rack": [{"dh": "data-hall-202", "rack": "A01", "optic_count": 48}],
  "interface_vs_optic": {"total_interfaces": 24891, "total_optics": 3847, "empty_ports": 21044}
}
```

---

## Terminal 1 — Schema + Ingest

**Owns:** `atlas_schema.sql`, `netbox_dashboard_ingest.py`
**Do NOT touch:** `netbox_dashboard_routes.py`, `netbox_dashboard.html`

```
You are working on the Atlas project at /Atlas/DCT_Scripts/Optic_Count/.

TASK: Add NetBox inventory item (physical optics) ingestion to the existing NetBox dashboard snapshot pipeline.

STEP 0 — VALIDATE GRAPHQL SCHEMA:
Before writing any code, run a GraphQL introspection query to confirm inventory_item_list exists:
    query = """{ __schema { queryType { fields { name } } } }"""
Use _graphql_request() from Source_count_Netbox.py. If inventory_item_list is NOT in the schema, STOP and report back.
If it IS available, also introspect:
    query = """{ __type(name: "InventoryItemListInput") { inputFields { name type { name } } } }"""

STEP 1 — SCHEMA (atlas_schema.sql):
Add netbox_optics table and optic_count column after the netbox_latest_snapshot view. Also update helm/atlas/files/atlas_schema.sql.

STEP 2 — GRAPHQL QUERY:
Add _build_inventory_query(site_slug, location_slug) that queries inventory_item_list. Filter optics by name__ic patterns: "sfp", "qsfp", "optic". Target fields: name, part_id, serial, description, role, parent device (name, status, location.slug, rack.name, position). Query one DH at a time (same pattern as _build_devices_query).

STEP 3 — INGESTION:
- Add OPTIC_COLS tuple matching netbox_optics columns.
- In _query_dh_inventory(), add third query call for inventory items. Return (device_rows, interface_rows, optic_rows).
- Update ingest_snapshot() to bulk-insert optic_rows into netbox_optics.
- Update _close_snapshot() to accept and write optic_count.
- Update return dict to include optic_count.
- Use _graphql_with_retry() for the inventory query.

STEP 4 — FILTER LOGIC:
An item is an optic if its name (case-insensitive) contains any of: "sfp", "qsfp", "optic", "transceiver", "xcvr", "osfp". Only include items on devices with status in ACTIVE_STATUSES.
```

---

## Terminal 2 — API Routes

**Owns:** `netbox_dashboard_routes.py`
**Do NOT touch:** `netbox_dashboard_ingest.py`, `netbox_dashboard.html`, `atlas_schema.sql`

```
You are working on the Atlas project at /Atlas/DCT_Scripts/Optic_Count/.

TASK: Add a new API endpoint for optic inventory data to netbox_dashboard_routes.py.

STEP 1 — New endpoint: GET /api/dashboard/optics-inventory
Follow the exact same pattern as existing endpoints (by_dh, devices_breakdown, optics_breakdown).
SQL queries needed:
1. Total optics: SELECT COUNT(*) FROM netbox_optics WHERE snapshot_id = %s
2. By DH: SELECT location_slug AS dh, site, COUNT(*) AS optic_count, COUNT(DISTINCT rack) AS rack_count FROM netbox_optics WHERE snapshot_id = %s GROUP BY location_slug, site ORDER BY location_slug
3. By type: SELECT item_name, COUNT(*) AS n FROM netbox_optics WHERE snapshot_id = %s GROUP BY item_name ORDER BY n DESC LIMIT 25
4. By rack: SELECT location_slug AS dh, rack, COUNT(*) AS optic_count FROM netbox_optics WHERE snapshot_id = %s AND rack IS NOT NULL AND rack <> '' GROUP BY location_slug, rack ORDER BY location_slug, rack
5. Interface count: SELECT COUNT(*) FROM netbox_interfaces WHERE snapshot_id = %s
Run all 5 queries in a single managed_connection() block, single cursor.

STEP 2 — Update summary() response: add optic_count.

STEP 3 — Update _latest_snapshot_row(): add optic_count to the SELECT list.
```

---

## Terminal 3 — Dashboard HTML

**Owns:** `netbox_dashboard.html`
**Do NOT touch:** `netbox_dashboard_routes.py`, `netbox_dashboard_ingest.py`, `atlas_schema.sql`

```
You are working on the Atlas project at /Atlas/DCT_Scripts/Optic_Count/.

TASK: Add optic inventory visualization to netbox_dashboard.html.

STEP 1 — New KPI tile: "Installed Optics" showing total_optics and "X% port utilization" sub-label.

STEP 2 — Port utilization visual: horizontal stacked bar showing ports with optics (green) vs empty ports (muted).

STEP 3 — Optic types chart: horizontal bar chart (same style as existing charts) showing top optic types from by_type.

STEP 4 — Per-rack table: optic density per rack, grouped by DH, sorted by optic count descending within each DH.

STEP 5 — Update DH cards: add optic count from the new endpoint data.

STEP 6 — Mock data: add to window.__MOCK:
"optics_inventory": {
  "ok": true, "snapshot_id": 42, "total_optics": 3847,
  "by_dh": [
    {"dh": "data-hall-201", "site": "us-central-08b", "optic_count": 921, "rack_count": 28},
    {"dh": "data-hall-202", "site": "us-central-08a", "optic_count": 1024, "rack_count": 30},
    {"dh": "data-hall-203", "site": "us-central-08b", "optic_count": 892, "rack_count": 27},
    {"dh": "data-hall-204", "site": "us-central-08a", "optic_count": 1010, "rack_count": 28}
  ],
  "by_type": [
    {"item_name": "QSFP-DD-400G-SR4", "n": 812}, {"item_name": "QSFP28-100G-LR4", "n": 641},
    {"item_name": "QSFP-DD-400G-DR4", "n": 589}, {"item_name": "SFP28-25G-SR", "n": 423},
    {"item_name": "QSFP56-200G-FR4", "n": 387}, {"item_name": "QSFP-DD-800G-SR8", "n": 312},
    {"item_name": "SFP-10G-LR", "n": 298}, {"item_name": "OSFP-400G-ZR", "n": 185},
    {"item_name": "SFP-1G-T", "n": 112}, {"item_name": "QSFP-DD-400G-LR4", "n": 88}
  ],
  "by_rack": [
    {"dh": "data-hall-201", "rack": "A01", "optic_count": 48}, {"dh": "data-hall-201", "rack": "A02", "optic_count": 52},
    {"dh": "data-hall-202", "rack": "A01", "optic_count": 56}, {"dh": "data-hall-202", "rack": "A02", "optic_count": 51},
    {"dh": "data-hall-203", "rack": "A01", "optic_count": 46}, {"dh": "data-hall-204", "rack": "A01", "optic_count": 53}
  ],
  "interface_vs_optic": {"total_interfaces": 24891, "total_optics": 3847, "empty_ports": 21044}
}
Add to fetch mock map: "/api/dashboard/optics-inventory": window.__MOCK.optics_inventory

STYLE: Match existing dark theme exactly. Same CSS variables (--bg-0, --bg-1, --accent, etc.), same panel class.
```

---

## Terminal 4 — Preview + Cutsheet Cross-Reference

**Owns:** `netbox_dashboard_preview.html` (in `/Atlas/` root)
**Do NOT touch:** Files in DCT_Scripts/Optic_Count/

```
You are working on the Atlas project at /Atlas/.

TASK: Update netbox_dashboard_preview.html to match new sections and add a cutsheet cross-reference section.

STEP 1 — Copy mock data structure from real dashboard (same optics_inventory mock data as Terminal 3).

STEP 2 — Add optic inventory sections (mirror Terminal 3): KPI tile, port utilization visual, optic types chart, per-rack table.

STEP 3 — Add "Cutsheet vs NetBox" reconciliation section:
Side-by-side: "Planned (Cutsheet)" vs "Installed (NetBox)". Table columns: Optic Type | Cutsheet Count | NetBox Count | Delta.
Color delta: green (NetBox >= Cutsheet), yellow (NetBox < Cutsheet), red (NetBox = 0 but Cutsheet > 0).
Mock data:
[
  {"optic_type": "QSFP-DD-400G-SR4", "cutsheet": 900, "netbox": 812, "delta": -88},
  {"optic_type": "QSFP28-100G-LR4", "cutsheet": 650, "netbox": 641, "delta": -9},
  {"optic_type": "QSFP-DD-400G-DR4", "cutsheet": 600, "netbox": 589, "delta": -11},
  {"optic_type": "SFP28-25G-SR", "cutsheet": 420, "netbox": 423, "delta": 3},
  {"optic_type": "QSFP56-200G-FR4", "cutsheet": 400, "netbox": 387, "delta": -13},
  {"optic_type": "QSFP-DD-800G-SR8", "cutsheet": 312, "netbox": 312, "delta": 0},
  {"optic_type": "SFP-10G-LR", "cutsheet": 310, "netbox": 298, "delta": -12},
  {"optic_type": "OSFP-400G-ZR", "cutsheet": 200, "netbox": 185, "delta": -15}
]
Label clearly as "Preview — Reconciliation" (actual API doesn't exist yet).
```

---

## Execution Notes (Part 1)

All 4 terminals can run in parallel. After all complete:
1. Deploy: `docker compose up -d --build web`
2. Terminal 1's Step 0 result determines if we proceed — check for "inventory_item_list not found" error
3. Hit `/api/dashboard/optics-inventory` to verify
4. Load dashboard, confirm new sections render

---

# Part 2: Multi-Site Dashboard

**Date:** 2026-04-30
**Goal:** Transform the NetBox dashboard from Ellendale-only (2 hardcoded sites) to all datacenters (50+ sites), dynamically discovered from NetBox, with a site dropdown selector.
**Status:** Implemented. BUG: pagination issue causes DH203/DH204 to be missing. Fix prompt below (Terminal 1 addendum).

---

## Architecture Decisions

- **Site Discovery:** Query `site_list` from NetBox GraphQL at ingest time. No hardcoded site registry.
- **Query Strategy:** Query at site level (no location filter). Each site gets 3 calls. Group by location in Python. Cuts total calls from ~hundreds to ~150.
- **Parallel Ingestion:** `ThreadPoolExecutor(max_workers=5)`. 50 sites ÷ 5 workers × ~15s each = ~2.5 min.
- **Snapshot Model:** One global snapshot per cycle. If one site fails, log and skip. Snapshot closes as `ok` with `sites_failed` count.
- **Dashboard:** Site dropdown at top. Default to "All Sites" aggregate. Selecting a site filters all KPIs, charts, tables.

---

## Shared Contracts

### New API endpoint: GET /api/dashboard/sites
```json
{
  "ok": true,
  "sites": [
    {"slug": "us-central-08a", "name": "US-CENTRAL-08A", "device_count": 381, "location_count": 2}
  ]
}
```

### All existing endpoints gain `?site=<slug>` query param
Pattern: `AND (%(site_filter)s IS NULL OR site = %(site_filter)s)`

### New columns on netbox_snapshots
```sql
ALTER TABLE netbox_snapshots ADD COLUMN IF NOT EXISTS site_count INTEGER DEFAULT 0;
ALTER TABLE netbox_snapshots ADD COLUMN IF NOT EXISTS sites_failed INTEGER DEFAULT 0;
ALTER TABLE netbox_snapshots ADD COLUMN IF NOT EXISTS sites_json JSONB;
```

---

## Terminal 1 — Ingest Refactor (Dynamic Site Discovery + Parallel)

**Owns:** `netbox_dashboard_ingest.py`

```
You are working on the Atlas project at /Atlas/DCT_Scripts/Optic_Count/.

TASK: Refactor netbox_dashboard_ingest.py to dynamically discover ALL sites from NetBox and ingest them in parallel.

STEP 0 — KEEP SITE_DH_MAP AS FALLBACK: Rename to _FALLBACK_SITE_DH_MAP.

STEP 1 — SITE DISCOVERY FUNCTION:
def _discover_sites() -> Dict[str, List[str]]:
GraphQL query:
    { site_list { name slug locations { slug } } }
Use _graphql_with_retry(). Filter sites with zero locations. Log count. Fall back to _FALLBACK_SITE_DH_MAP on failure.

STEP 2 — SITE-LEVEL QUERIES (DROP PER-DH LOOP):
Replace _build_devices_query(site_slug, location_slug) with _build_site_devices_query(site_slug) — queries ALL devices at a site (no location filter). location_slug comes from response data (device.location.slug). Same for interfaces and inventory items.
Devices without location.slug use "" (empty string).

STEP 3 — NEW PER-SITE QUERY FUNCTION:
def _query_site(site_slug: str, active_only: bool = True) -> Tuple[List[tuple], List[tuple], List[tuple]]:
Replaces _query_dh_inventory + _query_dh_optics. Returns (device_rows, interface_rows, optic_rows).

STEP 4 — PARALLEL INGESTION:
with ThreadPoolExecutor(max_workers=5) as pool:
    futures = {pool.submit(_ingest_site, slug): slug for slug in site_map}
Then bulk-insert all successful site results.

STEP 5 — UPDATE _close_snapshot: add site_count, sites_failed, sites_json params.

STEP 6 — REMOVE HARDCODED ELLENDALE REFERENCES: Delete ALL_DH_SLUGS, old per-DH query builders, _query_dh_inventory(), _query_dh_optics(). Keep _validate_inventory_item_list(), _graphql_with_retry(), _slug_to_enum(), _is_optic(), ACTIVE_STATUSES.

IMPORTANT: ingest_snapshot() function signature must remain the same. Add optic_count, site_count, sites_failed to return dict.
```

### Terminal 1 Addendum — Pagination Bug Fix

**Bug:** Site-level queries hit NetBox's default pagination limit (~4,000 results). us-central-08a has ~7,900 devices but only ~3,914 came back. Snapshots using old per-DH queries had all 4 DHs; new site-level queries lost DH203 and DH204.

```
You are working on the Atlas project at /Atlas/DCT_Scripts/Optic_Count/.

TASK: Fix pagination bug in _query_site() in netbox_dashboard_ingest.py.

The current _query_site() queries devices at site level with no location filter, hitting NetBox's pagination limit.
Fix: revert _query_site() to per-location queries using the dynamically discovered locations from _discover_sites().

Keep parallel execution at the site level (ThreadPoolExecutor). For each site, iterate over its discovered location slugs and make per-location queries (same pattern as the original per-DH approach). This restores complete data coverage while keeping dynamic discovery.

The site_map from _discover_sites() returns {site_slug: [location_slug, ...]}. Pass the location list into _query_site() or inline the per-location loop there.

Do NOT change the function signature of ingest_snapshot(). Do NOT change _discover_sites().
```

---

## Terminal 2 — API Routes (Site Filtering + Sites Endpoint)

**Owns:** `netbox_dashboard_routes.py`

```
You are working on the Atlas project at /Atlas/DCT_Scripts/Optic_Count/.

TASK: Add site filtering to all dashboard API endpoints and a new /api/dashboard/sites endpoint.

STEP 1 — SITE FILTER HELPER:
def _site_filter() -> Optional[str]:
    val = request.args.get("site", "").strip()
    return val if val else None

STEP 2 — NEW ENDPOINT: GET /api/dashboard/sites
SELECT site AS slug, COUNT(DISTINCT name) AS device_count, COUNT(DISTINCT location_slug) AS location_count
FROM netbox_devices WHERE snapshot_id = %s GROUP BY site ORDER BY site

STEP 3 — ADD SITE FILTER TO ALL EXISTING ENDPOINTS:
Pattern for each SQL query:
    AND (%s IS NULL OR site = %s)
Pass (snap["id"], site, site) — site value twice where filter appears.
Apply to: summary(), by_dh(), devices_breakdown(), optics_breakdown(), optics_inventory().

STEP 4 — UPDATE _latest_snapshot_row(): add site_count, sites_failed, duration_ms to SELECT.

STEP 5 — UPDATE summary() RESPONSE: include site_count and sites_failed. Remove hardcoded SITE_DH_MAP reference.
```

---

## Terminal 3 — Schema + Dashboard HTML

**Owns:** `atlas_schema.sql`, `netbox_dashboard.html`, `helm/atlas/files/atlas_schema.sql`

```
You are working on the Atlas project at /Atlas/DCT_Scripts/Optic_Count/.

PART A — SCHEMA: Add to atlas_schema.sql after existing netbox ALTER TABLE statements:
    ALTER TABLE netbox_snapshots ADD COLUMN IF NOT EXISTS site_count INTEGER DEFAULT 0;
    ALTER TABLE netbox_snapshots ADD COLUMN IF NOT EXISTS sites_failed INTEGER DEFAULT 0;
    ALTER TABLE netbox_snapshots ADD COLUMN IF NOT EXISTS sites_json JSONB;
Update netbox_latest_snapshot view to include new columns. Sync helm/atlas/files/atlas_schema.sql.

PART B — DASHBOARD HTML:
CRITICAL STEP 1: DELETE the entire first <script> block that defines window.__MOCK and overrides window.fetch.

STEP 2 — SITE DROPDOWN IN HEADER:
<select id="siteSelect" class="btn" style="appearance:auto; padding-right:28px; min-width:200px;">
  <option value="">All Sites</option>
</select>

STEP 3 — WIRE UP SITE SELECTOR:
let currentSite = "";
async function loadSites() { /* fetch /api/dashboard/sites, populate dropdown */ }
document.getElementById("siteSelect").addEventListener("change", (e) => { currentSite = e.target.value; loadAll(); });

STEP 4 — UPDATE fetchJSON TO PASS SITE PARAM:
async function fetchJSON(url, opts) {
  const sep = url.includes("?") ? "&" : "?";
  const fullUrl = currentSite ? url + sep + "site=" + encodeURIComponent(currentSite) : url;
  const r = await fetch(fullUrl, opts);
  if (!r.ok) throw new Error(url + " " + r.status);
  return r.json();
}

STEP 5 — UPDATE loadAll(): call loadSites() only on first load (let sitesLoaded = false guard).

STEP 6 — UPDATE HEADER: "Ellendale NetBox" → "CoreWeave NetBox". Subtitle updates dynamically: "Live infrastructure view · " + (currentSite || "all sites").

STEP 7 — DH CARDS: Remove SITE_LABELS dict. Show location_slug as tag and site slug as meta label.

STEP 8 — RESPONSIVE DH GRID: grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
```

---

## Terminal 4 — Preview + Integration Test Queries

**Owns:** `netbox_dashboard_preview.html` (in `/Atlas/` root)

```
You are working on the Atlas project at /Atlas/.

TASK: Update netbox_dashboard_preview.html to be a multi-site preview with realistic mock data for 5+ sites.

STEP 1 — MULTI-SITE MOCK DATA (5 sites):
- us-central-08a (Heron) — 2 locations (data-hall-202, data-hall-204)
- us-central-08b (Phoenix) — 2 locations (data-hall-201, data-hall-203)
- us-east-01a (Orangeburg) — 3 locations (data-hall-1, data-hall-2, data-hall-3)
- us-west-02a — 2 locations (data-hall-101, data-hall-102)
- eu-west-01a — 1 location (data-hall-001)

STEP 2 — MIRROR SITE DROPDOWN: Same <select id="siteSelect"> UI. Mock data respects currentSite filter.

STEP 3 — KEEP RECONCILIATION TABLE: Label "Preview — Reconciliation (Ellendale only)".

STEP 4 — ADD VERIFICATION SQL (as commented HTML section):
-- Verify all sites ingested
SELECT site, COUNT(DISTINCT location_slug) AS locations, COUNT(*) AS devices
FROM netbox_devices WHERE snapshot_id = (SELECT MAX(id) FROM netbox_snapshots WHERE status='ok')
GROUP BY site ORDER BY site;

-- Verify optic inventory per site
SELECT site, COUNT(*) AS optics FROM netbox_optics
WHERE snapshot_id = (SELECT MAX(id) FROM netbox_snapshots WHERE status='ok')
GROUP BY site ORDER BY site;

-- Snapshot metadata
SELECT id, site_count, sites_failed, device_count, interface_count, optic_count, duration_ms
FROM netbox_snapshots ORDER BY id DESC LIMIT 5;
```

---

## Execution Notes (Part 2)

All 4 terminals can run in parallel. After all complete:
1. `docker compose up -d --build web`
2. Check logs: `docker compose logs -f web` — look for "Discovered N sites with M total locations"
3. If you see "using fallback map" — GraphQL `site_list { locations { slug } }` query shape may differ. Check the actual schema.
4. `inventory_item_list not found` error — NetBox version doesn't expose it. Optic ingest fails but device/interface ingest still works.
5. Hit `/api/dashboard/sites` to verify site discovery
6. Load dashboard, confirm dropdown populates and filtering works
7. Run Terminal 4's verification SQL against the DB

**Risk:** `site_list { locations { slug } }` assumes locations are nested under sites. If NetBox structures this differently, discovery falls back to Ellendale-only. Apply pagination fix from Terminal 1 Addendum before testing.
