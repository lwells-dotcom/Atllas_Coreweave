#!/usr/bin/env python3
import os
import sys
import types
import unittest

mock_pg = types.ModuleType("psycopg2")
mock_pg.connect = lambda *a, **k: None
mock_pg.extras = types.ModuleType("psycopg2.extras")
mock_pg.extras.RealDictCursor = object
mock_pg.pool = types.ModuleType("psycopg2.pool")
mock_pg.pool.ThreadedConnectionPool = type(
    "ThreadedConnectionPool", (), {"__init__": lambda *a, **k: None}
)
mock_pg.sql = types.ModuleType("psycopg2.sql")
mock_pg.OperationalError = type("OperationalError", (Exception,), {})
mock_pg.InterfaceError = type("InterfaceError", (Exception,), {})
sys.modules["psycopg2"] = mock_pg
sys.modules["psycopg2.extras"] = mock_pg.extras
sys.modules["psycopg2.pool"] = mock_pg.pool
sys.modules["psycopg2.sql"] = mock_pg.sql

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "Optic_Count"))

from atlas_query_router import _SQL_TEMPLATES, _build_location_pattern  # noqa: E402
from atlas_query_router import build_query_params, classify_question, format_results_for_llm  # noqa: E402
from query_extractors import extract_location  # noqa: E402


class LocationRackRoutingTests(unittest.TestCase):
    def test_extract_location_handles_exact_rack_tokens(self):
        self.assertEqual(extract_location("What devices are in rack dh202:041?"), "dh202:041")
        self.assertEqual(extract_location("What devices are in rack dh2 041?"), "dh2:041")
        self.assertEqual(extract_location("What devices are in rack 41?"), "41")

    def test_extract_location_handles_hall_and_rack_human_phrasing(self):
        self.assertEqual(extract_location("What devices are in dh202 rack 41?"), "dh202:041")
        self.assertEqual(extract_location("What devices are in rack 41 in dh202?"), "dh202:041")
        self.assertEqual(extract_location("Which devices sit in cab 41 at dh202?"), "dh202:041")

    def test_build_location_pattern_scopes_partial_rack_queries(self):
        self.assertEqual(_build_location_pattern("dh202:041"), "dh202%:041:%")
        self.assertEqual(_build_location_pattern("dh2:041"), "dh2%:041:%")
        self.assertEqual(_build_location_pattern("41"), "%:041:%")
        self.assertEqual(_build_location_pattern("dh202"), "dh202%:%")
        self.assertEqual(_build_location_pattern("dh202:041:33"), "dh202:041:33")
        self.assertEqual(_build_location_pattern(""), "%__NO_LOCATION__%")

    def test_build_query_params_uses_scoped_location_pattern(self):
        params = build_query_params("What devices are in rack 41?", "location_lookup", 1)
        self.assertEqual(params["location_pattern"], "%:041:%")

        params = build_query_params("What devices are in rack dh202:041?", "location_lookup", 1)
        self.assertEqual(params["location_pattern"], "dh202%:041:%")

        params = build_query_params("What devices are in dh202 rack 41?", "location_lookup", 1)
        self.assertEqual(params["location_pattern"], "dh202%:041:%")

    def test_data_hall_lookup_phrasing_routes_to_location_lookup(self):
        self.assertEqual(classify_question("What devices are in dh202?"), "location_lookup")
        params = build_query_params("What devices are in dh202?", "location_lookup", 1)
        self.assertEqual(params["location_pattern"], "dh202%:%")

    def test_rack_summary_sql_uses_both_sides_and_true_totals(self):
        sql = _SQL_TEMPLATES["rack_summary"]
        self.assertIn("z_loc_cab_ru", sql)
        self.assertIn("COUNT(DISTINCT connection_key)", sql)
        self.assertIn("COUNT(DISTINCT rack_loc) AS total_racks", sql)

    def test_location_lookup_sql_projects_matching_side(self):
        sql = _SQL_TEMPLATES["location_lookup"]
        self.assertIn("AND a_loc_cab_ru ILIKE %(location_pattern)s", sql)
        self.assertIn("AND z_loc_cab_ru ILIKE %(location_pattern)s", sql)
        self.assertIn("z_device          AS device_name", sql)
        self.assertIn("z_loc_cab_ru      AS location", sql)

    def test_overview_and_summary_phrasing_routes_to_location_intent(self):
        # "overview" with explicit data-hall + rack number → rack-level rollup
        result = classify_question("Provide an overview of DH 202 Rack 002")
        self.assertIn(result, ("rack_summary", "location_lookup"))

        # "summary" with LOC:CAB token but no LOCATION_WORDS → rack_summary
        self.assertEqual(classify_question("Summary of dh202:002"), "rack_summary")

        # "tell me about rack" with LOC:CAB → specific connection lookup
        self.assertEqual(classify_question("Tell me about rack dh202:002"), "location_lookup")

        # "details on" with LOC:CAB (no rack keyword) → location_lookup
        self.assertEqual(classify_question("Details on dh204:121"), "location_lookup")

    def test_rack_summary_formatter_uses_total_racks_not_row_count(self):
        text = format_results_for_llm(
            "rack_summary",
            [
                {
                    "loc_cab_ru": "dh202:041",
                    "connections": 59,
                    "devices": 9,
                    "models": "SN2201, SN3700",
                    "total_racks": 87,
                    "site_unique_connections": 7200,
                },
                {
                    "loc_cab_ru": "dh202:042",
                    "connections": 58,
                    "devices": 9,
                    "models": "SN2201, SN3700",
                    "total_racks": 87,
                    "site_unique_connections": 7200,
                },
            ],
            "How many racks are represented in the cutsheet?",
        )
        self.assertIn("Total racks: 87", text)
        self.assertIn("Site unique connections: 7200", text)

    def test_location_lookup_formatter_deduplicates_to_unique_devices(self):
        # Simulate the new deduplicated SQL output: 38 unique device+side rows
        mock_rows = [
            {
                "device_name": f"device-{i:02d}",
                "model": "SN2201",
                "side": "A" if i % 2 == 0 else "Z",
                "connection_count": 4,
                "location": "dh202:002:10",
                "source": "cutsheet",
            }
            for i in range(38)
        ]
        text = format_results_for_llm(
            "location_lookup", mock_rows, "list all devices in dh202:002"
        )
        self.assertIn("38 unique device+side entries", text)
        self.assertIn("device-00", text)
        self.assertIn("A-side", text)
        self.assertIn("Z-side", text)
        self.assertIn("@ dh202:002:10", text)

    def test_extract_location_rejects_verb_tokens_after_rack(self):
        self.assertEqual(extract_location("which rack has the most connections"), "")
        self.assertEqual(extract_location("rack with the most cables"), "")
        self.assertEqual(extract_location("which rack are the most devices in"), "")

    def test_extract_location_accepts_digit_rack_identifiers(self):
        self.assertEqual(extract_location("show me rack 002"), "002")
        self.assertEqual(extract_location("devices in rack 41"), "41")
        self.assertEqual(extract_location("devices in rack dh202:002"), "dh202:002")


if __name__ == "__main__":
    unittest.main()
