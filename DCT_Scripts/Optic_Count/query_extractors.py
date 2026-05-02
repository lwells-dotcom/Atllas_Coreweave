"""
query_extractors.py - Focused structured extractors for query routing.

Each extractor returns a typed value (str, tuple, etc.) from the question text.
Regex is the right tool here: hostnames, LOC:CAB:RU tokens, model IDs, and
tier patterns are all structurally defined.

Extractors are run ONCE during QuestionContext construction and their results
are reused by all domain routers -- no duplicate regex work.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

from query_lexicon import ROLE_KEYWORD_MAP

# ---------------------------------------------------------------------------
# Pre-compiled patterns
# ---------------------------------------------------------------------------

# LOC:CAB:RU exact token (e.g. dh201:042:42)
_LOC_CAB_RU_RE = re.compile(r"\b([a-z]{1,4}\d+:\d+:\d+)\b", re.I)

# Rack token without RU (e.g. dh201:042)
_RACK_LOC_RE = re.compile(r"\b([a-z]{1,4}\d+:\d+)\b", re.I)

# Data-hall prefix (e.g. dh201, dh3)
_DH_PREFIX_RE = re.compile(r"\b(dh\d+)\b", re.I)

# Data-hall plus rack in spaced form (e.g. "dh2 041")
_DH_RACK_SPACED_RE = re.compile(r"\b(dh\d+)\s+(\d{1,4})\b", re.I)

# Data-hall plus rack with rack keyword (e.g. "dh202 rack 41")
_DH_RACK_KEYWORD_RE = re.compile(r"\b(dh\d+)\s+(?:rack|cabinet|cab)\s+#?(\d{1,4})\b", re.I)

# Spelled-out "Data Hall N rack M" (e.g. "Data Hall 4 rack 66")
_DH_FULL_RACK_RE = re.compile(
    r"\bdata[\s-]?hall\s*(\d+)\s+(?:rack|cabinet|cab)\s+#?(\d{1,4})\b", re.I
)

# Rack keyword plus data-hall (e.g. "rack 41 in dh202")
_RACK_DH_RE = re.compile(
    r"\b(?:rack|cabinet|cab)\s+#?(\d{1,4})\s+(?:in|at|on)\s+(dh\d+)\b",
    re.I,
)

# Cabinet / rack number after keyword — requires at least one digit so verbs
# like "has", "are", "with" don't become fake rack identifiers.
_RACK_KEYWORD_RE = re.compile(
    r"\b(?:rack|cabinet|cab)\s+#?([A-Z0-9:_-]*\d[A-Z0-9:_-]*)\b", re.I
)

# Optic types: QSFP28, SFP+, QSFP-DD, QSFPDD-400G-DR4, etc.
_OPTIC_RE = re.compile(r"\b(qsfp[\w-]*|sfp[+\w-]+)\b", re.I)

# Device hostname candidates (ordered by specificity)
_DEVICE_PATTERNS = [
    re.compile(r"\b([A-Z]{2,}\d{3,}[\w-]*)\b", re.I),        # SN5610, SN4700-01
    re.compile(r"\b(\w+-\w+-\w+(?:-\w+)*)\b", re.I),           # rack-device-port
    re.compile(r"\b([a-z]+-[a-z]+-\d+[\w-]*)\b", re.I),        # lowercase-name-123
]
_DEVICE_FALSE_POSITIVES = frozenset({
    "how-many", "tell-me", "show-me", "what-is", "how-are",
    "are-there", "there-any", "which-device", "the-most",
    "the-same", "any-z",
})

# Model ID candidates (ordered by specificity)
_MODEL_PATTERNS = [
    re.compile(r"\b([A-Z]{2,}-?\d{3,}(?:[\w-]*)?)\b", re.I),         # SN5610, PA-1420
    re.compile(r"\b(\d{4}-[A-Z]{2,}(?:[\w-]*)?)\b", re.I),            # 7750-SR-1SE
    re.compile(r"\b([A-Z]{2,}(?:-[A-Z0-9]+)+-\d{2,}[\w-]*)\b", re.I), # CPU-GP2-02
    re.compile(r"\b(\d{1,2}[A-Z]-[\w-]{3,})\b", re.I),                # 1U-1N-GEN5
]
_MODEL_FALSE_POSITIVES = frozenset({
    "how-many", "tell-me", "show-me", "what-is", "how-are",
    "are-there", "there-any", "which-device", "the-most",
    "the-same", "any-z",
})

_LOCATIONISH_TOKEN_RE = re.compile(r"^(?:dh|row|rack|cab|ru)\d+$", re.I)

# Tier-to-tier pattern (e.g. "TIER-3 TO TIER-2")
_TIER_RANGE_RE = re.compile(r"\b(tier[\s-]*\d\s+to\s+tier[\s-]*\d)\b", re.I)

# Named section prefixes (multi-word, e.g. "BACKBONE MGMT", "OOB-FW")
_NAMED_SECTION_RE = re.compile(
    r"\b(BACKBONE[\s/]*(?:MGMT|BFR|OPTICAL[\s/]*MGMT)?|OOB-?FW|FBS|"
    r"MGMT-?CORE|MGMT-?DIST|GRID-?AGG-?[A-E]?|INFRA-?DIST-?[A-E]?|"
    r"POD-?DIST-?[A-E]?\s*\d*|ROCE(?:-[\w]+)?)\b",
    re.I,
)

# Section name token near "section" keyword
_SECTION_STOP = frozenset({
    "the", "a", "an", "all", "topology", "different", "various", "this", "that",
    "how", "many", "connections", "cables", "combined", "together", "total", "in",
    "summary", "overview", "breakdown", "list", "for", "by", "count", "info",
    "site", "what", "which", "are", "is", "cutsheet", "exist", "defined",
    "has", "have", "had", "any", "some", "each", "every", "no", "do", "does",
    "best", "worst", "highest", "lowest", "most", "least", "zero", "where",
    "there", "percentage", "number", "rate", "with", "without", "complete",
    "incomplete", "concentration", "show", "shows", "contain", "contains",
})

# Section filter: topology keyword for optic_count section scoping
_SECTION_FILTER_RE = re.compile(
    r"\b(spine|border[\s-]?leaf|leaf|fabric|core|access|distribution|aggregation)\b",
    re.I,
)

# GG-prefix section names (e.g. GG1-c, GG20-A)
_GG_SECTION_RE = re.compile(r"\bGG\d+-?[A-Z]\b", re.I)

# Compound section names (NET-AGG, COMP-DIST, UFM-PATH, etc.)
_COMPOUND_SECTION_RE = re.compile(
    r"\b(?:NET-AGG|COMP-AGG|NET-DIST|COMP-DIST|UFM-PATH|GG\d)\b", re.I
)

# Side detection
_Z_SIDE_RE = re.compile(r"\bz[\s-]?side\b", re.I)
_A_SIDE_RE = re.compile(r"\ba[\s-]?side\b", re.I)

# Role keyword patterns (compiled once)
_ROLE_COMPILED = [(re.compile(pat, re.I), role) for pat, role in ROLE_KEYWORD_MAP]

# IP address
_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){1,3})\b")

# Cable media type (CAT6a, MPO12, MPO8, LC-TO-LC, SMF, MMF, fiber, copper)
_CABLE_TYPE_RE = re.compile(
    r"\b(cat6a?|mpo[\s-]?\d+|lc[\s-]?to[\s-]?lc|smf|mmf|"
    r"single[\s-]mode|multi[\s-]mode|fiber|copper)\b",
    re.I,
)

# Data-hall filter (e.g. "dh201", "DH 204", "data hall 201")
_DATA_HALL_FILTER_RE = re.compile(
    r"\bdata[\s-]?hall\s*(\d+)\b|\bdh\s*(\d+)\b",
    re.I,
)


# ---------------------------------------------------------------------------
# Public extractors
# ---------------------------------------------------------------------------

def extract_device_name(question: str) -> Optional[str]:
    """Extract a device hostname from the question text."""
    for pat in _DEVICE_PATTERNS:
        m = pat.search(question)
        if m:
            candidate = m.group(1)
            if candidate.lower() not in _DEVICE_FALSE_POSITIVES and not _LOCATIONISH_TOKEN_RE.fullmatch(candidate):
                return candidate
    return None


def extract_location(question: str) -> str:
    """Extract a rack/cabinet/LOC:CAB:RU identifier.

    Tries exact LOC:CAB:RU first, then rack token, then spaced data-hall+rack,
    then data-hall prefix, then rack keyword.
    Returns empty string if nothing found.
    """
    m = _LOC_CAB_RU_RE.search(question)
    if m:
        return m.group(1)
    m = _RACK_LOC_RE.search(question)
    if m:
        return m.group(1)
    m = _DH_FULL_RACK_RE.search(question)
    if m:
        return f"dh{m.group(1)}:{m.group(2).zfill(3)}"
    m = _DH_RACK_KEYWORD_RE.search(question)
    if m:
        return f"{m.group(1)}:{m.group(2).zfill(3)}"
    m = _RACK_DH_RE.search(question)
    if m:
        return f"{m.group(2)}:{m.group(1).zfill(3)}"
    m = _DH_RACK_SPACED_RE.search(question)
    if m:
        return f"{m.group(1)}:{m.group(2).zfill(3)}"
    m = _DH_PREFIX_RE.search(question)
    if m:
        return m.group(1)
    m = _RACK_KEYWORD_RE.search(question)
    if m:
        candidate = m.group(1)
        if candidate.upper() not in ("RU", "LOC", "CAB"):
            return candidate
    return ""


def extract_optic_type(question: str) -> str:
    """Extract optic type (e.g. QSFP28, SFP+, QSFP-DD). Returns '' if none."""
    m = _OPTIC_RE.search(question)
    return m.group(1).upper() if m else ""


def extract_section_name(question: str) -> str:
    """Extract section name from question (e.g. 'GG1-c', 'SPINE', 'TIER-3 TO TIER-2').

    Handles multi-word tier patterns, named section prefixes, GG-prefix names,
    and tokens adjacent to the word 'section'.
    """
    # Multi-word tier patterns
    m = _TIER_RANGE_RE.search(question)
    if m:
        raw = m.group(1).upper()
        raw = re.sub(r"TIER\s*(\d)", r"TIER-\1", raw)
        raw = re.sub(r"\s+TO\s+", " TO ", raw)
        return raw

    # Named section prefixes
    m = _NAMED_SECTION_RE.search(question)
    if m:
        return m.group(1).strip()

    # GG-prefix
    m = _GG_SECTION_RE.search(question)
    if m:
        return m.group(0)

    # Compound names
    m = _COMPOUND_SECTION_RE.search(question)
    if m:
        return m.group(0)

    # Token immediately before "section(s)"
    m = re.search(r"\b([A-Za-z0-9][\w-]{1,30})\s+sections?\b", question, re.I)
    if m and m.group(1).lower() not in _SECTION_STOP:
        return m.group(1)

    # Token after "section" keyword
    m = re.search(r"\bsections?\s+([A-Za-z0-9][\w-]{1,30})\b", question, re.I)
    if m and m.group(1).lower() not in _SECTION_STOP:
        return m.group(1)

    return ""


def extract_section_filter(question: str) -> str:
    """Extract topology section keyword for optic_count scoping (e.g. SPINE, LEAF)."""
    m = _SECTION_FILTER_RE.search(question)
    return m.group(1).upper() if m else ""


def extract_model(question: str) -> str:
    """Extract a hardware model identifier (e.g. SN5610, PA-1420, 7750-SR-1SE)."""
    for pat in _MODEL_PATTERNS:
        m = pat.search(question)
        if m:
            candidate = m.group(1)
            lower = candidate.lower()
            if lower not in _MODEL_FALSE_POSITIVES and not _LOCATIONISH_TOKEN_RE.fullmatch(candidate):
                last_segment = candidate.rsplit("-", 1)[-1]
                if lower.endswith("s") and re.search(r"\d", last_segment[:-1]):
                    return candidate[:-1]
                return candidate
    return ""


def extract_model_status_filter(question: str) -> tuple[list[str], str]:
    """Extract a model-scoped status filter from natural phrasing.

    Returns (status_normalized_values, human_label). Empty values mean no
    model-specific status constraint was detected.

    Notes:
      - "in service" matches the workbook-facing completion family:
        LLDP Passed, Human Verified, or Complete.
      - More specific phrases are checked first so "LLDP Failed" doesn't get
        swallowed by a generic "failed" check later.
    """
    normalized = " ".join(question.lower().split())

    if re.search(r"\bin[\s-]*service\b", normalized):
        return ["lldp_passed", "human_verified", "complete"], "In service"
    if re.search(r"\blldp[\s-]*passed\b", normalized):
        return ["lldp_passed"], "LLDP Passed"
    if re.search(r"\bhuman[\s-]*verified\b", normalized):
        return ["human_verified"], "Human Verified"
    if re.search(r"\blldp[\s-]*failed\b", normalized):
        return ["lldp_failed"], "LLDP Failed"
    if re.search(r"\bnot[\s-]*terminated\b", normalized):
        return ["not_terminated"], "Not Terminated"
    if re.search(r"\bnot[\s-]*run\b", normalized):
        return ["not_run"], "Not Run"
    if re.search(r"\b(?:cable\s+is\s+ran[:\s-]*)?complete\b", normalized):
        return ["complete"], "Complete"

    return [], ""


def extract_role_and_side(question: str) -> Tuple[str, str]:
    """Extract device role keyword and side (A/Z).

    Returns (role_filter, side_filter) where side_filter is 'A', 'Z', or ''.
    role_filter is a bare keyword (not wrapped in %); callers add ILIKE padding.
    """
    side = ""
    if _Z_SIDE_RE.search(question):
        side = "Z"
    elif _A_SIDE_RE.search(question):
        side = "A"

    for pat, role in _ROLE_COMPILED:
        if pat.search(question):
            return role, side

    return "", side


def extract_side(question: str) -> str:
    """Extract just the side (A/Z) from the question. Returns '' if none."""
    if _Z_SIDE_RE.search(question):
        return "Z"
    if _A_SIDE_RE.search(question):
        return "A"
    return ""


def extract_ip(question: str) -> str:
    """Extract an IP address from the question. Returns '' if none."""
    m = _IP_RE.search(question)
    return m.group(1) if m else ""


def extract_cable_type(question: str) -> str:
    """Extract cable media type (e.g. CAT6a, MPO12, SMF, fiber, copper). Returns '' if none."""
    m = _CABLE_TYPE_RE.search(question)
    return m.group(1).upper() if m else ""


def extract_data_hall(question: str) -> str:
    """Extract normalized data hall ID (e.g. 'dh202') from 'dh202', 'DH 204', 'data hall 201'.

    Returns '' if none found.
    """
    m = _DATA_HALL_FILTER_RE.search(question)
    if m:
        num = m.group(1) or m.group(2)
        return f"dh{num}"
    return ""


def has_loc_token(question: str) -> bool:
    """Check if question contains an exact LOC:CAB:RU token."""
    return bool(_LOC_CAB_RU_RE.search(question))


def has_optic_token(question: str) -> bool:
    """Check if question contains an optic type token."""
    return bool(_OPTIC_RE.search(question))


def has_model_token(question: str) -> bool:
    """Check if the question contains something that looks like a model ID."""
    return bool(extract_model(question))


def has_device_token(question: str) -> bool:
    """Check if the question contains something that looks like a device hostname."""
    return extract_device_name(question) is not None


# ---------------------------------------------------------------------------
# Upload ID extraction
# ---------------------------------------------------------------------------

_UPLOAD_ID_PATTERNS = [
    re.compile(r"upload\s+#?(\d+)\s+(?:and|vs|versus)\s+(?:upload\s+)?#?(\d+)", re.I),
    re.compile(r"between\s+(?:upload\s+)?#?(\d+)\s+and\s+(?:upload\s+)?#?(\d+)", re.I),
    re.compile(r"from\s+(?:upload\s+)?#?(\d+)\s+to\s+(?:upload\s+)?#?(\d+)", re.I),
    re.compile(r"#(\d+)\s+(?:vs|versus)\s+#?(\d+)", re.I),
    re.compile(r"diff\s+(?:upload\s+)?#?(\d+)\s+and\s+(?:upload\s+)?#?(\d+)", re.I),
]


def extract_upload_ids(question: str) -> Tuple[Optional[int], Optional[int]]:
    """Extract two upload IDs from a comparison question.

    Matches: "compare upload 5 and upload 8", "diff between #5 and #8",
    "from upload 5 to 8", "upload #5 vs #8".
    Returns (upload_id_a, upload_id_b) or (None, None).
    """
    for pat in _UPLOAD_ID_PATTERNS:
        m = pat.search(question)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None, None


# ---------------------------------------------------------------------------
# Site code extraction
# ---------------------------------------------------------------------------

_SITE_CODE_RE = re.compile(r"\b(US-[A-Z]{3}\d{2})\b", re.I)

_SITE_NAME_MAP = {
    "quincy": "QCY",
    "qcy": "QCY",
    "ellendale": "ELLENDALE",
    "salt lake": "US-SLO01",
    "slo": "US-SLO01",
    "lzl": "US-LZL01",
}

_KNOWN_SITE_ABBREVS = frozenset({
    "QCY", "ORD", "DFW", "LAX", "ATL", "DEN", "ELD",
    "PHE", "SLO", "LZL", "IAD", "SJC",
})


def extract_site_codes(question: str) -> list[str]:
    """Extract site codes from the question (US-XXX## format or known abbreviations)."""
    codes: set[str] = set()
    lower = question.lower()

    for name, code in _SITE_NAME_MAP.items():
        if name in lower:
            codes.add(code.upper())

    for m in _SITE_CODE_RE.finditer(question):
        codes.add(m.group(1).upper())

    for abbrev in _KNOWN_SITE_ABBREVS:
        if re.search(rf"\b{abbrev}\b", question, re.I):
            codes.add(abbrev)

    return sorted(codes)
