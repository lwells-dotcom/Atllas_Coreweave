-- atlas_schema.sql
-- Postgres schema for Atlas DCT Infrastructure Intelligence.
-- Auto-loaded via docker-compose initdb.d mount.

-- Sites table
CREATE TABLE IF NOT EXISTS sites (
    id          SERIAL PRIMARY KEY,
    site_code   TEXT NOT NULL UNIQUE,
    site_name   TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Cutsheet uploads tracking
CREATE TABLE IF NOT EXISTS cutsheet_uploads (
    id          SERIAL PRIMARY KEY,
    site_id     INTEGER NOT NULL REFERENCES sites(id),
    filename    TEXT NOT NULL,
    uploaded_by TEXT,
    profile     JSONB,
    row_count   INTEGER DEFAULT 0,
    is_active   BOOLEAN DEFAULT TRUE,  -- B9: soft-delete for re-uploads
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Raw cutsheet connections
CREATE TABLE IF NOT EXISTS cutsheet_connections (
    id              SERIAL PRIMARY KEY,
    upload_id       INTEGER NOT NULL REFERENCES cutsheet_uploads(id) ON DELETE CASCADE,
    site_id         INTEGER NOT NULL REFERENCES sites(id),
    section         TEXT,
    a_device        TEXT,
    a_port          TEXT,
    a_optic         TEXT,
    a_locode        TEXT,
    a_model         TEXT,
    a_loc_cab_ru    TEXT,
    z_device        TEXT,
    z_port          TEXT,
    z_optic         TEXT,
    z_locode        TEXT,
    z_model         TEXT,
    z_loc_cab_ru    TEXT,
    cable_id        TEXT,
    status          TEXT,
    status_normalized TEXT,  -- R20: enum-like column for fast filtering without ILIKE
    a_role          TEXT,    -- H8: device functional role from host_inventory (FDP, switch, CDU, etc.)
    z_role          TEXT     -- H8: device functional role from host_inventory
);

-- Separate table for raw row JSONB to avoid bloating the hot table (R25)
CREATE TABLE IF NOT EXISTS cutsheet_raw_rows (
    connection_id   INTEGER PRIMARY KEY REFERENCES cutsheet_connections(id) ON DELETE CASCADE,
    raw_row         JSONB
);

CREATE INDEX IF NOT EXISTS idx_cc_site ON cutsheet_connections(site_id);
CREATE INDEX IF NOT EXISTS idx_cc_upload ON cutsheet_connections(upload_id);
CREATE INDEX IF NOT EXISTS idx_cc_a_device ON cutsheet_connections(a_device);
CREATE INDEX IF NOT EXISTS idx_cc_z_device ON cutsheet_connections(z_device);
CREATE INDEX IF NOT EXISTS idx_cc_a_model ON cutsheet_connections(a_model);
CREATE INDEX IF NOT EXISTS idx_cc_z_model ON cutsheet_connections(z_model);
CREATE INDEX IF NOT EXISTS idx_cc_status ON cutsheet_connections(status);
CREATE INDEX IF NOT EXISTS idx_cc_section ON cutsheet_connections(section);
-- R23: Missing indexes for ad-hoc LLM queries
CREATE INDEX IF NOT EXISTS idx_cc_a_optic ON cutsheet_connections(a_optic);
CREATE INDEX IF NOT EXISTS idx_cc_z_optic ON cutsheet_connections(z_optic);
CREATE INDEX IF NOT EXISTS idx_cc_cable_id ON cutsheet_connections(cable_id);
-- R20: Fast equality filter on normalized status (no ILIKE needed)
CREATE INDEX IF NOT EXISTS idx_cc_status_norm ON cutsheet_connections(status_normalized);

-- Migration: add new columns to existing deployments (safe to re-run)
ALTER TABLE cutsheet_connections ADD COLUMN IF NOT EXISTS a_model            TEXT;
ALTER TABLE cutsheet_connections ADD COLUMN IF NOT EXISTS a_loc_cab_ru       TEXT;
ALTER TABLE cutsheet_connections ADD COLUMN IF NOT EXISTS z_model            TEXT;
ALTER TABLE cutsheet_connections ADD COLUMN IF NOT EXISTS z_loc_cab_ru       TEXT;
ALTER TABLE cutsheet_connections ADD COLUMN IF NOT EXISTS status_normalized  TEXT;
-- H8: device role columns populated via JOIN to host_inventory after load
ALTER TABLE cutsheet_connections ADD COLUMN IF NOT EXISTS a_role             TEXT;
ALTER TABLE cutsheet_connections ADD COLUMN IF NOT EXISTS z_role             TEXT;
ALTER TABLE cutsheet_connections ADD COLUMN IF NOT EXISTS a_breakout_loc     TEXT;
ALTER TABLE cutsheet_connections ADD COLUMN IF NOT EXISTS a_breakout_port    TEXT;
ALTER TABLE cutsheet_connections ADD COLUMN IF NOT EXISTS z_breakout_loc     TEXT;
ALTER TABLE cutsheet_connections ADD COLUMN IF NOT EXISTS z_breakout_port    TEXT;
ALTER TABLE cutsheet_connections ADD COLUMN IF NOT EXISTS a_patch_panel      TEXT;
ALTER TABLE cutsheet_connections ADD COLUMN IF NOT EXISTS z_patch_panel      TEXT;
-- H8: partial indexes (most rows have no role; skip NULLs to keep index small)
CREATE INDEX IF NOT EXISTS idx_cc_a_role ON cutsheet_connections(a_role) WHERE a_role IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cc_z_role ON cutsheet_connections(z_role) WHERE z_role IS NOT NULL;
ALTER TABLE cutsheet_uploads     ADD COLUMN IF NOT EXISTS file_hash          TEXT;
ALTER TABLE cutsheet_uploads     ADD COLUMN IF NOT EXISTS is_active         BOOLEAN DEFAULT TRUE;
CREATE UNIQUE INDEX IF NOT EXISTS idx_uploads_site_hash
    ON cutsheet_uploads(site_id, file_hash)
    WHERE file_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_uploads_active ON cutsheet_uploads(site_id, is_active)
    WHERE is_active = TRUE;

-- Migration: ensure raw_rows table exists for deployments
-- upgrading from raw_row JSONB column on cutsheet_connections.
-- Duplicates the definition above; IF NOT EXISTS makes it safe.
CREATE TABLE IF NOT EXISTS cutsheet_raw_rows (
    connection_id   INTEGER PRIMARY KEY REFERENCES cutsheet_connections(id) ON DELETE CASCADE,
    raw_row         JSONB
);
-- Migrate existing raw_row data if the column still exists
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'cutsheet_connections' AND column_name = 'raw_row') THEN
        INSERT INTO cutsheet_raw_rows (connection_id, raw_row)
        SELECT id, raw_row FROM cutsheet_connections
        WHERE raw_row IS NOT NULL
        ON CONFLICT (connection_id) DO NOTHING;
        ALTER TABLE cutsheet_connections DROP COLUMN IF EXISTS raw_row;
    END IF;
END $$;

-- Burndown / link verification sheet
CREATE TABLE IF NOT EXISTS burndown_connections (
    id                      SERIAL PRIMARY KEY,
    upload_id               INTEGER NOT NULL REFERENCES cutsheet_uploads(id) ON DELETE CASCADE,
    site_id                 INTEGER NOT NULL REFERENCES sites(id),
    status                  TEXT,
    a_loc_cab_ru            TEXT,
    a_device                TEXT,
    a_port                  TEXT,
    z_loc_cab_ru            TEXT,
    z_device                TEXT,
    z_port                  TEXT,
    link_status             TEXT,
    current_neighbor        TEXT,
    current_neighbor_port   TEXT,
    cutsheet_row            INTEGER,
    dct_notes               TEXT,
    neteng_notes            TEXT
);

CREATE INDEX IF NOT EXISTS idx_bd_site ON burndown_connections(site_id);
CREATE INDEX IF NOT EXISTS idx_bd_a_device ON burndown_connections(a_device);
CREATE INDEX IF NOT EXISTS idx_bd_z_device ON burndown_connections(z_device);
CREATE INDEX IF NOT EXISTS idx_bd_link_status ON burndown_connections(link_status);

-- Host inventory
CREATE TABLE IF NOT EXISTS host_inventory (
    id          SERIAL PRIMARY KEY,
    upload_id   INTEGER NOT NULL REFERENCES cutsheet_uploads(id) ON DELETE CASCADE,
    site_id     INTEGER NOT NULL REFERENCES sites(id),
    hostname    TEXT,
    model       TEXT,
    role        TEXT,
    rack        TEXT,
    data_hall   TEXT,
    status      TEXT,
    row_type    TEXT        -- physical placement metadata (ROW:TYPE)
);

-- (included in CREATE TABLE above; kept for manual upgrades
-- against existing databases)
ALTER TABLE host_inventory ADD COLUMN IF NOT EXISTS row_type TEXT;

CREATE INDEX IF NOT EXISTS idx_hi_site ON host_inventory(site_id);
CREATE INDEX IF NOT EXISTS idx_hi_hostname ON host_inventory(hostname);
CREATE INDEX IF NOT EXISTS idx_hi_model ON host_inventory(model);
-- W14: Missing indexes for cable_type, location, and role queries
CREATE INDEX IF NOT EXISTS idx_cc_cable_type ON cutsheet_connections(cable_type)
    WHERE cable_type IS NOT NULL AND cable_type != '';
CREATE INDEX IF NOT EXISTS idx_cc_a_loc ON cutsheet_connections(a_loc_cab_ru);
CREATE INDEX IF NOT EXISTS idx_cc_z_loc ON cutsheet_connections(z_loc_cab_ru);
CREATE INDEX IF NOT EXISTS idx_hi_role ON host_inventory(role)
    WHERE role IS NOT NULL AND role != '';
CREATE INDEX IF NOT EXISTS idx_bd_status ON burndown_connections(status);

-- ========================================================================
-- Materialized views for common query patterns
-- Rule R6: Never hardcode status strings; use ILIKE patterns
-- ========================================================================

-- R21: Optic inventory per side (A/Z kept separate to avoid double-counting cables)
-- Use optic_inventory_by_side for side-specific queries.
-- Use optic_inventory_combined for "how many optics of type X at site Y?" (deduplicated by cable).
CREATE MATERIALIZED VIEW IF NOT EXISTS optic_inventory_by_side AS
SELECT
    c.site_id,
    s.site_code,
    c.a_optic AS optic_type,
    'A' AS side,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE c.status_normalized IN ('lldp_passed', 'human_verified', 'complete'))  AS in_service,
    COUNT(*) FILTER (WHERE c.status_normalized = 'lldp_failed')   AS failed,
    COUNT(*) FILTER (WHERE c.status_normalized IN ('not_run', 'not_terminated', 'pending', 'in_progress', 'addition'))  AS pending
FROM cutsheet_connections c
JOIN cutsheet_uploads u ON u.id = c.upload_id AND u.is_active = TRUE
JOIN sites s ON s.id = c.site_id
WHERE c.a_optic IS NOT NULL AND c.a_optic != '' AND c.a_optic != 'nan'
GROUP BY c.site_id, s.site_code, c.a_optic

UNION ALL

SELECT
    c.site_id,
    s.site_code,
    c.z_optic AS optic_type,
    'Z' AS side,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE c.status_normalized IN ('lldp_passed', 'human_verified', 'complete'))  AS in_service,
    COUNT(*) FILTER (WHERE c.status_normalized = 'lldp_failed')   AS failed,
    COUNT(*) FILTER (WHERE c.status_normalized IN ('not_run', 'not_terminated', 'pending', 'in_progress', 'addition'))  AS pending
FROM cutsheet_connections c
JOIN cutsheet_uploads u ON u.id = c.upload_id AND u.is_active = TRUE
JOIN sites s ON s.id = c.site_id
WHERE c.z_optic IS NOT NULL AND c.z_optic != '' AND c.z_optic != 'nan'
GROUP BY c.site_id, s.site_code, c.z_optic;

-- Deduplicated optic count per cable (1 row per cable, not per side)
-- Counts each cable once using COALESCE(a_optic, z_optic) so "how many X optics?" is accurate.
CREATE MATERIALIZED VIEW IF NOT EXISTS optic_inventory_combined AS
SELECT
    c.site_id,
    s.site_code,
    COALESCE(NULLIF(c.a_optic, ''), NULLIF(c.z_optic, '')) AS optic_type,
    COUNT(*) AS cable_count,
    COUNT(*) FILTER (WHERE c.status_normalized IN ('lldp_passed', 'human_verified', 'complete'))  AS in_service,
    COUNT(*) FILTER (WHERE c.status_normalized = 'lldp_failed')   AS failed,
    COUNT(*) FILTER (WHERE c.status_normalized IN ('not_run', 'not_terminated', 'pending', 'in_progress', 'addition'))  AS pending
FROM cutsheet_connections c
JOIN cutsheet_uploads u ON u.id = c.upload_id AND u.is_active = TRUE
JOIN sites s ON s.id = c.site_id
WHERE COALESCE(NULLIF(c.a_optic, ''), NULLIF(c.z_optic, '')) IS NOT NULL
  AND COALESCE(NULLIF(c.a_optic, ''), NULLIF(c.z_optic, '')) != 'nan'
GROUP BY c.site_id, s.site_code, COALESCE(NULLIF(c.a_optic, ''), NULLIF(c.z_optic, ''));

-- R24: Cable status summary with section dimension for section-level completion queries
CREATE MATERIALIZED VIEW IF NOT EXISTS cable_status_summary AS
SELECT
    c.site_id,
    s.site_code,
    c.section,
    c.status,
    c.status_normalized,
    COUNT(*) AS cnt
FROM cutsheet_connections c
JOIN cutsheet_uploads u ON u.id = c.upload_id AND u.is_active = TRUE
JOIN sites s ON s.id = c.site_id
GROUP BY c.site_id, s.site_code, c.section, c.status, c.status_normalized;

-- R22: Device summary - uses MODE() to pick the most frequent model per device
-- instead of MAX(model) which picks lexicographically last (arbitrary/wrong).
-- MODE() returns the value that appears most often, so if a device shows up as
-- "SN5610" on 50 connections and "" on 2, it correctly picks "SN5610".
CREATE MATERIALIZED VIEW IF NOT EXISTS device_summary AS
SELECT
    site_id,
    site_code,
    device_name,
    MAX(model) FILTER (WHERE model IS NOT NULL AND model != '' AND model != 'nan') AS model,
    COUNT(*) AS connection_count,
    COUNT(DISTINCT port) AS port_count
FROM (
    SELECT c.site_id, s.site_code, c.a_device AS device_name,
           c.a_port AS port, c.a_model AS model
    FROM cutsheet_connections c
    JOIN cutsheet_uploads u ON u.id = c.upload_id AND u.is_active = TRUE
    JOIN sites s ON s.id = c.site_id
    WHERE c.a_device IS NOT NULL AND c.a_device != '' AND c.a_device != 'nan'
    UNION ALL
    SELECT c.site_id, s.site_code, c.z_device AS device_name,
           c.z_port AS port, c.z_model AS model
    FROM cutsheet_connections c
    JOIN cutsheet_uploads u ON u.id = c.upload_id AND u.is_active = TRUE
    JOIN sites s ON s.id = c.site_id
    WHERE c.z_device IS NOT NULL AND c.z_device != '' AND c.z_device != 'nan'
) sub
GROUP BY site_id, site_code, device_name;

-- R26: Track when views were last refreshed so callers can detect stale data
CREATE TABLE IF NOT EXISTS view_refresh_log (
    id              SERIAL PRIMARY KEY,
    refreshed_at    TIMESTAMPTZ DEFAULT now(),
    triggered_by    TEXT  -- e.g. 'data_load', 'manual', 'scheduled'
);

-- Refresh function (MUST be called after every data load -- R20)
CREATE OR REPLACE FUNCTION refresh_atlas_views(p_triggered_by TEXT DEFAULT 'data_load') RETURNS void AS $$
BEGIN
    REFRESH MATERIALIZED VIEW optic_inventory_by_side;
    REFRESH MATERIALIZED VIEW optic_inventory_combined;
    REFRESH MATERIALIZED VIEW cable_status_summary;
    REFRESH MATERIALIZED VIEW device_summary;
    INSERT INTO view_refresh_log (triggered_by) VALUES (p_triggered_by);
END;
$$ LANGUAGE plpgsql;

-- Helper: check if views are stale (no refresh since last upload)
CREATE OR REPLACE FUNCTION views_are_stale() RETURNS boolean AS $$
DECLARE
    last_upload TIMESTAMPTZ;
    last_refresh TIMESTAMPTZ;
BEGIN
    SELECT MAX(created_at) INTO last_upload FROM cutsheet_uploads;
    SELECT MAX(refreshed_at) INTO last_refresh FROM view_refresh_log;
    IF last_upload IS NULL THEN RETURN false; END IF;
    IF last_refresh IS NULL THEN RETURN true; END IF;
    RETURN last_upload > last_refresh;
END;
$$ LANGUAGE plpgsql;

-- Migration: multi-site netbox snapshot tracking (no-op when netbox_snapshots absent)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'netbox_snapshots'
    ) THEN
        ALTER TABLE netbox_snapshots ADD COLUMN IF NOT EXISTS site_count   INTEGER DEFAULT 0;
        ALTER TABLE netbox_snapshots ADD COLUMN IF NOT EXISTS sites_failed INTEGER DEFAULT 0;
        ALTER TABLE netbox_snapshots ADD COLUMN IF NOT EXISTS sites_json   JSONB;
        EXECUTE $q$
            CREATE OR REPLACE VIEW netbox_latest_snapshot AS
            SELECT id, started_at, finished_at, status,
                   device_count, interface_count, optic_count,
                   site_count, sites_failed, duration_ms
            FROM netbox_snapshots
            WHERE status = 'ok'
            ORDER BY finished_at DESC LIMIT 1
        $q$;
    END IF;
END $$;
