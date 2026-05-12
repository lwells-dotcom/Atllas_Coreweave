# Code Review: Atlas Cutsheet Pipeline

## Summary

The codebase is well-structured and clearly written, but it is architecturally bloated in one specific way: **data quality problems originating in the cut sheets are being solved at every layer of the stack** — regex, Python dicts, SQL templates, and schema design all carry compensating logic for inconsistent source data. This is the root cause of the "beyond bloated" feeling.

---

## Red Flags by File

### `cutsheet_preprocessor.py`

**`STATUS_MAP` — 171 entries (lines 51–116)**

This is the most glaring symptom. 171 manually maintained mappings for what is semantically a small set of states (~9 canonical values). Every new site or new data entry style adds entries here. The map will never stop growing.

```python
"lldp:  passed": LLDP_PASSED,   # two spaces — someone typed this once
"lldp: passed": LLDP_PASSED,    # one space variant
"lldp:passed": LLDP_PASSED,     # no space variant
"lldp - passed": LLDP_PASSED,   # dash variant
"LLDP:  Passed": LLDP_PASSED,   # mixed case, two spaces
"LLDP Passed": LLDP_PASSED,     # title case
```

Six separate entries for the same semantic value. An LLM at ingest time would handle all of these in zero lines of code.

**`SITE_SECTION_HEADERS` dict (lines 129–323)**

Nearly 200 lines of hardcoded section header strings per site code. When a new data hall, grid, or rack section is added to a cut sheet, someone has to manually add it here. This is site-specific institutional knowledge baked into source code.

**`'nan'` string handling (line 432)**

```python
raw_status = raw_status.where(raw_status.str.lower() != "nan", "")
```

`'nan'` strings reaching the status column means pandas `NaN` values are making it through the ingestion layer uncleaned. This check exists in at least 3 different places across the codebase (`cutsheet_preprocessor.py`, `cutsheet_profiles.py`, `atlas_data_loader.py`). The fix belongs at the point of read, not scattered through normalization functions.

---

### `cutsheet_profiles.py`

**`MODEL_ALIASES` — 40+ entries (lines 120–162)**

Same pattern as `STATUS_MAP`. Every manufacturer naming variant for every model is manually enumerated. Adding a new switch model means updating this dict.

```python
"sn5610": "SN5610",
"mellanox-sn5610": "SN5610",
"nvidia-sn5610": "SN5610",
```

Three entries per model minimum. This will grow with every new device type onboarded.

**Per-site column mapping profiles (lines 196–373)**

`PROFILE_STANDARD_V1`, `PROFILE_STANDARD_V2`, `PROFILE_ALTERNATE` each define their own column name → canonical name mappings. Every time a new site uses slightly different column headers, a new profile or new entries get added. The `apply_profile()` function then has to reconcile conflicts between columns that map to the same canonical target (CP1 logic, lines 461–491).

This conflict resolution code — comparing row-by-row values when two source columns both map to the same canonical column — is itself a sign that the schema isn't stable enough to be the source of truth.

**`normalize_status` and `normalize_model` duplicate `nan` handling (lines 499–515, 538–570)**

The same `math.isnan()` + `'nan'` string checks appear here again. This is defensive coding for a problem that should be fixed upstream.

---

### `sql_templates.py`

**27 SQL templates (961 lines)**

The template count is not inherently bad — parameterized queries are the right pattern. The problem is *what the SQL is doing*: deduplication, normalization, and data quality compensation that ideally happens at ingest.

**`COALESCE(NULLIF(..., ''), NULLIF(..., ''))` patterns**

Appearing in multiple templates. This exists to handle the case where A-optic or Z-optic might be empty on one side of a mixed cable. If data were clean at ingest, this complexity disappears.

**`WHERE col IS NOT NULL AND col != '' AND col != 'nan'`**

The `!= 'nan'` clause in SQL means the database itself contains the string `'nan'`. Postgres should never see this — it's a pandas artifact that means the ingestion pipeline isn't stripping it before INSERT.

**`ILIKE` with escape handling**

The `_escape_ilike()` function in `atlas_query_router.py` exists because user queries go into `ILIKE` patterns. This is a code smell: it means the query layer is doing user-input sanitization that could be avoided with a different search approach (e.g., full-text search or embeddings).

---

### `atlas_data_loader.py`

**`_SECTION_HEADER_PATTERNS` — 6 compiled regex patterns**

These patterns detect topology section headers at load time. But `SITE_SECTION_HEADERS` in `cutsheet_preprocessor.py` already handles this with explicit strings per site. The two approaches are doing the same job with different mechanisms, creating two places to update when a new section format appears.

**Cardinality-based cable type heuristic**

There is a heuristic that detects whether a `CABLE ID` column contains unique cable identifiers or repeated cable type strings, and reclassifies the column based on cardinality. This is compensating for a schema inconsistency in the source cut sheets — the same column is used for two different things depending on who created the sheet.

**File hash-based duplicate detection**

Good pattern. No issues here.

---

## Root Cause Diagnosis

Every layer of the stack is absorbing variation that originates in the cut sheets:

| Layer | How it compensates |
|---|---|
| `cutsheet_preprocessor.py` | 171-entry STATUS_MAP, 200-line SITE_SECTION_HEADERS |
| `cutsheet_profiles.py` | 40+ MODEL_ALIASES, multiple profile versions, conflict resolution logic |
| `atlas_data_loader.py` | Section header regex, cable type heuristic, nan filtering |
| `sql_templates.py` | COALESCE/NULLIF chains, `!= 'nan'` WHERE clauses, ILIKE escaping |
| `atlas_query_router.py` | Zero-padding logic, multi-tier location pattern building |

When a new cut sheet introduces a new status string variant, the developer has to touch `STATUS_MAP`. If it's a new section header, they touch `SITE_SECTION_HEADERS`. If it's a new model alias, they touch `MODEL_ALIASES`. If the column names differ, they touch a profile. **The same source problem (inconsistent cut sheet data) requires changes in 4+ files.**

---

## What Should Change

### Short term (without architectural changes)

1. **Centralize `nan` string filtering** into a single utility function called once at read time. Remove the repeated `!= 'nan'` checks from every downstream function and SQL template.

2. **Move `SITE_SECTION_HEADERS` to a config file** (YAML or JSON) so operations teams can update it without touching Python source code.

3. **Move `MODEL_ALIASES` and `STATUS_MAP` to config files** for the same reason. These are data, not code.

### Medium term (architectural)

4. **Replace STATUS_MAP with LLM classification at ingest.** A single prompt asking "normalize this status to one of: COMPLETE, NOT_TERMINATED, NOT_RUN, IN_PROGRESS, LLDP_PASSED, LLDP_FAILED, PENDING, ADDITION" would handle every variant without any manual maintenance. Unknown values get flagged for review rather than silently mapping to UNKNOWN.

5. **Replace column profile detection with LLM header mapping at ingest.** Instead of maintaining V1/V2/ALTERNATE profiles, ask an LLM to map the actual column headers to canonical names. One-time cost per new cut sheet format, no code changes required.

6. **Clean data fully at ingest, trust it downstream.** The SQL templates, query router, and context builder should not need to defend against empty strings, `'nan'` literals, or inconsistent formats. If ingest is clean, all of that code disappears.

### Long term

7. **Evaluate vector search for the query layer.** The `atlas_query_router.py` 29-intent classification + ILIKE pattern matching is doing manually what semantic search does automatically. Users asking about locations, models, or devices in natural language would benefit from embedding-based retrieval rather than regex extraction + SQL template selection.

---

## What Is Working Well

- Parameterized SQL templates with `%(name)s` syntax — no SQL injection risk.
- `is_active` soft-delete pattern — clean multi-upload versioning.
- `cutsheet_raw_rows` separation from the hot table — good schema design.
- Materialized views with `view_refresh_log` — correct performance optimization.
- File hash-based duplicate detection — prevents silent re-ingestion.
- ThreadedConnectionPool — appropriate for Flask's threading model.
- The `Canon` class as single source of truth for column names — good pattern, just needs the upstream variation problem solved so it can actually be relied on.

---

*Review generated: 2026-05-12*
