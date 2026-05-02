"""
netbox_dashboard_routes.py
==========================
Flask blueprint serving the live NetBox dashboard and its JSON endpoints.
Reads from the latest 'ok' row in netbox_snapshots and joins to the
per-snapshot device + interface tables.

Endpoints
---------
GET  /dashboard                      -> HTML page (Tailwind CDN + Chart.js)
GET  /api/dashboard/summary          -> KPIs for top tiles
GET  /api/dashboard/sites            -> distinct sites in the latest snapshot
GET  /api/dashboard/by-dh            -> per-DH device + optic counts
GET  /api/dashboard/devices          -> device-by-model breakdown
GET  /api/dashboard/optics           -> optic-by-type breakdown
GET  /api/dashboard/optics-inventory -> per-DH/type/rack inventory + utilization
GET  /api/dashboard/snapshots        -> recent snapshot history (10)
POST /api/dashboard/refresh          -> trigger an ingestion run synchronously

All GET endpoints that query devices or interfaces accept an optional
?site=<slug> query parameter to scope results to a single site.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2.extras
from flask import Blueprint, Response, jsonify, request

from atlas_data_loader import managed_connection
import netbox_dashboard_ingest as ingest

log = logging.getLogger(__name__)

netbox_dashboard_bp = Blueprint("netbox_dashboard", __name__)

# Dashboard HTML lives next to this module so it can be edited without
# touching Python. Read once at module import time.
_DASHBOARD_HTML_PATH = Path(__file__).resolve().parent / "netbox_dashboard.html"
try:
    _DASHBOARD_HTML = _DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
except OSError as _err:
    log.exception("Failed to load %s", _DASHBOARD_HTML_PATH)
    _DASHBOARD_HTML = (
        "<!doctype html><meta charset='utf-8'><title>Dashboard error</title>"
        "<pre style='font-family:monospace;color:#f87171;background:#0d1117;"
        "padding:24px;'>Dashboard HTML missing at " + str(_DASHBOARD_HTML_PATH)
        + "</pre>"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _latest_snapshot_row() -> Dict[str, Any] | None:
    sql = """
        SELECT id, started_at, finished_at, status,
               device_count, interface_count, optic_count,
               site_count, sites_failed, duration_ms
        FROM netbox_snapshots
        WHERE status = 'ok'
        ORDER BY finished_at DESC
        LIMIT 1
    """
    with managed_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            row = cur.fetchone()
    return dict(row) if row else None


def _no_data_payload() -> Response:
    return jsonify({"ok": False, "error": "no_snapshot", "message": "No NetBox snapshot yet — wait for the first ingestion."}), 200


def _site_filter() -> Optional[str]:
    """Return the site slug from ?site= query param, or None for all-sites."""
    val = request.args.get("site", "").strip()
    return val if val else None


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@netbox_dashboard_bp.get("/api/dashboard/summary")
def summary():
    snap = _latest_snapshot_row()
    if not snap:
        return _no_data_payload()

    site = _site_filter()

    sql_status = """
        SELECT status, COUNT(*) AS n
        FROM netbox_devices
        WHERE snapshot_id = %s
          AND (%s IS NULL OR site = %s)
        GROUP BY status
    """
    sql_recent = """
        SELECT id, started_at, finished_at, status, device_count, interface_count, duration_ms
        FROM netbox_snapshots
        ORDER BY started_at DESC
        LIMIT 10
    """

    with managed_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql_status, (snap["id"], site, site))
            status_rows = [dict(r) for r in cur.fetchall()]
            cur.execute(sql_recent)
            recent_rows = [dict(r) for r in cur.fetchall()]

    status_counts = {r["status"] or "unknown": r["n"] for r in status_rows}
    healthy = status_counts.get("active", 0) + status_counts.get("provisioned", 0)

    return jsonify({
        "ok": True,
        "snapshot": snap,
        "status_counts": status_counts,
        "healthy_active_count": healthy,
        "recent_snapshots": recent_rows,
    })


@netbox_dashboard_bp.get("/api/dashboard/sites")
def sites_list():
    snap = _latest_snapshot_row()
    if not snap:
        return _no_data_payload()

    sql = """
        SELECT site AS slug,
               COUNT(DISTINCT name) AS device_count,
               COUNT(DISTINCT location_slug) AS location_count
        FROM netbox_devices
        WHERE snapshot_id = %s
        GROUP BY site
        ORDER BY site
    """
    with managed_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (snap["id"],))
            rows = [dict(r) for r in cur.fetchall()]

    return jsonify({"ok": True, "snapshot_id": snap["id"], "sites": rows})


@netbox_dashboard_bp.get("/api/dashboard/by-dh")
def by_dh():
    snap = _latest_snapshot_row()
    if not snap:
        return _no_data_payload()

    site = _site_filter()

    sql_dev = """
        SELECT location_slug AS dh, site, COUNT(*) AS device_count
        FROM netbox_devices
        WHERE snapshot_id = %s
          AND (%s IS NULL OR site = %s)
        GROUP BY location_slug, site
        ORDER BY site, location_slug
    """
    sql_iface = """
        SELECT location_slug AS dh, site, COUNT(*) AS interface_count
        FROM netbox_interfaces
        WHERE snapshot_id = %s
          AND (%s IS NULL OR site = %s)
        GROUP BY location_slug, site
        ORDER BY site, location_slug
    """
    sql_racks = """
        SELECT location_slug AS dh, site, COUNT(DISTINCT rack) AS rack_count
        FROM netbox_devices
        WHERE snapshot_id = %s AND rack IS NOT NULL AND rack <> ''
          AND (%s IS NULL OR site = %s)
        GROUP BY location_slug, site
    """

    with managed_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql_dev, (snap["id"], site, site))
            dev_rows = [dict(r) for r in cur.fetchall()]
            cur.execute(sql_iface, (snap["id"], site, site))
            iface_rows = [dict(r) for r in cur.fetchall()]
            cur.execute(sql_racks, (snap["id"], site, site))
            rack_rows = [dict(r) for r in cur.fetchall()]

    # Key by (site, dh) so the same location slug at two sites does not collide.
    by_dh: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in dev_rows:
        k = (r["site"] or "", r["dh"] or "")
        by_dh.setdefault(
            k,
            {
                "dh": r["dh"],
                "site": r["site"],
                "device_count": 0,
                "interface_count": 0,
                "rack_count": 0,
            },
        )
        by_dh[k]["device_count"] = r["device_count"]
        by_dh[k]["site"] = r["site"]
        by_dh[k]["dh"] = r["dh"]
    for r in iface_rows:
        k = (r["site"] or "", r["dh"] or "")
        by_dh.setdefault(
            k,
            {
                "dh": r["dh"],
                "site": r["site"] or "",
                "device_count": 0,
                "interface_count": 0,
                "rack_count": 0,
            },
        )
        by_dh[k]["interface_count"] = r["interface_count"]
        by_dh[k]["site"] = r["site"] or by_dh[k]["site"]
        by_dh[k]["dh"] = r["dh"] or by_dh[k]["dh"]
    for r in rack_rows:
        k = (r["site"] or "", r["dh"] or "")
        by_dh.setdefault(
            k,
            {
                "dh": r["dh"],
                "site": r["site"] or "",
                "device_count": 0,
                "interface_count": 0,
                "rack_count": 0,
            },
        )
        by_dh[k]["rack_count"] = r["rack_count"]
        by_dh[k]["site"] = r["site"] or by_dh[k]["site"]
        by_dh[k]["dh"] = r["dh"] or by_dh[k]["dh"]

    return jsonify({
        "ok": True,
        "snapshot_id": snap["id"],
        "by_dh": [by_dh[k] for k in sorted(by_dh.keys())],
    })


@netbox_dashboard_bp.get("/api/dashboard/devices")
def devices_breakdown():
    snap = _latest_snapshot_row()
    if not snap:
        return _no_data_payload()

    site = _site_filter()

    sql_overall = """
        SELECT model, COUNT(*) AS n
        FROM netbox_devices
        WHERE snapshot_id = %s
          AND (%s IS NULL OR site = %s)
        GROUP BY model
        ORDER BY n DESC
        LIMIT 20
    """
    sql_per_dh = """
        SELECT location_slug AS dh, model, COUNT(*) AS n
        FROM netbox_devices
        WHERE snapshot_id = %s
          AND (%s IS NULL OR site = %s)
        GROUP BY location_slug, model
        ORDER BY location_slug, n DESC
    """
    with managed_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql_overall, (snap["id"], site, site))
            overall = [dict(r) for r in cur.fetchall()]
            cur.execute(sql_per_dh, (snap["id"], site, site))
            per_dh_rows = [dict(r) for r in cur.fetchall()]

    per_dh: Dict[str, List[Dict[str, Any]]] = {}
    for r in per_dh_rows:
        per_dh.setdefault(r["dh"], []).append({"model": r["model"], "n": r["n"]})

    return jsonify({
        "ok": True,
        "snapshot_id": snap["id"],
        "overall": overall,
        "per_dh": per_dh,
    })


@netbox_dashboard_bp.get("/api/dashboard/optics")
def optics_breakdown():
    snap = _latest_snapshot_row()
    if not snap:
        return _no_data_payload()

    site = _site_filter()

    # Interfaces whose type_category implies a physical transceiver
    _OPT = (
        "(type_category ILIKE '%%optical%%'"
        " OR type_category ILIKE '%%infiniband%%'"
        " OR type_category ILIKE '%%fibre%%')"
    )

    sql_overall = """
        SELECT type_label AS label, type_category AS category, COUNT(*) AS n
        FROM netbox_interfaces
        WHERE snapshot_id = %s
          AND (%s IS NULL OR site = %s)
        GROUP BY type_label, type_category
        ORDER BY n DESC
        LIMIT 25
    """
    sql_per_dh = """
        SELECT location_slug AS dh, type_label AS label, COUNT(*) AS n
        FROM netbox_interfaces
        WHERE snapshot_id = %s
          AND (%s IS NULL OR site = %s)
        GROUP BY location_slug, type_label
        ORDER BY location_slug, n DESC
    """
    sql_categories = """
        SELECT type_category AS category, COUNT(*) AS n
        FROM netbox_interfaces
        WHERE snapshot_id = %s
          AND (%s IS NULL OR site = %s)
        GROUP BY type_category
        ORDER BY n DESC
    """
    # installed_count + port_utilization: total optical ports vs all ports
    sql_port_util = f"""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE {_OPT}) AS used
        FROM netbox_interfaces
        WHERE snapshot_id = %s
          AND (%s IS NULL OR site = %s)
    """
    # by_rack: per-rack optic and port counts for the rack density table
    sql_by_rack = f"""
        SELECT location_slug AS dh, rack,
               COUNT(*) AS port_count,
               COUNT(*) FILTER (WHERE {_OPT}) AS optic_count
        FROM netbox_interfaces
        WHERE snapshot_id = %s AND rack IS NOT NULL AND rack != ''
          AND (%s IS NULL OR site = %s)
        GROUP BY location_slug, rack
        ORDER BY location_slug, rack
    """

    with managed_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql_overall, (snap["id"], site, site))
            overall = [dict(r) for r in cur.fetchall()]
            cur.execute(sql_per_dh, (snap["id"], site, site))
            per_dh_rows = [dict(r) for r in cur.fetchall()]
            cur.execute(sql_categories, (snap["id"], site, site))
            categories = [dict(r) for r in cur.fetchall()]
            cur.execute(sql_port_util, (snap["id"], site, site))
            util_row = dict(cur.fetchone())
            cur.execute(sql_by_rack, (snap["id"], site, site))
            rack_rows = [dict(r) for r in cur.fetchall()]

    per_dh: Dict[str, List[Dict[str, Any]]] = {}
    for r in per_dh_rows:
        per_dh.setdefault(r["dh"], []).append({"label": r["label"], "n": r["n"]})

    return jsonify({
        "ok": True,
        "snapshot_id": snap["id"],
        "overall": overall,
        "per_dh": per_dh,
        "categories": categories,
        "installed_count": util_row["used"],
        "port_utilization": {
            "used": util_row["used"],
            "total": util_row["total"],
        },
        "by_rack": [
            {
                "dh": r["dh"],
                "rack": r["rack"],
                "optic_count": r["optic_count"],
                "port_count": r["port_count"],
            }
            for r in rack_rows
        ],
    })


@netbox_dashboard_bp.get("/api/dashboard/optics-inventory")
def optics_inventory():
    snap = _latest_snapshot_row()
    if not snap:
        return _no_data_payload()

    site = _site_filter()

    # Interfaces whose type_category implies a physical transceiver (not copper/virtual)
    _OPT = (
        "(type_category ILIKE '%%optical%%'"
        " OR type_category ILIKE '%%infiniband%%'"
        " OR type_category ILIKE '%%fibre%%')"
    )

    sql_by_dh = f"""
        SELECT location_slug AS dh, site,
               COUNT(*) AS interfaces,
               COUNT(*) FILTER (WHERE {_OPT}) AS optics
        FROM netbox_interfaces
        WHERE snapshot_id = %s
          AND (%s IS NULL OR site = %s)
        GROUP BY location_slug, site
        ORDER BY location_slug
    """
    sql_dh_type = """
        SELECT location_slug AS dh, type_label AS label, type_category AS category,
               COUNT(*) AS n
        FROM netbox_interfaces
        WHERE snapshot_id = %s
          AND (%s IS NULL OR site = %s)
        GROUP BY location_slug, type_label, type_category
        ORDER BY location_slug, n DESC
    """
    sql_by_type = """
        SELECT type_label AS label, type_category AS category, COUNT(*) AS n
        FROM netbox_interfaces
        WHERE snapshot_id = %s
          AND (%s IS NULL OR site = %s)
        GROUP BY type_label, type_category
        ORDER BY n DESC
    """
    sql_by_rack = f"""
        SELECT location_slug AS dh, rack,
               COUNT(*) AS interfaces,
               COUNT(*) FILTER (WHERE {_OPT}) AS optics
        FROM netbox_interfaces
        WHERE snapshot_id = %s AND rack IS NOT NULL AND rack != ''
          AND (%s IS NULL OR site = %s)
        GROUP BY location_slug, rack
        ORDER BY location_slug, rack
    """

    with managed_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql_by_dh, (snap["id"], site, site))
            dh_rows = [dict(r) for r in cur.fetchall()]
            cur.execute(sql_dh_type, (snap["id"], site, site))
            dh_type_rows = [dict(r) for r in cur.fetchall()]
            cur.execute(sql_by_type, (snap["id"], site, site))
            type_rows = [dict(r) for r in cur.fetchall()]
            cur.execute(sql_by_rack, (snap["id"], site, site))
            rack_rows = [dict(r) for r in cur.fetchall()]

    # Per-DH type detail keyed by DH slug
    dh_types: Dict[str, List[Dict[str, Any]]] = {}
    for r in dh_type_rows:
        dh_types.setdefault(r["dh"], []).append(
            {"label": r["label"], "category": r["category"], "n": r["n"]}
        )

    by_dh = []
    for r in dh_rows:
        ifaces = r["interfaces"] or 0
        optics = r["optics"] or 0
        by_dh.append({
            "dh": r["dh"],
            "site": r["site"],
            "interfaces": ifaces,
            "optics": optics,
            "utilization_pct": round(optics / ifaces * 100, 1) if ifaces else 0.0,
            "by_type": dh_types.get(r["dh"], []),
        })

    total_ifaces = sum(r["interfaces"] for r in by_dh)
    total_optics = sum(r["optics"] for r in by_dh)

    by_rack = []
    for r in rack_rows:
        ifaces = r["interfaces"] or 0
        optics = r["optics"] or 0
        by_rack.append({
            "dh": r["dh"],
            "rack": r["rack"],
            "interfaces": ifaces,
            "optics": optics,
            "utilization_pct": round(optics / ifaces * 100, 1) if ifaces else 0.0,
        })

    return jsonify({
        "ok": True,
        "snapshot_id": snap["id"],
        "totals": {
            "interfaces": total_ifaces,
            "optics": total_optics,
            "utilization_pct": round(total_optics / total_ifaces * 100, 1) if total_ifaces else 0.0,
        },
        "by_dh": by_dh,
        "by_type": [{"label": r["label"], "category": r["category"], "n": r["n"]} for r in type_rows],
        "by_rack": by_rack,
    })


@netbox_dashboard_bp.get("/api/dashboard/snapshots")
def recent_snapshots():
    sql = """
        SELECT id, started_at, finished_at, status,
               device_count, interface_count, duration_ms, error_message
        FROM netbox_snapshots
        ORDER BY started_at DESC
        LIMIT 10
    """
    with managed_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            rows = [dict(r) for r in cur.fetchall()]
    return jsonify({"ok": True, "snapshots": rows})


@netbox_dashboard_bp.post("/api/dashboard/refresh")
def manual_refresh():
    """Trigger a synchronous ingestion. Returns 202 with the result."""
    try:
        result = ingest.ingest_snapshot()
        return jsonify({"ok": True, "result": result}), 202
    except (RuntimeError, OSError) as exc:
        log.exception("Manual NetBox refresh failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

@netbox_dashboard_bp.get("/dashboard")
def dashboard_html():
    return Response(_DASHBOARD_HTML, mimetype="text/html")
