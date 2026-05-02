# NetBox Dashboard — Open Issues & Context

Snapshot of where we left off on 2026-04-27. Hand this to the next session.

## What's working

- Live dashboard at `http://localhost:5050/dashboard`.
- Postgres tables `netbox_snapshots`, `netbox_devices`, `netbox_interfaces` populated.
- APScheduler runs ingestion every 15 min. `POST /api/dashboard/refresh` for manual.
- GraphQL filters by `site.slug` (not name — names are uppercase, slugs lowercase).
- Slug-to-enum normalizer handles NetBox 4 kebab-case interface types
  (`1000base-t` → `TYPE_1000BASE_T`), so optic categories + labels render correctly.
- 502 retries with exponential backoff (4 attempts) for transient gateway errors.
- Devices and interfaces queried separately to dodge the heavy-combined-payload 502.
- Times displayed in Central Time (CDT/CST auto-switch via `Intl`).
- Hand-written CSS, no Tailwind CDN warning.

## ~~Open issue 1 (HIGH): `in_list` only matches first slug per site~~ — RESOLVED 2026-04-27

**Resolution:** went with option 1 (per-DH loop). Dropped the `in_list` filter
entirely. `_build_devices_query` and `_build_interfaces_query` now take a
single `location_slug` and use `slug: { exact: "..." }`. `_query_dh_inventory`
loops the slugs and accumulates rows. 8 small calls instead of 4 combined,
each wrapped in the existing retry/backoff. Per-DH device/interface counts
log as we go so a partial failure is easy to spot.

**Verify after deploy:**

```sql
SELECT location_slug, site, COUNT(*) AS devices
FROM netbox_devices
WHERE snapshot_id = (SELECT MAX(id) FROM netbox_snapshots WHERE status='ok')
GROUP BY 1, 2 ORDER BY 1;
```

Should now show all four rows (data-hall-201 through data-hall-204).

**Original symptom (kept for context):** snapshot returned devices for DH201 +
DH202 only, not DH203/DH204. NetBox 4's `in_list` filter on nested
location.slug accepted the syntax but only matched the first slug. Switching
to per-slug `exact` sidesteps it.

## Open issue 2 (LOW): print/PDF layout collapses to single column

**Symptom:** when exporting to PDF or printing, the dashboard renders as a
single-column stack instead of the multi-column desktop layout. Slack uploads
would look the same.

**Cause:** print/PDF uses a virtual viewport ~700px wide, which trips the
mobile breakpoint at `min-width: 768px` in `netbox_dashboard.html`.

**Fix:** add a print stylesheet that pins the desktop layout regardless of
width. Drop into the `<style>` block in `netbox_dashboard.html`:

```css
@media print {
  .grid-4 { grid-template-columns: repeat(4, 1fr) !important; }
  .grid-3 { grid-template-columns: repeat(3, 1fr) !important; }
  .col-span-2 { grid-column: span 2 !important; }
  body { background: #07090d !important; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  .site-header { position: static !important; }
}
```

Five-minute change.

## Architectural note: why interface count ≈ device count

NetBox at Coreweave appears to track **management interfaces and switch
uplinks only**, not the full data hall fabric. The 7,820 interfaces vs 7,858
devices (~1:1) is consistent with that — most active devices have a single
mgmt port modeled, plus a smaller number of switches with 100G/400G uplinks.

Where the rest lives: cutsheet pipeline (the existing Atlas project tables).
The dashboard is correctly showing what NetBox knows; deep optic/cable
inventory belongs in the cutsheet flow, not here.

This is not a bug to fix, just a fact to communicate to stakeholders if they
ask "why so few optics?"

## Slack/sharing follow-ups

When ready to share more broadly:

1. Deploy on GCP Cloud Run with VPC connector so it can reach NetBox. Behind
   IAP for SSO. Internal team gets a real URL.
2. Add a Slack notifier (small `notify.py` cron job) that POSTs the latest
   snapshot summary to a channel daily. Endpoints already in place:
   `/api/dashboard/summary`, `/api/dashboard/snapshots`.
3. Once on Cloud Run, wire a webhook that pings Slack `:warning:` if two
   consecutive snapshots fail.

## Files touched in this session

```
DCT_Scripts/Optic_Count/atlas_schema.sql           # added 3 tables + view
DCT_Scripts/Optic_Count/atlas_web_app.py           # blueprint, scheduler, CSP
DCT_Scripts/Optic_Count/docker-compose.yml         # new env vars
DCT_Scripts/Optic_Count/Dockerfile                 # COPY netbox_dashboard.html
DCT_Scripts/Optic_Count/requirements.txt           # APScheduler
DCT_Scripts/Optic_Count/netbox_dashboard_ingest.py # NEW
DCT_Scripts/Optic_Count/netbox_dashboard_routes.py # NEW
DCT_Scripts/Optic_Count/netbox_dashboard.html      # NEW
```

Memory updated:
```
.auto-memory/reference_netbox_ellendale.md  # corrected slugs (data-hall-N not dhN)
```

Nothing committed yet. When the `in_list` fix lands and you've verified all
4 DHs return data, commit with something like:

```
git add DCT_Scripts/Optic_Count/atlas_schema.sql \
        DCT_Scripts/Optic_Count/atlas_web_app.py \
        DCT_Scripts/Optic_Count/docker-compose.yml \
        DCT_Scripts/Optic_Count/Dockerfile \
        DCT_Scripts/Optic_Count/requirements.txt \
        DCT_Scripts/Optic_Count/netbox_dashboard_ingest.py \
        DCT_Scripts/Optic_Count/netbox_dashboard_routes.py \
        DCT_Scripts/Optic_Count/netbox_dashboard.html \
        DCT_Scripts/Optic_Count/netbox_issues.md
git commit -m "Add live NetBox dashboard for DH 201-204 (Heron + Phoenix)"
git push origin lamars-branch
```
