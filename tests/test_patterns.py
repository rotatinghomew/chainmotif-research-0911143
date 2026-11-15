import json
import tempfile
import unittest
from datetime import datetime

from chainmotif.patterns import (ShipmentContext, _adjust, _rate_ratio_ci, build_report,
                                 find_matches, resolve_parameters)
from chainmotif.service import Application


def moment(text):
    return datetime.fromisoformat(text)


def timeline(*entries):
    return [(moment(at), {name: list(ids) for name, ids in names.items()}) for at, names in entries]


class MatcherTests(unittest.TestCase):
    def test_repeat_stage_uses_distinct_events(self):
        line = timeline(("2026-05-01T00:00:00+08:00", {"中转": ["E1"]}),
                        ("2026-05-02T00:00:00+08:00", {"中转": ["E2"]}))
        matches = find_matches(line, (frozenset(["中转"]), frozenset(["中转"])), 36.0)
        self.assertEqual(len(matches), 1)
        self.assertEqual([ids for _, ids in matches[0]], [("E1",), ("E2",)])
        self.assertEqual(find_matches(line[:1], (frozenset(["中转"]), frozenset(["中转"])), 36.0), [])

    def test_parallel_step_shares_moment(self):
        line = timeline(("2026-05-01T00:00:00+08:00", {"换箱": ["E1"], "装车": ["E2"]}),
                        ("2026-05-01T06:00:00+08:00", {"换箱": ["E3"]}))
        pair = (frozenset(["换箱", "装车"]),)
        matches = find_matches(line, pair, 24.0)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][0][1], ("E1", "E2"))
        sequential = (frozenset(["换箱"]), frozenset(["装车"]))
        self.assertEqual(find_matches(line, sequential, 24.0), [])

    def test_steps_require_strictly_later_moments(self):
        line = timeline(("2026-05-01T00:00:00+08:00", {"中转": ["E1"], "等待": ["E2"]}))
        self.assertEqual(find_matches(line, (frozenset(["中转"]), frozenset(["等待"])), 24.0), [])

    def test_max_gap_enforced(self):
        line = timeline(("2026-05-01T00:00:00+08:00", {"中转": ["E1"]}),
                        ("2026-05-02T06:00:00+08:00", {"换箱": ["E2"]}))
        steps = (frozenset(["中转"]), frozenset(["换箱"]))
        # 间隔恰为三十小时：上限为闭区间。
        self.assertEqual(find_matches(line, steps, 29.0), [])
        self.assertEqual(len(find_matches(line, steps, 30.0)), 1)

    def test_same_moment_same_name_counts_once(self):
        line = timeline(("2026-05-01T00:00:00+08:00", {"中转": ["E1", "E2"]}),
                        ("2026-05-01T06:00:00+08:00", {"等待": ["E3"]}))
        matches = find_matches(line, (frozenset(["中转"]), frozenset(["等待"])), 24.0)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][0][1], ("E1", "E2"))

    def test_limit_stops_enumeration(self):
        line = timeline(("2026-05-01T00:00:00+08:00", {"中转": ["E1"]}),
                        ("2026-05-01T06:00:00+08:00", {"中转": ["E2"]}),
                        ("2026-05-01T12:00:00+08:00", {"中转": ["E3"]}))
        steps = (frozenset(["中转"]), frozenset(["中转"]))
        self.assertEqual(len(find_matches(line, steps, 24.0)), 3)
        self.assertEqual(len(find_matches(line, steps, 24.0, limit=1)), 1)


def make_shipment(identifier, day):
    return {"id": identifier, "start_at": f"2026-05-{day:02d}T00:00:00+08:00",
            "end_at": f"2026-05-{day + 2:02d}T00:00:00+08:00", "route": "R", "weight_kg": 100}


def make_outcome(identifier, day, returned):
    return {"id": "O" + identifier, "shipment_id": identifier, "returned": returned,
            "known_at": f"2026-05-{day + 3:02d}T00:00:00+08:00",
            "decision_at": f"2026-05-{day + 2:02d}T00:00:00+08:00"}


def make_window(identifier, day, from_offset=0, gap=None):
    return {"id": "W" + identifier, "shipment_id": identifier,
            "from_at": f"2026-05-{day:02d}T{from_offset:02d}:00:00+08:00",
            "to_at": f"2026-05-{day + 2:02d}T00:00:00+08:00",
            "gap_from": gap[0] if gap else None, "gap_to": gap[1] if gap else None}


def make_event(identifier, shipment, name, occurred, received):
    return {"id": identifier, "shipment_id": shipment, "event": name,
            "occurred_at": occurred, "received_at": received, "scanner": "SC0"}


class ContextTests(unittest.TestCase):
    def test_late_received_is_not_prior_clue(self):
        shipment = make_shipment("S", 1)
        outcome = make_outcome("S", 1, False)
        window = make_window("S", 1)
        events = [make_event("E1", "S", "中转", "2026-05-01T06:00:00+08:00", "2026-05-01T07:00:00+08:00"),
                  make_event("E2", "S", "换箱", "2026-05-01T12:00:00+08:00", "2026-05-04T00:00:00+08:00")]
        context = ShipmentContext(shipment, outcome, window, events)
        self.assertEqual([r["id"] for r in context.eligible], ["E1"])
        self.assertEqual(context.late_received, 1)

    def test_outside_window_excluded(self):
        shipment = make_shipment("S", 1)
        outcome = make_outcome("S", 1, False)
        events = [make_event("E1", "S", "中转", "2026-05-04T06:00:00+08:00", "2026-05-04T07:00:00+08:00")]
        context = ShipmentContext(shipment, outcome, make_window("S", 1), events)
        self.assertEqual(context.eligible, [])
        self.assertEqual(context.outside_window, 1)

    def test_completeness_roles(self):
        shipment = make_shipment("S", 1)
        outcome = make_outcome("S", 1, False)
        self.assertTrue(ShipmentContext(shipment, outcome, make_window("S", 1), []).complete)
        short = ShipmentContext(shipment, outcome, make_window("S", 1, from_offset=6), [])
        self.assertFalse(short.complete)
        self.assertIn("观察窗起点晚于历程起点", short.reasons)
        gapped = ShipmentContext(shipment, outcome,
                                 make_window("S", 1, gap=("2026-05-01T12:00:00+08:00", "2026-05-01T18:00:00+08:00")), [])
        self.assertFalse(gapped.complete)
        self.assertIn("采集缺口与前置区间相交", gapped.reasons)
        self.assertLess(gapped.observed_hours, 48.0)


class StatisticTests(unittest.TestCase):
    def test_rate_ratio_normalizes_exposure(self):
        ratio, _, _ = _rate_ratio_ci(10, 100.0, 10, 100.0, 0.05)
        self.assertAlmostEqual(ratio, 1.0)
        ratio, _, _ = _rate_ratio_ci(20, 100.0, 10, 100.0, 0.05)
        self.assertAlmostEqual(ratio, 2.0)
        # 事件更多但暴露成比例更大时，率比仍为一：长记录不会仅因事件多被判高风险。
        ratio, _, p_value = _rate_ratio_ci(20, 200.0, 10, 100.0, 0.05)
        self.assertAlmostEqual(ratio, 1.0)
        self.assertGreater(p_value, 0.05)

    def test_rate_ratio_undefined_without_occurrences(self):
        ratio, ci, p_value = _rate_ratio_ci(0, 100.0, 0, 100.0, 0.05)
        self.assertIsNone(ratio)
        self.assertIsNone(p_value)

    def test_benjamini_hochberg(self):
        adjusted = _adjust([0.01, 0.04, 0.05], "bh")
        self.assertAlmostEqual(adjusted[0], 0.03)
        self.assertAlmostEqual(adjusted[1], 0.05)
        self.assertAlmostEqual(adjusted[2], 0.05)
        self.assertEqual(_adjust([0.01, 0.04], "bonferroni"), [0.02, 0.08])
        self.assertEqual(_adjust([0.01], "none"), [0.01])
        self.assertEqual(_adjust([], "bh"), [])


class FakeCatalog:
    fingerprint = "fake"

    def __init__(self, tables):
        self.tables = tables

    def rows(self, name):
        return json.loads(json.dumps(self.tables[name]))


def synthetic_catalog():
    tables = {"shipments": [], "events": [], "outcomes": [], "observation_windows": []}
    for index, day in enumerate((1, 2, 3, 4)):
        sid = f"S{index}"
        tables["shipments"].append(make_shipment(sid, day))
        tables["outcomes"].append(make_outcome(sid, day, sid == "S3"))
        tables["observation_windows"].append(
            make_window(sid, day, from_offset=6 if sid == "S3" else 0))
        cycles = 2 if sid == "S2" else 1
        for cycle in range(cycles):
            base = day * 24 + cycle * 24
            for offset, name in ((0, "中转"), (6, "等待"), (12, "换箱")):
                hour = base + offset
                occurred = f"2026-05-{hour // 24:02d}T{hour % 24:02d}:00:00+08:00"
                received_hour = hour + 1
                received = f"2026-05-{received_hour // 24:02d}T{received_hour % 24:02d}:00:00+08:00"
                tables["events"].append(make_event(f"E{index}-{cycle}-{offset}", sid, name, occurred, received))
    # S0 补传一个决策后才收到的换箱，不应成为事前线索。
    tables["events"].append(make_event("E0-late", "S0", "换箱", "2026-05-01T18:00:00+08:00",
                                       "2026-05-05T00:00:00+08:00"))
    return FakeCatalog(tables)


class ReportTests(unittest.TestCase):
    def test_roles_occurrences_and_split(self):
        report = build_report(synthetic_catalog(), {"min_support": 2, "max_gap_hours": 24.0})
        self.assertEqual(report["cohorts"]["discovery"]["shipments"], 2)
        self.assertEqual(report["cohorts"]["validation"]["shipments"], 2)
        roles = {r["id"]: r for r in report["shipment_roles"]}
        self.assertEqual(roles["S3"]["role"], "部分")
        self.assertEqual(roles["S0"]["role"], "完整")
        self.assertEqual(roles["S0"]["late_received"], 1)
        by_display = {p["display"]: p for p in report["patterns"]}
        target = by_display["中转 → 等待 → 换箱"]
        # 验证集里的 S2 出现两次：出现次数与涉及货批数分开。
        self.assertEqual(target["discovery"]["occurrences"], 2)
        self.assertEqual(target["validation"]["occurrences"], 3)
        self.assertEqual(target["validation"]["shipments"], 2)
        # 验证集里 S2 完整、S3 部分：对照表只含完整货批。
        self.assertEqual(target["association"]["matched"], 1)
        self.assertEqual(target["association"]["unmatched"], 0)
        self.assertEqual(target["partial_role"]["shipments"], 1)
        self.assertEqual(target["partial_role"]["matched"], 1)
        self.assertEqual(target["partial_role"]["returned_matched"], 1)
        # 匹配记录给出真实事件与时间边界。
        match = target["matches"]["discovery"][0]
        self.assertTrue(match["events"] if "events" in match else match["steps"])
        self.assertIn("started_at", match)
        self.assertIn("ended_at", match)

    def test_late_backfill_cannot_create_match(self):
        report = build_report(synthetic_catalog(), {"min_support": 2, "max_gap_hours": 24.0})
        by_display = {p["display"]: p for p in report["patterns"]}
        pair = by_display["等待 → 换箱"]
        # S0 决策后补传的换箱不产生额外出现：每个货批只计一次。
        self.assertEqual(pair["discovery"]["occurrences"], 2)

    def test_discovery_precedes_validation(self):
        report = build_report(synthetic_catalog(), {"min_support": 2})
        starts = {r["id"]: r for r in report["shipment_roles"]}
        self.assertEqual(starts["S0"]["cohort"], "discovery")
        self.assertEqual(starts["S3"]["cohort"], "validation")

    def test_parameter_validation(self):
        with self.assertRaises(ValueError):
            resolve_parameters({"min_support": 0})
        with self.assertRaises(ValueError):
            resolve_parameters({"unknown": 1})
        with self.assertRaises(ValueError):
            resolve_parameters({"max_gap_hours": 0})
        with self.assertRaises(ValueError):
            resolve_parameters({"discovery_fraction": 0.05})
        with self.assertRaises(ValueError):
            resolve_parameters({"correction": "fdr"})
        with self.assertRaises(ValueError):
            resolve_parameters(["min_support"])
        self.assertEqual(resolve_parameters(None)["min_support"], 8)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.application = Application("data", f"{self.workspace.name}/state")

    def test_report_endpoint(self):
        status, report = self.application.dispatch("GET", "/pattern-report?matches=0&min_support=10")
        self.assertEqual(status, 200)
        self.assertEqual(report["parameters"]["min_support"], 10)
        self.assertEqual(report["source_fingerprint"], self.application.catalog.fingerprint)
        cohorts = report["cohorts"]
        total = cohorts["discovery"]["shipments"] + cohorts["validation"]["shipments"]
        self.assertEqual(total, len(self.application.catalog.rows("shipments")))
        self.assertEqual(len(report["shipment_roles"]), total)
        self.assertTrue(report["patterns"])
        for entry in report["patterns"]:
            self.assertEqual(entry["matches"], {"discovery": [], "validation": []})
            self.assertIn("q_value", entry["association"])
            self.assertIn("rate_ratio", entry["exposure"])
        roles = {r["role"] for r in report["shipment_roles"]}
        self.assertEqual(roles, {"完整", "部分"})

    def test_report_endpoint_rejects_bad_params(self):
        self.assertEqual(self.application.dispatch("GET", "/pattern-report?bogus=1")[0], 400)
        self.assertEqual(self.application.dispatch("GET", "/pattern-report?max_gap_hours=-1")[0], 400)
        self.assertEqual(self.application.dispatch("GET", "/pattern-report?correction=fdr")[0], 400)
        self.assertEqual(self.application.dispatch("GET", "/pattern-report?matches=2")[0], 400)

    def test_matches_included_by_default(self):
        status, report = self.application.dispatch("GET", "/pattern-report?min_support=10")
        self.assertEqual(status, 200)
        self.assertTrue(any(entry["matches"]["discovery"] or entry["matches"]["validation"]
                            for entry in report["patterns"]))

    def test_saved_report_round_trip(self):
        status, created = self.application.dispatch(
            "POST", "/pattern-reports",
            {"label": "冷链复盘", "params": {"min_support": 10}, "notes": "调度核对"})
        self.assertEqual(status, 201)
        self.assertEqual(created["options"]["parameters"]["min_support"], 10)
        self.assertIn("report", created)
        status, restored = self.application.dispatch("GET", "/pattern-reports/" + created["id"])
        self.assertEqual(status, 200)
        self.assertEqual(restored, created)
        status, index = self.application.dispatch("GET", "/pattern-reports")
        self.assertEqual([r["id"] for r in index["pattern_reports"]], [created["id"]])

    def test_saved_report_rejects_bad_input(self):
        self.assertEqual(self.application.dispatch("POST", "/pattern-reports", {"label": "x", "extra": 1})[0], 400)
        self.assertEqual(self.application.dispatch("POST", "/pattern-reports", {"params": {}})[0], 400)
        self.assertEqual(self.application.dispatch(
            "POST", "/pattern-reports", {"label": "x", "params": {"min_support": 0}})[0], 400)
        self.assertEqual(self.application.dispatch("GET", "/pattern-reports/" + "0" * 32)[0], 404)

    def test_saved_report_integrity(self):
        _, created = self.application.dispatch("POST", "/pattern-reports", {"label": "复核"})
        target = f"{self.workspace.name}/state/pattern-reports/{created['id']}.json"
        envelope = json.loads(open(target).read())
        envelope["content"]["label"] = "被改动"
        open(target, "w").write(json.dumps(envelope))
        with self.assertRaises(ValueError):
            self.application.pattern_reports.read(created["id"])

    def test_report_is_deterministic(self):
        first = self.application.dispatch("GET", "/pattern-report?matches=0")[1]
        second = self.application.dispatch("GET", "/pattern-report?matches=0")[1]
        first.pop("generated_at")
        second.pop("generated_at")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
