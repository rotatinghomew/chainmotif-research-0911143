import unittest
from chainmotif.catalog import Catalog
from chainmotif.domain import event_lines


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog("data")

    def test_parallel_events_keep_distinct_ids(self):
        lines = event_lines(self.catalog)
        self.assertEqual(sum(len(records) for records in lines.values()), len(self.catalog.rows("events")))
        first = lines["S0"]
        self.assertGreater(len(first), len({record["occurred_at"] for record in first}))


    def test_occurrence_not_receipt_orders_display(self):
        records = [{"id": "A", "shipment_id": "S", "occurred_at": "2026-09-01T10:00:00+08:00", "received_at": "2026-09-02T00:00:00+08:00"},
                   {"id": "B", "shipment_id": "S", "occurred_at": "2026-09-01T11:00:00+08:00", "received_at": "2026-09-01T12:00:00+08:00"}]
        self.assertEqual([record["id"] for record in event_lines(FakeCatalog({"events": list(reversed(records))}))["S"]], ["A", "B"])

    def test_empty_event_lines(self):
        self.assertEqual(event_lines(FakeCatalog({"events": []})), {})

    def test_repeat_stage_is_not_deduplicated(self):
        records = [{"id": identifier, "shipment_id": "S", "occurred_at": "2026-09-01T10:00:00+08:00", "event": "中转"} for identifier in ("A", "B")]
        self.assertEqual(len(event_lines(FakeCatalog({"events": records}))["S"]), 2)

    def test_observation_gaps_remain_explicit(self):
        self.assertTrue(any(record["gap_from"] is not None for record in self.catalog.rows("observation_windows")))


class FakeCatalog:
    def __init__(self, tables):
        self.tables = tables

    def rows(self, name):
        return self.tables[name]
