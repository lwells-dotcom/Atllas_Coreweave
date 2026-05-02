"""
atlas_web_app.py — MOCKUP
Full web version of the Atlas desktop app (Optic_Count_GUI.py).
Adds NetBox streaming (SSE) on top of the existing demo_web_app.py foundation.

To run:
    pip install flask
    python atlas_web_app.py

Desktop app is unchanged — still launch via Optic_Count_GUI_Main.py as before.
"""

import json
import logging
import os
import queue
import re
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import io
from flask import Flask, Response, jsonify, request, send_file, stream_with_context
from werkzeug.utils import secure_filename
import openpyxl.utils.exceptions

import Define_Optic_Count
import Source_count_Netbox
import demo_auth_ai
import build_sheet_processor
import cutsheet_preprocessor
import netbox_dashboard_ingest
from netbox_dashboard_routes import netbox_dashboard_bp

log = logging.getLogger(__name__)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB

_DASHBOARD_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data:; "
    "connect-src 'self'"
)
_DEFAULT_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'"
)


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # The /dashboard route loads Tailwind, Chart.js, and Google Fonts from CDNs.
    # All other routes keep the stricter default CSP.
    if request.path == "/dashboard":
        response.headers["Content-Security-Policy"] = _DASHBOARD_CSP
    else:
        response.headers["Content-Security-Policy"] = _DEFAULT_CSP
    return response


# Register the NetBox dashboard blueprint
app.register_blueprint(netbox_dashboard_bp)

UPLOAD_DIR = Path(os.getenv("ATLAS_UPLOAD_DIR", "./uploads"))
UPLOAD_DIR.mkdir(exist_ok=True)

# Server-side session store (keyed by demo token subject)
USER_CONTEXT = {}
USER_SITE = {}  # username -> {"site_code": str, "site_id": int, "upload_id": int}
AUDIT_LOG = []
_state_lock = threading.Lock()

_CONTEXT_TTL_SECONDS = 2 * 60 * 60  # 2 hours
_AUDIT_LOG_MAX = 1000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bearer(auth_header):
    if not auth_header:
        return None
    parts = auth_header.split(" ", 1)
    return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None


def _evict_stale_contexts():
    with _state_lock:
        cutoff = time.time() - _CONTEXT_TTL_SECONDS
        stale = [u for u, ctx in USER_CONTEXT.items() if ctx.get("ts", 0) < cutoff]
        for u in stale:
            del USER_CONTEXT[u]


def _audit(event, user, details):
    with _state_lock:
        AUDIT_LOG.append({"event": event, "user": user, "details": details, "ts": int(time.time())})
        if len(AUDIT_LOG) > _AUDIT_LOG_MAX:
            del AUDIT_LOG[:-_AUDIT_LOG_MAX]


_RATE_LIMIT_STORE: dict = {}
_RATE_LIMIT_MAX = 10
_RATE_LIMIT_WINDOW = 60
_RATE_LIMIT_MAX_KEYS = 5000


def _get_client_ip() -> str:
    # Trust only the direct connection — X-Forwarded-For is trivially spoofable.
    # Add a TRUSTED_PROXIES allowlist if a reverse proxy is deployed in front.
    return request.remote_addr or "unknown"


def _check_rate_limit(key: str) -> bool:
    now = time.monotonic()
    with _state_lock:
        if len(_RATE_LIMIT_STORE) > _RATE_LIMIT_MAX_KEYS:
            expired = [k for k, (_, ws) in _RATE_LIMIT_STORE.items()
                       if now - ws > _RATE_LIMIT_WINDOW][:100]
            for k in expired:
                del _RATE_LIMIT_STORE[k]
        count, window_start = _RATE_LIMIT_STORE.get(key, (0, now))
        if now - window_start > _RATE_LIMIT_WINDOW:
            _RATE_LIMIT_STORE[key] = (1, now)
            return True
        if count >= _RATE_LIMIT_MAX:
            return False
        _RATE_LIMIT_STORE[key] = (count + 1, window_start)
    return True


def _normalize_rack_id(rack: str) -> str:
    rack = (rack or "").strip()
    if rack.isdigit():
        return rack.zfill(3)
    return rack.lower()


def _rack_location_key(room: str, rack: str) -> str:
    room_norm = (room or "").strip().lower()
    rack_norm = _normalize_rack_id(rack)
    return f"{room_norm}:{rack_norm}" if room_norm and rack_norm else ""


def _question_matches_rack_result(question: str, rack_result: dict) -> bool:
    """Check whether a question appears to target the cached rack-analysis result."""
    try:
        import query_extractors as ext
    except Exception:
        return False

    lower = (question or "").lower()
    room = (rack_result.get("room") or "").strip().lower()
    rack = _normalize_rack_id(rack_result.get("rack") or "")
    if not room or not rack:
        return False

    extracted = (ext.extract_location(question) or "").strip().lower()
    if extracted:
        if extracted == f"{room}:{rack}":
            return True
        # Exact hall variants like dh202:041 should still match a DH2 rack-analysis result.
        if extracted.endswith(f":{rack}") and room.startswith("dh") and extracted.startswith(room):
            return True

    rack_tokens = {rack, rack.lstrip("0") or "0"}
    if "rack" in lower or "cab" in lower or "cabinet" in lower:
        if room in lower and any(re.search(rf"\b{re.escape(tok)}\b", lower) for tok in rack_tokens):
            return True

    return False


def _build_rack_context_for_llm(rack_result: dict) -> dict:
    """Convert Rack Analyzer output into compact preformatted context for the LLM."""
    room = rack_result.get("room", "")
    rack = rack_result.get("rack", "")
    location_key = _rack_location_key(room, rack)
    devices = rack_result.get("devices") or []
    optics = rack_result.get("optic_summary") or {}
    internal_labels = rack_result.get("internal_labels") or []
    cab_labels = rack_result.get("cab_to_cab_labels") or []

    lines = [
        f"Rack Analyzer result for {room} rack {rack}",
        f"Rack key: {location_key}" if location_key else "Rack key: unknown",
        (
            f"Total cables touching this rack: {rack_result.get('total_cables', 0)} | "
            f"Staying inside rack: {rack_result.get('internal_count', 0)} | "
            f"Leaving rack: {rack_result.get('cab_to_cab_count', 0)}"
        ),
    ]
    if rack_result.get("cab_type"):
        lines.append(f"Cab type: {rack_result['cab_type']}")

    lines.append("Devices Physically in Rack:")
    if devices:
        for dev in devices:
            lines.append(
                f"  RU {dev.get('ru') or '?'} | {dev.get('location') or '?'} | "
                f"{dev.get('dns_name') or '(no dns)'} | {dev.get('model') or '(no model)'} | "
                f"{dev.get('status') or '(no status)'}"
            )
    else:
        lines.append("  (none)")

    lines.append("Optic Summary:")
    if optics:
        for optic, count in optics.items():
            lines.append(f"  {optic}: {count}")
    else:
        lines.append("  (none)")

    lines.append("Cables Staying Inside This Rack:")
    if internal_labels:
        for label in internal_labels:
            lines.append(f"  {label}")
    else:
        lines.append("  (none)")

    lines.append("Cables Leaving This Rack:")
    if cab_labels:
        for label in cab_labels:
            lines.append(f"  {label}")
    else:
        lines.append("  (none)")

    context_text = "\n".join(lines)
    return {
        "source": "RACK_ANALYZER",
        "question_type": "rack_summary",
        "confidence": "high",
        "classification_reason": "cached Rack Analyzer workbook result",
        "room": room,
        "rack": rack,
        "location_key": location_key,
        "context": context_text,
        "row_count": len(devices) + len(internal_labels) + len(cab_labels),
        "query_elapsed_seconds": 0.0,
        "token_estimate": len(context_text.split()),
    }


def _sse_stream(target_fn, *args, **kwargs):
    """
    Run target_fn(*args, output_queue) in a background thread and yield
    its output as Server-Sent Events.  target_fn must signal completion
    by putting None into the queue (same contract as the desktop version).
    """
    q = queue.Queue(maxsize=500)
    threading.Thread(target=target_fn, args=(*args, q), kwargs=kwargs, daemon=True).start()

    def generate():
        while True:
            try:
                msg = q.get(timeout=60)
            except queue.Empty:
                break
            if msg is None:
                yield "data: [DONE]\n\n"
                break
            yield f"data: {json.dumps(msg)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Site code extraction (mirrors demo_web_app logic)
# ---------------------------------------------------------------------------

_KNOWN_SITES = {
    "QCY": "QCY", "QUINCY": "QCY",
    "ELD": "ELD", "ELLENDALE": "ELD",
    "DTN": "DTN", "DALTON": "DTN",
    "AUS": "AUS", "AUSTIN": "AUS",
    "PHX": "PHX", "PHOENIX": "PHX",
    "DFW": "DFW", "DALLAS": "DFW",
    "ORD": "ORD", "CHICAGO": "ORD",
    "IAD": "IAD", "ASHBURN": "IAD",
    "PDX": "PDX", "PORTLAND": "PDX",
    "SEA": "SEA", "SEATTLE": "SEA",
}
_LOCODE_RE = re.compile(r"\b([A-Z]{2}-[A-Z0-9]{3,6})\b", re.I)


def _extract_site_code(save_path, prebuilt=None):
    """Extract site code from prebuilt context, SITE-VARS sheet, or filename."""
    if prebuilt:
        qr = prebuilt.get("quick_reference", {})
        site = qr.get("Site code?", "") or qr.get("site_code", "")
        if site and site.upper() != "UNKNOWN":
            return site.upper()
    if str(save_path).lower().endswith(".xlsx"):
        try:
            import pandas as pd
            xls = pd.ExcelFile(str(save_path), engine="calamine")
            for sn in xls.sheet_names:
                if sn.strip().casefold() in ("site-vars", "site_vars", "site vars", "sitevars"):
                    sv = pd.read_excel(xls, sheet_name=sn, header=None, engine="calamine")
                    for _, row in sv.iterrows():
                        key = str(row.iloc[0]).strip().lower() if len(row) > 0 else ""
                        val = str(row.iloc[1]).strip() if len(row) > 1 else ""
                        if key in ("site_code", "site code", "site", "locode") and val:
                            return val.upper()
        except Exception:
            pass
    filename = Path(str(save_path)).stem.upper()
    for token, code in _KNOWN_SITES.items():
        if token in filename:
            return code
    m = _LOCODE_RE.search(filename)
    if m:
        return m.group(1).upper()
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Health (liveness / readiness probe target)
# ---------------------------------------------------------------------------

_pg_cache: dict = {"ok": False, "ts": 0.0}
_PG_CACHE_TTL = 10.0  # seconds


def _check_postgres() -> bool:
    """Return True if Postgres is reachable. Cached for _PG_CACHE_TTL seconds
    so probe storms don't hammer the DB."""
    now = time.monotonic()
    if now - _pg_cache["ts"] < _PG_CACHE_TTL:
        return _pg_cache["ok"]
    try:
        from atlas_data_loader import check_postgres
        result = bool(check_postgres())
    except Exception:
        result = False
    _pg_cache["ok"] = result
    _pg_cache["ts"] = now
    return result


def _run_postgres_upload_job(username: str, save_path_str: str, site_code: str, gen: int) -> None:
    """Run atlas_data_loader.load_file after /api/upload-count returned (background)."""
    pg_result = None
    err_msg = None
    try:
        import atlas_data_loader

        pg_result = atlas_data_loader.load_file(
            save_path_str, site_code, uploaded_by=username
        )
    except Exception as exc:  # noqa: BLE001
        err_msg = str(exc)
        log.exception("Background Postgres load failed for user=%s", username)

    with _state_lock:
        ctx = USER_CONTEXT.get(username)
        if not ctx or ctx.get("_pg_import_gen") != gen:
            log.info(
                "Discarding pg upload result (stale or missing context) user=%s",
                username,
            )
            return
        ctx.pop("_postgres_import_pending", None)
        ctx.pop("_pg_import_gen", None)
        if err_msg:
            ctx["_postgres_import_error"] = err_msg[:800]
        elif pg_result and not pg_result.get("ok"):
            ctx["_postgres_import_error"] = str(pg_result.get("error") or "load_failed")[:800]
        else:
            ctx.pop("_postgres_import_error", None)
        USER_CONTEXT[username] = ctx

        if pg_result and pg_result.get("ok"):
            uid = (
                pg_result.get("upload_id")
                if not pg_result.get("skipped")
                else pg_result.get("existing_upload_id")
            )
            sid = pg_result.get("site_id")
            if sid is not None and uid is not None:
                USER_SITE[username] = {
                    "site_code": site_code,
                    "site_id": sid,
                    "upload_id": uid,
                }
                log.info(
                    "Postgres load OK (async): site=%s upload_id=%s rows=%s",
                    site_code,
                    uid,
                    pg_result.get("connections_loaded"),
                )


@app.get("/api/health")
def health():
    """Kubernetes probe endpoint. Always returns 200 so a transient DB blip
    doesn't take the pod down; surface the DB state in the payload so ops
    can see it but liveness stays up as long as Flask is serving."""
    return jsonify({"ok": True, "postgres": _check_postgres()})


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.post("/api/verify-pin")
def verify_pin():
    if not _check_rate_limit(_get_client_ip()):
        return jsonify({"ok": False, "error": "Too many attempts. Try again later."}), 429

    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "demo_user").strip()
    pin = (payload.get("pin") or "").strip()

    if not demo_auth_ai.verify_demo_pin(pin):
        _audit("verify_failed", username, {"reason": "invalid_pin"})
        return jsonify({"ok": False, "error": "Invalid PIN"}), 401

    token = demo_auth_ai.create_demo_token(username)
    _audit("verify_success", username, {})
    return jsonify({"ok": True, "token": token})


# ---------------------------------------------------------------------------
# Sheet upload + count
# ---------------------------------------------------------------------------

@app.post("/api/upload-count")
def upload_count():
    token = _bearer(request.headers.get("Authorization"))
    if not token:
        return jsonify({"error": "Missing bearer token"}), 401
    try:
        claims = demo_auth_ai.parse_and_validate_demo_token(token)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 401

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".xlsx"):
        return jsonify({"error": "Only .xlsx files are supported"}), 400

    safe_name = secure_filename(f.filename)
    if not safe_name:
        return jsonify({"error": "Invalid filename"}), 400
    unique_name = f"{int(time.time())}_{claims['sub']}_{safe_name}"
    save_path = UPLOAD_DIR / unique_name
    f.save(save_path)

    include_by_status = request.form.get("include_by_status", "").strip().lower() in (
        "1", "true", "yes", "on",
    )

    # Site code is derivable from filename/SITE-VARS before the full parse, so
    # extract it now. Run preprocessor *before* Postgres so bad sheets fail fast
    # and the browser is not stuck behind a long DB ingest with no response.
    site_code = _extract_site_code(save_path)

    pg_available = _check_postgres()

    try:
        # ── Preprocessor first (calamine): normalize, strip section headers ──
        try:
            prep = cutsheet_preprocessor.preprocess_upload(str(save_path))
            result_text = cutsheet_preprocessor.format_optic_count_text(prep["optic_counts"])

            if prep["unknown_statuses"]:
                unknowns = ", ".join(f"{v} ({c})" for v, c in prep["unknown_statuses"])
                result_text += f"\n\nWarning: {len(prep['unknown_statuses'])} unknown status values: {unknowns}"
        except Exception as prep_exc:
            log.warning("Preprocessor failed, falling back to legacy path: %s", prep_exc)
            result_text = Define_Optic_Count.count_all_files_gui([str(save_path)])

        if pg_available:
            context = {"files": [{"file_name": safe_name, "file_path": str(save_path)}], "ts": time.time()}
        else:
            _, context = Define_Optic_Count.count_and_build_context([str(save_path)])
            context["ts"] = time.time()

        # Optional: same request as upload — avoids a second round-trip and
        # "No file loaded" if the first request timed out before context was set.
        if include_by_status:
            try:
                status_block = Define_Optic_Count.count_all_files_gui_by_status(
                    [str(save_path)]
                )
                result_text = result_text + "\n\n" + ("=" * 72) + "\n\n" + status_block
            except Exception as status_exc:  # noqa: BLE001
                log.warning("include_by_status block failed: %s", status_exc)
                result_text += (
                    f"\n\n(Warning: in-service sort failed: {status_exc})\n"
                )
    except Exception:
        log.exception("File upload processing failed")
        return jsonify({"error": "File processing failed"}), 500
    finally:
        Define_Optic_Count.clear_excel_cache()

    # Return JSON as soon as counting is done; Postgres ingest can take minutes
    # on large workbooks and was blocking the browser for 90s+.
    pg_import_gen = int(time.time() * 1000) & 0x7FFFFFFF
    if pg_available:
        context["_postgres_import_pending"] = True
        context["_pg_import_gen"] = pg_import_gen

    _evict_stale_contexts()
    with _state_lock:
        USER_CONTEXT[claims["sub"]] = context

    if pg_available:
        threading.Thread(
            target=_run_postgres_upload_job,
            args=(claims["sub"], str(save_path), site_code, pg_import_gen),
            name="atlas-pg-upload",
            daemon=True,
        ).start()

    _audit("upload_count", claims["sub"], {"file": safe_name})
    if not pg_available:
        pg_status = "skipped"
    else:
        pg_status = "pending"
    resp = {
        "ok": True,
        "file": safe_name,
        "output": result_text,
        "pg_loaded": pg_status,
    }
    if pg_available:
        resp["pg_message"] = (
            "Saving to the database in the background — counts above are ready now."
        )
    return jsonify(resp)


@app.post("/api/count-by-status")
def count_by_status():
    token = _bearer(request.headers.get("Authorization"))
    if not token:
        return jsonify({"error": "Missing bearer token"}), 401
    try:
        claims = demo_auth_ai.parse_and_validate_demo_token(token)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 401

    if claims["sub"] not in USER_CONTEXT:
        return jsonify({"error": "No file loaded — upload first"}), 400

    # Re-run against the saved file path stored in context
    files = [f["file_path"] for f in USER_CONTEXT[claims["sub"]].get("files", [])]
    try:
        result_text = Define_Optic_Count.count_all_files_gui_by_status(files)
    except (FileNotFoundError, OSError):
        return jsonify({"error": "File no longer available — re-upload to refresh"}), 400
    return jsonify({"ok": True, "output": result_text})


# ---------------------------------------------------------------------------
# NetBox — SSE streaming endpoints
# The queue-based contract in Source_count_Netbox is reused unchanged.
# ---------------------------------------------------------------------------

@app.get("/api/stream/netbox")
def stream_netbox():
    """Single-site NetBox inventory — streams live as results arrive."""
    site_name = request.args.get("site", "").strip() or "us-west-09a"
    active_only = request.args.get("active_only", "true").lower() != "false"
    include_optic_locations = request.args.get("include_optic_locations", "false").lower() == "true"
    return _sse_stream(Source_count_Netbox.get_site_inventory, site_name, active_only=active_only, include_optic_locations=include_optic_locations)


@app.get("/api/stream/all-sites")
def stream_all_sites():
    """All-sites NetBox inventory — streams per-site progress live."""
    active_only = request.args.get("active_only", "true").lower() != "false"
    include_optic_locations = request.args.get("include_optic_locations", "false").lower() == "true"
    return _sse_stream(Source_count_Netbox.get_all_sites_inventory, active_only=active_only, include_optic_locations=include_optic_locations)


# ---------------------------------------------------------------------------
# Rack Analyzer
# ---------------------------------------------------------------------------

@app.post("/api/buildsheet")
def buildsheet():
    token = _bearer(request.headers.get("Authorization"))
    claims = None
    if token:
        try:
            claims = demo_auth_ai.parse_and_validate_demo_token(token)
        except Exception:
            claims = None

    if "cutsheet" not in request.files:
        return jsonify({"error": "'cutsheet' file is required"}), 400

    room = request.form.get("room", "").strip()
    rack = request.form.get("rack", "").strip()
    if not room or not rack:
        return jsonify({"error": "room and rack are required"}), 400

    cutsheet_file = request.files["cutsheet"]
    template_file = request.files.get("template")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp_cut:
        cutsheet_file.save(tmp_cut.name)
        cut_path = tmp_cut.name

    tpl_path = None
    if template_file:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp_tpl:
            template_file.save(tmp_tpl.name)
            tpl_path = tmp_tpl.name

    try:
        result = build_sheet_processor.process_rack(cut_path, tpl_path, room, rack)
    except ValueError:
        return jsonify({"error": "Invalid input parameters"}), 400
    except (FileNotFoundError, OSError, openpyxl.utils.exceptions.InvalidFileException, zipfile.BadZipFile):
        log.exception("Rack sheet generation failed")
        return jsonify({"error": "File processing failed"}), 500
    finally:
        try:
            os.unlink(cut_path)
            if tpl_path:
                os.unlink(tpl_path)
        except OSError:
            pass

    if claims:
        _evict_stale_contexts()
        with _state_lock:
            user_ctx = USER_CONTEXT.get(claims["sub"], {"summary": {}, "files": []})
            user_ctx["ts"] = time.time()
            user_ctx["_last_rack_result"] = result
            USER_CONTEXT[claims["sub"]] = user_ctx
        _audit(
            "buildsheet",
            claims["sub"],
            {"room": room, "rack": rack, "total_cables": result.get("total_cables", 0)},
        )

    return jsonify({"ok": True, "data": result})


@app.post("/api/buildsheet/layout")
def buildsheet_layout():
    if "cutsheet" not in request.files:
        return jsonify({"error": "'cutsheet' file is required"}), 400
    room = request.form.get("room", "").strip()
    if not room:
        return jsonify({"error": "room is required"}), 400

    cutsheet_file = request.files["cutsheet"]
    template_file = request.files.get("template")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp_cut:
        cutsheet_file.save(tmp_cut.name)
        cut_path = tmp_cut.name

    tpl_path = None
    if template_file:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp_tpl:
            template_file.save(tmp_tpl.name)
            tpl_path = tmp_tpl.name

    try:
        excel_bytes = build_sheet_processor.generate_layout_workbook(cut_path, tpl_path, room)
    except ValueError:
        return jsonify({"error": "Invalid input parameters"}), 400
    except Exception:
        log.exception("Layout workbook generation failed")
        return jsonify({"error": "File processing failed"}), 500
    finally:
        try:
            os.unlink(cut_path)
            if tpl_path:
                os.unlink(tpl_path)
        except OSError:
            pass

    return send_file(
        io.BytesIO(excel_bytes),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"layout_{room.upper()}.xlsx",
    )


@app.post("/api/buildsheet/dh")
def buildsheet_dh():
    if "cutsheet" not in request.files:
        return jsonify({"error": "'cutsheet' file is required"}), 400

    room = request.form.get("room", "").strip()
    if not room:
        return jsonify({"error": "room is required"}), 400

    cutsheet_file = request.files["cutsheet"]
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp_cut:
        cutsheet_file.save(tmp_cut.name)
        cut_path = tmp_cut.name

    try:
        result = build_sheet_processor.process_room(cut_path, room)
    except ValueError:
        return jsonify({"error": "Invalid input parameters"}), 400
    except (FileNotFoundError, OSError, openpyxl.utils.exceptions.InvalidFileException, zipfile.BadZipFile):
        log.exception("Room sheet generation failed")
        return jsonify({"error": "File processing failed"}), 500
    finally:
        try:
            os.unlink(cut_path)
        except OSError:
            pass

    return jsonify({"ok": True, "data": result})


# ---------------------------------------------------------------------------
# AI Q&A
# ---------------------------------------------------------------------------

def _get_latest_upload_for_user(conn, username):
    """Return {site_code, site_id, upload_id} for the user's latest active upload, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT s.id AS site_id, s.site_code, cu.id AS upload_id
               FROM cutsheet_uploads cu
               JOIN sites s ON cu.site_id = s.id
               WHERE cu.uploaded_by = %s AND cu.is_active = TRUE
               ORDER BY cu.created_at DESC LIMIT 1""",
            (username,),
        )
        row = cur.fetchone()
    if row:
        return {"site_code": row[1], "site_id": row[0], "upload_id": row[2]}
    return None


@app.post("/api/ask")
def ask_ai():
    token = _bearer(request.headers.get("Authorization"))
    if not token:
        return jsonify({"error": "Missing bearer token"}), 401
    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400
    try:
        claims = demo_auth_ai.parse_and_validate_demo_token(token)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 401

    username = claims["sub"]

    # --- If a cutsheet upload just started Postgres ingest, wait (bounded) ---
    with _state_lock:
        _pend_ctx = USER_CONTEXT.get(username)
    if _pend_ctx and _pend_ctx.get("_postgres_import_pending"):
        _deadline = time.monotonic() + 120.0
        while time.monotonic() < _deadline:
            time.sleep(0.25)
            with _state_lock:
                if not USER_CONTEXT.get(username, {}).get("_postgres_import_pending"):
                    break
        with _state_lock:
            if USER_CONTEXT.get(username, {}).get("_postgres_import_pending"):
                return jsonify(
                    {
                        "error": (
                            "Database import still running (large file). "
                            "Wait up to two minutes and try again."
                        ),
                        "detail": "pending_postgres_import",
                    }
                ), 503

    # --- Try Postgres context first ---
    pg_fallback_reason = None

    with _state_lock:
        site_info = USER_SITE.get(username)

    if not site_info and not _check_postgres():
        pg_fallback_reason = "postgres_unreachable"

    # If no in-memory site info, try recovering from Postgres
    if not site_info and _check_postgres():
        try:
            from atlas_data_loader import managed_connection
            with managed_connection() as conn:
                recovered = _get_latest_upload_for_user(conn, username)
            if recovered:
                site_info = recovered
                with _state_lock:
                    USER_SITE[username] = site_info
                log.info("Recovered site context from Postgres for user=%s", username)
        except Exception as exc:
            log.warning("Postgres site recovery failed: %s", exc)

    if not site_info and pg_fallback_reason is None:
        pg_fallback_reason = "no_active_upload_for_user"

    pg_context = None
    if site_info and _check_postgres():
        try:
            from atlas_postgres_context import build_postgres_context
            _t0 = time.monotonic()
            pg_context = build_postgres_context(
                question, site_info["site_id"],
                upload_id=site_info.get("upload_id"),
            )
            _elapsed = time.monotonic() - _t0
            if _elapsed > 30:
                log.warning(
                    "build_postgres_context took %.1fs (>30s threshold) for user=%s",
                    _elapsed, username,
                )
            if pg_context and "error" not in pg_context:
                if site_info and site_info.get("site_code"):
                    pg_context["site_code"] = site_info["site_code"]
                log.info(
                    "Postgres context: type=%s tokens=%s elapsed=%ss",
                    pg_context.get("question_type"),
                    pg_context.get("token_estimate"),
                    pg_context.get("query_elapsed_seconds"),
                )
        except Exception as exc:
            log.warning("Postgres context build failed (falling back): %s", exc)
            pg_context = None
            pg_fallback_reason = f"build_failed: {exc}"

    if pg_context and "error" in pg_context:
        pg_fallback_reason = f"context_error: {pg_context['error']}"
        pg_context = None

    # Build sheet context: try in-memory, fall back to Postgres
    with _state_lock:
        sheet_context = USER_CONTEXT.get(username)

    if not sheet_context and not pg_context:
        return jsonify({"error": "No sheet loaded — upload a file first"}), 400

    if not sheet_context:
        sheet_context = {"summary": {}, "files": [], "ts": time.time()}

    rack_ctx = None
    if sheet_context.get("_last_rack_result") and _question_matches_rack_result(question, sheet_context["_last_rack_result"]):
        rack_ctx = _build_rack_context_for_llm(sheet_context["_last_rack_result"])
        # Prefer the cached Rack Analyzer when:
        # (a) Postgres returned nothing or low confidence, OR
        # (b) Postgres returned a generic rack_summary (all racks) but the
        #     cached result has specific per-rack data (devices, optics, cables)
        pg_is_generic_rack = (
            pg_context
            and pg_context.get("question_type") == "rack_summary"
            and pg_context.get("row_count", 0) > 1
        )
        rack_has_detail = rack_ctx.get("row_count", 0) > 0
        if (not pg_context
                or pg_context.get("row_count", 0) == 0
                or pg_context.get("confidence") == "low"
                or (pg_is_generic_rack and rack_has_detail)):
            with _state_lock:
                sheet_context["_active_rack_context"] = rack_ctx
            pg_context = None
            log.info(
                "Using cached Rack Analyzer context for user=%s room=%s rack=%s",
                username,
                rack_ctx.get("room"),
                rack_ctx.get("rack"),
            )

    if pg_context and "error" not in pg_context:
        with _state_lock:
            sheet_context["_postgres_context"] = pg_context

    result = demo_auth_ai.qa_with_token(token, question, sheet_context)

    # Add Postgres metadata to response
    resp = {"ok": True, "result": result}

    # Determine context source and add diagnostic fields
    if pg_context and "error" not in pg_context:
        resp["context_source"] = "POSTGRES"
        resp["question_type"] = pg_context.get("question_type", "")
        resp["upload_id"] = site_info.get("upload_id") if site_info else None
        resp["row_count"] = pg_context.get("row_count", 0)
        resp["classification_confidence"] = pg_context.get("confidence")
        resp["classification_reason"] = pg_context.get("classification_reason")
    elif rack_ctx:
        resp["context_source"] = "RACK_ANALYZER"
        resp["question_type"] = rack_ctx.get("question_type", "")
    elif sheet_context.get("summary") or sheet_context.get("files"):
        resp["context_source"] = "IN_MEMORY"
    else:
        resp["context_source"] = "EMPTY_FALLBACK"

    if resp["context_source"] != "POSTGRES" and pg_fallback_reason:
        resp["pg_fallback_reason"] = pg_fallback_reason

    _audit("ask_ai", username, {"question": question})
    return jsonify(resp)


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return HTML_PAGE


HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🔬</text></svg>"/>
  <title>Atlas — DCT Infrastructure Intelligence</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: Arial, sans-serif; max-width: 1000px; margin: 24px auto; padding: 0 12px; background: #F9FAFC; color: #343338; }
    h1 { font-size: 1.4rem; margin-bottom: 4px; color: #343338; }
    h3 { margin: 0 0 10px 0; font-size: 1rem; color: #343338; }
    .section { border: 1px solid #CDCED6; border-radius: 6px; padding: 14px; margin-bottom: 14px; background: #fff; }
    input[type=text], input[type=password], input[type=file] { width: 100%; padding: 7px; margin: 4px 0 8px 0; border: 1px solid #CDCED6; border-radius: 4px; }
    input[type=text]:focus, input[type=password]:focus { outline: none; border-color: #2741E7; box-shadow: 0 0 0 2px #DAE5FF; }
    .btn-row { display: flex; flex-wrap: wrap; gap: 8px; margin: 6px 0; }
    button { padding: 7px 14px; border: 1px solid #2741E7; border-radius: 4px; background: #2741E7; color: #fff; cursor: pointer; }
    button:hover { background: #4665FF; border-color: #4665FF; }
    button:disabled { opacity: 0.5; cursor: default; }
    .output {
      background: #F3F3F5; border: 1px solid #CDCED6; border-radius: 4px;
      padding: 10px; font-family: monospace; font-size: 0.85rem;
      white-space: pre-wrap; word-break: break-word; min-height: 60px;
      max-height: 400px; overflow-y: auto;
    }
    .status-bar { font-size: 0.8rem; color: #747283; margin: 4px 0 6px 0; min-height: 18px; }
    .spinner { display: none; }
    .spinner.active { display: inline-block; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .spinner svg { animation: spin 0.9s linear infinite; vertical-align: middle; }
    #verifyBadge { font-weight: bold; margin-left: 10px; }
    #verifyBadge.ok { color: #2741E7; }
    #verifyBadge.fail { color: red; }
    nav { display: flex; gap: 2px; margin: 16px 0 0 0; background: #343338; border-radius: 6px 6px 0 0; padding: 6px 8px 0 8px; }
    nav a { padding: 8px 18px; color: #CDCED6; text-decoration: none; border-radius: 4px 4px 0 0; font-size: 0.95rem; cursor: pointer; }
    nav a:hover { background: #2741E7; color: #fff; }
    nav a.active { background: #fff; color: #2741E7; font-weight: bold; }
    .page { display: none; padding-top: 14px; }
    .page.active { display: block; }
  </style>
</head>
<body>
  <h1>Atlas — DCT Infrastructure Intelligence</h1>

  <!-- 1. Auth -->
  <div class="section">
    <h3>Identity Verification</h3>
    <form onsubmit="verify(); return false;" autocomplete="on">
      <input type="text" id="username" placeholder="Username" value="demo_user" style="width:200px" autocomplete="username"/>
      <input type="password" id="pin" placeholder="PIN" style="width:140px" autocomplete="current-password"/>
      <button type="submit">Verify</button>
    </form>
    <span id="verifyBadge"></span>
  </div>

  <nav>
    <a class="active" onclick="showPage('main', this)">Main</a>
    <a onclick="showPage('buildsheet', this)">Rack Analyzer</a>
  </nav>

  <!-- Page: Main -->
  <div class="page active" id="page-main">

  <!-- 2. Sheet Count -->
  <div class="section">
    <h3>Cutsheet Optic Count</h3>
    <input type="file" id="sheetFile" accept=".xlsx"/>
    <div class="btn-row">
      <button onclick="uploadCount()">Count</button>
      <button onclick="countByStatus()">Count, Sort by In Service</button>
    </div>
    <div class="status-bar" id="countStatus"></div>
    <div class="output" id="countOut"></div>
  </div>

  <!-- 3. NetBox -->
  <div class="section">
    <h3>NetBox Inventory</h3>
    <input type="text" id="netboxSite" placeholder="Site slug or name (e.g. us-west-09a)" style="width:300px"/>
    <div style="margin: 6px 0;">
      <label><input type="checkbox" id="netboxActiveOnly" checked/> Count In Service items only</label>
    </div>
    <div style="margin: 6px 0;">
      <label><input type="checkbox" id="netboxOpticLocations"/> Include itemized optic locations</label>
    </div>
    <div class="btn-row">
      <button onclick="streamNetbox()">Netbox</button>
      <button onclick="streamAllSites()">All Sites</button>
    </div>
    <div class="status-bar" id="netboxStatus">
      <span class="spinner" id="netboxSpinner">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3">
          <path d="M12 2a10 10 0 0 1 10 10"/>
        </svg>
      </span>
      <span id="netboxStatusText"></span>
    </div>
    <div class="output" id="netboxOut"></div>
  </div>

  <!-- 4. AI Q&A -->
  <div class="section">
    <h3>Ask Atlas (Sheet Context)</h3>
    <input type="text" id="question" placeholder="Ask a question about your loaded cutsheet..."/>
    <div class="btn-row">
      <button onclick="askAi()">Ask AI</button>
    </div>
    <div class="status-bar" id="qaStatus"></div>
    <div class="output" id="qaOut"></div>
  </div>

  </div><!-- end page-main -->

  <!-- Page: Rack Analyzer -->
  <div class="page" id="page-buildsheet">

  <div class="section">
    <h3>Rack Analyzer — Rack Query</h3>
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:10px;">
      <div>
        <label style="font-size:0.85rem; font-weight:bold;">Cutsheet Master</label>
        <input type="file" id="bsCutsheet" accept=".xlsx"/>
      </div>
      <div>
        <label style="font-size:0.85rem; font-weight:bold;">Master Region Template <span style="font-weight:normal; color:#747283;">(optional — enables cab type, elevation &amp; unused devices)</span></label>
        <input type="file" id="bsTemplate" accept=".xlsx"/>
      </div>
    </div>
    <div style="display:flex; gap:12px; align-items:flex-end; flex-wrap:wrap; margin-bottom:10px;">
      <div>
        <label style="font-size:0.85rem; font-weight:bold; display:block;">Room Designator</label>
        <input type="text" id="bsRoom" placeholder="e.g. DH2" style="width:160px; margin:0;"/>
      </div>
      <div>
        <label style="font-size:0.85rem; font-weight:bold; display:block;">Rack Number</label>
        <input type="text" id="bsRack" placeholder="e.g. 121" style="width:120px; margin:0;"/>
      </div>
      <button onclick="runBuildSheet()" id="bsBtn">Query Rack</button>
      <button onclick="clearBuildSheetFiles()" style="background:#F3F3F5; color:#747283; border-color:#CDCED6;">Clear Files</button>
    </div>
    <div class="status-bar" id="bsStatus"></div>
  </div>

  <!-- DH-wide label download -->
  <div class="section" id="bsDHSection" style="display:none;">
    <button id="bsDHBtn" onclick="downloadDHLabels()" style="font-size:1rem; padding:9px 18px;">Download all DH labels</button>
    <div class="status-bar" id="bsDHStatus"></div>
  </div>

  <!-- Summary -->
  <div class="section" id="bsSummarySection" style="display:none;">
    <div style="display:flex; align-items:center; gap:12px; margin-bottom:8px;">
      <h3 style="margin:0;">Summary</h3>
      <button onclick="downloadAllLabels()">Download all Rack labels</button>
      <button onclick="downloadLayout()" id="bsLayoutBtn">Download Used Cab Layouts</button>
      <span id="bsLayoutStatus" style="font-size:0.8rem; color:#747283;"></span>
    </div>
    <div id="bsSummary" style="font-family:monospace; font-size:0.9rem;"></div>
    <div id="bsCabType" style="margin-top:8px; font-size:0.9rem;"></div>
    <div id="bsCabTypeSummaryWrap" style="display:none; margin-top:12px;">
      <strong style="font-size:0.9rem;">Cab Types in DH</strong>
      <table id="bsCabTypeSummaryTable" style="margin-top:6px; border-collapse:collapse; font-size:0.85rem;">
        <thead>
          <tr style="background:#DAE5FF;">
            <th style="text-align:left; padding:4px 10px; border:1px solid #CDCED6;">Cab Type</th>
            <th style="text-align:right; padding:4px 10px; border:1px solid #CDCED6;">Count</th>
          </tr>
        </thead>
        <tbody id="bsCabTypeSummaryBody"></tbody>
      </table>
    </div>
  </div>

  <!-- Elevation -->
  <div class="section" id="bsElevationSection" style="display:none;">
    <h3>Cab Elevation — <span id="bsCabTypeLabel"></span> <span style="font-size:0.8rem; font-weight:normal; color:#747283;">— Source: Region Template</span></h3>
    <table id="bsElevationTable" style="width:100%; border-collapse:collapse; font-size:0.85rem;">
      <thead>
        <tr style="background:#DAE5FF;">
          <th style="text-align:left; padding:5px 8px; border:1px solid #CDCED6;">RU</th>
          <th style="text-align:left; padding:5px 8px; border:1px solid #CDCED6;">Device Name</th>
          <th style="text-align:left; padding:5px 8px; border:1px solid #CDCED6;">Device Type</th>
          <th style="text-align:left; padding:5px 8px; border:1px solid #CDCED6;">Cabling Found</th>
        </tr>
      </thead>
      <tbody id="bsElevationBody"></tbody>
    </table>
  </div>

  <!-- Devices -->
  <div class="section" id="bsDevicesSection" style="display:none;">
    <h3>Devices Physically in Rack <span style="font-size:0.8rem; font-weight:normal; color:#747283;">— Source: Cutsheet and SITE-HOSTS</span></h3>
    <div style="font-size:0.82rem; color:#747283; margin:0 0 8px 0;">
      This section lists equipment located in the queried rack. It does not mean every listed cable stays inside the rack.
    </div>
    <table id="bsDevicesTable" style="width:100%; border-collapse:collapse; font-size:0.85rem;">
      <thead>
        <tr style="background:#DAE5FF;">
          <th style="text-align:left; padding:5px 8px; border:1px solid #CDCED6;">RU</th>
          <th style="text-align:left; padding:5px 8px; border:1px solid #CDCED6;">Location</th>
          <th style="text-align:left; padding:5px 8px; border:1px solid #CDCED6;">DNS Name</th>
          <th style="text-align:left; padding:5px 8px; border:1px solid #CDCED6;">Model</th>
          <th style="text-align:left; padding:5px 8px; border:1px solid #CDCED6;">Status</th>
        </tr>
      </thead>
      <tbody id="bsDevicesBody"></tbody>
    </table>
  </div>

  <!-- Optic Summary -->
  <div class="section" id="bsOpticSection" style="display:none;">
    <h3>Optic Summary</h3>
    <div style="display:flex; gap:24px; flex-wrap:wrap; align-items:flex-start;">
      <div>
        <table id="bsOpticTable" style="border-collapse:collapse; font-size:0.85rem;">
          <thead>
            <tr style="background:#DAE5FF;">
              <th style="text-align:left; padding:5px 8px; border:1px solid #CDCED6;">Optic Type</th>
              <th style="text-align:right; padding:5px 8px; border:1px solid #CDCED6;">Count</th>
            </tr>
          </thead>
          <tbody id="bsOpticBody"></tbody>
        </table>
      </div>
      <div style="flex:1; min-width:320px;">
        <table id="bsOpticLocTable" style="width:100%; border-collapse:collapse; font-size:0.85rem;">
          <thead>
            <tr style="background:#DAE5FF;">
              <th style="text-align:left; padding:5px 8px; border:1px solid #CDCED6;">Location</th>
              <th style="text-align:left; padding:5px 8px; border:1px solid #CDCED6;">Port</th>
              <th style="text-align:left; padding:5px 8px; border:1px solid #CDCED6;">Optic Type</th>
            </tr>
          </thead>
          <tbody id="bsOpticLocBody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Internal Cables -->
  <div class="section" id="bsInternalSection" style="display:none;">
    <div style="display:flex; align-items:center; gap:12px; margin-bottom:8px;">
      <h3 style="margin:0;">Cables Staying Inside This Rack (<span id="bsInternalCount">0</span>)</h3>
      <button onclick="downloadCableMapCsv('internal')">Download Cable Map</button>
      <button onclick="downloadLabels('internal')">Download Labels</button>
    </div>
    <div style="font-size:0.82rem; color:#747283; margin:0 0 8px 0;">
      Both cable endpoints are in the queried rack.
    </div>
    <div class="output" id="bsInternalOut" style="max-height:300px;"></div>
  </div>

  <!-- Cab-to-Cab Cables -->
  <div class="section" id="bsCabSection" style="display:none;">
    <div style="display:flex; align-items:center; gap:12px; margin-bottom:8px;">
      <h3 style="margin:0;">Cables Leaving This Rack (<span id="bsCabCount">0</span>)</h3>
      <button onclick="downloadCableMapCsv('cab')">Download Cable Map</button>
      <button onclick="downloadLabels('cab')">Download Labels</button>
    </div>
    <div style="font-size:0.82rem; color:#747283; margin:0 0 8px 0;">
      One cable endpoint is in the queried rack and the other endpoint is in a different rack, room, or hall.
    </div>
    <div class="output" id="bsCabOut" style="max-height:300px;"></div>
  </div>

  </div><!-- end page-buildsheet -->

<script>
  let token = null;

  function showPage(name, el) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('nav a').forEach(a => a.classList.remove('active'));
    document.getElementById('page-' + name).classList.add('active');
    el.classList.add('active');
  }

  // --- Auth ---
  async function verify() {
    const res = await fetch('/api/verify-pin', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        username: document.getElementById('username').value,
        pin: document.getElementById('pin').value
      })
    });
    const data = await res.json();
    const badge = document.getElementById('verifyBadge');
    if (!res.ok) {
      badge.textContent = '✗ ' + (data.error || 'Failed');
      badge.className = 'fail';
      return;
    }
    token = data.token;
    badge.textContent = '✓ Verified as ' + document.getElementById('username').value;
    badge.className = 'ok';
  }

  // --- Auto re-verify on 401 ---
  async function _reVerify() {
    const u = document.getElementById('username').value;
    const p = document.getElementById('pin').value;
    if (!u || !p) return false;
    try {
      const res = await fetch('/api/verify-pin', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username: u, pin: p})
      });
      if (!res.ok) return false;
      const data = await res.json();
      token = data.token;
      return true;
    } catch (_) { return false; }
  }
  async function _authFetch(url, opts) {
    let res = await fetch(url, opts);
    if (res.status === 401 && await _reVerify()) {
      opts.headers = opts.headers || {};
      opts.headers['Authorization'] = 'Bearer ' + token;
      res = await fetch(url, opts);
    }
    return res;
  }

  // --- Elapsed timer helper ---
  let _timerInterval = null;
  function _startTimer() {
    const el = document.getElementById('countStatus');
    const t0 = Date.now();
    el.textContent = 'Processing... 0s';
    _timerInterval = setInterval(() => {
      el.textContent = 'Processing... ' + Math.round((Date.now() - t0) / 1000) + 's';
    }, 1000);
  }
  function _stopTimer() {
    clearInterval(_timerInterval);
    _timerInterval = null;
    document.getElementById('countStatus').textContent = '';
  }

  // --- Sheet count ---
  async function uploadCount() {
    if (!token) { alert('Verify identity first.'); return; }
    const fileInput = document.getElementById('sheetFile');
    if (!fileInput.files.length) { alert('Select a file first.'); return; }
    _startTimer();
    try {
      const form = new FormData();
      form.append('file', fileInput.files[0]);
      const res = await _authFetch('/api/upload-count', {
        method: 'POST',
        headers: {'Authorization': 'Bearer ' + token},
        body: form
      });
      let data;
      try { data = await res.json(); } catch (_) { data = {error: 'Server returned non-JSON (status ' + res.status + ')'}; }
      if (!res.ok) {
        document.getElementById('countOut').textContent = 'Error: ' + (data.error || 'Unknown error');
      } else {
        document.getElementById('countOut').textContent = data.output;
        if (data.pg_loaded === 'pending' && data.pg_message) {
          const info = document.createElement('div');
          info.style.cssText = 'background:#E8F0FE; border:1px solid #2741E7; border-radius:4px; padding:10px; margin-bottom:10px; color:#1a1a2e; font-size:0.9rem;';
          info.textContent = data.pg_message;
          const countOut = document.getElementById('countOut');
          countOut.parentNode.insertBefore(info, countOut);
        }
        if (data.pg_loaded === 'failed') {
          const banner = document.createElement('div');
          banner.style.cssText = 'background:#FFFACD; border:1px solid #FFD700; border-radius:4px; padding:10px; margin-bottom:10px; color:#333;';
          banner.textContent = 'Warning: Cutsheet counted but database load failed: ' + (data.pg_error || 'Unknown error') + '. Queries will use in-memory context only.';
          const countOut = document.getElementById('countOut');
          countOut.parentNode.insertBefore(banner, countOut);
        }
      }
    } catch (err) {
      document.getElementById('countOut').textContent = 'Error: ' + err.message;
    } finally {
      _stopTimer();
    }
  }

  async function countByStatus() {
    if (!token) { alert('Verify identity first.'); return; }
    const fileInput = document.getElementById('sheetFile');
    if (!fileInput.files.length) { alert('Select a file first.'); return; }
    _startTimer();
    try {
      const form = new FormData();
      form.append('file', fileInput.files[0]);
      form.append('include_by_status', '1');
      const res = await _authFetch('/api/upload-count', {
        method: 'POST',
        headers: {'Authorization': 'Bearer ' + token},
        body: form
      });
      let data;
      try { data = await res.json(); } catch (_) { data = {error: 'Server returned non-JSON (status ' + res.status + ')'}; }
      if (!res.ok) {
        document.getElementById('countOut').textContent = 'Error: ' + (data.error || 'Upload failed');
        return;
      }
      document.getElementById('countOut').textContent = data.output;
      if (data.pg_loaded === 'pending' && data.pg_message) {
        const info = document.createElement('div');
        info.style.cssText = 'background:#E8F0FE; border:1px solid #2741E7; border-radius:4px; padding:10px; margin-bottom:10px; color:#1a1a2e; font-size:0.9rem;';
        info.textContent = data.pg_message;
        const countOut = document.getElementById('countOut');
        countOut.parentNode.insertBefore(info, countOut);
      }
      if (data.pg_loaded === 'failed') {
        const banner = document.createElement('div');
        banner.style.cssText = 'background:#FFFACD; border:1px solid #FFD700; border-radius:4px; padding:10px; margin-bottom:10px; color:#333;';
        banner.textContent = 'Warning: Cutsheet counted but database load failed: ' + (data.pg_error || 'Unknown error') + '. Queries will use in-memory context only.';
        const countOut = document.getElementById('countOut');
        countOut.parentNode.insertBefore(banner, countOut);
      }
    } catch (err) {
      document.getElementById('countOut').textContent = 'Error: ' + err.message;
    } finally {
      _stopTimer();
    }
  }

  // --- NetBox SSE ---
  function _startNetboxSSE(url) {
    const out = document.getElementById('netboxOut');
    const spinner = document.getElementById('netboxSpinner');
    const statusText = document.getElementById('netboxStatusText');
    out.textContent = '';
    spinner.classList.add('active');
    statusText.textContent = 'Querying...';

    const es = new EventSource(url);
    es.onmessage = (e) => {
      if (e.data === '[DONE]') {
        es.close();
        spinner.classList.remove('active');
        statusText.textContent = 'Done.';
        return;
      }
      out.textContent += JSON.parse(e.data);
      out.scrollTop = out.scrollHeight;
    };
    es.onerror = () => {
      es.close();
      spinner.classList.remove('active');
      statusText.textContent = 'Connection error.';
    };
  }

  function streamNetbox() {
    const site = document.getElementById('netboxSite').value.trim() || 'us-west-09a';
    const activeOnly = document.getElementById('netboxActiveOnly').checked ? 'true' : 'false';
    const opticLoc = document.getElementById('netboxOpticLocations').checked ? 'true' : 'false';
    _startNetboxSSE('/api/stream/netbox?site=' + encodeURIComponent(site) + '&active_only=' + activeOnly + '&include_optic_locations=' + opticLoc);
  }

  function streamAllSites() {
    const activeOnly = document.getElementById('netboxActiveOnly').checked ? 'true' : 'false';
    const opticLoc = document.getElementById('netboxOpticLocations').checked ? 'true' : 'false';
    _startNetboxSSE('/api/stream/all-sites?active_only=' + activeOnly + '&include_optic_locations=' + opticLoc);
  }

  // --- Rack Analyzer ---
  let _bsLastResult = null;

  function clearBuildSheetFiles() {
    document.getElementById('bsCutsheet').value = '';
    document.getElementById('bsTemplate').value = '';
    document.getElementById('bsStatus').textContent = 'Files cleared.';
  }

  function _buildLabelRows(cables) {
    const rows = [];
    cables.forEach(c => {
      const aLabel = ((c.a_loc || '') + ' ' + (c.a_port || '')).trim();
      const zLabel = ((c.z_loc || '') + ' ' + (c.z_port || '')).trim();
      rows.push([aLabel + '\\n' + zLabel, zLabel + '\\n' + aLabel]);
    });
    return rows;
  }

  function _rowsToCsv(rows) {
    return rows.map(r => r.map(cell => '"' + cell.replace(/"/g, '""') + '"').join(',')).join('\\r\\n');
  }

  function _triggerDownload(csv, filename) {
    const blob = new Blob([csv], {type: 'text/csv'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function downloadLayout() {
    if (!_bsLastResult) return;
    const cutFile = document.getElementById('bsCutsheet').files[0];
    if (!cutFile) { alert('Cutsheet file is no longer selected — please re-select it.'); return; }

    const btn = document.getElementById('bsLayoutBtn');
    const status = document.getElementById('bsLayoutStatus');
    btn.disabled = true;
    status.textContent = 'Generating layout...';

    const form = new FormData();
    form.append('cutsheet', cutFile);
    const tplFile = document.getElementById('bsTemplate').files[0];
    if (tplFile) form.append('template', tplFile);
    form.append('room', _bsLastResult.room);

    try {
      const res = await fetch('/api/buildsheet/layout', { method: 'POST', body: form });
      if (!res.ok) {
        const json = await res.json().catch(() => ({}));
        status.textContent = 'Error: ' + (json.error || res.statusText);
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'layout_' + _bsLastResult.room.toUpperCase() + '.xlsx';
      a.click();
      URL.revokeObjectURL(url);
      status.textContent = 'Done.';
    } catch(e) {
      status.textContent = 'Request failed: ' + e.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function downloadDHLabels() {
    if (!_bsLastResult) return;
    const cutFile = document.getElementById('bsCutsheet').files[0];
    if (!cutFile) { alert('Cutsheet file is no longer selected — please re-select it.'); return; }

    const btn = document.getElementById('bsDHBtn');
    const status = document.getElementById('bsDHStatus');
    btn.disabled = true;
    status.textContent = 'Processing full DH — this may take a moment...';

    const form = new FormData();
    form.append('cutsheet', cutFile);
    form.append('room', _bsLastResult.room);

    try {
      const res = await fetch('/api/buildsheet/dh', { method: 'POST', body: form });
      const json = await res.json();
      if (!res.ok) { status.textContent = 'Error: ' + (json.error || 'Unknown'); return; }
      const d = json.data;
      const internalRows = [['Internal labels', ''], ..._buildLabelRows(d.internal_cables)];
      const cabRows      = [['Cab to Cab Labels', ''], ..._buildLabelRows(d.cab_to_cab_cables)];
      const csv = _rowsToCsv([...internalRows, ['', ''], ...cabRows]);
      _triggerDownload(csv, 'all_labels_' + d.room.toUpperCase() + '.csv');
      status.textContent = 'Done.';
    } catch(e) {
      status.textContent = 'Request failed: ' + e.message;
    } finally {
      btn.disabled = false;
    }
  }

  function downloadAllLabels() {
    if (!_bsLastResult) return;
    const d = _bsLastResult;
    const internalRows = [['Internal labels', ''], ..._buildLabelRows(d.internal_cables)];
    const cabRows     = [['Cab to Cab Labels', ''], ..._buildLabelRows(d.cab_to_cab_cables)];
    const csv = _rowsToCsv([...internalRows, ['', ''], ...cabRows]);
    _triggerDownload(csv, 'all_labels_' + d.room + '_rack' + d.rack + '.csv');
  }

  function _rackFromLoc(loc) {
    // Extract rack portion from 'dh202:041:10' → 'dh202:041'
    const parts = (loc || '').split(':');
    return parts.length >= 2 ? parts[0] + ':' + parts[1] : loc;
  }

  function _fmtPort(loc, port) {
    // Strip any existing leading 'port ' from the field to avoid 'port port X'
    const p = (port || '').replace(/^port\\s+/i, '');
    return ((loc || '') + ' port ' + p).trim();
  }

function downloadCableMapCsv(type) {
    if (!_bsLastResult) return;
    const isInternal = type === 'internal';
    const cables = isInternal ? _bsLastResult.internal_cables : _bsLastResult.cab_to_cab_cables;
    const headers = isInternal
      ? ['Source', 'Destination', 'Cable Type', 'Cable Length']
      : ['Source', 'Destination', 'Cable Type', 'Cable Length', 'Cable Bundle'];

    // Build bundle map for cab-to-cab: (a_rack|z_rack) → letter
    const bundleMap = {};
    let letterCode = 65; // 'A'
    if (!isInternal) {
      cables.forEach(c => {
        const key = _rackFromLoc(c.a_loc) + '|' + _rackFromLoc(c.z_loc);
        if (!(key in bundleMap)) {
          bundleMap[key] = _bsLastResult.rack + ':' + String.fromCharCode(letterCode++);
        }
      });
    }

    const sortedCables = isInternal ? cables : [...cables].sort((a, b) => {
      const ka = bundleMap[_rackFromLoc(a.a_loc) + '|' + _rackFromLoc(a.z_loc)] || '';
      const kb = bundleMap[_rackFromLoc(b.a_loc) + '|' + _rackFromLoc(b.z_loc)] || '';
      return ka < kb ? -1 : ka > kb ? 1 : 0;
    });

    const rows = [headers];
    sortedCables.forEach(c => {
      const src = _fmtPort(c.a_loc, c.a_port);
      const dst = _fmtPort(c.z_loc, c.z_port);
      const row = [src, dst, c.cable_type || '', ''];
      if (!isInternal) {
        const key = _rackFromLoc(c.a_loc) + '|' + _rackFromLoc(c.z_loc);
        row.push(bundleMap[key] || '');
      }
      rows.push(row);
    });
    const prefix = isInternal ? 'internal_cables' : 'cab_to_cab_cables';
    _triggerDownload(_rowsToCsv(rows), prefix + '_' + _bsLastResult.room + '_rack' + _bsLastResult.rack + '.csv');
  }

  function downloadLabels(type) {
    if (!_bsLastResult) return;
    const isInternal = type === 'internal';
    const cables = isInternal ? _bsLastResult.internal_cables : _bsLastResult.cab_to_cab_cables;
    const title = isInternal ? 'Internal labels' : 'Cab to Cab Labels';
    const rows = [[title, ''], ..._buildLabelRows(cables)];
    const prefix = isInternal ? 'internal_labels' : 'cab_to_cab_labels';
    _triggerDownload(_rowsToCsv(rows), prefix + '_' + _bsLastResult.room + '_rack' + _bsLastResult.rack + '.csv');
  }

  async function runBuildSheet() {
    const cutFile = document.getElementById('bsCutsheet').files[0];
    const tplFile = document.getElementById('bsTemplate').files[0];
    const room = document.getElementById('bsRoom').value.trim();
    const rack = document.getElementById('bsRack').value.trim();

    if (!cutFile) { alert('Select the Cutsheet Master file.'); return; }
    if (!room) { alert('Enter a room designator (e.g. DH2).'); return; }
    if (!rack) { alert('Enter a rack number (e.g. 121).'); return; }

    const btn = document.getElementById('bsBtn');
    btn.disabled = true;
    document.getElementById('bsStatus').textContent = 'Processing — this may take a moment for large cutsheets...';
    ['bsDHSection','bsSummarySection','bsElevationSection','bsDevicesSection','bsOpticSection','bsInternalSection','bsCabSection']
      .forEach(id => document.getElementById(id).style.display = 'none');

    const form = new FormData();
    form.append('cutsheet', cutFile);
    if (tplFile) form.append('template', tplFile);
    form.append('room', room);
    form.append('rack', rack);

    try {
      const res = await fetch('/api/buildsheet', {
        method: 'POST',
        headers: token ? {'Authorization': 'Bearer ' + token} : {},
        body: form
      });
      const json = await res.json();
      if (!res.ok) {
        document.getElementById('bsStatus').textContent = 'Error: ' + (json.error || 'Unknown error');
        return;
      }
      _bsLastResult = json.data;
      _renderBuildSheet(json.data);
      document.getElementById('bsStatus').textContent = 'Done.';
    } catch(e) {
      document.getElementById('bsStatus').textContent = 'Request failed: ' + e.message;
    } finally {
      btn.disabled = false;
    }
  }

  function _renderBuildSheet(d) {
    // DH section — dynamic button label
    document.getElementById('bsDHBtn').textContent = 'Download all ' + d.room.toUpperCase() + ' labels';
    document.getElementById('bsDHStatus').textContent = '';
    document.getElementById('bsDHSection').style.display = '';

    // Summary
    document.getElementById('bsSummary').textContent =
      'Room: ' + d.room + '   Rack: ' + d.rack + '\\n' +
      'Total cables touching this rack: ' + d.total_cables +
      '   Staying inside rack: ' + d.internal_count +
      '   Leaving rack: ' + d.cab_to_cab_count;
    const cabTypeEl = document.getElementById('bsCabType');
    cabTypeEl.textContent = d.cab_type ? 'Cab Type: ' + d.cab_type : '';
    cabTypeEl.style.fontWeight = 'bold';

    // Cab type summary table
    const cabSummary = d.cab_type_summary || {};
    const cabSummaryEntries = Object.entries(cabSummary).filter(([, v]) => v > 0);
    const cabSummaryWrap = document.getElementById('bsCabTypeSummaryWrap');
    if (cabSummaryEntries.length) {
      const tbody = document.getElementById('bsCabTypeSummaryBody');
      tbody.innerHTML = '';
      cabSummaryEntries.forEach(([type, count]) => {
        const tr = document.createElement('tr');
        const td1 = document.createElement('td');
        td1.style.cssText = 'padding:3px 10px; border:1px solid #CDCED6;';
        td1.textContent = type;
        const td2 = document.createElement('td');
        td2.style.cssText = 'padding:3px 10px; border:1px solid #CDCED6; text-align:right; font-weight:bold;';
        td2.textContent = count;
        tr.appendChild(td1); tr.appendChild(td2);
        tbody.appendChild(tr);
      });
      cabSummaryWrap.style.display = '';
    } else {
      cabSummaryWrap.style.display = 'none';
    }

    document.getElementById('bsSummarySection').style.display = '';

    // Elevation
    if (d.cab_type && d.elevation && d.elevation.length) {
      document.getElementById('bsCabTypeLabel').textContent = d.cab_type;

      // Build set of rack:ru pairs that appear in the cable data
      const cableRuSet = new Set();
      [...(d.internal_cables || []), ...(d.cab_to_cab_cables || [])].forEach(c => {
        [c.a_loc, c.z_loc].forEach(loc => {
          if (!loc) return;
          const parts = loc.split(':');
          if (parts.length >= 3) {
            cableRuSet.add((parts[1].replace(/^0+/, '') || '0') + ':' + parts[2]);
          }
        });
      });
      const queriedRack = (d.rack || '').replace(/^0+/, '') || '0';

      const elevBody = document.getElementById('bsElevationBody');
      elevBody.innerHTML = '';
      d.elevation.forEach(item => {
        const tr = document.createElement('tr');
        [item.ru, item.device_name, item.device_type].forEach(val => {
          const td = document.createElement('td');
          td.style.cssText = 'padding:4px 8px; border:1px solid #CDCED6;';
          td.textContent = val || '';
          tr.appendChild(td);
        });
        const found = item.ru && cableRuSet.has(queriedRack + ':' + item.ru);
        const tdFound = document.createElement('td');
        tdFound.style.cssText = 'padding:4px 8px; border:1px solid #CDCED6; font-weight:bold;';
        tdFound.textContent = item.ru ? (found ? 'YES' : 'NO') : '';
        tdFound.style.color = found ? 'green' : (item.ru ? 'red' : '');
        tr.appendChild(tdFound);
        elevBody.appendChild(tr);
      });
      document.getElementById('bsElevationSection').style.display = '';
    } else {
      document.getElementById('bsElevationSection').style.display = 'none';
    }

    // Devices
    const devBody = document.getElementById('bsDevicesBody');
    devBody.innerHTML = '';
    (d.devices || []).forEach(dev => {
      const tr = document.createElement('tr');
      ['ru','location','dns_name','model','status'].forEach(f => {
        const td = document.createElement('td');
        td.style.cssText = 'padding:4px 8px; border:1px solid #CDCED6;';
        td.textContent = dev[f] || '';
        if (f === 'status') {
          if (dev[f] === 'Pending')   td.style.background = '#DAE5FF';
          if (dev[f] === 'Installed') td.style.background = '#d4edda';
        }
        tr.appendChild(td);
      });
      devBody.appendChild(tr);
    });
    document.getElementById('bsDevicesSection').style.display = '';

    // Optics — summary counts
    const opticBody = document.getElementById('bsOpticBody');
    opticBody.innerHTML = '';
    Object.entries(d.optic_summary || {}).forEach(([optic, count]) => {
      const tr = document.createElement('tr');
      const td1 = document.createElement('td');
      td1.style.cssText = 'padding:4px 8px; border:1px solid #CDCED6;';
      td1.textContent = optic;
      const td2 = document.createElement('td');
      td2.style.cssText = 'padding:4px 8px; border:1px solid #CDCED6; text-align:right; font-weight:bold;';
      td2.textContent = count;
      tr.appendChild(td1); tr.appendChild(td2);
      opticBody.appendChild(tr);
    });

    // Optics — per-port locations
    const opticLocBody = document.getElementById('bsOpticLocBody');
    opticLocBody.innerHTML = '';
    (d.optic_locations || []).forEach(item => {
      const tr = document.createElement('tr');
      [item.location, item.port, item.optic].forEach(val => {
        const td = document.createElement('td');
        td.style.cssText = 'padding:4px 8px; border:1px solid #CDCED6;';
        td.textContent = val || '';
        tr.appendChild(td);
      });
      opticLocBody.appendChild(tr);
    });
    document.getElementById('bsOpticSection').style.display = '';

    // Internal cables
    document.getElementById('bsInternalCount').textContent = d.internal_count;
    document.getElementById('bsInternalOut').textContent = (d.internal_labels || []).join('\\n') || '(none)';
    document.getElementById('bsInternalSection').style.display = '';

    // Cab-to-cab cables
    document.getElementById('bsCabCount').textContent = d.cab_to_cab_count;
    document.getElementById('bsCabOut').textContent = (d.cab_to_cab_labels || []).join('\\n') || '(none)';
    document.getElementById('bsCabSection').style.display = '';
  }

  // --- AI Q&A ---
  async function askAi() {
    if (!token) { alert('Verify identity first.'); return; }
    const q = document.getElementById('question').value.trim();
    if (!q) { alert('Enter a question.'); return; }
    document.getElementById('qaStatus').textContent = 'Thinking...';
    const res = await fetch('/api/ask', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
      body: JSON.stringify({question: q})
    });
    const data = await res.json();
    document.getElementById('qaStatus').textContent = '';
    if (!res.ok) {
      document.getElementById('qaOut').textContent = 'Error: ' + data.error;
      return;
    }
    const r = data.result || {};
    const ts = r.timestamp ? new Date(r.timestamp * 1000).toLocaleString() : '';
    const stats = r.provider
      ? r.provider + ' / ' + r.model + '  |  ' + (r.input_tokens||0).toLocaleString()
        + ' in + ' + (r.output_tokens||0).toLocaleString() + ' out  |  ' + r.elapsed_seconds + 's'
      : '';

    // Build context source badge
    const contextSource = data.context_source || 'UNKNOWN';
    const badgeColors = {
      'POSTGRES': '#2741E7',
      'RACK_ANALYZER': '#0070CC',
      'IN_MEMORY': '#0066B2',
      'EMPTY_FALLBACK': '#CC0000'
    };
    const badgeColor = badgeColors[contextSource] || '#666';
    const badgeHtml = '<span style="display:inline-block; background:' + badgeColor
      + '; color:white; padding:3px 8px; border-radius:3px; font-size:0.75rem; font-weight:bold; margin-left:8px;">'
      + contextSource + '</span>';

    const answerWithBadge = (r.answer || '') + '\\n\\nContext source: ' + badgeHtml;

    const qaOutEl = document.getElementById('qaOut');
    qaOutEl.textContent =
      'User: ' + (r.user || '') + '\\n' +
      'Time: ' + ts + '\\n' +
      (stats ? stats + '\\n' : '') +
      '\\n' + (r.answer || '');

    // Add context source badge after answer
    const badgeSpan = document.createElement('span');
    badgeSpan.style.cssText = 'display:inline-block; background:' + badgeColor
      + '; color:white; padding:4px 10px; border-radius:4px; font-size:0.8rem; font-weight:bold; margin-left:8px; margin-top:8px;';
    badgeSpan.textContent = contextSource;
    qaOutEl.appendChild(document.createElement('br'));
    qaOutEl.appendChild(document.createElement('br'));
    const sourceLabel = document.createElement('span');
    sourceLabel.textContent = 'Context: ';
    sourceLabel.style.cssText = 'color:#747283; font-size:0.85rem;';
    qaOutEl.appendChild(sourceLabel);
    qaOutEl.appendChild(badgeSpan);

    // Add pg_warning if present
    if (data.pg_warning) {
      qaOutEl.appendChild(document.createElement('br'));
      const warning = document.createElement('span');
      warning.textContent = 'Warning: ' + data.pg_warning;
      warning.style.cssText = 'display:block; margin-top:8px; color:#D97706; font-size:0.85rem;';
      qaOutEl.appendChild(warning);
    }
  }
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Background NetBox ingestion scheduler
# ---------------------------------------------------------------------------
# Pulls a fresh snapshot every 15 minutes so the /dashboard endpoints always
# read recent data. Single-worker gunicorn config (Dockerfile) means this fires
# once per process. Disable by setting ATLAS_RUN_SCHEDULER=0.

_SCHEDULER_STARTED = False
_scheduler = None


def _run_netbox_ingest_safe():
    """Wrapper that swallows exceptions so the scheduler keeps ticking."""
    try:
        result = netbox_dashboard_ingest.ingest_snapshot()
        log.info("NetBox snapshot complete: %s", result)
    except (RuntimeError, OSError) as exc:
        log.warning("NetBox ingest failed: %s", exc)
    except Exception:  # noqa: BLE001 — keep scheduler alive on unexpected errors
        log.exception("NetBox ingest crashed")


def _start_netbox_scheduler():
    global _SCHEDULER_STARTED, _scheduler
    if _SCHEDULER_STARTED:
        return
    if os.getenv("ATLAS_RUN_SCHEDULER", "1") != "1":
        log.info("NetBox scheduler disabled (ATLAS_RUN_SCHEDULER != 1)")
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        log.warning("APScheduler not installed; NetBox scheduler skipped")
        return

    interval_min = int(os.getenv("NETBOX_INGEST_INTERVAL_MIN", "15"))
    sched = BackgroundScheduler(daemon=True, timezone="UTC")
    # Run once shortly after startup so the dashboard isn't empty.
    sched.add_job(
        _run_netbox_ingest_safe,
        trigger=IntervalTrigger(minutes=interval_min),
        id="netbox_ingest",
        next_run_time=None,
        max_instances=1,
        coalesce=True,
    )
    sched.start()
    _scheduler = sched
    _SCHEDULER_STARTED = True
    log.info("NetBox scheduler started (every %d min)", interval_min)

    # Seed first snapshot in a background thread so app startup isn't blocked
    # by a slow NetBox query.
    threading.Thread(target=_run_netbox_ingest_safe, name="netbox-seed", daemon=True).start()


# Start the scheduler when the app module is imported (e.g. by gunicorn).
_start_netbox_scheduler()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5050")), debug=False)
