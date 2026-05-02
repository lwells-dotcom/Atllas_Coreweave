"""
query_intent.py - Intent classification layer for the Atlas query router.

Replaces the monolithic _PATTERNS first-match-wins regex list with domain routers
that each own one concern.  Domain routers run in priority order and return an
IntentResult on match or None to defer.

Key improvements over the old approach:
  - Keyword set intersection instead of mirrored regex pairs
  - Each domain router is self-contained and testable in isolation
  - QuestionContext extracts once, reused everywhere (no duplicate regex work)
  - Debug metadata (IntentResult.reason, matched_signals) for observability
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import query_extractors as ext
from query_lexicon import (
    BURNDOWN_WORDS,
    CABLE_WORDS,
    COMPLETION_WORDS,
    CONNECTION_WORDS,
    COUNT_WORDS,
    CROSS_SITE_WORDS,
    DETAIL_WORDS,
    DEVICE_WORDS,
    DIFF_WORDS,
    FAIL_WORDS,
    IP_WORDS,
    LLDP_WORDS,
    LIST_WORDS,
    LOCATION_WORDS,
    MISMATCH_WORDS,
    OPTIC_WORDS,
    RANKING_WORDS,
    ROLE_WORDS,
    SECTION_WORDS,
    SIDE_WORDS,
    SITE_WORDS,
    STATUS_WORDS,
    TREND_WORDS,
    UPLOAD_WORDS,
)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class QuestionContext:
    """Pre-processed question with extracted entities and token flags."""
    raw: str
    normalized: str
    tokens: list[str]
    token_set: frozenset[str]
    # Extractor results (run once)
    has_loc_token: bool
    has_model_token: bool
    has_device_token: bool
    has_optic_token: bool
    extracted_device: Optional[str]
    extracted_location: str
    extracted_model: str
    extracted_section: str
    extracted_section_filter: str
    extracted_optic: str
    extracted_role: str
    extracted_side: str
    extracted_ip: str
    extracted_cable_type: str
    extracted_data_hall: str


@dataclass
class IntentResult:
    """Classification output with audit trail."""
    question_type: str
    confidence: str  # "high", "medium", "low"
    reason: str
    matched_domain: str
    matched_signals: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def build_context(question: str) -> QuestionContext:
    """Normalize the question and run all extractors once."""
    normalized = " ".join(question.lower().split())
    tokens = re.findall(r"[a-z0-9][\w+-]*", normalized)
    token_set = frozenset(tokens)

    role, side = ext.extract_role_and_side(question)

    return QuestionContext(
        raw=question,
        normalized=normalized,
        tokens=tokens,
        token_set=token_set,
        has_loc_token=ext.has_loc_token(question),
        has_model_token=ext.has_model_token(question),
        has_device_token=ext.has_device_token(question),
        has_optic_token=ext.has_optic_token(question),
        extracted_device=ext.extract_device_name(question),
        extracted_location=ext.extract_location(question),
        extracted_model=ext.extract_model(question),
        extracted_section=ext.extract_section_name(question),
        extracted_section_filter=ext.extract_section_filter(question),
        extracted_optic=ext.extract_optic_type(question),
        extracted_role=role,
        extracted_side=side,
        extracted_ip=ext.extract_ip(question),
        extracted_cable_type=ext.extract_cable_type(question),
        extracted_data_hall=ext.extract_data_hall(question),
    )


# ---------------------------------------------------------------------------
# Helper: token set overlap check
# ---------------------------------------------------------------------------

def _hits(token_set: frozenset[str], word_set: frozenset[str]) -> frozenset[str]:
    """Return the intersection of token_set and word_set."""
    return token_set & word_set


# Strong diff signals: words that unambiguously mean "compare two things".
# Excludes "new", "added", "removed", "missing", "changed" which are common
# infrastructure vocabulary and cause false positives with UPLOAD_WORDS.
_STRONG_DIFF_SIGNALS: frozenset[str] = frozenset({
    "diff", "compare", "comparison", "versus", "vs", "delta",
    "difference", "differences", "modified",
})


# ---------------------------------------------------------------------------
# Domain routers (each returns IntentResult or None)
# ---------------------------------------------------------------------------

def route_burndown_intent(ctx: QuestionContext) -> Optional[IntentResult]:
    """Burndown / link-status sheet queries."""
    if "burndown" in ctx.token_set:
        return IntentResult("link_status", "high", "explicit burndown keyword",
                            "burndown", ["burndown"])

    lldp = _hits(ctx.token_set, LLDP_WORDS)
    mismatch = _hits(ctx.token_set, MISMATCH_WORDS)

    # "neighbor mismatch" - must check before generic LLDP routes
    if "neighbor" in ctx.token_set and mismatch:
        return IntentResult("lldp_neighbor_mismatch", "high",
                            "neighbor + mismatch words",
                            "burndown", ["neighbor"] + sorted(mismatch))

    # LLDP neighbor (not mismatch) -> still burndown territory
    if lldp and "neighbor" in ctx.token_set:
        # Check for mismatch/expect/wrong context
        if mismatch:
            return IntentResult("lldp_neighbor_mismatch", "high",
                                "lldp neighbor + mismatch context",
                                "burndown", sorted(lldp | mismatch))
        return IntentResult("link_status", "medium",
                            "lldp neighbor without mismatch words",
                            "burndown", sorted(lldp) + ["neighbor"])

    # "wrong neighbors" standalone
    if re.search(r"\bwrong\s+neighbors?\b", ctx.normalized):
        return IntentResult("lldp_neighbor_mismatch", "high",
                            "wrong neighbor phrase",
                            "burndown", ["wrong", "neighbor"])

    # "expected/actual neighbor"
    if mismatch and "neighbor" in ctx.token_set:
        return IntentResult("lldp_neighbor_mismatch", "medium",
                            "mismatch words + neighbor",
                            "burndown", sorted(mismatch) + ["neighbor"])

    # Link status keywords (but NOT "link health" which is generic connection_status)
    if re.search(r"\blink\s*(?:status|state|up|down)\b", ctx.normalized):
        return IntentResult("link_status", "high",
                            "link status/state/up/down phrase",
                            "burndown", ["link_status"])

    # Ports/links + up/down/fail
    if re.search(r"\b(?:which|what)\b.*\b(?:links?|ports?)\b.*\b(?:down|up|fail)\b",
                 ctx.normalized):
        return IntentResult("link_status", "medium",
                            "which links/ports up/down/fail",
                            "burndown", ["link_port_status"])

    return None


def route_lldp_intent(ctx: QuestionContext) -> Optional[IntentResult]:
    """LLDP-specific routing (failures, counts, ratios, generic LLDP mention).

    Runs AFTER route_burndown_intent so neighbor-mismatch is already handled.
    Defers to trend router when trend words are present.
    """
    lldp = _hits(ctx.token_set, LLDP_WORDS)
    if not lldp:
        return None

    # Defer to trend router if trend signals present
    trend_hits = _hits(ctx.token_set, TREND_WORDS)
    if trend_hits & {"trend", "trending", "trends", "progression", "progress",
                     "trajectory", "evolution", "timeline", "historical"}:
        return None

    fail = _hits(ctx.token_set, FAIL_WORDS)
    count = _hits(ctx.token_set, COUNT_WORDS)

    # Ratio/percentage questions -> connection_status
    if ctx.token_set & {"ratio", "percentage", "percent"}:
        return IntentResult("connection_status", "high",
                            "lldp + ratio/percentage",
                            "lldp", sorted(lldp) + ["ratio"])

    # LLDP + fail -> lldp_failures
    if fail:
        return IntentResult("lldp_failures", "high",
                            "lldp + fail words",
                            "lldp", sorted(lldp | fail))

    # LLDP + count/how many -> connection_status
    if count:
        return IntentResult("connection_status", "medium",
                            "lldp + count words",
                            "lldp", sorted(lldp | count))

    # Any remaining LLDP mention -> connection_status
    return IntentResult("connection_status", "low",
                        "bare lldp mention",
                        "lldp", sorted(lldp))


def route_role_intent(ctx: QuestionContext) -> Optional[IntentResult]:
    """Role-based queries (FDP, CDU, spine, etc.) with optional side."""
    if ctx.extracted_role:
        # If optic words are also present, defer to optic router
        # (e.g. "optics in the spine section" should be optic_count, not role_lookup)
        if _hits(ctx.token_set, OPTIC_WORDS) or ctx.has_optic_token:
            return None
        signals = [ctx.extracted_role]
        if ctx.extracted_side:
            signals.append(f"{ctx.extracted_side}-side")
        return IntentResult("role_lookup", "high",
                            f"role keyword '{ctx.extracted_role}' detected"
                            + (f" on {ctx.extracted_side}-side" if ctx.extracted_side else ""),
                            "role", signals)

    # Generic role inventory: "list device roles", "what roles exist"
    role_hits = _hits(ctx.token_set, {"role", "roles"})
    list_hits = _hits(ctx.token_set, LIST_WORDS)
    if role_hits and list_hits:
        return IntentResult("role_lookup", "medium",
                            "role + list/show/which words",
                            "role", sorted(role_hits | list_hits))

    return None


def route_location_intent(ctx: QuestionContext) -> Optional[IntentResult]:
    """Rack/cabinet/LOC:CAB:RU queries."""
    loc_hits = _hits(ctx.token_set, LOCATION_WORDS)
    rank_hits = _hits(ctx.token_set, RANKING_WORDS)
    count_hits = _hits(ctx.token_set, COUNT_WORDS)
    conn_hits = _hits(ctx.token_set, CONNECTION_WORDS)
    # Extended lookup words: added overview/detail/info/breakdown so phrasing
    # like "overview of rack X" or "details on dh202:002" reaches the router.
    lookup_hits = ctx.token_set & {
        "on", "in", "at", "devices", "what", "which", "list", "show",
        "overview", "detail", "details", "info", "information", "breakdown",
    }

    # Exact LOC:CAB:RU -> location_lookup
    if ctx.has_loc_token:
        return IntentResult("location_lookup", "high",
                            "exact LOC:CAB:RU token found",
                            "location", ["loc_token"])

    # Model token + data hall: the data hall is a scoping filter on the model query,
    # not the primary intent. Return model_search so build_query_params can populate
    # data_hall_filter. This wins over any location-based routing below.
    if ctx.has_model_token and ctx.extracted_data_hall:
        return IntentResult(
            "model_search", "high",
            f"model '{ctx.extracted_model}' scoped to data hall '{ctx.extracted_data_hall}'",
            "device", [ctx.extracted_model, ctx.extracted_data_hall],
        )

    # Rack/cabinet + ranking words -> rack_summary
    if loc_hits and rank_hits:
        return IntentResult("rack_summary", "high",
                            "rack/cabinet + ranking words",
                            "location", sorted(loc_hits | rank_hits))

    # "how many racks" / "count of racks" -> rack_summary
    if loc_hits & {"rack", "racks"} and count_hits:
        return IntentResult("rack_summary", "high",
                            "rack + count words",
                            "location", sorted(loc_hits | count_hits))

    # Connections per rack/cabinet -> rack_summary
    if loc_hits and conn_hits and "per" in ctx.token_set:
        return IntentResult("rack_summary", "high",
                            "connections per rack/cabinet",
                            "location", sorted(loc_hits | conn_hits | {"per"}))

    # "unique locations" / "how many locations" -> rack_summary
    if ("unique" in ctx.token_set and ctx.token_set & {"locations", "location"}) and count_hits:
        return IntentResult("rack_summary", "medium",
                            "count + unique/location words",
                            "location", sorted(count_hits | {"unique"}))

    # Rack/cabinet + summary/ranking/breakdown/count -> rack_summary
    if loc_hits and (ctx.token_set & {"summary", "ranking", "breakdown", "count"}):
        return IntentResult("rack_summary", "medium",
                            "rack/cabinet + summary words",
                            "location", sorted(loc_hits))

    # When a specific location is extracted and the question uses summary/overview/
    # breakdown language, return the rack-level rollup even without LOCATION_WORDS
    # (e.g. "Summary of dh202:002" has no "rack" keyword but still needs rack_summary).
    # Guard: skip when a section name is also present so route_section_intent wins.
    if not ctx.extracted_section and ctx.extracted_location:
        _summary_words = ctx.token_set & {"summary", "overview", "breakdown"}
        if _summary_words:
            return IntentResult("rack_summary", "high",
                                "extracted location + summary/overview words",
                                "location", sorted(_summary_words))

        # Unambiguous data-hall + location pair: both fields explicitly present.
        # Route based on the intent signal carried by the remaining words.
        if ctx.extracted_data_hall:
            # Bare rack number (digits only, no colon) → prefer rack-level rollup
            if re.fullmatch(r"\d{1,4}", ctx.extracted_location):
                return IntentResult("rack_summary", "high",
                                    "data hall + bare rack number → rack rollup",
                                    "location", [ctx.extracted_data_hall, ctx.extracted_location])
            # Detail/lookup words present → specific connection lookup
            _detail_words = ctx.token_set & {
                "detail", "details", "info", "information", "about", "tell", "describe",
            }
            if _detail_words or lookup_hits:
                return IntentResult("location_lookup", "high",
                                    "data hall + location + detail/lookup words",
                                    "location", [ctx.extracted_data_hall, ctx.extracted_location])
            # No qualifying words — unambiguous rack reference, default to lookup
            return IntentResult("location_lookup", "high",
                                "data hall + location (unambiguous reference)",
                                "location", [ctx.extracted_data_hall, ctx.extracted_location])

    # "what racks are [in/at] [this location/here]?" with no specific location
    # extracted → user wants a list of racks at the site, not devices inside one rack.
    # rack_summary with empty location_filter returns all racks for the site.
    if loc_hits & {"rack", "racks"} and lookup_hits and not ctx.extracted_location:
        return IntentResult("rack_summary", "medium",
                            "rack list question with no specific location → site-wide rack summary",
                            "location", sorted(loc_hits & {"rack", "racks"}))

    # Rack/cabinet + list/show/what/on/in/overview/detail -> location_lookup
    if loc_hits and lookup_hits:
        return IntentResult("location_lookup", "medium",
                            "rack/cabinet + lookup words",
                            "location", sorted(loc_hits))

    # Explicit data-hall shorthand like "what devices are in dh202?"
    if re.fullmatch(r"dh\d+", ctx.extracted_location, re.I) and lookup_hits:
        return IntentResult("location_lookup", "medium",
                            "explicit data hall token + lookup words",
                            "location", [ctx.extracted_location])

    # "stored/installed/located at" -> location_lookup
    if ctx.extracted_section:
        return None

    if re.search(r"\b(?:stored|installed|located|sitting)\s+(?:on|in|at)\b", ctx.normalized):
        return IntentResult("location_lookup", "medium",
                            "stored/installed/located at phrase",
                            "location", ["positional_verb"])

    return None


def route_cable_type_intent(ctx: QuestionContext) -> Optional[IntentResult]:
    """Cable media type queries (CAT6a, MPO, SMF/MMF, fiber, copper).

    Runs before route_optic_intent so physical-cable questions don't get
    absorbed into optic_count.
    """
    if re.search(r"\bcable\s*types?\b", ctx.normalized):
        return IntentResult("cable_type_summary", "high",
                            "cable type keyword",
                            "cable_type", ["cable_type"])
    if re.search(r"\bhow\s+many\s+(?:cat6|mpo|fiber|copper|smf|mmf)\b", ctx.normalized):
        return IntentResult("cable_type_summary", "high",
                            "how many + cable media keyword",
                            "cable_type", ["cable_count"])
    if ctx.extracted_cable_type and (
        _hits(ctx.token_set, COUNT_WORDS)
        or ctx.token_set & {"summary", "breakdown", "inventory", "list"}
    ):
        return IntentResult("cable_type_summary", "high",
                            f"cable type token '{ctx.extracted_cable_type}' + count/summary words",
                            "cable_type", [ctx.extracted_cable_type])
    if re.search(r"\b(?:fiber|copper)\s*cables?\b", ctx.normalized):
        return IntentResult("cable_type_summary", "medium",
                            "fiber/copper cables phrase",
                            "cable_type", ["fiber_copper"])
    return None


def route_optic_intent(ctx: QuestionContext) -> Optional[IntentResult]:
    """Optic/transceiver queries."""
    optic_hits = _hits(ctx.token_set, OPTIC_WORDS)
    count_hits = _hits(ctx.token_set, COUNT_WORDS)
    cable_hits = _hits(ctx.token_set, CABLE_WORDS)

    # Optic token extracted (QSFP28 etc) + count/summary -> optic_count
    if ctx.has_optic_token and count_hits:
        return IntentResult("optic_count", "high",
                            "optic token + count words",
                            "optic", [ctx.extracted_optic] + sorted(count_hits))

    # Optic words + count/summary words
    if optic_hits and count_hits:
        return IntentResult("optic_count", "high",
                            "optic words + count words",
                            "optic", sorted(optic_hits | count_hits))

    # "optic inventory/breakdown/summary"
    if re.search(r"\boptic\s*(?:inventory|breakdown|summary)\b", ctx.normalized):
        return IntentResult("optic_count", "high",
                            "optic inventory/breakdown/summary phrase",
                            "optic", ["optic_summary"])

    # Cable type queries (lc-to-lc, smf, mmf, single-mode, multi-mode, fiber type)
    if re.search(r"\bcable\s*types?\b", ctx.normalized):
        return IntentResult("optic_count", "medium",
                            "cable type query",
                            "optic", ["cable_type"])
    if re.search(r"\bfiber\s*type\b", ctx.normalized):
        return IntentResult("optic_count", "medium",
                            "fiber type query",
                            "optic", ["fiber_type"])
    if re.search(r"\blc[\s-]*to[\s-]*lc\b", ctx.normalized):
        return IntentResult("optic_count", "medium",
                            "LC-to-LC connector query",
                            "optic", ["lc_connector"])
    if re.search(r"\b(?:smf|mmf|single[\s-]mode|multi[\s-]mode)\b", ctx.normalized):
        return IntentResult("optic_count", "medium",
                            "fiber mode query (SMF/MMF)",
                            "optic", ["fiber_mode"])

    # Optic mismatch queries
    if optic_hits and _hits(ctx.token_set, MISMATCH_WORDS):
        return IntentResult("optic_count", "medium",
                            "optic + mismatch words",
                            "optic", sorted(optic_hits))

    # Optic + empty/missing/populated
    if optic_hits and (ctx.token_set & {"empty", "blank", "missing", "populated", "both"}):
        return IntentResult("optic_count", "medium",
                            "optic + empty/missing/populated",
                            "optic", sorted(optic_hits))

    # "which sections have optics"
    if optic_hits and _hits(ctx.token_set, SECTION_WORDS):
        return IntentResult("optic_count", "medium",
                            "optic + section words",
                            "optic", sorted(optic_hits))

    # Standalone optic token without count words -> still optic_count
    if ctx.has_optic_token:
        return IntentResult("optic_count", "low",
                            "optic token present without explicit count words",
                            "optic", [ctx.extracted_optic])

    return None


def route_status_intent(ctx: QuestionContext) -> Optional[IntentResult]:
    """Connection status and cable status queries.

    Runs AFTER lldp and optic routers so those specific domains are already handled.
    Defers to trend router when trend words are present.
    """
    # Defer to trend router if trend/progression signals present
    trend_hits = _hits(ctx.token_set, TREND_WORDS)
    if trend_hits & {"trend", "trending", "trends", "progression", "progress",
                     "progressed", "trajectory", "evolution", "evolve",
                     "improving", "worsening", "timeline", "historical"}:
        return None

    cable_hits = _hits(ctx.token_set, CABLE_WORDS)
    status_hits = _hits(ctx.token_set, STATUS_WORDS)
    conn_hits = _hits(ctx.token_set, CONNECTION_WORDS)
    count_hits = _hits(ctx.token_set, COUNT_WORDS)
    completion_hits = _hits(ctx.token_set, COMPLETION_WORDS)
    section_hits = _hits(ctx.token_set, SECTION_WORDS)
    verification_hits = set(ctx.token_set & {"passed", "verified", "unverified"})
    if re.search(r"\b(?:lldp|human)[\s-]*verified\b", ctx.normalized):
        verification_hits.add("verified")
    model_inventory_phrase = bool(
        re.search(r"\bdevice\s+models?\b", ctx.normalized)
        or re.search(r"\bmodel\s+inventory\b", ctx.normalized)
    )

    # Let section/model/site-total queries fall through to their specialized routers.
    if ctx.extracted_section or ctx.has_model_token or model_inventory_phrase:
        return None
    if re.search(r"\b(?:how\s+many)\b.{0,50}\bconnections?\b.{0,50}\b(?:total|listed|cutsheet|defined)\b",
                 ctx.normalized):
        return None
    if re.search(r"\btotal\b.{0,30}\bconnections?\b.{0,50}\b(?:cutsheet|listed|defined)\b",
                 ctx.normalized):
        return None
    if section_hits and completion_hits:
        return None

    # Verification terms should route to connection_status even if the user says
    # "cables", because these statuses reflect verification state, not cable-run state.
    if count_hits and verification_hits:
        return IntentResult("connection_status", "high",
                            "count + verification keywords",
                            "status", sorted(count_hits | verification_hits))
    if verification_hits and (cable_hits or conn_hits or status_hits):
        return IntentResult("connection_status", "high",
                            "verification keywords present",
                            "status", sorted(verification_hits | cable_hits | conn_hits | status_hits))

    # Cable + status/progress/completion -> cable_status
    # BUT if section words are also present with completion words, defer to section router
    section_present = bool(section_hits)
    if cable_hits and (status_hits | (ctx.token_set & {"progress", "completion", "not", "run", "terminated"})):
        if section_present and completion_hits:
            pass  # defer to route_section_intent
        else:
            return IntentResult("cable_status", "high",
                                "cable + status/progress words",
                                "status", sorted(cable_hits | status_hits))

    # "what cables" / cable + completion
    if re.search(r"\bwhat\s+cables?\b", ctx.normalized):
        return IntentResult("cable_status", "medium",
                            "'what cables' phrase",
                            "status", ["what_cables"])
    if cable_hits and (ctx.token_set & {"completion"}):
        return IntentResult("cable_status", "medium",
                            "cable + completion",
                            "status", sorted(cable_hits | {"completion"}))

    # "completion rate" / "complete rate"
    if re.search(r"\b(?:completion|complete)\s+rate\b", ctx.normalized):
        return IntentResult("cable_status", "medium",
                            "completion rate phrase",
                            "status", ["completion_rate"])

    # "how many complete/not run/terminated/pending"
    if count_hits and (ctx.token_set & {"complete", "not", "run", "terminated", "pending"}):
        return IntentResult("cable_status", "medium",
                            "count + cable status keywords",
                            "status", sorted(count_hits))

    # Connection/link + status/state/health -> connection_status
    if conn_hits and status_hits:
        return IntentResult("connection_status", "high",
                            "connection/link + status words",
                            "status", sorted(conn_hits | status_hits))

    # "how many passed/verified"
    if count_hits and (ctx.token_set & {"passed", "verified"}):
        return IntentResult("connection_status", "medium",
                            "count + passed/verified",
                            "status", sorted(count_hits))

    # "unverified" / "remaining gap"
    if ctx.token_set & {"unverified"}:
        return IntentResult("connection_status", "medium",
                            "unverified keyword",
                            "status", ["unverified"])
    if "remaining" in ctx.token_set and "gap" in ctx.token_set:
        return IntentResult("connection_status", "medium",
                            "remaining gap phrase",
                            "status", ["remaining_gap"])

    # "overall status" / "overall health"
    if "overall" in ctx.token_set and status_hits:
        return IntentResult("connection_status", "medium",
                            "overall + status words",
                            "status", sorted(status_hits | {"overall"}))

    # "how many connections" without other domain context
    if count_hits and conn_hits:
        return IntentResult("connection_status", "medium",
                            "count + connection words",
                            "status", sorted(count_hits | conn_hits))

    # "how many cables" without status words -> cable_status
    if count_hits and cable_hits:
        return IntentResult("cable_status", "medium",
                            "count + cable words",
                            "status", sorted(count_hits | cable_hits))

    return None


def route_section_intent(ctx: QuestionContext) -> Optional[IntentResult]:
    """Section summary and section completion queries."""
    section_hits = _hits(ctx.token_set, SECTION_WORDS)
    count_hits = _hits(ctx.token_set, COUNT_WORDS)
    completion_hits = _hits(ctx.token_set, COMPLETION_WORDS)
    fail_hits = _hits(ctx.token_set, FAIL_WORDS)

    # Defer to trend router if trend signals present
    trend_hits = _hits(ctx.token_set, TREND_WORDS)
    if trend_hits & {"trend", "trending", "trends", "progression", "progress",
                     "progressed", "trajectory", "evolution", "timeline", "historical"}:
        return None

    # "sections where" + completion/fail words -> section_completion
    if re.search(r"\bsections?\s+where\b", ctx.normalized) and (completion_hits or fail_hits):
        return IntentResult("section_completion", "high",
                            "'sections where' + completion/fail words",
                            "section", ["section_where"] + sorted(completion_hits | fail_hits))

    # "sections where" without completion context -> section_summary
    if re.search(r"\bsections?\s+where\b", ctx.normalized):
        return IntentResult("section_summary", "high",
                            "'sections where' phrase",
                            "section", ["section_where"])

    # Section + completion words -> section_completion
    if section_hits and completion_hits:
        return IntentResult("section_completion", "high",
                            "section + completion words",
                            "section", sorted(section_hits | completion_hits))

    # Tier-to-tier pattern
    if re.search(r"\btier[\s-]*\d\s+to\s+tier[\s-]*\d\b", ctx.normalized):
        return IntentResult("section_summary", "high",
                            "tier-to-tier pattern",
                            "section", ["tier_range"])

    # Tiers represented/present/defined
    if re.search(r"\btiers?\b.{0,30}\b(?:represented|present|defined)\b", ctx.normalized):
        return IntentResult("section_summary", "high",
                            "tiers represented/present/defined",
                            "section", ["tier_presence"])

    # Named section + fail/completion words -> section_completion
    if ctx.extracted_section and (fail_hits or completion_hits):
        return IntentResult("section_completion", "high",
                            f"named section '{ctx.extracted_section}' + fail/completion words",
                            "section", [ctx.extracted_section] + sorted(fail_hits | completion_hits))

    # Named section keywords (BACKBONE, OOB-FW, etc.) + connection/count context
    if ctx.extracted_section:
        conn_or_count = _hits(ctx.token_set, CONNECTION_WORDS | COUNT_WORDS)
        if conn_or_count or section_hits:
            return IntentResult("section_summary", "high",
                                f"named section '{ctx.extracted_section}' detected",
                                "section", [ctx.extracted_section])

    # GG-prefix or compound section name
    if re.search(r"\bGG\d+-?[A-Z]\b", ctx.raw, re.I):
        return IntentResult("section_summary", "high",
                            "GG-prefix section name",
                            "section", ["gg_section"])
    if re.search(r"\b(?:NET-AGG|COMP-AGG|NET-DIST|COMP-DIST|UFM-PATH)\b", ctx.raw, re.I):
        return IntentResult("section_summary", "high",
                            "compound section name",
                            "section", ["compound_section"])

    # "management plane" / "locode"
    if "management" in ctx.token_set and "plane" in ctx.token_set:
        return IntentResult("section_summary", "medium",
                            "management plane phrase",
                            "section", ["management_plane"])
    if "locode" in ctx.token_set:
        return IntentResult("section_summary", "medium",
                            "locode keyword",
                            "section", ["locode"])

    # Section + list words (what sections exist, list sections, which sections)
    list_hits = _hits(ctx.token_set, LIST_WORDS)
    if section_hits and list_hits:
        return IntentResult("section_summary", "medium",
                            "section + list/show/what words",
                            "section", sorted(section_hits | list_hits))

    # Section + count / "how many sections"
    if section_hits and count_hits:
        return IntentResult("section_summary", "medium",
                            "section + count words",
                            "section", sorted(section_hits | count_hits))

    # "section summary/overview/breakdown/list"
    if section_hits and (ctx.token_set & {"summary", "overview", "breakdown", "list"}):
        return IntentResult("section_summary", "medium",
                            "section + summary words",
                            "section", sorted(section_hits))

    # "topology section"
    if "topology" in ctx.token_set and "section" in ctx.token_set:
        return IntentResult("section_summary", "medium",
                            "topology section phrase",
                            "section", ["topology", "section"])

    # Connections/cables + "in section" or "section X"
    conn_or_cable = _hits(ctx.token_set, CONNECTION_WORDS | CABLE_WORDS)
    if conn_or_cable and section_hits:
        return IntentResult("section_summary", "medium",
                            "connections/cables + section words",
                            "section", sorted(conn_or_cable | section_hits))

    # "in ... section" pattern (loose)
    if "in" in ctx.token_set and section_hits:
        return IntentResult("section_summary", "low",
                            "'in section' phrase",
                            "section", sorted(section_hits))

    # "sections combined/together/total"
    if section_hits and (ctx.token_set & {"combined", "together", "total"}):
        return IntentResult("section_summary", "medium",
                            "sections combined/together/total",
                            "section", sorted(section_hits))

    return None


def route_data_hall_intent(ctx: QuestionContext) -> Optional[IntentResult]:
    """Data hall summary queries.

    Placed AFTER location and section so rack-level and section-level
    queries win first. Only triggers on explicit "data hall" mentions.
    """
    if re.search(r"\bdata\s*halls?\b", ctx.normalized):
        # But "how many racks ... data hall" already went to rack_summary
        # in route_location_intent, so this only fires for data-hall-centric questions
        return IntentResult("data_hall_summary", "high",
                            "data hall keyword",
                            "data_hall", ["data_hall"])
    if re.search(r"\bhall\s*(?:summary|overview|breakdown)\b", ctx.normalized):
        return IntentResult("data_hall_summary", "medium",
                            "hall summary/overview phrase",
                            "data_hall", ["hall_summary"])
    return None


def route_device_intent(ctx: QuestionContext) -> Optional[IntentResult]:
    """Device-level queries: detail, connections, model search, device list.

    Runs late so role, optic, section, and status routers get priority.
    """
    device_hits = _hits(ctx.token_set, DEVICE_WORDS)
    count_hits = _hits(ctx.token_set, COUNT_WORDS)
    list_hits = _hits(ctx.token_set, LIST_WORDS)
    detail_hits = _hits(ctx.token_set, DETAIL_WORDS)
    conn_hits = _hits(ctx.token_set, CONNECTION_WORDS)
    side = ctx.extracted_side

    # Model-family questions can still mention "connections" ("how many connections
    # do these devices have?"). Keep those on model_search when the phrasing is
    # clearly plural/inventory-oriented rather than a single-device lookup.
    if ctx.has_model_token and conn_hits and count_hits and device_hits:
        return IntentResult("model_search", "high",
                            f"model token '{ctx.extracted_model}' + device/count/connection words",
                            "device", [ctx.extracted_model] + sorted(device_hits | count_hits | conn_hits))

    # Device-specific: "connections for/on/of <device>"
    if ctx.has_device_token and conn_hits:
        return IntentResult("device_connections", "high",
                            f"device token + connection words",
                            "device", [ctx.extracted_device or "?"] + sorted(conn_hits))

    # Device-specific: "detail/info/about <device>"
    if ctx.has_device_token and detail_hits:
        return IntentResult("device_detail", "high",
                            "device token + detail words",
                            "device", [ctx.extracted_device or "?"] + sorted(detail_hits))

    # Model search: model token + count/list words
    if ctx.has_model_token and (count_hits or list_hits):
        return IntentResult("model_search", "high",
                            f"model token '{ctx.extracted_model}' + count/list words",
                            "device", [ctx.extracted_model] + sorted(count_hits | list_hits))

    if ctx.has_model_token and "unique" in ctx.token_set:
        return IntentResult("model_search", "high",
                            f"model token '{ctx.extracted_model}' + unique",
                            "device", [ctx.extracted_model, "unique"])

    # "which/what device models" / model inventory
    if re.search(r"\b(?:which|what)\s+device\s+models?\b", ctx.normalized):
        return IntentResult("model_search", "high",
                            "'which/what device models' phrase",
                            "device", ["device_models"])
    if re.search(r"\bdevice\s+models?\b.{0,60}\b(?:inventory|sorted|most\s+connections|breakdown|inconsistent|complete)\b",
                 ctx.normalized):
        return IntentResult("model_search", "high",
                            "device models + inventory/sorted phrase",
                            "device", ["model_inventory"])

    # Inconsistent casing/naming or z-model mentions
    if re.search(r"\b(?:inconsistent\s+(?:casing|naming)|z-?model)\b", ctx.normalized):
        return IntentResult("model_search", "medium",
                            "inconsistent casing/naming or z-model",
                            "device", ["z_model"])

    # Model token without count words -> still model_search
    if ctx.has_model_token:
        return IntentResult("model_search", "medium",
                            f"model token '{ctx.extracted_model}' present",
                            "device", [ctx.extracted_model])

    # Side-specific device list
    if side == "Z" and device_hits:
        return IntentResult("z_device_list", "high",
                            "Z-side + device words",
                            "device", ["z_side"] + sorted(device_hits))
    if side == "Z" and "unique" in ctx.token_set:
        return IntentResult("z_device_list", "medium",
                            "unique + Z-side",
                            "device", ["z_side", "unique"])
    if side == "A" and device_hits:
        return IntentResult("a_device_list", "high",
                            "A-side + device words",
                            "device", ["a_side"] + sorted(device_hits))
    if side == "A" and "unique" in ctx.token_set:
        return IntentResult("a_device_list", "medium",
                            "unique + A-side",
                            "device", ["a_side", "unique"])

    # Generic device list
    if device_hits and (list_hits | count_hits):
        return IntentResult("device_list", "medium",
                            "device words + list/count words",
                            "device", sorted(device_hits | list_hits | count_hits))
    if "unique" in ctx.token_set and (device_hits or ctx.token_set & {"devices"}):
        return IntentResult("device_list", "medium",
                            "unique + device words",
                            "device", ["unique"] + sorted(device_hits))

    return None


def route_site_intent(ctx: QuestionContext) -> Optional[IntentResult]:
    """Site overview queries."""
    site_hits = _hits(ctx.token_set, SITE_WORDS)

    if "site" in ctx.token_set and (ctx.token_set & {"overview", "summary", "stats", "status"}):
        return IntentResult("site_overview", "high",
                            "site + overview/summary words",
                            "site", sorted(site_hits))
    if (ctx.token_set & {"overview", "summary"}) and "site" in ctx.token_set:
        return IntentResult("site_overview", "medium",
                            "overview/summary + site",
                            "site", sorted(site_hits))
    if re.search(r"\b(?:what|tell)\b.*\b(?:about|me)\b.*\bsite\b", ctx.normalized):
        return IntentResult("site_overview", "medium",
                            "'tell me about the site' phrase",
                            "site", ["tell_about_site"])
    # Total connection count for the whole cutsheet
    if re.search(r"\b(?:how\s+many)\b.{0,50}\bconnections?\b.{0,50}\b(?:total|listed|cutsheet|defined)\b",
                 ctx.normalized):
        return IntentResult("site_overview", "medium",
                            "total connections in cutsheet",
                            "site", ["total_connections"])
    if re.search(r"\btotal\b.{0,30}\bconnections?\b.{0,50}\b(?:cutsheet|listed|defined)\b",
                 ctx.normalized):
        return IntentResult("site_overview", "medium",
                            "total connections in cutsheet (alt)",
                            "site", ["total_connections"])
    return None


def route_ip_intent(ctx: QuestionContext) -> Optional[IntentResult]:
    """IP address / VRF lookup queries."""
    ip_hits = _hits(ctx.token_set, IP_WORDS)
    if ip_hits:
        return IntentResult("ip_lookup", "high",
                            "IP/address/VRF keyword",
                            "ip", sorted(ip_hits))
    return None


def route_node_compute_intent(ctx: QuestionContext) -> Optional[IntentResult]:
    """Node/compute/GPU/server inventory queries."""
    compute_hits = ctx.token_set & {"node", "compute", "gpu", "server"}
    count_or_list = _hits(ctx.token_set, COUNT_WORDS | LIST_WORDS)
    if compute_hits and count_or_list:
        return IntentResult("node_compute", "high",
                            "compute words + count/list words",
                            "compute", sorted(compute_hits | count_or_list))
    return None


def route_diff_intent(ctx: QuestionContext) -> Optional[IntentResult]:
    """Upload diff and upload list queries."""
    diff_hits = _hits(ctx.token_set, DIFF_WORDS)
    upload_hits = _hits(ctx.token_set, UPLOAD_WORDS)

    # "list uploads" / "show uploads" / "upload history"
    # Use phrase match for "all uploads" to avoid false positives from
    # "across all uploads" which is a trend/diff scoping phrase, not a list request.
    if re.search(r"\b(?:list|show|previous|recent)\s+(?:all\s+)?uploads?\b", ctx.normalized):
        return IntentResult("upload_list", "high",
                            "upload + list/show words",
                            "diff", sorted(upload_hits))
    if re.search(r"\ball\s+uploads?\b", ctx.normalized) and not (
            ctx.token_set & {"across", "over", "from", "between"}):
        return IntentResult("upload_list", "high",
                            "'all uploads' without scoping preposition",
                            "diff", sorted(upload_hits))
    if "upload" in ctx.token_set and "history" in ctx.token_set:
        return IntentResult("upload_list", "high",
                            "upload history phrase",
                            "diff", ["upload", "history"])

    # Require a strong diff signal (compare/diff/delta/versus) to avoid false
    # positives from generic words like "missing", "new", "added" combined
    # with common words like "last", "latest", "recent" from UPLOAD_WORDS.
    strong_diff = _hits(ctx.token_set, _STRONG_DIFF_SIGNALS)
    if strong_diff and upload_hits:
        return IntentResult("upload_diff", "high",
                            "strong diff signal + upload/version words",
                            "diff", sorted(strong_diff | upload_hits))

    # "what changed" + between/since/from (explicit temporal scoping)
    if "changed" in ctx.token_set and (ctx.token_set & {"between", "since", "from"}):
        return IntentResult("upload_diff", "high",
                            "'what changed' + between/since/from",
                            "diff", ["changed"] + sorted(ctx.token_set & {"between", "since", "from"}))

    # Weak diff words + explicit upload reference + temporal scoping
    if diff_hits and upload_hits and (ctx.token_set & {"between", "since", "from", "two"}):
        return IntentResult("upload_diff", "medium",
                            "diff words + upload words + temporal scoping",
                            "diff", sorted(diff_hits | upload_hits))

    return None


def route_cross_site_intent(ctx: QuestionContext) -> Optional[IntentResult]:
    """Cross-site comparison queries (models, optics, status across sites)."""
    cross_hits = _hits(ctx.token_set, CROSS_SITE_WORDS)
    # "by site" / "per site" / "each site" also signals cross-site intent
    by_site = bool(re.search(r"\b(?:by|per|each)\s+site\b", ctx.normalized))
    if not cross_hits and not by_site:
        return None

    explicit_site_phrase = bool(
        re.search(r"\b(?:across|between|compare|comparison)\s+(?:all\s+)?sites?\b", ctx.normalized)
        or re.search(r"\bcross[\s-]?site\b", ctx.normalized)
    )
    site_code_mentions = ext.extract_site_codes(ctx.raw)

    # Need a site-level signal (not just "across sections" / "both sides" / "entire cutsheet")
    has_site_signal = (
        bool(ctx.token_set & {"site", "sites"})
        or explicit_site_phrase
        or len(site_code_mentions) >= 2
        or by_site
    )
    # "across sections" or "across uploads" without site words -> not cross-site
    if ("across" in cross_hits
            and ctx.token_set & {"section", "sections", "upload", "uploads", "cutsheet", "side", "sides"}
            and not ctx.token_set & {"site", "sites"}):
        return None

    if not has_site_signal:
        return None

    optic_hits = _hits(ctx.token_set, OPTIC_WORDS)
    status_hits = _hits(ctx.token_set, STATUS_WORDS)
    model_hits = ctx.token_set & {"model", "models", "hardware"}
    device_hits = _hits(ctx.token_set, DEVICE_WORDS)

    signals = sorted(cross_hits)

    if optic_hits or ctx.has_optic_token:
        return IntentResult("cross_site_optics", "high",
                            "cross-site + optic words",
                            "cross_site", signals)
    completion_hits = _hits(ctx.token_set, COMPLETION_WORDS)
    if status_hits or completion_hits:
        return IntentResult("cross_site_status", "high",
                            "cross-site + status/completion words",
                            "cross_site", signals)
    if model_hits or device_hits or ctx.has_model_token:
        return IntentResult("cross_site_models", "high",
                            "cross-site + model/device words",
                            "cross_site", signals)

    # Default to models for ambiguous cross-site queries
    return IntentResult("cross_site_models", "medium",
                        "cross-site signal without specific domain",
                        "cross_site", signals)


def route_trend_intent(ctx: QuestionContext) -> Optional[IntentResult]:
    """Trend and progression queries across uploads over time.

    Runs AFTER status and section routers so 'cable status' still routes
    to cable_status, but 'cable status trend' goes to trend_status.
    """
    trend_hits = _hits(ctx.token_set, TREND_WORDS)
    if not trend_hits:
        return None

    # Need a time-scoping signal alongside trend words
    time_ctx = ctx.token_set & {"time", "over", "across", "timeline", "history", "evolution"}
    if not time_ctx and not (trend_hits & {"progression", "progress", "progressed",
                                            "trend", "trending", "trends", "trajectory",
                                            "improving", "worsening"}):
        return None

    section_hits = _hits(ctx.token_set, SECTION_WORDS)
    status_hits = _hits(ctx.token_set, STATUS_WORDS)
    cable_hits = _hits(ctx.token_set, CABLE_WORDS)
    completion_hits = _hits(ctx.token_set, COMPLETION_WORDS)
    lldp_hits = _hits(ctx.token_set, LLDP_WORDS)
    fail_hits = _hits(ctx.token_set, FAIL_WORDS)

    # Also detect section from compound tokens like "section-level"
    has_section_signal = bool(section_hits) or bool(
        re.search(r"\bsection", ctx.normalized)
    )

    # Section + trend -> trend_section
    if has_section_signal:
        return IntentResult("trend_section", "high",
                            f"section + trend/progression words",
                            "trend", sorted(section_hits | trend_hits))

    # Status/completion/lldp/cable + trend -> trend_status
    if status_hits | cable_hits | completion_hits | lldp_hits | fail_hits:
        return IntentResult("trend_status", "high",
                            "status/completion/lldp + trend words",
                            "trend", sorted(trend_hits | status_hits | cable_hits))

    # Bare trend phrase -> trend_status
    return IntentResult("trend_status", "medium",
                        "trend words without specific domain context",
                        "trend", sorted(trend_hits))


# ---------------------------------------------------------------------------
# Router chain (priority order)
# ---------------------------------------------------------------------------

_ROUTER_CHAIN: List[Callable[[QuestionContext], Optional[IntentResult]]] = [
    route_diff_intent,           # upload diff/list (highest priority)
    route_cross_site_intent,     # cross-site comparisons (before single-site routers)
    route_burndown_intent,
    route_lldp_intent,
    route_role_intent,
    route_location_intent,
    route_cable_type_intent,   # before optic — cable media is more specific than optic type
    route_optic_intent,
    route_status_intent,
    route_section_intent,
    route_trend_intent,          # after status/section so bare "cable status" wins, but "trend" overrides
    route_data_hall_intent,
    route_site_intent,
    route_node_compute_intent,
    route_device_intent,
    route_ip_intent,
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_question(question: str) -> str:
    """Classify a question into one of the known types.

    Drop-in replacement for the old regex-list classify_question().
    Returns the question_type string.
    """
    result = classify_question_full(question)
    return result.question_type


def classify_question_full(question: str) -> IntentResult:
    """Classify with full debug metadata.

    Returns IntentResult with question_type, confidence, reason, matched_domain,
    and matched_signals.
    """
    ctx = build_context(question)
    for router in _ROUTER_CHAIN:
        result = router(ctx)
        if result is not None:
            return result

    return IntentResult("general", "low", "no domain matched", "general", [])


def classify_with_context(question: str) -> tuple[IntentResult, QuestionContext]:
    """Classify and return both the result and the pre-built context.

    Used by build_query_params() so extractors don't re-run.
    """
    ctx = build_context(question)
    for router in _ROUTER_CHAIN:
        result = router(ctx)
        if result is not None:
            return result, ctx

    return IntentResult("general", "low", "no domain matched", "general", []), ctx
