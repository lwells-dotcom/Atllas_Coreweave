"""SQL templates for atlas_query_router. Parameterized, no injection risk."""
from typing import Dict

# ---------------------------------------------------------------------------
# SQL templates (parameterized, no injection risk)
# All templates support upload_id scoping:
#   AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
# When upload_id is None this condition is always true (full site scope).
# ---------------------------------------------------------------------------

_SQL_TEMPLATES: Dict[str, str] = {
    # B11: UNION ALL per-side aggregation. Counts each optic on the side it
    # actually appears on. Mixed-optic cables (A=X, Z=Y) count once for each
    # optic type instead of being silently grouped under the A-side optic.
    # cable_count = total physical optic instances (a_count + z_count).
    # Status counts are per-physical-optic, not per-connection-row.
    #
    # Breakout deduplication: a single QSFPDD in a 4-way breakout generates
    # 4 connection rows in the DB (one per fan-out fiber) but is one physical
    # optic. We deduplicate A-side breakouts by (upload_id, a_loc_cab_ru,
    # a_port) and Z-side breakouts by (upload_id, z_loc_cab_ru, z_port),
    # keeping the lowest-id row (first in the sheet) to preserve its status.
    # Non-breakout rows are already 1:1 with physical optics.
    "optic_count": """
        WITH a_deduped AS (
            SELECT a_optic AS optic_type, 'A' AS side, status_normalized
            FROM (
                SELECT
                    a_optic, status_normalized,
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            CASE WHEN a_breakout_loc IS NOT NULL AND a_breakout_loc != ''
                                 THEN upload_id::text || '|' || COALESCE(a_loc_cab_ru, '') || '|' || COALESCE(a_port, '')
                                 ELSE id::text
                            END
                        ORDER BY id
                    ) AS rn
                FROM cutsheet_connections
                WHERE site_id = %(site_id)s
                  AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
                  AND a_optic IS NOT NULL AND a_optic != '' AND a_optic != 'nan'
                  AND (%(optic_filter)s = '' OR a_optic ILIKE %(optic_filter)s)
                  AND (%(section_filter)s = '' OR section ILIKE %(section_filter)s)
                  AND (%(location_filter)s = '' OR a_loc_cab_ru ILIKE %(location_filter)s
                       OR z_loc_cab_ru ILIKE %(location_filter)s)
            ) t WHERE rn = 1
        ),
        z_deduped AS (
            SELECT z_optic AS optic_type, 'Z' AS side, status_normalized
            FROM (
                SELECT
                    z_optic, status_normalized,
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            CASE WHEN z_breakout_loc IS NOT NULL AND z_breakout_loc != ''
                                 THEN upload_id::text || '|' || COALESCE(z_loc_cab_ru, '') || '|' || COALESCE(z_port, '')
                                 ELSE id::text
                            END
                        ORDER BY id
                    ) AS rn
                FROM cutsheet_connections
                WHERE site_id = %(site_id)s
                  AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
                  AND z_optic IS NOT NULL AND z_optic != '' AND z_optic != 'nan'
                  AND (%(optic_filter)s = '' OR z_optic ILIKE %(optic_filter)s)
                  AND (%(section_filter)s = '' OR section ILIKE %(section_filter)s)
                  AND (%(location_filter)s = '' OR a_loc_cab_ru ILIKE %(location_filter)s
                       OR z_loc_cab_ru ILIKE %(location_filter)s)
            ) t WHERE rn = 1
        )
        SELECT
            optic_type,
            a_count + z_count                                              AS cable_count,
            a_count,
            z_count,
            in_service,
            failed,
            pending
        FROM (
            SELECT
                optic_type,
                SUM(CASE WHEN side = 'A' THEN 1 ELSE 0 END)              AS a_count,
                SUM(CASE WHEN side = 'Z' THEN 1 ELSE 0 END)              AS z_count,
                COUNT(*) FILTER (WHERE status_normalized IN
                    ('lldp_passed', 'human_verified', 'complete'))         AS in_service,
                COUNT(*) FILTER (WHERE status_normalized = 'lldp_failed') AS failed,
                COUNT(*) FILTER (WHERE status_normalized IN
                    ('not_run', 'not_terminated', 'pending', 'in_progress', 'addition')) AS pending
            FROM (
                SELECT * FROM a_deduped
                UNION ALL
                SELECT * FROM z_deduped
            ) sides
            GROUP BY optic_type
        ) sub
        ORDER BY cable_count DESC
    """,

    "z_device_list": """
        SELECT device_name, connections, ports,
               COUNT(*) OVER () AS total_unique
        FROM (
            SELECT z_device AS device_name,
                   COUNT(*) AS connections,
                   COUNT(DISTINCT z_port) AS ports
            FROM cutsheet_connections
            WHERE site_id = %(site_id)s
              AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
              AND z_device IS NOT NULL AND z_device != '' AND z_device != 'nan'
            GROUP BY z_device
        ) sub
        ORDER BY connections DESC
        LIMIT 200
    """,

    "a_device_list": """
        SELECT device_name, connections, ports,
               COUNT(*) OVER () AS total_unique
        FROM (
            SELECT a_device AS device_name,
                   COUNT(*) AS connections,
                   COUNT(DISTINCT a_port) AS ports
            FROM cutsheet_connections
            WHERE site_id = %(site_id)s
              AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
              AND a_device IS NOT NULL AND a_device != '' AND a_device != 'nan'
            GROUP BY a_device
        ) sub
        ORDER BY connections DESC
        LIMIT 200
    """,

    "role_lookup": """
        WITH role_rows AS (
            SELECT 'A'   AS side,
                   a_role AS role,
                   a_device AS device_name,
                   a_model  AS model
            FROM cutsheet_connections
            WHERE site_id    = %(site_id)s
              AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
              AND a_role  IS NOT NULL AND a_role  != '' AND a_role  != 'nan'
              AND a_device IS NOT NULL AND a_device != '' AND a_device != 'nan'
              AND (%(role_filter)s  = '' OR a_role  ILIKE %(role_filter)s)
              AND (%(side_filter)s  = '' OR %(side_filter)s = 'A')
              AND (%(device_filter)s = '' OR a_device ILIKE %(device_filter)s)
            UNION ALL
            SELECT 'Z',
                   z_role,
                   z_device,
                   z_model
            FROM cutsheet_connections
            WHERE site_id    = %(site_id)s
              AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
              AND z_role  IS NOT NULL AND z_role  != '' AND z_role  != 'nan'
              AND z_device IS NOT NULL AND z_device != '' AND z_device != 'nan'
              AND (%(role_filter)s  = '' OR z_role  ILIKE %(role_filter)s)
              AND (%(side_filter)s  = '' OR %(side_filter)s = 'Z')
              AND (%(device_filter)s = '' OR z_device ILIKE %(device_filter)s)
        )
        SELECT role, side, device_name,
               MODE() WITHIN GROUP (ORDER BY model)
                   FILTER (WHERE model IS NOT NULL AND model != '') AS model,
               COUNT(*) AS connection_count
        FROM role_rows
        GROUP BY role, side, device_name
        ORDER BY role, side, device_name
        LIMIT 200
    """,

    "device_list": """
        SELECT device_name, COUNT(*) AS connections, COUNT(DISTINCT port) AS ports
        FROM (
            SELECT a_device AS device_name, a_port AS port
            FROM cutsheet_connections
            WHERE site_id = %(site_id)s
              AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
              AND a_device IS NOT NULL AND a_device != '' AND a_device != 'nan'
            UNION ALL
            SELECT z_device, z_port
            FROM cutsheet_connections
            WHERE site_id = %(site_id)s
              AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
              AND z_device IS NOT NULL AND z_device != '' AND z_device != 'nan'
        ) sub
        GROUP BY device_name
        ORDER BY connections DESC
        LIMIT 200
    """,

    "device_detail": """
        SELECT device_name, COUNT(*) AS connections, COUNT(DISTINCT port) AS ports
        FROM (
            SELECT a_device AS device_name, a_port AS port
            FROM cutsheet_connections
            WHERE site_id = %(site_id)s
              AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
              AND a_device IS NOT NULL AND a_device != '' AND a_device != 'nan'
              AND a_device ILIKE %(device_pattern)s
            UNION ALL
            SELECT z_device, z_port
            FROM cutsheet_connections
            WHERE site_id = %(site_id)s
              AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
              AND z_device IS NOT NULL AND z_device != '' AND z_device != 'nan'
              AND z_device ILIKE %(device_pattern)s
        ) sub
        GROUP BY device_name
        ORDER BY connections DESC
    """,

    "device_connections": """
        SELECT section, a_device, a_port, a_optic,
               z_device, z_port, z_optic, cable_id, status
        FROM cutsheet_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
          AND (a_device ILIKE %(device_pattern)s OR z_device ILIKE %(device_pattern)s)
        ORDER BY section, a_port
        LIMIT 200
    """,

    "connection_status": """
        SELECT status_normalized, status, COUNT(*) AS cnt
        FROM cutsheet_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
          AND status_normalized IN ('lldp_passed', 'lldp_failed', 'human_verified')
        GROUP BY status_normalized, status
        ORDER BY cnt DESC
    """,

    "cable_status": """
        SELECT status_normalized, status, COUNT(*) AS cnt
        FROM cutsheet_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
          AND status_normalized IN ('not_run', 'not_terminated', 'complete',
                                    'in_progress', 'addition', 'pending')
        GROUP BY status_normalized, status
        ORDER BY cnt DESC
    """,

    "section_summary": """
        SELECT section,
               COUNT(*) AS connections,
               COUNT(DISTINCT a_device) AS a_devices,
               COUNT(DISTINCT z_device) AS z_devices
        FROM cutsheet_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
          AND (%(section_name_filter)s = '' OR section ILIKE %(section_name_filter)s)
        GROUP BY section
        ORDER BY connections DESC
    """,

    "section_completion": """
        SELECT section,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE status_normalized IN ('complete', 'lldp_passed', 'human_verified')) AS complete,
               COUNT(*) FILTER (WHERE status_normalized NOT IN ('complete', 'lldp_passed', 'human_verified')) AS incomplete,
               ROUND(100.0 * COUNT(*) FILTER (WHERE status_normalized IN ('complete', 'lldp_passed', 'human_verified')) / NULLIF(COUNT(*), 0), 1) AS pct_complete
        FROM cutsheet_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
          AND (%(section_name_filter)s = '' OR section ILIKE %(section_name_filter)s)
        GROUP BY section
        ORDER BY incomplete DESC, total DESC
    """,

    "lldp_failures": """
        SELECT section, a_device, a_port, z_device, z_port, cable_id, status
        FROM cutsheet_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
          AND status_normalized = 'lldp_failed'
        ORDER BY section, a_device
        LIMIT 100
    """,

    "site_overview": """
        SELECT
            (SELECT COUNT(*) FROM cutsheet_connections
             WHERE site_id = %(site_id)s
               AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
            ) AS total_connections,
            (SELECT COUNT(DISTINCT a_device) FROM cutsheet_connections
             WHERE site_id = %(site_id)s
               AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
               AND a_device IS NOT NULL AND a_device != '' AND a_device != 'nan'
            ) AS total_devices,
            (SELECT COUNT(DISTINCT section) FROM cutsheet_connections
             WHERE site_id = %(site_id)s
               AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
            ) AS total_sections
    """,

    "data_hall_summary": """
        SELECT split_part(a_loc_cab_ru, ':', 1) AS data_hall,
               COUNT(*) AS connections,
               COUNT(DISTINCT a_device) AS devices
        FROM cutsheet_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
          AND a_loc_cab_ru IS NOT NULL AND a_loc_cab_ru != ''
          AND (%(hall_filter)s = '' OR split_part(a_loc_cab_ru, ':', 1) ILIKE %(hall_filter)s)
        GROUP BY split_part(a_loc_cab_ru, ':', 1)
        ORDER BY connections DESC
    """,

    "ip_lookup": """
        SELECT cc.a_device, cc.z_device, cc.a_port, cc.z_port, cc.status, rr.raw_row
        FROM cutsheet_connections cc
        LEFT JOIN cutsheet_raw_rows rr ON rr.connection_id = cc.id
        WHERE cc.site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR cc.upload_id = %(upload_id)s::bigint)
          AND rr.raw_row::text ILIKE %(search_pattern)s
        LIMIT 50
    """,

    "node_compute": """
        SELECT device_name, COUNT(*) AS connections, COUNT(DISTINCT port) AS ports
        FROM (
            SELECT a_device AS device_name, a_port AS port
            FROM cutsheet_connections
            WHERE site_id = %(site_id)s
              AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
              AND a_device IS NOT NULL AND a_device != '' AND a_device != 'nan'
              AND (a_device ILIKE '%%node%%'
                   OR a_device ILIKE '%%compute%%'
                   OR a_device ILIKE '%%gpu%%'
                   OR a_device ILIKE '%%server%%')
            UNION ALL
            SELECT z_device, z_port
            FROM cutsheet_connections
            WHERE site_id = %(site_id)s
              AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
              AND z_device IS NOT NULL AND z_device != '' AND z_device != 'nan'
              AND (z_device ILIKE '%%node%%'
                   OR z_device ILIKE '%%compute%%'
                   OR z_device ILIKE '%%gpu%%'
                   OR z_device ILIKE '%%server%%')
        ) sub
        GROUP BY device_name
        ORDER BY connections DESC
    """,

    "model_search": """
        SELECT device_name, model, connections,
               COUNT(*) OVER () AS total_unique
        FROM (
            SELECT device_name, model, SUM(connections) AS connections
            FROM (
                SELECT a_device AS device_name, a_model AS model, 1 AS connections
                FROM cutsheet_connections
                WHERE site_id = %(site_id)s
                  AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
                  AND (a_model ILIKE %(model_pattern)s OR a_device ILIKE %(model_pattern)s)
                  AND a_device IS NOT NULL AND a_device != '' AND a_device != 'nan'
                  AND (%(model_status_filters)s::text[] IS NULL OR status_normalized = ANY(%(model_status_filters)s::text[]))
                  AND (%(location_filter)s = '' OR a_loc_cab_ru ILIKE %(location_filter)s)
                  AND (%(data_hall_filter)s = '' OR a_loc_cab_ru ILIKE %(data_hall_filter)s
                       OR z_loc_cab_ru ILIKE %(data_hall_filter)s)
                UNION ALL
                SELECT z_device AS device_name, z_model AS model, 1 AS connections
                FROM cutsheet_connections
                WHERE site_id = %(site_id)s
                  AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
                  AND (z_model ILIKE %(model_pattern)s OR z_device ILIKE %(model_pattern)s)
                  AND z_device IS NOT NULL AND z_device != '' AND z_device != 'nan'
                  AND (%(model_status_filters)s::text[] IS NULL OR status_normalized = ANY(%(model_status_filters)s::text[]))
                  AND (%(location_filter)s = '' OR z_loc_cab_ru ILIKE %(location_filter)s)
                  AND (%(data_hall_filter)s = '' OR a_loc_cab_ru ILIKE %(data_hall_filter)s
                       OR z_loc_cab_ru ILIKE %(data_hall_filter)s)
                UNION ALL
                SELECT hostname AS device_name, model, 0 AS connections
                FROM host_inventory
                WHERE site_id = %(site_id)s
                  AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
                  AND (model ILIKE %(model_pattern)s OR hostname ILIKE %(model_pattern)s)
                  AND %(model_status_filters)s::text[] IS NULL
                  AND %(location_filter)s = ''
                  AND (%(data_hall_filter)s = '' OR rack ILIKE %(data_hall_filter)s)
            ) combined
            GROUP BY device_name, model
        ) sub
        ORDER BY connections DESC, device_name
        LIMIT 200
    """,

    "link_status": """
        SELECT a_device, a_port, z_device, z_port, link_status, status,
               current_neighbor, current_neighbor_port, dct_notes
        FROM burndown_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
        ORDER BY link_status, a_device
        LIMIT 200
    """,

    "lldp_neighbor_mismatch": """
        SELECT a_device, a_port, z_device, z_port,
               current_neighbor, current_neighbor_port,
               link_status, status, dct_notes
        FROM burndown_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
          AND current_neighbor IS NOT NULL
          AND current_neighbor != ''
          AND LOWER(TRIM(current_neighbor)) != LOWER(TRIM(z_device))
        ORDER BY a_device, a_port
        LIMIT 200
    """,

    "rack_summary": """
        WITH endpoint_rows AS (
            SELECT
                split_part(a_loc_cab_ru, ':', 1) || ':' ||
                LPAD(split_part(a_loc_cab_ru, ':', 2), 3, '0') AS rack_loc,
                a_device AS device_name,
                a_model AS model,
                a_optic AS optic,
                COALESCE(
                    NULLIF(cable_id, ''),
                    CONCAT_WS('|',
                        COALESCE(a_device, ''), COALESCE(a_port, ''),
                        COALESCE(z_device, ''), COALESCE(z_port, '')
                    )
                ) AS connection_key
            FROM cutsheet_connections
            WHERE site_id = %(site_id)s
              AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
              AND a_loc_cab_ru IS NOT NULL AND a_loc_cab_ru != '' AND a_loc_cab_ru != 'nan'
              AND split_part(a_loc_cab_ru, ':', 1) != ''
              AND split_part(a_loc_cab_ru, ':', 2) != ''
              AND (%(location_filter)s = '' OR a_loc_cab_ru ILIKE %(location_filter)s)

            UNION ALL

            SELECT
                split_part(z_loc_cab_ru, ':', 1) || ':' ||
                LPAD(split_part(z_loc_cab_ru, ':', 2), 3, '0') AS rack_loc,
                z_device AS device_name,
                z_model AS model,
                z_optic AS optic,
                COALESCE(
                    NULLIF(cable_id, ''),
                    CONCAT_WS('|',
                        COALESCE(a_device, ''), COALESCE(a_port, ''),
                        COALESCE(z_device, ''), COALESCE(z_port, '')
                    )
                ) AS connection_key
            FROM cutsheet_connections
            WHERE site_id = %(site_id)s
              AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
              AND z_loc_cab_ru IS NOT NULL AND z_loc_cab_ru != '' AND z_loc_cab_ru != 'nan'
              AND split_part(z_loc_cab_ru, ':', 1) != ''
              AND split_part(z_loc_cab_ru, ':', 2) != ''
              AND (%(location_filter)s = '' OR z_loc_cab_ru ILIKE %(location_filter)s)
        ),
        rack_agg AS (
            SELECT
                rack_loc AS loc_cab_ru,
                COUNT(DISTINCT connection_key) AS connections,
                COUNT(DISTINCT device_name) FILTER (
                    WHERE device_name IS NOT NULL AND device_name != '' AND device_name != 'nan'
                ) AS devices,
                STRING_AGG(DISTINCT model, ', ' ORDER BY model) FILTER (
                    WHERE model IS NOT NULL AND model != '' AND model != 'nan'
                ) AS models,
                STRING_AGG(DISTINCT optic, ', ' ORDER BY optic) FILTER (
                    WHERE optic IS NOT NULL AND optic != '' AND optic != 'nan'
                ) AS optics,
                COUNT(optic) FILTER (
                    WHERE optic IS NOT NULL AND optic != '' AND optic != 'nan'
                ) AS optic_count
            FROM endpoint_rows
            GROUP BY rack_loc
        ),
        optic_type_counts AS (
            SELECT
                rack_loc,
                optic,
                COUNT(*) AS cnt
            FROM endpoint_rows
            WHERE optic IS NOT NULL AND optic != '' AND optic != 'nan'
            GROUP BY rack_loc, optic
        ),
        optic_breakdown_agg AS (
            SELECT
                rack_loc,
                STRING_AGG(optic || ': ' || cnt::text, ', ' ORDER BY cnt DESC) AS optic_breakdown
            FROM optic_type_counts
            GROUP BY rack_loc
        ),
        site_totals AS (
            SELECT
                COUNT(DISTINCT rack_loc) AS total_racks,
                COUNT(DISTINCT connection_key) AS site_unique_connections
            FROM endpoint_rows
        )
        SELECT
            r.loc_cab_ru,
            r.connections,
            r.devices,
            r.models,
            r.optics,
            r.optic_count,
            ob.optic_breakdown,
            s.total_racks,
            s.site_unique_connections
        FROM rack_agg r
        LEFT JOIN optic_breakdown_agg ob ON ob.rack_loc = r.loc_cab_ru
        CROSS JOIN site_totals s
        ORDER BY r.connections DESC, r.loc_cab_ru
        LIMIT 50
    """,

    "location_lookup": """
        WITH endpoints AS (
            SELECT a_device          AS device_name,
                   a_model           AS model,
                   'A'               AS side,
                   a_loc_cab_ru      AS location,
                   'cutsheet'        AS source
            FROM cutsheet_connections
            WHERE site_id = %(site_id)s
              AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
              AND a_device IS NOT NULL AND a_device != '' AND a_device != 'nan'
              AND a_loc_cab_ru ILIKE %(location_pattern)s
            UNION ALL
            SELECT z_device          AS device_name,
                   z_model           AS model,
                   'Z'               AS side,
                   z_loc_cab_ru      AS location,
                   'cutsheet'        AS source
            FROM cutsheet_connections
            WHERE site_id = %(site_id)s
              AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
              AND z_device IS NOT NULL AND z_device != '' AND z_device != 'nan'
              AND z_loc_cab_ru ILIKE %(location_pattern)s
        ),
        inventory AS (
            SELECT hostname           AS device_name,
                   model,
                   'inventory'        AS side,
                   rack               AS location,
                   'inventory'        AS source
            FROM host_inventory
            WHERE site_id = %(site_id)s
              AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
              AND rack ILIKE %(location_pattern)s
        )
        SELECT device_name,
               MODE() WITHIN GROUP (ORDER BY model)
                   FILTER (WHERE model IS NOT NULL AND model != '' AND model != 'nan') AS model,
               side,
               COUNT(*)          AS connection_count,
               MIN(location)     AS location,
               source
        FROM (
            SELECT device_name, model, side, location, source FROM endpoints
            UNION ALL
            SELECT device_name, model, side, location, source FROM inventory
        ) combined
        GROUP BY device_name, side, source
        ORDER BY source, connection_count DESC, device_name
        LIMIT 200
    """,

    "upload_diff": """
        WITH upload_a AS (
            SELECT upload_id, site_id, section, a_device, a_port, a_optic,
                   z_device, z_port, z_optic, cable_id, status, status_normalized,
                   a_model, z_model, a_loc_cab_ru, z_loc_cab_ru, a_role, z_role
            FROM cutsheet_connections
            WHERE upload_id = %(upload_id_a)s::bigint
              AND site_id = %(site_id)s::bigint
        ),
        upload_b AS (
            SELECT upload_id, site_id, section, a_device, a_port, a_optic,
                   z_device, z_port, z_optic, cable_id, status, status_normalized,
                   a_model, z_model, a_loc_cab_ru, z_loc_cab_ru, a_role, z_role
            FROM cutsheet_connections
            WHERE upload_id = %(upload_id_b)s::bigint
              AND site_id = %(site_id)s::bigint
        ),
        removed AS (
            SELECT 'removed' AS change_type, a.section, a.a_device, a.a_port,
                   a.z_device, a.z_port, a.a_optic, a.z_optic, a.status,
                   a.a_model, a.z_model, a.a_role, a.z_role,
                   a.a_loc_cab_ru, a.z_loc_cab_ru, a.cable_id
            FROM upload_a a
            WHERE NOT EXISTS (
                SELECT 1 FROM upload_b b
                WHERE a.a_device = b.a_device AND a.a_port = b.a_port
                  AND a.z_device = b.z_device AND a.z_port = b.z_port
            )
        ),
        added AS (
            SELECT 'added' AS change_type, b.section, b.a_device, b.a_port,
                   b.z_device, b.z_port, b.a_optic, b.z_optic, b.status,
                   b.a_model, b.z_model, b.a_role, b.z_role,
                   b.a_loc_cab_ru, b.z_loc_cab_ru, b.cable_id
            FROM upload_b b
            WHERE NOT EXISTS (
                SELECT 1 FROM upload_a a
                WHERE a.a_device = b.a_device AND a.a_port = b.a_port
                  AND a.z_device = b.z_device AND a.z_port = b.z_port
            )
        ),
        status_changed AS (
            SELECT 'status_changed' AS change_type, a.section, a.a_device, a.a_port,
                   a.z_device, a.z_port, a.a_optic, a.z_optic,
                   a.status || ' -> ' || b.status AS status,
                   a.a_model, a.z_model, a.a_role, a.z_role,
                   a.a_loc_cab_ru, a.z_loc_cab_ru, a.cable_id
            FROM upload_a a
            INNER JOIN upload_b b
                ON a.a_device = b.a_device AND a.a_port = b.a_port
               AND a.z_device = b.z_device AND a.z_port = b.z_port
            WHERE COALESCE(a.status, '') != COALESCE(b.status, '')
        ),
        optic_changed AS (
            SELECT 'optic_changed' AS change_type, a.section, a.a_device, a.a_port,
                   a.z_device, a.z_port,
                   a.a_optic || ' -> ' || b.a_optic || ' / ' || a.z_optic || ' -> ' || b.z_optic AS a_optic,
                   NULL AS z_optic, a.status,
                   a.a_model, a.z_model, a.a_role, a.z_role,
                   a.a_loc_cab_ru, a.z_loc_cab_ru, a.cable_id
            FROM upload_a a
            INNER JOIN upload_b b
                ON a.a_device = b.a_device AND a.a_port = b.a_port
               AND a.z_device = b.z_device AND a.z_port = b.z_port
            WHERE (COALESCE(a.a_optic, '') != COALESCE(b.a_optic, '')
                OR COALESCE(a.z_optic, '') != COALESCE(b.z_optic, ''))
              AND COALESCE(a.status, '') = COALESCE(b.status, '')
        )
        SELECT change_type, COUNT(*) AS count,
               ARRAY_AGG(
                   JSON_BUILD_OBJECT(
                       'section', section, 'a_device', a_device, 'a_port', a_port,
                       'z_device', z_device, 'z_port', z_port, 'a_optic', a_optic,
                       'z_optic', z_optic, 'status', status, 'a_model', a_model,
                       'z_model', z_model, 'cable_id', cable_id
                   ) ORDER BY section, a_device, a_port
               ) AS items
        FROM (
            SELECT * FROM removed UNION ALL SELECT * FROM added
            UNION ALL SELECT * FROM status_changed UNION ALL SELECT * FROM optic_changed
        ) AS all_changes
        GROUP BY change_type
        ORDER BY CASE change_type
            WHEN 'removed' THEN 1 WHEN 'added' THEN 2
            WHEN 'status_changed' THEN 3 WHEN 'optic_changed' THEN 4 ELSE 5
        END
    """,

    "upload_list": """
        SELECT id, filename, row_count, created_at, is_active, uploaded_by, profile
        FROM cutsheet_uploads
        WHERE site_id = %(site_id)s::bigint
        ORDER BY created_at DESC
        LIMIT 50
    """,

    # Cross-site queries intentionally ignore upload_id — they join across all
    # active uploads (cu.is_active = TRUE) for every site. The upload_id param
    # is present in the params dict but unused by these templates.
    "cross_site_models": """
        SELECT model, site_code, sites_present, connection_count
        FROM (
            SELECT
                COALESCE(NULLIF(a_model, ''), NULLIF(z_model, '')) AS model,
                s.site_code,
                COUNT(DISTINCT s.id) OVER (
                    PARTITION BY COALESCE(NULLIF(a_model, ''), NULLIF(z_model, ''))
                ) AS sites_present,
                COUNT(*) AS connection_count
            FROM cutsheet_connections cc
            JOIN cutsheet_uploads cu ON cc.upload_id = cu.id
            JOIN sites s ON cc.site_id = s.id
            WHERE cu.is_active = TRUE
              AND (%(site_codes)s::text[] IS NULL OR s.site_code = ANY(%(site_codes)s::text[]))
              AND COALESCE(NULLIF(a_model, ''), NULLIF(z_model, '')) IS NOT NULL
              AND COALESCE(NULLIF(a_model, ''), NULLIF(z_model, '')) != 'nan'
            GROUP BY COALESCE(NULLIF(a_model, ''), NULLIF(z_model, '')), s.site_code, s.id
        ) sub
        ORDER BY model, site_code
    """,

    # Per-side UNION ALL mirrors the optic_count template so mixed-optic cables
    # (A=OSFP-800G, Z=QSFP112-400G) count once for each optic type instead of
    # being collapsed to only the A-side optic via COALESCE.
    "cross_site_optics": """
        WITH a_deduped AS (
            SELECT t.a_optic AS optic_type, s.site_code, t.status_normalized
            FROM (
                SELECT
                    cc.a_optic, cc.status_normalized, cc.upload_id, cc.site_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            cc.site_id,
                            CASE WHEN cc.a_breakout_loc IS NOT NULL AND cc.a_breakout_loc != ''
                                 THEN cc.upload_id::text || '|' || COALESCE(cc.a_loc_cab_ru, '') || '|' || COALESCE(cc.a_port, '')
                                 ELSE cc.id::text
                            END
                        ORDER BY cc.id
                    ) AS rn
                FROM cutsheet_connections cc
                JOIN cutsheet_uploads cu ON cc.upload_id = cu.id
                WHERE cu.is_active = TRUE
                  AND cc.a_optic IS NOT NULL AND cc.a_optic != '' AND cc.a_optic != 'nan'
            ) t
            JOIN sites s ON t.site_id = s.id
            WHERE t.rn = 1
              AND (%(site_codes)s::text[] IS NULL OR s.site_code = ANY(%(site_codes)s::text[]))
        ),
        z_deduped AS (
            SELECT t.z_optic AS optic_type, s.site_code, t.status_normalized
            FROM (
                SELECT
                    cc.z_optic, cc.status_normalized, cc.upload_id, cc.site_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            cc.site_id,
                            CASE WHEN cc.z_breakout_loc IS NOT NULL AND cc.z_breakout_loc != ''
                                 THEN cc.upload_id::text || '|' || COALESCE(cc.z_loc_cab_ru, '') || '|' || COALESCE(cc.z_port, '')
                                 ELSE cc.id::text
                            END
                        ORDER BY cc.id
                    ) AS rn
                FROM cutsheet_connections cc
                JOIN cutsheet_uploads cu ON cc.upload_id = cu.id
                WHERE cu.is_active = TRUE
                  AND cc.z_optic IS NOT NULL AND cc.z_optic != '' AND cc.z_optic != 'nan'
            ) t
            JOIN sites s ON t.site_id = s.id
            WHERE t.rn = 1
              AND (%(site_codes)s::text[] IS NULL OR s.site_code = ANY(%(site_codes)s::text[]))
        )
        SELECT optic_type, site_code, cable_count, in_service, failed, pending
        FROM (
            SELECT
                optic_type,
                site_code,
                COUNT(*)                                                    AS cable_count,
                COUNT(*) FILTER (WHERE status_normalized IN
                    ('lldp_passed', 'human_verified', 'complete'))          AS in_service,
                COUNT(*) FILTER (WHERE status_normalized = 'lldp_failed')   AS failed,
                COUNT(*) FILTER (WHERE status_normalized IN
                    ('not_run', 'not_terminated', 'pending', 'in_progress', 'addition')) AS pending
            FROM (
                SELECT * FROM a_deduped
                UNION ALL
                SELECT * FROM z_deduped
            ) sides
            GROUP BY optic_type, site_code
        ) sub
        ORDER BY optic_type, site_code
    """,

    "cross_site_status": """
        SELECT site_code, status_normalized, connection_count
        FROM (
            SELECT s.site_code, cc.status_normalized, COUNT(*) AS connection_count
            FROM cutsheet_connections cc
            JOIN cutsheet_uploads cu ON cc.upload_id = cu.id
            JOIN sites s ON cc.site_id = s.id
            WHERE cu.is_active = TRUE
              AND (%(site_codes)s::text[] IS NULL OR s.site_code = ANY(%(site_codes)s::text[]))
            GROUP BY s.site_code, cc.status_normalized
        ) sub
        ORDER BY site_code, connection_count DESC
    """,

    # NOTE: trend_status intentionally includes ALL uploads (active and inactive)
    # to show the full historical timeline. If only active uploads are desired,
    # add: AND u.is_active = TRUE to the WHERE clause.
    "trend_status": """
        SELECT
            u.id AS upload_id, u.filename, u.created_at,
            COUNT(*) AS total_connections,
            COUNT(*) FILTER (WHERE c.status_normalized = 'lldp_passed') AS lldp_passed_count,
            COUNT(*) FILTER (WHERE c.status_normalized = 'lldp_failed') AS lldp_failed_count,
            COUNT(*) FILTER (WHERE c.status_normalized = 'complete') AS complete_count,
            COUNT(*) FILTER (WHERE c.status_normalized = 'human_verified') AS human_verified_count,
            COUNT(*) FILTER (WHERE c.status_normalized = 'not_run') AS not_run_count,
            COUNT(*) FILTER (WHERE c.status_normalized = 'not_terminated') AS not_terminated_count,
            COUNT(*) FILTER (WHERE c.status_normalized IN
                ('lldp_passed', 'human_verified', 'complete')) AS completion_total,
            ROUND(100.0 * COUNT(*) FILTER (WHERE c.status_normalized IN
                ('lldp_passed', 'human_verified', 'complete')) / NULLIF(COUNT(*), 0), 1) AS completion_percentage
        FROM cutsheet_uploads u
        LEFT JOIN cutsheet_connections c ON u.id = c.upload_id AND c.site_id = %(site_id)s
        WHERE u.site_id = %(site_id)s
        GROUP BY u.id, u.filename, u.created_at
        ORDER BY u.created_at ASC
        LIMIT 10
    """,

    # NOTE: trend_section intentionally includes ALL uploads for historical view.
    # Add u.is_active = TRUE filter if only active snapshots are desired.
    "trend_section": """
        SELECT
            u.id AS upload_id, u.filename, u.created_at, c.section,
            COUNT(*) AS total_connections,
            COUNT(*) FILTER (WHERE c.status_normalized = 'lldp_passed') AS lldp_passed_count,
            COUNT(*) FILTER (WHERE c.status_normalized = 'lldp_failed') AS lldp_failed_count,
            COUNT(*) FILTER (WHERE c.status_normalized = 'complete') AS complete_count,
            COUNT(*) FILTER (WHERE c.status_normalized = 'human_verified') AS human_verified_count,
            COUNT(*) FILTER (WHERE c.status_normalized = 'not_run') AS not_run_count,
            COUNT(*) FILTER (WHERE c.status_normalized = 'not_terminated') AS not_terminated_count,
            COUNT(*) FILTER (WHERE c.status_normalized IN
                ('lldp_passed', 'human_verified', 'complete')) AS completion_total,
            ROUND(100.0 * COUNT(*) FILTER (WHERE c.status_normalized IN
                ('lldp_passed', 'human_verified', 'complete')) / NULLIF(COUNT(*), 0), 1) AS completion_percentage
        FROM cutsheet_uploads u
        LEFT JOIN cutsheet_connections c ON u.id = c.upload_id AND c.site_id = %(site_id)s
        WHERE u.site_id = %(site_id)s
          AND (%(section_name_filter)s = '' OR c.section ILIKE %(section_name_filter)s)
        GROUP BY u.id, u.filename, u.created_at, c.section
        ORDER BY u.created_at ASC, c.section ASC
        LIMIT 100
    """,

    "cable_type_summary": """
        SELECT cable_type,
               COUNT(*) AS cable_count,
               COUNT(DISTINCT a_device) AS a_devices,
               COUNT(DISTINCT z_device) AS z_devices
        FROM cutsheet_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
          AND cable_type IS NOT NULL AND cable_type != '' AND cable_type != 'nan'
          AND (%(cable_type_filter)s = '' OR cable_type ILIKE %(cable_type_filter)s)
        GROUP BY cable_type
        ORDER BY cable_count DESC
    """,

    "general": """
        SELECT 'device_count' AS metric,
               COUNT(DISTINCT a_device)::text AS value
        FROM cutsheet_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
          AND a_device IS NOT NULL AND a_device != '' AND a_device != 'nan'
        UNION ALL
        SELECT 'connection_count',
               COUNT(*)::text
        FROM cutsheet_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
        UNION ALL
        SELECT 'section_count',
               COUNT(DISTINCT section)::text
        FROM cutsheet_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
    """,
}

_MODEL_SEARCH_RAW_COUNT_SQL = """
    SELECT
        COUNT(*) AS cutsheet_occurrences,
        COUNT(*) FILTER (WHERE side = 'A') AS a_side_occurrences,
        COUNT(*) FILTER (WHERE side = 'Z') AS z_side_occurrences,
        COUNT(DISTINCT device_name) AS cutsheet_unique_devices
    FROM (
        SELECT a_device AS device_name, 'A' AS side
        FROM cutsheet_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
          AND (a_model ILIKE %(model_pattern)s OR a_device ILIKE %(model_pattern)s)
          AND a_device IS NOT NULL AND a_device != '' AND a_device != 'nan'
          AND (%(location_filter)s = '' OR a_loc_cab_ru ILIKE %(location_filter)s)
          AND (%(data_hall_filter)s = '' OR a_loc_cab_ru ILIKE %(data_hall_filter)s)
        UNION ALL
        SELECT z_device AS device_name, 'Z' AS side
        FROM cutsheet_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
          AND (z_model ILIKE %(model_pattern)s OR z_device ILIKE %(model_pattern)s)
          AND z_device IS NOT NULL AND z_device != '' AND z_device != 'nan'
          AND (%(location_filter)s = '' OR z_loc_cab_ru ILIKE %(location_filter)s)
          AND (%(data_hall_filter)s = '' OR z_loc_cab_ru ILIKE %(data_hall_filter)s)
    ) combined
"""

_MODEL_SEARCH_STATUS_COUNT_SQL = """
    SELECT
        COUNT(DISTINCT location) FILTER (
            WHERE location IS NOT NULL AND location != '' AND location != 'nan'
        ) AS matching_device_locations,
        COUNT(DISTINCT device_name) FILTER (
            WHERE device_name IS NOT NULL AND device_name != '' AND device_name != 'nan'
        ) AS matching_device_names,
        COUNT(*) AS matching_cutsheet_rows,
        COUNT(*) FILTER (WHERE side = 'A') AS a_side_rows,
        COUNT(*) FILTER (WHERE side = 'Z') AS z_side_rows
    FROM (
        SELECT a_loc_cab_ru AS location, a_device AS device_name, 'A' AS side
        FROM cutsheet_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
          AND (a_model ILIKE %(model_pattern)s OR a_device ILIKE %(model_pattern)s)
          AND a_device IS NOT NULL AND a_device != '' AND a_device != 'nan'
          AND status_normalized = ANY(%(model_status_filters)s::text[])
          AND (%(location_filter)s = '' OR a_loc_cab_ru ILIKE %(location_filter)s)
          AND (%(data_hall_filter)s = '' OR a_loc_cab_ru ILIKE %(data_hall_filter)s)
        UNION ALL
        SELECT z_loc_cab_ru AS location, z_device AS device_name, 'Z' AS side
        FROM cutsheet_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
          AND (z_model ILIKE %(model_pattern)s OR z_device ILIKE %(model_pattern)s)
          AND z_device IS NOT NULL AND z_device != '' AND z_device != 'nan'
          AND status_normalized = ANY(%(model_status_filters)s::text[])
          AND (%(location_filter)s = '' OR z_loc_cab_ru ILIKE %(location_filter)s)
          AND (%(data_hall_filter)s = '' OR z_loc_cab_ru ILIKE %(data_hall_filter)s)
    ) combined
"""

_MODEL_SEARCH_UNIQUE_COUNT_SQL = """
    SELECT
        COUNT(DISTINCT device_name) AS total_unique_devices,
        COUNT(DISTINCT device_name) FILTER (WHERE source = 'cutsheet') AS cutsheet_unique_devices,
        COUNT(DISTINCT device_name) FILTER (WHERE source = 'host_inventory') AS inventory_unique_devices
    FROM (
        SELECT a_device AS device_name, 'cutsheet' AS source
        FROM cutsheet_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
          AND (a_model ILIKE %(model_pattern)s OR a_device ILIKE %(model_pattern)s)
          AND a_device IS NOT NULL AND a_device != '' AND a_device != 'nan'
          AND (%(location_filter)s = '' OR a_loc_cab_ru ILIKE %(location_filter)s)
          AND (%(data_hall_filter)s = '' OR a_loc_cab_ru ILIKE %(data_hall_filter)s)
        UNION
        SELECT z_device AS device_name, 'cutsheet' AS source
        FROM cutsheet_connections
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
          AND (z_model ILIKE %(model_pattern)s OR z_device ILIKE %(model_pattern)s)
          AND z_device IS NOT NULL AND z_device != '' AND z_device != 'nan'
          AND (%(location_filter)s = '' OR z_loc_cab_ru ILIKE %(location_filter)s)
          AND (%(data_hall_filter)s = '' OR z_loc_cab_ru ILIKE %(data_hall_filter)s)
        UNION
        SELECT hostname AS device_name, 'host_inventory' AS source
        FROM host_inventory
        WHERE site_id = %(site_id)s
          AND (%(upload_id)s::bigint IS NULL OR upload_id = %(upload_id)s::bigint)
          AND (model ILIKE %(model_pattern)s OR hostname ILIKE %(model_pattern)s)
          AND hostname IS NOT NULL AND hostname != '' AND hostname != 'nan'
          AND %(location_filter)s = ''
          AND (%(data_hall_filter)s = '' OR rack ILIKE %(data_hall_filter)s)
    ) combined
"""

