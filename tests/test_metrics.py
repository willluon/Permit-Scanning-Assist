"""Tests for the operational telemetry layer.

The two properties that matter:
  1. metric() must NEVER raise — a telemetry failure must not break scanning.
  2. The events table must stay content-free: only event names, classes,
     sources, timings, statuses, token counts. No document text, no addresses,
     no permit numbers.

Run from the repo root:  python -m unittest discover -s tests -v
"""
import os
import sys
import sqlite3
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import permit_scan as ps


class MetricsBase(unittest.TestCase):
    def setUp(self):
        self._orig_db = ps.METRICS_DB_FILE
        self._orig_batch = dict(ps._ACTIVE_BATCH)
        self.tmpdir = tempfile.mkdtemp()
        ps.METRICS_DB_FILE = os.path.join(self.tmpdir, "metrics.db")

    def tearDown(self):
        ps.METRICS_DB_FILE = self._orig_db
        ps._ACTIVE_BATCH.clear()
        ps._ACTIVE_BATCH.update(self._orig_batch)

    def rows(self):
        con = sqlite3.connect(ps.METRICS_DB_FILE)
        try:
            return con.execute(
                "SELECT event, batch_id, doc_class, source, field, status,"
                " model, duration_ms, tokens_in, tokens_out, cost_usd, detail"
                " FROM events ORDER BY id").fetchall()
        finally:
            con.close()


class TestMetricWrites(MetricsBase):
    def test_event_row_is_written(self):
        ps.metric("document_ingested", detail=".pdf")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "document_ingested")
        self.assertEqual(rows[0][11], ".pdf")

    def test_batch_id_attached_automatically(self):
        bid = ps._new_batch_id()
        ps.metric("field_filled", field="permit", source="native")
        self.assertEqual(self.rows()[0][1], bid)

    def test_new_batch_id_changes_attribution(self):
        first = ps._new_batch_id()
        ps.metric("document_ingested")
        second = ps._new_batch_id()
        ps.metric("document_ingested")
        rows = self.rows()
        self.assertEqual(rows[0][1], first)
        self.assertEqual(rows[1][1], second)
        self.assertNotEqual(first, second)

    def test_vision_event_carries_tokens_and_cost(self):
        ps.metric("vision_invoked", model="claude-haiku-4-5-20251001",
                  duration_ms=1234, tokens_in=2000, tokens_out=100,
                  cost_usd=ps._claude_cost("claude-haiku-4-5-20251001", 2000, 100))
        row = self.rows()[0]
        self.assertEqual(row[8], 2000)
        self.assertEqual(row[9], 100)
        self.assertAlmostEqual(row[10], (2000 * 1.00 + 100 * 5.00) / 1_000_000)


class TestMetricNeverRaises(MetricsBase):
    def test_unwritable_db_path_is_swallowed(self):
        # A directory path can't be opened as a database file — the insert
        # fails, and metric() must swallow it silently
        ps.METRICS_DB_FILE = self.tmpdir
        try:
            ps.metric("document_ingested")
        except Exception as e:  # pragma: no cover
            self.fail(f"metric() raised: {e}")

    def test_bogus_argument_types_are_swallowed(self):
        try:
            ps.metric("x", duration_ms=object())
        except Exception as e:  # pragma: no cover
            self.fail(f"metric() raised: {e}")


class TestCostTable(unittest.TestCase):
    def test_known_model_rates(self):
        # Haiku 4.5: $1/M in, $5/M out. Sonnet 4.6: $3/M in, $15/M out.
        self.assertAlmostEqual(
            ps._claude_cost("claude-haiku-4-5-20251001", 1_000_000, 1_000_000), 6.00)
        self.assertAlmostEqual(
            ps._claude_cost("claude-sonnet-4-6", 1_000_000, 1_000_000), 18.00)

    def test_unknown_model_returns_none(self):
        self.assertIsNone(ps._claude_cost("some-future-model", 100, 100))

    def test_missing_usage_returns_none(self):
        self.assertIsNone(ps._claude_cost("claude-sonnet-4-6", None, None))


class TestSchemaStaysSanitized(MetricsBase):
    """The schema itself is the privacy contract: no column may be a home for
    document content. If someone adds one, this test forces them to look here
    and read why that's not allowed."""

    ALLOWED = {"id", "ts", "event", "batch_id", "doc_class", "source", "field",
               "status", "model", "duration_ms", "tokens_in", "tokens_out",
               "cost_usd", "detail"}

    def test_no_unexpected_columns(self):
        ps.metric("probe")
        con = sqlite3.connect(ps.METRICS_DB_FILE)
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(events)")}
        finally:
            con.close()
        self.assertEqual(cols, self.ALLOWED)


if __name__ == "__main__":
    unittest.main()
