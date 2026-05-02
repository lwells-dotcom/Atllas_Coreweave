"""
atlas_data_loader.py - Load cutsheet Excel/CSV data into Postgres.

Handles:
  - Site upsert
  - Upload tracking
  - Cutsheet connection ingestion (with profile canonicalization)
  - Host inventory ingestion
  - Materialized view refresh
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Generator, List, Optional, Tuple  # Tuple already imported

import pandas as pd
import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

try:
    from cutsheet_profiles import (
        Canon,
        STATUS_NORMALIZATION,
        canonicalize,
        normalize_model,
        normalize_status,
        profile_to_dict,
    )
    HAS_PROFILES = True
except ImportError:
    HAS_PROFILES = False
    STATUS_NORMALIZATION = {}

log = logging.getLogger(__name__)


# Sheet tabs we skip when scanning a workbook - backups, copies, legend/overhead
# reference tabs, and Excel default names. Keeps us from picking up stale data
# or paying parse cost on dead weight.
_SKIP_SHEET_PATTERNS = (
    re.compile(r"\bbackup\b", re.IGNORECASE),
    re.compile(r"^copy[\s_]of[\s_]", re.IGNORECASE),  # matches "Copy of X" and "COPY_OF_X"
    re.compile(r"^sheet\d+$", re.IGNORECASE),
    re.compile(r"^legend(-|_|$)", re.IGNORECASE),
    re.compile(r"^overhead$", re.IGNORECASE),
    re.compile(r"\bold\b|\barchive\b|\bdeprecated\b", re.IGNORECASE),
)


def _should_skip_sheet(name: str) -> bool:
    if not name:
        return True
    n = name.strip()
    return any(pat.search(n) for pat in _SKIP_SHEET_PATTERNS)


# Positive-match patterns for topology section headers.  A candidate row must
# match at least one of these to be treated as a section header during
# forward-fill derivation.  This prevents random non-status text from being
# promoted to section names.
_SECTION_HEADER_PATTERNS = (
    # Common DCT topology tiers and device roles
    re.compile(r"\b(TIER|SPINE|LEAF|TOR|FDP|CDU|PDU|EOR|MOR|AGG|CORE|BORDER|MGMT|OOB|FABRIC|ROW|DH|ISL)\b", re.IGNORECASE),
    # Numbered topology sections like "Section 1", "SECTION-A", "T1-SPINE"
    re.compile(r"^(section|topology)\s*[-_]?\s*\w", re.IGNORECASE),
    # LOC:CAB:RU style rack references sometimes used as section names
    re.compile(r"\w+-\w+-\w+.*(?:SPINE|LEAF|TOR|FDP|CDU|AGG)", re.IGNORECASE),
    # GPU/compute/storage/network infrastructure labels (Ellendale style)
    re.compile(r"\b(GPU|NVLINK|COMPUTE|STORAGE|NETWORK|INFRA|UPLINK|DOWNLINK|INTERCONNECT)\b", re.IGNORECASE),
    # Hyphenated multi-word topology names like "FDP-TO-SPINE", "TOR-UPLINK"
    re.compile(r"^[A-Z][A-Z0-9]*[-_][A-Z][A-Z0-9]*", re.IGNORECASE),
)


def _safe_error(exc: Exception) -> str:
    """Sanitize exception message to avoid leaking connection details."""
    if isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError)):
        return "Database connection error"
    return str(exc)


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------

_pool: Optional[ThreadedConnectionPool] = None
_pool_lock = threading.Lock()


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadedConnectionPool(
                    minconn=1,
                    maxconn=int(os.getenv("DB_POOL_MAX", "10")),
                    host=os.getenv("DB_HOST", "localhost"),
                    port=int(os.getenv("DB_PORT", "5432")),
                    dbname=os.getenv("DB_NAME", "atlas"),
                    user=os.getenv("DB_USER", "atlas"),
                    password=os.getenv("DB_PASSWORD", "atlas"),
                )
    return _pool


@contextmanager
def managed_connection() -> Generator:
    """Yield a pooled connection; return it to the pool on exit."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


_pg_ok_at: float = 0.0
_PG_TTL: float = 10.0


def check_postgres() -> bool:
    """Return True if Postgres is reachable. Cached for 10 seconds."""
    global _pg_ok_at
    now = time.monotonic()
    if (now - _pg_ok_at) < _PG_TTL:
        return True
    try:
        with managed_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        _pg_ok_at = now
        return True
    except Exception:
        _pg_ok_at = 0.0  # Reset so next call retries immediately
        return False


# ---------------------------------------------------------------------------
# Site management
# ---------------------------------------------------------------------------

def _file_hash(file_path: str) -> str:
    """Return the SHA-256 hex digest of a file for duplicate detection."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def upsert_site(conn, site_code: str, site_name: str = "") -> int:
    """Insert or fetch site, return site_id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sites (site_code, site_name) VALUES (%s, %s) "
            "ON CONFLICT (site_code) DO UPDATE SET site_name = EXCLUDED.site_name "
            "RETURNING id",
            (site_code, site_name or site_code),
        )
        site_id = cur.fetchone()[0]
    return site_id


def get_site_by_code(conn, site_code: str) -> Optional[int]:
    """Look up site_id by code. Returns None if not found."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM sites WHERE site_code = %s", (site_code,))
        row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Upload tracking
# ---------------------------------------------------------------------------

def create_upload(conn, site_id: int, filename: str, uploaded_by: str = "",
                  profile_dict: Optional[Dict] = None, row_count: int = 0,
                  file_hash: Optional[str] = None) -> int:
    """Record a new upload, return upload_id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cutsheet_uploads "
            "(site_id, filename, uploaded_by, profile, row_count, file_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (site_id, filename, uploaded_by,
             json.dumps(profile_dict) if profile_dict else None,
             row_count, file_hash),
        )
        upload_id = cur.fetchone()[0]
    return upload_id


def get_latest_upload(conn, site_id: int) -> Optional[Dict[str, Any]]:
    """Get most recent active upload for a site (R28: respects soft-delete)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM cutsheet_uploads "
            "WHERE site_id = %s AND is_active = TRUE "
            "ORDER BY created_at DESC LIMIT 1",
            (site_id,),
        )
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Cutsheet loading
# ---------------------------------------------------------------------------

def _clean(val: Any) -> str:
    """Convert a cell value to a clean string."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    return str(val).strip()


def _normalize_status_value(raw: str) -> str:
    """Normalize status, using profiles if available, else passthrough."""
    if HAS_PROFILES:
        return normalize_status(raw)
    return _clean(raw)


# R20: Auto-generate status enum map from STATUS_NORMALIZATION canonical values.
# Prevents silent drift when new statuses are added to cutsheet_profiles.
# Explicit overrides preserve backward compatibility with existing DB data and
# materialized view filters that reference the short slug forms.
_STATUS_SLUG_OVERRIDES: Dict[str, str] = {
    "Cable Is Ran Complete": "complete",
    "Cable Is Ran Not Terminated": "not_terminated",
    "Cable Not Run": "not_run",
}


def _build_status_enum_map() -> Dict[str, str]:
    """Build compact enum map from STATUS_NORMALIZATION canonical values."""
    if not STATUS_NORMALIZATION:
        return {}
    result: Dict[str, str] = {}
    for canonical in sorted(set(STATUS_NORMALIZATION.values())):
        slug = _STATUS_SLUG_OVERRIDES.get(
            canonical,
            re.sub(r'\W+', '_', canonical).strip('_').lower(),
        )
        if slug in result.values():
            raise ValueError(
                f"Status enum slug collision: '{slug}' produced by both "
                f"'{canonical}' and another canonical value"
            )
        result[canonical] = slug
    return result


_STATUS_ENUM_MAP = _build_status_enum_map()


def _to_status_enum(normalized: str) -> str:
    """Convert normalized status string to a compact enum value for indexing."""
    if not normalized:
        return "not_run"  # Blank status in source = cable exists but not yet touched
    return _STATUS_ENUM_MAP.get(normalized, "other")


def _normalize_model_value(raw: str) -> str:
    """Normalize model, using profiles if available, else passthrough."""
    if HAS_PROFILES:
        return normalize_model(raw)
    return _clean(raw)


# B10: Required columns after canonicalization. Catch profile misconfigs at load time.
_REQUIRED_CUTSHEET_COLS = {
    Canon.STATUS, Canon.A_DEVICE, Canon.A_PORT, Canon.A_OPTIC,
    Canon.Z_DEVICE, Canon.Z_PORT, Canon.Z_OPTIC,
} if HAS_PROFILES else set()

_REQUIRED_HOST_COLS = {
    Canon.HOST_HOSTNAME,
} if HAS_PROFILES else set()


def load_cutsheet(conn, upload_id: int, site_id: int, df: pd.DataFrame) -> int:
    """
    Load cutsheet DataFrame into cutsheet_connections table.
    Applies profile canonicalization if available.
    Returns number of rows inserted.
    """
    # Canonicalize columns and values
    profile_used = None
    if HAS_PROFILES:
        df, profile_used = canonicalize(df, sheet_type="cutsheet")
        log.info("Profile detected: %s", profile_used.name if profile_used else "none")
        missing = _REQUIRED_CUTSHEET_COLS - set(df.columns)
        if missing:
            raise ValueError(
                f"Cutsheet ingestion rejected: required canonical columns "
                f"{sorted(missing)} are missing after canonicalization "
                f"(profile: {profile_used.name if profile_used else 'none'}). "
                f"This usually means the file format doesn't match any known "
                f"profile or the cutsheet tab has unexpected headers."
            )

    # Column name resolution
    status_col = Canon.STATUS if HAS_PROFILES else "STATUS"
    section_col = Canon.SECTION if HAS_PROFILES else "SECTION"
    a_loc_col = Canon.A_LOC_CAB_RU if HAS_PROFILES else "A-LOC:CAB:RU"
    z_loc_col = Canon.Z_LOC_CAB_RU if HAS_PROFILES else "Z-LOC:CAB:RU"
    a_dev_col = Canon.A_DEVICE if HAS_PROFILES else "A-SIDE DEVICE NAME"
    a_optic_col = Canon.A_OPTIC if HAS_PROFILES else "A-OPTIC"
    z_optic_col = Canon.Z_OPTIC if HAS_PROFILES else "Z-OPTIC"

    def _filled(col: str) -> "pd.Series":
        return df[col].fillna("").astype(str).str.strip().ne("") if col in df.columns else pd.Series(False, index=df.index)

    # --- Derive sections from header rows (before filtering them out) ---
    # If a SECTION column already exists and is populated, use it.
    # Otherwise, detect section headers embedded in the STATUS column:
    # rows where STATUS is filled but A-LOC and A-DEVICE are both empty,
    # and the STATUS value is NOT a known real status.
    has_explicit_section = (
        section_col in df.columns
        and df[section_col].fillna("").astype(str).str.strip().ne("").any()
    )

    if not has_explicit_section:
        status_vals = df[status_col].fillna("").astype(str).str.strip() if status_col in df.columns else pd.Series("", index=df.index)
        a_loc_vals = df[a_loc_col].fillna("").astype(str).str.strip() if a_loc_col in df.columns else pd.Series("", index=df.index)
        a_dev_vals = df[a_dev_col].fillna("").astype(str).str.strip() if a_dev_col in df.columns else pd.Series("", index=df.index)

        # Candidate: STATUS filled, both A-LOC and A-DEVICE empty
        is_candidate = (status_vals != "") & (a_loc_vals == "") & (a_dev_vals == "")
        # Exclude rows whose STATUS is a known real status
        _known_canonical = set(STATUS_NORMALIZATION.values())
        is_known = status_vals.str.lower().isin(STATUS_NORMALIZATION) | status_vals.isin(_known_canonical)
        not_status = is_candidate & ~is_known
        # Positive validation: candidate must also look like a real topology
        # section name (not just "not a status").  This prevents random text
        # like notes or instructions from being promoted to section headers.
        looks_like_section = status_vals.apply(
            lambda v: any(pat.search(v) for pat in _SECTION_HEADER_PATTERNS)
        )
        is_section_header = not_status & looks_like_section
        # Log rows that passed the negative filter but failed positive match
        # so we can expand _SECTION_HEADER_PATTERNS if needed.
        rejected = not_status & ~looks_like_section
        if rejected.sum() > 0:
            samples = status_vals[rejected].head(5).tolist()
            log.info(
                "Section derivation: %d candidate rows rejected by positive "
                "match (not recognized as topology names). Samples: %s",
                rejected.sum(), samples,
            )

        # Forward-fill section names from header rows onto data rows
        section_markers = pd.Series(pd.NA, index=df.index, dtype=object)
        section_markers[is_section_header] = status_vals[is_section_header]
        df[section_col] = section_markers.ffill().fillna("UNKNOWN")
        log.info("load_cutsheet: derived %d section headers via forward-fill", is_section_header.sum())

    # Filter section header rows and blank rows: keep only rows with actual data.
    # Include optic columns so rows with optic data but no device/location
    # (pre-staged cables, spare inventory) aren't silently dropped.
    has_data = (_filled(a_loc_col) | _filled(a_dev_col) | _filled(z_loc_col)
                | _filled(a_optic_col) | _filled(z_optic_col))
    n_filtered = (~has_data).sum()
    df = df[has_data].copy()
    log.info("load_cutsheet: %d data rows after filtering %d header/blank rows",
             len(df), n_filtered)

    # B2: Vectorize column cleaning upfront to avoid per-cell _clean() calls.
    # Column order must match the INSERT statement below.
    cable_id_col = Canon.CABLE_ID if HAS_PROFILES else "CABLE ID"
    cable_type_col = Canon.CABLE_TYPE if HAS_PROFILES else "CABLE TYPE"

    _col_map = [
        (Canon.SECTION if HAS_PROFILES else "SECTION"),
        (Canon.A_DEVICE if HAS_PROFILES else "A-SIDE DEVICE NAME"),
        (Canon.A_PORT if HAS_PROFILES else "A-SIDE PORT"),
        (Canon.A_OPTIC if HAS_PROFILES else "A-OPTIC"),
        (Canon.A_LOCODE if HAS_PROFILES else "A-SIDE LOCODE"),
        (Canon.A_MODEL if HAS_PROFILES else "A-MODEL"),
        (Canon.A_LOC_CAB_RU if HAS_PROFILES else "A-LOC:CAB:RU"),
        (Canon.Z_DEVICE if HAS_PROFILES else "Z-SIDE DEVICE NAME"),
        (Canon.Z_PORT if HAS_PROFILES else "Z-SIDE PORT"),
        (Canon.Z_OPTIC if HAS_PROFILES else "Z-OPTIC"),
        (Canon.Z_LOCODE if HAS_PROFILES else "Z-SIDE LOCODE"),
        (Canon.Z_MODEL if HAS_PROFILES else "Z-MODEL"),
        (Canon.Z_LOC_CAB_RU if HAS_PROFILES else "Z-LOC:CAB:RU"),
        cable_id_col,
        cable_type_col,
        (Canon.A_BREAKOUT_LOC if HAS_PROFILES else "A-BREAKOUT LOC:CAB:RU"),
        (Canon.A_BREAKOUT_PORT if HAS_PROFILES else "A-BREAKOUT SLOT:PORT"),
        (Canon.Z_BREAKOUT_LOC if HAS_PROFILES else "Z-BREAKOUT LOC:CAB:RU"),
        (Canon.Z_BREAKOUT_PORT if HAS_PROFILES else "Z-BREAKOUT SLOT:PORT"),
        (Canon.A_PATCH_PANEL if HAS_PROFILES else "A-PATCH-PANEL LOC:CAB:RU:PORT"),
        (Canon.Z_PATCH_PANEL if HAS_PROFILES else "Z-PATCH-PANEL LOC:CAB:RU:PORT"),
    ]
    # Pre-clean: fillna + str + strip on all mapped columns (vectorized)
    for col in _col_map:
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str).str.strip()
        else:
            df[col] = ""

    # B11: Detect when cable_id contains cable *type* (media type like "CAT6a",
    # "MPO12-SMF") instead of unique cable identifiers. If the column has very
    # low cardinality relative to row count, it's a type column, not an ID
    # column. Reclassify to cable_type to prevent the unique constraint on
    # cable_id from nuking all but one row per type.
    if len(df) > 0 and cable_id_col in df.columns:
        cable_vals = df[cable_id_col]
        non_empty = cable_vals[cable_vals != ""]
        if len(non_empty) > 0:
            nunique = non_empty.nunique()
            ratio = nunique / len(non_empty)
            # Heuristic: if < 5% unique values, it's a type column, not an ID
            if ratio < 0.05 and nunique < 50:
                log.warning(
                    "cable_id column reclassified as cable_type: %d unique "
                    "values across %d rows (%.1f%% cardinality). Values: %s",
                    nunique, len(non_empty), ratio * 100,
                    sorted(non_empty.unique()[:10]),
                )
                df[cable_type_col] = df[cable_id_col]
                df[cable_id_col] = ""

    # Status needs special normalization (not just cleaning)
    status_col_name = Canon.STATUS if HAS_PROFILES else "STATUS"
    if status_col_name in df.columns:
        df[status_col_name] = df[status_col_name].fillna("").astype(str).str.strip()
    else:
        df[status_col_name] = ""

    rows = []
    raw_json = []
    for row in df.to_dict("records"):
        status_raw = row.get(status_col_name, "")
        status = _normalize_status_value(status_raw) if status_raw else ""
        status_norm = _to_status_enum(status)

        # R25: raw row stored in separate table to avoid bloating hot table.
        # R12: float NaN is truthy, so `if v` won't catch it. Must explicitly
        # exclude NaN to produce valid JSON (Postgres rejects bare NaN tokens).
        raw_j = json.dumps(
            {k: v for k, v in row.items()
             if v and not (isinstance(v, float) and math.isnan(v))},
            separators=(",", ":"),
            default=str,
        )

        # Bundle raw JSON into the main row tuple so we can insert both
        # atomically and keep the positional mapping correct even when
        # ON CONFLICT DO NOTHING skips duplicates.
        rows.append((
            upload_id,
            site_id,
            *(row.get(c, "") for c in _col_map),
            status,
            status_norm,
            raw_j,
        ))

    if not rows:
        return 0

    with conn.cursor() as cur:
        # Single CTE: insert connections and their raw rows atomically.
        # The named input CTE exposes raw_json so raw_ins can pair each
        # connection_id with its source row's JSON — correct even when
        # ON CONFLICT DO NOTHING skips duplicates (no positional zip needed).
        psycopg2.extras.execute_values(
            cur,
            """WITH input(upload_id, site_id, section,
                          a_device, a_port, a_optic, a_locode, a_model, a_loc_cab_ru,
                          z_device, z_port, z_optic, z_locode, z_model, z_loc_cab_ru,
                          cable_id, cable_type,
                          a_breakout_loc, a_breakout_port, z_breakout_loc, z_breakout_port,
                          a_patch_panel, z_patch_panel,
                          status, status_normalized,
                          raw_json) AS (
                 VALUES %s
               ),
               ins AS (
                 INSERT INTO cutsheet_connections
                   (upload_id, site_id, section,
                    a_device, a_port, a_optic, a_locode, a_model, a_loc_cab_ru,
                    z_device, z_port, z_optic, z_locode, z_model, z_loc_cab_ru,
                    cable_id, cable_type,
                    a_breakout_loc, a_breakout_port, z_breakout_loc, z_breakout_port,
                    a_patch_panel, z_patch_panel,
                    status, status_normalized)
                 SELECT upload_id, site_id, section,
                        a_device, a_port, a_optic, a_locode, a_model, a_loc_cab_ru,
                        z_device, z_port, z_optic, z_locode, z_model, z_loc_cab_ru,
                        cable_id, cable_type,
                        a_breakout_loc, a_breakout_port, z_breakout_loc, z_breakout_port,
                        a_patch_panel, z_patch_panel,
                        status, status_normalized
                 FROM input
                 ON CONFLICT DO NOTHING
                 RETURNING id, upload_id, a_device, a_port, z_device, z_port, cable_id
               ),
               raw_ins AS (
                 INSERT INTO cutsheet_raw_rows (connection_id, raw_row)
                 SELECT ins.id, input.raw_json::jsonb
                 FROM ins
                 JOIN input
                   ON input.upload_id  = ins.upload_id
                  AND input.a_device   = ins.a_device
                  AND input.a_port     = ins.a_port
                  AND input.z_device   = ins.z_device
                  AND input.z_port     = ins.z_port
                  AND COALESCE(input.cable_id, '') = COALESCE(ins.cable_id, '')
                 ON CONFLICT (connection_id) DO NOTHING
               )
               SELECT count(*) FROM ins""",
            rows,
            page_size=500,
        )
        result = cur.fetchone()
        n_inserted = result[0] if result else len(rows)
        n_dupes = len(rows) - n_inserted
        if n_dupes > 0:
            log.warning(
                "load_cutsheet: %d duplicate rows skipped "
                "(deduped by cable_id or port identity)", n_dupes,
            )

    return n_inserted


def load_site_hosts(conn, upload_id: int, site_id: int, df: pd.DataFrame,
                    profile=None) -> int:
    """
    Load host inventory DataFrame into host_inventory table.
    Applies profile canonicalization if available.

    profile: pass the already-detected cutsheet profile so the host_columns
    mapping (e.g. DNS-A-RECORD -> HOSTNAME) is applied even though
    detect_profile() can't fingerprint a SITE-HOSTS sheet directly.

    Returns number of rows inserted.
    """
    profile_used = None
    if HAS_PROFILES:
        df, profile_used = canonicalize(df, sheet_type="hosts", profile=profile)
        missing = _REQUIRED_HOST_COLS - set(df.columns)
        if missing:
            raise ValueError(
                f"Host inventory ingestion rejected: required canonical columns "
                f"{sorted(missing)} are missing after canonicalization "
                f"(profile: {profile_used.name if profile_used else 'none'}). "
                f"Check that the SITE-HOSTS tab has a hostname column."
            )

    rows = []
    for _, row in df.iterrows():
        hostname = _clean(row.get(Canon.HOST_HOSTNAME if HAS_PROFILES else "HOSTNAME", ""))
        if not hostname:
            continue
        model_raw = _clean(row.get(Canon.HOST_MODEL if HAS_PROFILES else "MODEL", ""))
        model = _normalize_model_value(model_raw) if model_raw else ""
        status_raw = _clean(row.get(Canon.HOST_STATUS if HAS_PROFILES else "STATUS", ""))
        status = _normalize_status_value(status_raw) if status_raw else ""

        rows.append((
            upload_id,
            site_id,
            hostname,
            model,
            _clean(row.get(Canon.HOST_ROLE if HAS_PROFILES else "ROLE", "")),
            _clean(row.get(Canon.HOST_RACK if HAS_PROFILES else "RACK", "")),
            _clean(row.get(Canon.HOST_DATA_HALL if HAS_PROFILES else "DATA_HALL", "")),
            status,
            _clean(row.get(Canon.HOST_ROW_TYPE if HAS_PROFILES else "ROW_TYPE", "")),
        ))

    if not rows:
        return 0

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO host_inventory
               (upload_id, site_id, hostname, model, role, rack, data_hall, status, row_type)
               VALUES %s""",
            rows,
            page_size=500,
        )
    return len(rows)


def load_burndown(conn, upload_id: int, site_id: int, df: pd.DataFrame) -> int:
    """
    Load BURNDOWN sheet into burndown_connections table.
    Contains LINK-STATUS, CURRENT-NEIGHBOR, DCT/NetEng notes.
    Returns number of rows inserted.
    """
    # B8: Use profile canonicalization (R8 compliance)
    if HAS_PROFILES:
        df, _ = canonicalize(df, sheet_type="burndown")

    # Column names: use Canon constants if available, else hardcoded fallback
    a_dev_col = Canon.BD_A_DEVICE if HAS_PROFILES else "A-SIDE-DNS-NAME"
    z_dev_col = Canon.BD_Z_DEVICE if HAS_PROFILES else "Z-SIDE-DNS-NAME"
    a_port_col = Canon.BD_A_PORT if HAS_PROFILES else "A-PORT"
    z_port_col = Canon.BD_Z_PORT if HAS_PROFILES else "Z-PORT"
    a_loc_col = Canon.BD_A_LOC_CAB_RU if HAS_PROFILES else "A-LOC:CAB:RU"
    z_loc_col = Canon.BD_Z_LOC_CAB_RU if HAS_PROFILES else "Z-LOC:CAB:RU"
    status_col = Canon.STATUS if HAS_PROFILES else "STATUS"
    link_status_col = Canon.BD_LINK_STATUS if HAS_PROFILES else "LINK-STATUS"
    neighbor_col = Canon.BD_CURRENT_NEIGHBOR if HAS_PROFILES else "CURRENT-NEIGHBOR"
    neighbor_port_col = Canon.BD_CURRENT_NEIGHBOR_PORT if HAS_PROFILES else "CURRENT-NEIGHBOR-PORT"
    cutsheet_row_col = Canon.BD_CUTSHEET_ROW if HAS_PROFILES else "CUTSHEET Row"
    dct_notes_col = Canon.BD_DCT_NOTES if HAS_PROFILES else "DCT notes"
    neteng_notes_col = Canon.BD_NETENG_NOTES if HAS_PROFILES else "NetEng notes"

    # B2 consistency: Vectorize column cleaning upfront (same pattern as load_cutsheet)
    _text_cols = [
        a_dev_col, z_dev_col, a_port_col, z_port_col,
        a_loc_col, z_loc_col, status_col, link_status_col,
        neighbor_col, neighbor_port_col, dct_notes_col, neteng_notes_col,
    ]
    for col in _text_cols:
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str).str.strip()
        else:
            df[col] = ""

    # cutsheet_row_col is numeric, handle separately
    if cutsheet_row_col in df.columns:
        df[cutsheet_row_col] = pd.to_numeric(
            df[cutsheet_row_col], errors="coerce"
        )
    else:
        df[cutsheet_row_col] = pd.NA

    # Filter rows where both a_device and z_device are empty
    has_device = (df[a_dev_col] != "") | (df[z_dev_col] != "")
    df = df[has_device].copy()

    rows = []
    for row in df.to_dict("records"):
        status_raw = row.get(status_col, "")
        status = _normalize_status_value(status_raw) if status_raw else ""

        cutsheet_row_val = row.get(cutsheet_row_col, None)
        try:
            cutsheet_row = int(cutsheet_row_val) if pd.notna(cutsheet_row_val) else None
        except (ValueError, TypeError):
            cutsheet_row = None

        link_status = row.get(link_status_col, "")
        link_status = link_status.lower() if link_status else None

        rows.append((
            upload_id,
            site_id,
            status,
            row.get(a_loc_col, ""),
            row.get(a_dev_col, ""),
            row.get(a_port_col, ""),
            row.get(z_loc_col, ""),
            row.get(z_dev_col, ""),
            row.get(z_port_col, ""),
            link_status or None,
            row.get(neighbor_col, "") or None,
            row.get(neighbor_port_col, "") or None,
            cutsheet_row,
            row.get(dct_notes_col, "") or None,
            row.get(neteng_notes_col, "") or None,
        ))

    if not rows:
        return 0

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO burndown_connections
               (upload_id, site_id, status,
                a_loc_cab_ru, a_device, a_port,
                z_loc_cab_ru, z_device, z_port,
                link_status, current_neighbor, current_neighbor_port,
                cutsheet_row, dct_notes, neteng_notes)
               VALUES %s""",
            rows,
            page_size=500,
        )
    return len(rows)


def backfill_device_roles(conn, upload_id: int, site_id: int) -> Dict[str, int]:
    """
    H8: Populate a_role and z_role in cutsheet_connections by joining to
    host_inventory for the same upload.  Must be called after both
    load_cutsheet() and load_site_hosts() have committed.

    Returns counts of rows updated for each side.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE cutsheet_connections cc
            SET a_role = hi.role
            FROM host_inventory hi
            WHERE cc.site_id        = %(site_id)s
              AND cc.upload_id      = %(upload_id)s
              AND hi.site_id        = %(site_id)s
              AND hi.upload_id      = %(upload_id)s
              AND hi.role           IS NOT NULL
              AND hi.role           != ''
              AND LOWER(TRIM(cc.a_device)) = LOWER(TRIM(hi.hostname))
            """,
            {"site_id": site_id, "upload_id": upload_id},
        )
        a_updated = cur.rowcount

        cur.execute(
            """
            UPDATE cutsheet_connections cc
            SET z_role = hi.role
            FROM host_inventory hi
            WHERE cc.site_id        = %(site_id)s
              AND cc.upload_id      = %(upload_id)s
              AND hi.site_id        = %(site_id)s
              AND hi.upload_id      = %(upload_id)s
              AND hi.role           IS NOT NULL
              AND hi.role           != ''
              AND LOWER(TRIM(cc.z_device)) = LOWER(TRIM(hi.hostname))
            """,
            {"site_id": site_id, "upload_id": upload_id},
        )
        z_updated = cur.rowcount

    log.info(
        "backfill_device_roles: a_role updated=%d  z_role updated=%d",
        a_updated, z_updated,
    )
    return {"a_updated": a_updated, "z_updated": z_updated}


def refresh_views(conn) -> None:
    """Refresh all materialized views after data load."""
    with conn.cursor() as cur:
        cur.execute("SELECT refresh_atlas_views()")
    conn.commit()


# ---------------------------------------------------------------------------
# High-level load function
# ---------------------------------------------------------------------------

def load_file(file_path: str, site_code: str, uploaded_by: str = "") -> Dict[str, Any]:
    """
    Full load pipeline: read file, canonicalize, insert into Postgres,
    refresh views.  Returns summary dict.
    """
    try:
        sha256 = _file_hash(file_path)
        with managed_connection() as conn:
            site_id = upsert_site(conn, site_code)

            # Reject duplicate uploads for the same site (idempotency guard)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM cutsheet_uploads "
                    "WHERE site_id = %s AND file_hash = %s LIMIT 1",
                    (site_id, sha256),
                )
                existing = cur.fetchone()
            if existing:
                log.info("Duplicate upload skipped: site=%s hash=%s upload_id=%s",
                         site_code, sha256[:8], existing[0])
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "duplicate_file",
                    "existing_upload_id": existing[0],
                    "site_id": site_id,
                    "site_code": site_code,
                }

            # Read the cutsheet tab
            if file_path.lower().endswith(".csv"):
                df = pd.read_csv(file_path)
            else:
                xls = pd.ExcelFile(file_path, engine="calamine")
                # Filter backups/copies/legends before we do anything else.
                active_sheets = [s for s in xls.sheet_names if not _should_skip_sheet(s)]
                if len(active_sheets) != len(xls.sheet_names):
                    skipped = set(xls.sheet_names) - set(active_sheets)
                    log.info("Skipping %d backup/junk tab(s): %s",
                             len(skipped), sorted(skipped))

                # --- Sheet selection with post-heuristic schema verification ---
                # Minimum columns a real cutsheet tab must have (at least one
                # optic column AND at least one device/port column).
                _SCHEMA_OPTIC = {"A-OPTIC", "Z-OPTIC", "A OPTIC", "Z OPTIC",
                                 "SOURCE OPTIC", "DEST OPTIC"}
                _SCHEMA_DEVICE = {"A-SIDE-DNS-NAME", "A-SIDE DEVICE NAME",
                                  "A SIDE DEVICE", "SOURCE DEVICE",
                                  "Z-SIDE-DNS-NAME", "Z-SIDE DEVICE NAME",
                                  "Z SIDE DEVICE", "DEST DEVICE"}
                _SCHEMA_PORT = {"A-SIDE PORT", "A-PORT", "A SIDE PORT",
                                "SOURCE PORT", "Z-SIDE PORT", "Z-PORT",
                                "Z SIDE PORT", "DEST PORT"}

                def _verify_cutsheet_schema(df_sample: pd.DataFrame) -> Tuple[bool, str]:
                    """
                    Verify that a cutsheet sample can be canonicalized to produce
                    all required columns.  Returns (is_valid, reason_if_invalid).

                    Tightened from loose column-name check to actual canonicalization
                    validation: we run detect_profile + apply_profile on the sample
                    and verify all _REQUIRED_CUTSHEET_COLS will be present after.
                    """
                    try:
                        # First pass: loose structural check to avoid unnecessary
                        # canonicalization attempt on clearly wrong sheets
                        cols_upper = {str(c).strip().upper() for c in df_sample.columns}
                        has_optic = bool(cols_upper & _SCHEMA_OPTIC) or any("OPTIC" in c for c in cols_upper)
                        has_device = bool(cols_upper & _SCHEMA_DEVICE)
                        has_port = bool(cols_upper & _SCHEMA_PORT)
                        if not (has_optic and (has_device or has_port)):
                            return False, "Loose structure check failed: no optic or device/port columns"

                        # Second pass: actual canonicalization test
                        if not HAS_PROFILES:
                            # If profiles aren't available, we can't validate canonicalization,
                            # so pass the structural check above as sufficient.
                            return True, ""

                        # Run canonicalize on the sample to verify the required columns
                        # will be present after profile detection + column mapping
                        df_test = df_sample.copy()
                        df_test, profile = canonicalize(df_test, sheet_type="cutsheet")

                        missing = _REQUIRED_CUTSHEET_COLS - set(df_test.columns)
                        if missing:
                            return False, (
                                f"Canonicalization succeeded but required canonical columns "
                                f"{sorted(missing)} are missing. "
                                f"(Profile: {profile.name if profile else 'none'})"
                            )

                        return True, ""
                    except Exception as e:
                        return False, f"Canonicalization failed: {str(e)}"

                sheet_name = None
                # Pass 0: prefer pre-cleaned tab (matches cutsheet_preprocessor priority)
                for sn in active_sheets:
                    if sn.strip().upper() == "CUTSHEET_CLEAN":
                        df_check = pd.read_excel(xls, sheet_name=sn, nrows=5)
                        is_valid, reason = _verify_cutsheet_schema(df_check)
                        if is_valid:
                            sheet_name = sn
                            break

                # Pass 1: explicit name match
                if not sheet_name:
                    for sn in active_sheets:
                        if sn.strip().casefold() in ("cutsheet", "connections"):
                            df_check = pd.read_excel(xls, sheet_name=sn, nrows=5)
                            is_valid, reason = _verify_cutsheet_schema(df_check)
                            if is_valid:
                                sheet_name = sn
                                break
                            else:
                                log.warning(
                                    "Tab '%s' has a cutsheet-like name but failed "
                                    "schema verification: %s. Skipping.",
                                    sn, reason,
                                )

                # Pass 2: heuristic scan with schema verification
                if not sheet_name:
                    for sn in active_sheets:
                        df_check = pd.read_excel(xls, sheet_name=sn, nrows=5)  # Sample first 5 rows
                        is_valid, reason = _verify_cutsheet_schema(df_check)
                        if is_valid:
                            sheet_name = sn
                            break

                if not sheet_name:
                    raise ValueError(
                        f"No cutsheet-like tab found in '{os.path.basename(file_path)}'. "
                        f"Checked {len(active_sheets)} tabs, none passed schema "
                        f"verification (need optic + device/port columns). "
                        f"Active tabs: {active_sheets}"
                    )
                df = pd.read_excel(xls, sheet_name=sheet_name)

            # Detect profile for audit trail
            detected_profile = None
            _profile_score = 0.0
            if HAS_PROFILES:
                from cutsheet_profiles import detect_profile, profile_to_dict as p2d
                detected_profile, _profile_score = detect_profile(df)
                log.info("Profile detection: %s (score=%.0f%%)",
                         detected_profile.name if detected_profile else "none",
                         _profile_score * 100)

            # B9: Soft-delete previous uploads for this site to prevent
            # double-counting in materialized views. Preserves audit trail.
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE cutsheet_uploads SET is_active = FALSE "
                    "WHERE site_id = %s AND is_active = TRUE",
                    (site_id,),
                )
                deactivated = cur.rowcount
            if deactivated:
                log.info("Deactivated %d previous upload(s) for site %s",
                         deactivated, site_code)

            upload_id = create_upload(
                conn, site_id, os.path.basename(file_path), uploaded_by,
                profile_dict=profile_to_dict(detected_profile) if HAS_PROFILES and detected_profile else None,
                row_count=len(df),
                file_hash=sha256,
            )

            rows_loaded = load_cutsheet(conn, upload_id, site_id, df)

            # Try loading SITE-HOSTS tab if present (reuse xls from above).
            # Use active_sheets so "Copy of SITE-HOSTS" gets skipped.
            hosts_loaded = 0
            if not file_path.lower().endswith(".csv"):
                with conn.cursor() as _cur:
                    _cur.execute("SAVEPOINT sp_hosts")
                try:
                    for sn in active_sheets:
                        if sn.strip().casefold() in ("site-hosts", "hosts", "host inventory", "devices"):
                            host_df = pd.read_excel(xls, sheet_name=sn)
                            hosts_loaded = load_site_hosts(
                                conn, upload_id, site_id, host_df,
                                profile=detected_profile,
                            )
                            break
                    with conn.cursor() as _cur:
                        _cur.execute("RELEASE SAVEPOINT sp_hosts")
                except Exception as exc:
                    with conn.cursor() as _cur:
                        _cur.execute("ROLLBACK TO SAVEPOINT sp_hosts")
                    log.warning("Host loading failed: %s", exc)

            # Try loading BURNDOWN tab if present (reuse xls from above)
            burndown_loaded = 0
            if not file_path.lower().endswith(".csv"):
                with conn.cursor() as _cur:
                    _cur.execute("SAVEPOINT sp_burndown")
                try:
                    for sn in xls.sheet_names:
                        if sn.strip().casefold() == "burndown":
                            bd_df = pd.read_excel(xls, sheet_name=sn)
                            burndown_loaded = load_burndown(conn, upload_id, site_id, bd_df)
                            log.info("Burndown loaded: %s rows", burndown_loaded)
                            break
                    with conn.cursor() as _cur:
                        _cur.execute("RELEASE SAVEPOINT sp_burndown")
                except Exception as exc:
                    with conn.cursor() as _cur:
                        _cur.execute("ROLLBACK TO SAVEPOINT sp_burndown")
                    log.warning("Burndown loading failed (non-fatal): %s", exc)

            # Commit all essential data before running optional post-processing.
            # backfill_device_roles runs in its own connection so that if the
            # process dies during the (potentially slow) UPDATE, the lock is
            # scoped to the backfill transaction only and cannot block new uploads.
            conn.commit()

        # H8: Backfill device roles in a separate transaction after the main commit.
        # Running this inside the main transaction held locks on cutsheet_connections
        # that could outlive the container process and block all subsequent uploads.
        roles_backfilled: Dict[str, int] = {"a_updated": 0, "z_updated": 0}
        if hosts_loaded > 0:
            try:
                with managed_connection() as _bc:
                    roles_backfilled = backfill_device_roles(_bc, upload_id, site_id)
                    _bc.commit()
            except Exception as exc:
                log.warning("Role backfill failed (non-fatal): %s", exc)

        # Refresh materialized views in background so the upload response
        # isn't blocked by the (potentially slow) view rebuild.
        def _bg_refresh():
            try:
                with managed_connection() as _conn:
                    refresh_views(_conn)
            except Exception as exc:
                log.warning("View refresh failed (background): %s", exc)
        threading.Thread(target=_bg_refresh, daemon=True).start()

        return {
            "ok": True,
            "site_id": site_id,
            "site_code": site_code,
            "upload_id": upload_id,
            "connections_loaded": rows_loaded,
            "hosts_loaded": hosts_loaded,
            "burndown_loaded": burndown_loaded,
            "roles_backfilled": roles_backfilled,
            "profile": profile_to_dict(detected_profile) if HAS_PROFILES and detected_profile else None,
        }
    except Exception as exc:
        log.exception("load_file failed")
        return {"ok": False, "error": _safe_error(exc)}
