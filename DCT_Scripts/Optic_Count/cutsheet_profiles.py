"""
cutsheet_profiles.py - Column mapping and normalization for multi-site cutsheets.

Defines canonical column names, per-site profile mappings, status normalization,
and model alias resolution.  Integrated at ingestion time by both
atlas_data_loader.py (Postgres path) and cutsheet_normalizer.py (in-memory path).
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from cutsheet_preprocessor import (
    STATUS_MAP,
    COMPLETE, NOT_TERMINATED, NOT_RUN, ADDITION,
    HUMAN_VERIFIED, LLDP_PASSED, LLDP_FAILED, IN_PROGRESS, PENDING,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical column names used throughout the pipeline
# ---------------------------------------------------------------------------

class Canon:
    """Single source of truth for column names after canonicalization."""

    SECTION = "SECTION"
    A_DEVICE = "A-SIDE DEVICE NAME"
    A_PORT = "A-SIDE PORT"
    A_OPTIC = "A-OPTIC"
    A_LOCODE = "A-SIDE LOCODE"
    A_MODEL = "A-MODEL"
    A_LOC_CAB_RU = "A-LOC:CAB:RU"
    Z_DEVICE = "Z-SIDE DEVICE NAME"
    Z_PORT = "Z-SIDE PORT"
    Z_OPTIC = "Z-OPTIC"
    Z_LOCODE = "Z-SIDE LOCODE"
    Z_MODEL = "Z-MODEL"
    Z_LOC_CAB_RU = "Z-LOC:CAB:RU"
    CABLE_ID = "CABLE ID"
    STATUS = "STATUS"
    # Breakout columns (seen in Ellendale / multi-site cutsheets)
    A_BREAKOUT_LOC = "A-BREAKOUT LOC:CAB:RU"
    A_BREAKOUT_PORT = "A-BREAKOUT SLOT:PORT"
    Z_BREAKOUT_LOC = "Z-BREAKOUT LOC:CAB:RU"
    Z_BREAKOUT_PORT = "Z-BREAKOUT SLOT:PORT"
    # Patch panel columns
    A_PATCH_PANEL = "A-PATCH-PANEL LOC:CAB:RU:PORT"
    Z_PATCH_PANEL = "Z-PATCH-PANEL LOC:CAB:RU:PORT"
    CABLE_TYPE = "CABLE TYPE"  # Cable media type (CAT6a, MPO12-SMF, etc.)
    MODEL = "MODEL"
    HOSTNAME = "HOSTNAME"
    DATA_HALL = "DATA HALL"
    SITE_CODE = "SITE CODE"

    # Host-sheet canonical names
    HOST_HOSTNAME = "HOSTNAME"
    HOST_MODEL = "MODEL"
    HOST_ROLE = "ROLE"
    HOST_RACK = "RACK"
    HOST_DATA_HALL = "DATA_HALL"
    HOST_STATUS = "STATUS"
    HOST_ROW_TYPE = "ROW_TYPE"   # physical placement metadata (ROW:TYPE)
    HOST_LOCODE = "LOCODE"

    # Burndown-sheet canonical names
    # NOTE: BD_A_LOC_CAB_RU / BD_Z_LOC_CAB_RU intentionally share string values
    # with A_LOC_CAB_RU / Z_LOC_CAB_RU. STATUS is also shared across all sheet
    # types. This is by design — these columns have the same semantics across tabs.
    BD_A_DEVICE = "A-DEVICE"
    BD_Z_DEVICE = "Z-DEVICE"
    BD_A_PORT = "A-PORT"
    BD_Z_PORT = "Z-PORT"
    BD_A_LOC_CAB_RU = "A-LOC:CAB:RU"
    BD_Z_LOC_CAB_RU = "Z-LOC:CAB:RU"
    BD_LINK_STATUS = "LINK-STATUS"
    BD_CURRENT_NEIGHBOR = "CURRENT-NEIGHBOR"
    BD_CURRENT_NEIGHBOR_PORT = "CURRENT-NEIGHBOR-PORT"
    BD_CUTSHEET_ROW = "CUTSHEET ROW"
    BD_DCT_NOTES = "DCT NOTES"
    BD_NETENG_NOTES = "NETENG NOTES"


# ---------------------------------------------------------------------------
# Status normalization map — derived from STATUS_MAP (single source of truth).
# Maps lowercased raw status strings to mixed-case display strings used for
# Postgres ingest.  SECTION_HEADER / BLANK / UNKNOWN entries are excluded.
# ---------------------------------------------------------------------------

_ENUM_TO_DISPLAY: Dict[str, str] = {
    COMPLETE:       "Cable Is Ran Complete",
    NOT_TERMINATED: "Cable Is Ran Not Terminated",
    NOT_RUN:        "Cable Not Run",
    ADDITION:       "Addition",
    HUMAN_VERIFIED: "Human Verified",
    LLDP_PASSED:    "LLDP Passed",
    LLDP_FAILED:    "LLDP Failed",
    IN_PROGRESS:    "In Progress",
    PENDING:        "Pending",
}

STATUS_NORMALIZATION: Dict[str, str] = {
    k.lower(): _ENUM_TO_DISPLAY[v]
    for k, v in STATUS_MAP.items()
    if v in _ENUM_TO_DISPLAY
}


# ---------------------------------------------------------------------------
# Model alias map
# ---------------------------------------------------------------------------

MODEL_ALIASES: Dict[str, str] = {
    # Mellanox / NVIDIA SN series
    "sn4700": "SN4700",
    "mellanox-sn4700": "SN4700",
    "nvidia-sn4700": "SN4700",
    "sn2201": "SN2201",
    "mellanox-sn2201": "SN2201",
    "nvidia-sn2201": "SN2201",
    "sn3700": "SN3700",
    "mellanox-sn3700": "SN3700",
    "nvidia-sn3700": "SN3700",
    "sn3420": "SN3420",
    "mellanox-sn3420": "SN3420",
    "nvidia-sn3420": "SN3420",
    "sn5600": "SN5600",
    "mellanox-sn5600": "SN5600",
    "nvidia-sn5600": "SN5600",
    "sn5610": "SN5610",
    "mellanox-sn5610": "SN5610",
    "nvidia-sn5610": "SN5610",
    # Nokia
    "7750-sr-1se": "7750-SR-1SE",
    "nokia-7750-sr-1se": "7750-SR-1SE",
    "7750 sr 1se": "7750-SR-1SE",
    # Celestica
    "om2216-c14": "OM2216-C14",
    "om2216c14": "OM2216-C14",
    # Supermicro
    "cm8148": "CM8148",
    # Palo Alto
    "pa-1420": "PA-1420",
    "pa1420": "PA-1420",
    # Juniper
    "ptx10002-36qdd": "PTX10002-36QDD",
    "juniper-ptx10002-36qdd": "PTX10002-36QDD",
    # FortiGate / NGFW
    "ngfw-4245": "NGFW-4245",
    "fortigate-4245": "NGFW-4245",
    # Dell
    "r760": "R760",
    "dell-r760": "R760",
    "poweredge-r760": "R760",
}


# ---------------------------------------------------------------------------
# Profile definitions
# ---------------------------------------------------------------------------

@dataclass
class CutsheetProfile:
    """Defines how a specific cutsheet format maps to canonical columns."""

    name: str
    version: str

    # Cutsheet tab column mapping: {source_col -> Canon.XXX}
    cutsheet_columns: Dict[str, str] = field(default_factory=dict)

    # Hosts tab column mapping: {source_col -> Canon.HOST_XXX}
    host_columns: Dict[str, str] = field(default_factory=dict)

    # Burndown tab column mapping: {source_col -> Canon.BD_XXX}
    burndown_columns: Dict[str, str] = field(default_factory=dict)

    # Extra status overrides beyond the global STATUS_NORMALIZATION
    status_overrides: Dict[str, str] = field(default_factory=dict)

    # Extra model overrides beyond the global MODEL_ALIASES
    model_overrides: Dict[str, str] = field(default_factory=dict)

    # Columns whose presence fingerprints this profile (for auto-detection)
    fingerprint_columns: List[str] = field(default_factory=list)


# --- Quincy / standard production format (V1) ---
# Real cutsheets use A-SIDE-DNS-NAME (hyphenated) not "A-SIDE DEVICE NAME".
# A-MODEL / Z-MODEL are explicit model columns on every connection row.
PROFILE_STANDARD_V1 = CutsheetProfile(
    name="standard_v1",
    version="1.0",
    # CP1: When multiple source columns map to the same Canon target, dict order
    # determines priority (first match wins in apply_profile). Production columns
    # are listed first; legacy/fallback variants follow with a comment.
    cutsheet_columns={
        "Section": Canon.SECTION,
        "A-SIDE-DNS-NAME": Canon.A_DEVICE,       # production column
        "A-SIDE DEVICE NAME": Canon.A_DEVICE,     # fallback; lower priority
        "A-SIDE PORT": Canon.A_PORT,              # production column
        "A-PORT": Canon.A_PORT,                   # fallback; lower priority
        "A-OPTIC": Canon.A_OPTIC,
        "A-SIDE LOCODE": Canon.A_LOCODE,
        "A-MODEL": Canon.A_MODEL,
        "A-LOC:CAB:RU": Canon.A_LOC_CAB_RU,
        "Z-SIDE-DNS-NAME": Canon.Z_DEVICE,       # production column
        "Z-SIDE DEVICE NAME": Canon.Z_DEVICE,     # fallback; lower priority
        "Z-SIDE PORT": Canon.Z_PORT,              # production column
        "Z-PORT": Canon.Z_PORT,                   # fallback; lower priority
        "Z-OPTIC": Canon.Z_OPTIC,
        "Z-SIDE LOCODE": Canon.Z_LOCODE,
        "Z-MODEL": Canon.Z_MODEL,
        "Z-LOC:CAB:RU": Canon.Z_LOC_CAB_RU,
        "CABLE ID": Canon.CABLE_ID,               # production column
        "CABLE": Canon.CABLE_ID,                   # fallback; lower priority
        "STATUS": Canon.STATUS,
        # Breakout columns (populated in Ellendale and larger sites)
        "A-BREAKOUT LOC:CAB:RU": Canon.A_BREAKOUT_LOC,
        "A-BREAKOUT SLOT:PORT": Canon.A_BREAKOUT_PORT,
        "Z-BREAKOUT LOC:CAB:RU": Canon.Z_BREAKOUT_LOC,
        "Z-BREAKOUT SLOT:PORT": Canon.Z_BREAKOUT_PORT,
        # Patch panel columns
        "A-PATCH-PANEL LOC:CAB:RU:PORT": Canon.A_PATCH_PANEL,
        "Z-PATCH-PANEL LOC:CAB:RU:PORT": Canon.Z_PATCH_PANEL,
    },
    host_columns={
        "DNS-A-RECORD": Canon.HOST_HOSTNAME,      # production column
        "Hostname": Canon.HOST_HOSTNAME,           # fallback; lower priority
        "NETBOX MODEL": Canon.HOST_MODEL,          # production column
        "Model": Canon.HOST_MODEL,                 # fallback; lower priority
        "Role": Canon.HOST_ROLE,                   # production column
        "ROLE": Canon.HOST_ROLE,                   # fallback; lower priority
        "ROW:TYPE": Canon.HOST_ROW_TYPE,           # physical placement, NOT functional role
        "LOC:ROW:TYPE": Canon.HOST_ROW_TYPE,       # OBG01+ variant with LOC: prefix
        "LOC:CAB:RU": Canon.HOST_RACK,             # production column
        "Rack": Canon.HOST_RACK,                   # fallback; lower priority
        "LOCODE": Canon.HOST_LOCODE,
        "Data Hall": Canon.HOST_DATA_HALL,
        "Status": Canon.HOST_STATUS,               # production column (Title Case)
        "STATUS": Canon.HOST_STATUS,               # fallback; lower priority
    },
    burndown_columns={
        "A-SIDE-DNS-NAME": Canon.BD_A_DEVICE,
        "A-SIDE DEVICE NAME": Canon.BD_A_DEVICE,
        "Z-SIDE-DNS-NAME": Canon.BD_Z_DEVICE,
        "Z-SIDE DEVICE NAME": Canon.BD_Z_DEVICE,
        "A-PORT": Canon.BD_A_PORT,
        "A-SIDE PORT": Canon.BD_A_PORT,
        "Z-PORT": Canon.BD_Z_PORT,
        "Z-SIDE PORT": Canon.BD_Z_PORT,
        "A-LOC:CAB:RU": Canon.BD_A_LOC_CAB_RU,
        "Z-LOC:CAB:RU": Canon.BD_Z_LOC_CAB_RU,
        "STATUS": Canon.STATUS,
        "LINK-STATUS": Canon.BD_LINK_STATUS,
        "CURRENT-NEIGHBOR": Canon.BD_CURRENT_NEIGHBOR,
        "CURRENT-NEIGHBOR-PORT": Canon.BD_CURRENT_NEIGHBOR_PORT,
        "CUTSHEET Row": Canon.BD_CUTSHEET_ROW,
        "Cutsheet Row": Canon.BD_CUTSHEET_ROW,
        "CUTSHEET-ROW": Canon.BD_CUTSHEET_ROW,     # OBG01+ all-caps hyphen variant
        "DCT notes/fixes": Canon.BD_DCT_NOTES,
        "DCT notes": Canon.BD_DCT_NOTES,
        "NetEng notes": Canon.BD_NETENG_NOTES,
    },
    fingerprint_columns=["A-SIDE-DNS-NAME", "A-OPTIC", "Z-OPTIC", "A-MODEL"],
)

# --- V2: space-separated fallback ---
# NOTE: Real Ellendale (US-LZL01) uses V1 column format (hyphenated, same as
# Quincy). V2 exists as a fallback for any future site that uses space-separated
# column names (e.g. "A SIDE DEVICE" instead of "A-SIDE-DNS-NAME").
PROFILE_STANDARD_V2 = CutsheetProfile(
    name="standard_v2",
    version="2.0",
    cutsheet_columns={
        "TOPOLOGY SECTION": Canon.SECTION,
        "A SIDE DEVICE": Canon.A_DEVICE,
        "A SIDE PORT": Canon.A_PORT,
        "A OPTIC": Canon.A_OPTIC,
        "A LOCODE": Canon.A_LOCODE,
        "Z SIDE DEVICE": Canon.Z_DEVICE,
        "Z SIDE PORT": Canon.Z_PORT,
        "Z OPTIC": Canon.Z_OPTIC,
        "Z LOCODE": Canon.Z_LOCODE,
        "A LOC:CAB:RU": Canon.A_LOC_CAB_RU,
        "A-LOC:CAB:RU": Canon.A_LOC_CAB_RU,
        "Z LOC:CAB:RU": Canon.Z_LOC_CAB_RU,
        "Z-LOC:CAB:RU": Canon.Z_LOC_CAB_RU,
        "A MODEL": Canon.A_MODEL,
        "A-MODEL": Canon.A_MODEL,
        "Z MODEL": Canon.Z_MODEL,
        "Z-MODEL": Canon.Z_MODEL,
        "CABLE": Canon.CABLE_ID,
        "INSTALL STATUS": Canon.STATUS,
        # Some V2 sheets use these variants
        "A-SIDE DEVICE": Canon.A_DEVICE,
        "Z-SIDE DEVICE": Canon.Z_DEVICE,
        "A-OPTIC": Canon.A_OPTIC,
        "Z-OPTIC": Canon.Z_OPTIC,
    },
    host_columns={
        "Device Name": Canon.HOST_HOSTNAME,
        "Device Model": Canon.HOST_MODEL,
        "Device Role": Canon.HOST_ROLE,
        "Rack Location": Canon.HOST_RACK,
        "Hall": Canon.HOST_DATA_HALL,
        "Install Status": Canon.HOST_STATUS,
    },
    burndown_columns={
        "A SIDE DEVICE": Canon.BD_A_DEVICE,
        "A-SIDE DEVICE": Canon.BD_A_DEVICE,
        "Z SIDE DEVICE": Canon.BD_Z_DEVICE,
        "Z-SIDE DEVICE": Canon.BD_Z_DEVICE,
        "A PORT": Canon.BD_A_PORT,
        "A-PORT": Canon.BD_A_PORT,
        "Z PORT": Canon.BD_Z_PORT,
        "Z-PORT": Canon.BD_Z_PORT,
        "A LOC:CAB:RU": Canon.BD_A_LOC_CAB_RU,
        "A-LOC:CAB:RU": Canon.BD_A_LOC_CAB_RU,
        "Z LOC:CAB:RU": Canon.BD_Z_LOC_CAB_RU,
        "Z-LOC:CAB:RU": Canon.BD_Z_LOC_CAB_RU,
        "STATUS": Canon.STATUS,
        "INSTALL STATUS": Canon.STATUS,
        "LINK-STATUS": Canon.BD_LINK_STATUS,
        "LINK STATUS": Canon.BD_LINK_STATUS,
        "CURRENT-NEIGHBOR": Canon.BD_CURRENT_NEIGHBOR,
        "CURRENT NEIGHBOR": Canon.BD_CURRENT_NEIGHBOR,
        "CURRENT-NEIGHBOR-PORT": Canon.BD_CURRENT_NEIGHBOR_PORT,
        "CURRENT NEIGHBOR PORT": Canon.BD_CURRENT_NEIGHBOR_PORT,
        "CUTSHEET Row": Canon.BD_CUTSHEET_ROW,
        "Cutsheet Row": Canon.BD_CUTSHEET_ROW,
        "DCT notes/fixes": Canon.BD_DCT_NOTES,
        "DCT notes": Canon.BD_DCT_NOTES,
        "NetEng notes": Canon.BD_NETENG_NOTES,
    },
    fingerprint_columns=["TOPOLOGY SECTION", "A SIDE DEVICE", "Z SIDE DEVICE", "INSTALL STATUS"],
)

# --- Catch-all for oddball naming ---
PROFILE_ALTERNATE = CutsheetProfile(
    name="alternate",
    version="0.1",
    cutsheet_columns={
        "Topology": Canon.SECTION,
        "Source Device": Canon.A_DEVICE,
        "Source Port": Canon.A_PORT,
        "Source Optic": Canon.A_OPTIC,
        "Source Location": Canon.A_LOCODE,
        "Dest Device": Canon.Z_DEVICE,
        "Dest Port": Canon.Z_PORT,
        "Dest Optic": Canon.Z_OPTIC,
        "Dest Location": Canon.Z_LOCODE,
        "Cable Number": Canon.CABLE_ID,
        "State": Canon.STATUS,
    },
    host_columns={},
    # 4 fingerprint cols so 75% threshold detects at 3-of-4 match (CP9)
    fingerprint_columns=["Source Device", "Dest Device", "Source Optic", "Cable Number"],
)

# Ordered by specificity: most specific fingerprint first
PROFILE_REGISTRY: List[CutsheetProfile] = [
    PROFILE_STANDARD_V1,
    PROFILE_STANDARD_V2,
    PROFILE_ALTERNATE,
]


# ---------------------------------------------------------------------------
# Detection and normalization functions
# ---------------------------------------------------------------------------

def detect_profile(
    df: pd.DataFrame,
    min_score: float = 0.75,
) -> Tuple[Optional[CutsheetProfile], float]:
    """Auto-detect which profile matches the DataFrame columns.

    Returns (profile, score).  Score is 0.0-1.0 representing fraction of
    fingerprint columns matched.  Returns (None, 0.0) if no profile meets
    *min_score*.
    """
    cols = {str(c).strip() for c in df.columns}
    cols_upper = {c.upper() for c in cols}

    best_profile = None
    best_score = 0.0
    best_matched: List[str] = []
    best_missing: List[str] = []

    for profile in PROFILE_REGISTRY:
        if not profile.fingerprint_columns:
            continue
        matched = []
        missed = []
        for fp in profile.fingerprint_columns:
            if fp in cols or fp.upper() in cols_upper:
                matched.append(fp)
            else:
                missed.append(fp)
        score = len(matched) / len(profile.fingerprint_columns)
        if score > best_score:
            best_score = score
            best_profile = profile
            best_matched = matched
            best_missing = missed

    if best_score >= min_score:
        if best_missing:
            log.warning(
                "Profile '%s' matched at %.0f%% (missing fingerprint cols: %s). "
                "Proceeding but data quality may be reduced.",
                best_profile.name, best_score * 100, best_missing,
            )
        return best_profile, best_score
    return None, best_score


def apply_profile(df: pd.DataFrame, profile: CutsheetProfile, sheet_type: str = "cutsheet") -> pd.DataFrame:
    """Rename columns according to the profile mapping."""
    if sheet_type == "cutsheet":
        mapping = profile.cutsheet_columns
    elif sheet_type == "burndown":
        mapping = profile.burndown_columns
    else:
        mapping = profile.host_columns
    if not mapping:
        log.warning("Profile '%s' has no %s column mapping — "
                    "canonicalization skipped for this sheet type",
                    profile.name, sheet_type)
        return df

    # Normalize column headers: collapse newlines (Excel cell-wrap) to spaces
    df.columns = [str(c).replace("\n", " ").strip() for c in df.columns]

    # Build case-insensitive rename map
    rename_map = {}
    cols_lower = {str(c).strip().lower(): str(c).strip() for c in df.columns}

    for source_col, canon_col in mapping.items():
        # Try exact match first
        if source_col in df.columns:
            rename_map[source_col] = canon_col
        # Try case-insensitive
        elif source_col.lower() in cols_lower:
            actual = cols_lower[source_col.lower()]
            rename_map[actual] = canon_col

    # CP1: When multiple source columns map to the same Canon target
    # (e.g. both "A-SIDE-DNS-NAME" and "A-SIDE DEVICE NAME" -> Canon.A_DEVICE),
    # compare their values row-by-row.  If they disagree on non-empty values,
    # log the conflict count so it's auditable.  Keep the primary (first-mapped)
    # column and drop the fallback.
    seen_targets: Dict[str, str] = {}  # canon_col -> first source_col
    drop_cols = []
    for source_col, canon_col in list(rename_map.items()):
        if canon_col in seen_targets:
            primary_col = seen_targets[canon_col]
            # Check for value conflicts: both columns non-empty and different
            if source_col in df.columns and primary_col in df.columns:
                p = df[primary_col].fillna("").astype(str).str.strip()
                s = df[source_col].fillna("").astype(str).str.strip()
                empty_primary = (p == "")
                if empty_primary.any():
                    df.loc[empty_primary, primary_col] = df.loc[empty_primary, source_col]
                both_filled = (p != "") & (s != "")
                conflicts = both_filled & (p != s)
                n_conflicts = conflicts.sum()
                if n_conflicts > 0:
                    log.warning(
                        "Column conflict: '%s' and '%s' both map to '%s' "
                        "but disagree on %d rows. Keeping '%s', dropping '%s'. "
                        "First conflict at row %d: '%s' vs '%s'.",
                        primary_col, source_col, canon_col,
                        n_conflicts, primary_col, source_col,
                        conflicts.idxmax(),
                        p[conflicts.idxmax()], s[conflicts.idxmax()],
                    )
            drop_cols.append(source_col)
            del rename_map[source_col]
        else:
            seen_targets[canon_col] = source_col

    if drop_cols:
        df = df.drop(columns=drop_cols, errors="ignore")

    if rename_map:
        df = df.rename(columns=rename_map)
    return df


def normalize_status(raw: str) -> str:
    """Normalize a single status string to canonical form."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        # Handle NaN (float NaN is truthy, so 'not raw' won't catch it)
        try:
            if math.isnan(raw):
                return ""
        except (TypeError, ValueError):
            pass
        return str(raw).strip() if raw else ""
    cleaned = raw.strip()
    if not cleaned or cleaned.lower() == "nan":
        return ""
    key = cleaned.lower()
    return STATUS_NORMALIZATION.get(key, cleaned)


def normalize_status_column(df: pd.DataFrame, col: str = Canon.STATUS) -> pd.DataFrame:
    """Normalize the STATUS column in-place (vectorized via .map())."""
    if col in df.columns:
        cleaned = df[col].fillna("").astype(str).str.strip()
        # Replace "nan" strings with empty
        cleaned = cleaned.where(cleaned.str.lower() != "nan", "")
        # Vectorized lookup: map lowercase → canonical, fall back to cleaned value
        mapped = cleaned.str.lower().map(STATUS_NORMALIZATION)
        df[col] = mapped.fillna(cleaned)
    return df


# Pre-built regex for fuzzy model matching: strips common suffixes like
# -revA, -revB, -rev2, _v2, trailing whitespace/punctuation so that
# "SN5610-revB" and "SN5610 " both resolve via the alias table.
_MODEL_SUFFIX_RE = re.compile(
    r"[-_]\s*(?:rev[a-z0-9]*|v\d+|r\d+)$", re.IGNORECASE
)


def normalize_model(raw: str) -> str:
    """Normalize a single model string to canonical form.

    Resolution order:
      1. Exact lowercase match in MODEL_ALIASES
      2. Strip known suffixes (-revB, -v2, etc.) and retry
      3. Return the cleaned (whitespace-stripped) original
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        try:
            if math.isnan(raw):
                return ""
        except (TypeError, ValueError):
            pass
        return str(raw).strip() if raw else ""
    cleaned = raw.strip()
    if not cleaned or cleaned.lower() == "nan":
        return ""
    key = cleaned.lower()
    # 1. Exact match
    result = MODEL_ALIASES.get(key)
    if result:
        return result
    # 2. Strip known revision/version suffixes and retry
    stripped = _MODEL_SUFFIX_RE.sub("", key).strip()
    if stripped != key:
        result = MODEL_ALIASES.get(stripped)
        if result:
            return result
    # 3. No alias found, return cleaned original
    return cleaned


def normalize_model_column(df: pd.DataFrame, col: str = Canon.MODEL) -> pd.DataFrame:
    """Normalize a MODEL column in-place (vectorized via .map()).

    Uses the same resolution order as normalize_model(): exact match first,
    then suffix-stripped retry.
    """
    if col in df.columns:
        cleaned = df[col].fillna("").astype(str).str.strip()
        # Replace "nan" strings with empty
        cleaned = cleaned.where(cleaned.str.lower() != "nan", "")
        lowered = cleaned.str.lower()
        # 1. Exact alias match
        mapped = lowered.map(MODEL_ALIASES)
        # 2. For unresolved rows, strip suffixes and retry
        unresolved = mapped.isna() & (cleaned != "")
        if unresolved.any():
            stripped = lowered[unresolved].str.replace(
                _MODEL_SUFFIX_RE, "", regex=True
            ).str.strip()
            mapped[unresolved] = stripped.map(MODEL_ALIASES)
        # 3. Fall back to cleaned original for anything still unresolved
        df[col] = mapped.fillna(cleaned)
    return df


def canonicalize(
    df: pd.DataFrame,
    sheet_type: str = "cutsheet",
    profile: Optional[CutsheetProfile] = None,
) -> Tuple[pd.DataFrame, Optional[CutsheetProfile]]:
    """
    Full canonicalization pipeline:
      1. Detect profile (or use provided one)
      2. Rename columns via profile mapping
      3. Normalize STATUS values
      4. Normalize MODEL values (if column exists)

    Returns (modified_df, detected_profile).
    """
    if profile is None:
        profile, _score = detect_profile(df)

    if profile is not None:
        df = apply_profile(df, profile, sheet_type=sheet_type)

    df = normalize_status_column(df)

    # CP8: Apply per-profile status overrides after global normalization.
    # Overrides take precedence over global STATUS_NORMALIZATION.
    # Vectorized via .map() to match CP6 pattern (no .apply() lambdas).
    if profile is not None and profile.status_overrides:
        if Canon.STATUS in df.columns:
            override_map = {k.lower(): v for k, v in profile.status_overrides.items()}
            mapped = df[Canon.STATUS].str.lower().map(override_map)
            df[Canon.STATUS] = mapped.fillna(df[Canon.STATUS])

    # Normalize model in any column that looks like a model column
    # NOTE: burndown sheets have no model column; intentional omission of BD_* here
    for col in [Canon.MODEL, Canon.HOST_MODEL, Canon.A_MODEL, Canon.Z_MODEL]:
        if col in df.columns:
            df = normalize_model_column(df, col)

    # CP8: Apply per-profile model overrides after global normalization.
    # Vectorized via .map() to match CP6 pattern.
    if profile is not None and profile.model_overrides:
        model_override_map = {k.lower(): v for k, v in profile.model_overrides.items()}
        for col in [Canon.MODEL, Canon.HOST_MODEL, Canon.A_MODEL, Canon.Z_MODEL]:
            if col in df.columns:
                mapped = df[col].str.lower().map(model_override_map)
                df[col] = mapped.fillna(df[col])

    # Also normalize A-OPTIC / Z-OPTIC as model-like values
    # R12: fillna before astype(str) to prevent NaN→"nan", then replace "nan"→""
    for optic_col in [Canon.A_OPTIC, Canon.Z_OPTIC]:
        if optic_col in df.columns:
            s = df[optic_col].fillna("").astype(str).str.strip()
            df[optic_col] = s.where(s.str.lower() != "nan", "")

    return df, profile


def profile_to_dict(profile: Optional[CutsheetProfile]) -> Optional[Dict[str, Any]]:
    """Serialize a profile for audit trail / API response."""
    if profile is None:
        return None
    return {
        "name": profile.name,
        "version": profile.version,
        "fingerprint_columns": profile.fingerprint_columns,
    }
