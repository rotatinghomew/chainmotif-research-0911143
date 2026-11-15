"""不完整冷链事件中的时序组合发现。

口径要点（详见 docs/patterns.md）：

- 序列按发生时刻匹配，但只有在决策时点前发生且已接收的事件才参与，
  退货之后才补传的事件不能成为事前线索。
- 模式步是环节名的集合：单名表示一个环节，两名表示同一发生时刻的并行扫描；
  相邻步之间要求发生时刻严格更晚且不超过间隔上限。
- 同一环节重复出现按不同事件匹配，不按环节名去重；同一发生时刻的同名
  多扫描视为同一时间锚点的一次出现，避免双扫描器重复计数。
- 出现次数（时间锚点组合数）与涉及货批数（货批去重）分开报告。
- 观察窗覆盖不了完整前置区间的货批：匹配到计为“出现”，未匹配不能视为
  “未出现”，因此不进入对照表，只单独描述。
- 暴露调整比较以可观察前置小时为分母，历程长、事件多的货批不会仅因
  机会更多而被判高风险。
- 关联筛查不等于致损原因。
"""

from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from math import exp, log, sqrt
from statistics import median

from scipy.stats import beta, binomtest, fisher_exact, norm

DEFAULT_PARAMETERS = {
    "min_support": 8,
    "max_length": 3,
    "max_gap_hours": 24.0,
    "discovery_fraction": 0.5,
    "alpha": 0.05,
    "correction": "bh",
}

CORRECTIONS = ("bh", "bonferroni", "none")

METHOD_NOTES = [
    "发现阶段只使用较早货批的事件流，不使用退货结果；与退货的关联只在较晚货批上评估。",
    "出现次数按不同时间锚点组合计数，涉及货批数按货批去重，两者分列，不可互相替代。",
    "只有在决策时点前发生且已接收的事件参与匹配；决策后补传或补做的事件不计入事前线索。",
    "观察窗无法覆盖完整前置区间的货批：匹配到模式计为“出现”，但未匹配不能视为“未出现”，故不进入对照表，仅单独描述。",
    "暴露调整比较以可观察前置小时为分母，避免历程长、事件多的货批仅因观察机会多而被判高风险。",
    "跨采集缺口的匹配会单独标记，缺口内可能有未采集的中间事件，核对流程时应注意。",
    "多重比较按所选方法校正，检验数以进入验证阶段的模式数为准并随参数变化。",
    "本报告为关联筛查：匹配序列与退货的关联不等于致损原因，需结合流程核对与后续验证。",
]


def _ts(value):
    return datetime.fromisoformat(value)


def _hours(delta):
    return delta.total_seconds() / 3600.0


def resolve_parameters(overrides=None):
    """合并默认参数并校验，未知字段或越界取值直接拒绝。"""
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise ValueError("参数应为对象")
    unknown = set(overrides) - set(DEFAULT_PARAMETERS)
    if unknown:
        raise ValueError("未知参数：" + ",".join(sorted(unknown)))
    parameters = dict(DEFAULT_PARAMETERS)
    parameters.update(overrides)
    try:
        parameters["min_support"] = int(parameters["min_support"])
        parameters["max_length"] = int(parameters["max_length"])
        parameters["max_gap_hours"] = float(parameters["max_gap_hours"])
        parameters["discovery_fraction"] = float(parameters["discovery_fraction"])
        parameters["alpha"] = float(parameters["alpha"])
    except (TypeError, ValueError) as error:
        raise ValueError("参数类型无效") from error
    if not 1 <= parameters["min_support"] <= 1000:
        raise ValueError("min_support 需在 1 至 1000 之间")
    if not 1 <= parameters["max_length"] <= 4:
        raise ValueError("max_length 需在 1 至 4 之间")
    if not 0 < parameters["max_gap_hours"] <= 720:
        raise ValueError("max_gap_hours 需在 (0, 720] 之间")
    if not 0.1 <= parameters["discovery_fraction"] <= 0.9:
        raise ValueError("discovery_fraction 需在 [0.1, 0.9] 之间")
    if not 0 < parameters["alpha"] < 0.5:
        raise ValueError("alpha 需在 (0, 0.5) 之间")
    if parameters["correction"] not in CORRECTIONS:
        raise ValueError("correction 仅支持：" + ",".join(CORRECTIONS))
    return parameters


class ShipmentContext:
    """单个货批的前置观察口径：资格事件、排除原因与完整性角色。"""

    def __init__(self, shipment, outcome, window, events):
        self.id = shipment["id"]
        self.route = shipment["route"]
        self.start_at = _ts(shipment["start_at"])
        self.end_at = _ts(shipment["end_at"])
        self.outcome = outcome
        self.decision_at = _ts(outcome["decision_at"]) if outcome else self.end_at
        self.returned = outcome["returned"] if outcome else None
        self.need_from = self.start_at
        self.need_to = min(self.end_at, self.decision_at)
        self.window = window
        self.window_from = _ts(window["from_at"]) if window else None
        self.window_to = _ts(window["to_at"]) if window else None
        self.gap = (_ts(window["gap_from"]), _ts(window["gap_to"])) if window and window["gap_from"] else None
        self.reasons = self._completeness_reasons()
        self.complete = not self.reasons
        self.eligible = []
        self.late_received = 0
        self.outside_window = 0
        for record in events:
            occurred = _ts(record["occurred_at"])
            if not (self.need_from <= occurred < self.need_to):
                self.outside_window += 1
            elif _ts(record["received_at"]) > self.decision_at:
                self.late_received += 1
            else:
                self.eligible.append(record)
        self.observed_hours = self._observed_hours()

    def _completeness_reasons(self):
        reasons = []
        if self.need_to <= self.need_from:
            reasons.append("决策时点不晚于历程起点")
            return reasons
        if self.window is None:
            reasons.append("无观察窗记录")
            return reasons
        if self.window_from > self.need_from:
            reasons.append("观察窗起点晚于历程起点")
        if self.window_to < self.need_to:
            reasons.append("观察窗终点早于前置区间终点")
        if self.gap and self.gap[0] < self.need_to and self.gap[1] > self.need_from:
            reasons.append("采集缺口与前置区间相交")
        return reasons

    def _observed_hours(self):
        if self.window is None or self.need_to <= self.need_from:
            return 0.0
        low = max(self.window_from, self.need_from)
        high = min(self.window_to, self.need_to)
        hours = max(0.0, _hours(high - low))
        if self.gap:
            gap_low = max(low, self.gap[0])
            gap_high = min(high, self.gap[1])
            hours -= max(0.0, _hours(gap_high - gap_low))
        return max(0.0, hours)

    def timeline(self):
        """资格事件按发生时刻分组：[(时刻, {环节名: [事件 ID]})]，时刻升序。"""
        by_moment = defaultdict(lambda: defaultdict(list))
        for record in self.eligible:
            by_moment[_ts(record["occurred_at"])][record["event"]].append(record["id"])
        return [(moment, {name: sorted(ids) for name, ids in sorted(names.items())})
                for moment, names in sorted(by_moment.items())]


def build_contexts(catalog):
    shipments = {record["id"]: record for record in catalog.rows("shipments")}
    outcomes = {record["shipment_id"]: record for record in catalog.rows("outcomes")}
    windows = {record["shipment_id"]: record for record in catalog.rows("observation_windows")}
    events = defaultdict(list)
    for record in catalog.rows("events"):
        events[record["shipment_id"]].append(record)
    contexts = []
    for identifier in sorted(shipments):
        contexts.append(ShipmentContext(shipments[identifier], outcomes.get(identifier),
                                        windows.get(identifier), events.get(identifier, [])))
    return contexts


def find_matches(timeline, steps, max_gap_hours, limit=None):
    """枚举模式在一条时间线上的全部出现。

    步为 frozenset：单元素为普通环节，两元素为同一发生时刻的并行环节。
    相邻步要求发生时刻严格更晚且间隔不超过 max_gap_hours。
    一次出现由各步时间锚点唯一确定；同一时刻的同名多扫描不重复计数。
    """
    matches = []

    def visit(index, previous, chosen):
        if limit is not None and len(matches) >= limit:
            return
        if index == len(steps):
            matches.append(tuple(chosen))
            return
        wanted = steps[index]
        for moment, names in timeline:
            if previous is not None:
                gap = _hours(moment - previous)
                if gap <= 0:
                    continue
                if gap > max_gap_hours:
                    break
            if all(name in names for name in wanted):
                ids = []
                for name in sorted(wanted):
                    ids.extend(names[name])
                visit(index + 1, moment, chosen + [(moment, tuple(ids))])

    visit(0, None, [])
    return matches


def _display(steps):
    parts = []
    for step in steps:
        names = sorted(step)
        parts.append(names[0] if len(names) == 1 else "（" + "∥".join(names) + "）")
    return " → ".join(parts)


def _candidates(timelines, min_support, max_length):
    """Apriori 候选：字母表为达到最小支持的单环节与并行环节对。"""
    singleton_support = defaultdict(set)
    pair_support = defaultdict(set)
    for identifier, timeline in timelines.items():
        names_here = set()
        pairs_here = set()
        for _, names in timeline:
            distinct = sorted(names)
            names_here.update(distinct)
            for pair in combinations(distinct, 2):
                pairs_here.add(pair)
        for name in names_here:
            singleton_support[name].add(identifier)
        for pair in pairs_here:
            pair_support[pair].add(identifier)
    alphabet = [frozenset([name]) for name in sorted(singleton_support)
                if len(singleton_support[name]) >= min_support]
    alphabet += [frozenset(pair) for pair in sorted(pair_support)
                 if len(pair_support[pair]) >= min_support]
    # 字母表中的步在发现集均已达到最小支持；长度一的模式里只有并行对是组合。
    frequent = {(step,) for step in alphabet}
    candidates = [(step,) for step in alphabet if len(step) == 2]
    level = [(step,) for step in alphabet]
    length = 1
    while length < max_length:
        extended = set()
        for pattern in level:
            for step in alphabet:
                candidate = pattern + (step,)
                parts = [candidate[:i] + candidate[i + 1:] for i in range(len(candidate))]
                if all(part in frequent for part in parts):
                    extended.add(candidate)
        level = sorted(extended)
        frequent.update(level)
        candidates.extend(level)
        length += 1
    return candidates


def _risk_ratio_ci(a, b, c, d, alpha):
    """对数法风险比置信区间；零格子或空行返回 None。"""
    if a + b == 0 or c + d == 0 or a == 0 or c == 0:
        return None, None
    ratio = (a / (a + b)) / (c / (c + d))
    se = sqrt(1 / a - 1 / (a + b) + 1 / c - 1 / (c + d))
    z = norm.ppf(1 - alpha / 2)
    return ratio, [exp(log(ratio) - z * se), exp(log(ratio) + z * se)]


def _rate_ratio_ci(k1, t1, k0, t0, alpha):
    """条件二项法（精确泊松）率比：以暴露小时为分母。"""
    total = k1 + k0
    if total == 0 or t1 <= 0 or t0 <= 0:
        return None, None, None
    expected = t1 / (t1 + t0)
    p_value = binomtest(k1, total, expected).pvalue
    low = 0.0 if k1 == 0 else beta.ppf(alpha / 2, k1, total - k1 + 1)
    high = 1.0 if k0 == 0 else beta.ppf(1 - alpha / 2, k1 + 1, total - k1)

    def to_ratio(p):
        if p >= 1.0:
            return None
        if p <= 0.0:
            return 0.0
        return (p / (1 - p)) * (t0 / t1)

    ratio = to_ratio(k1 / total)
    return ratio, [to_ratio(low), to_ratio(high)], p_value


def _adjust(p_values, method):
    m = len(p_values)
    if m == 0:
        return []
    if method == "none":
        return list(p_values)
    if method == "bonferroni":
        return [min(1.0, p * m) for p in p_values]
    order = sorted(range(m), key=lambda i: (p_values[i], i))
    adjusted = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        running = min(running, p_values[order[rank - 1]] * m / rank)
        adjusted[order[rank - 1]] = min(1.0, running)
    return adjusted


def _match_record(context, match):
    started, ended = match[0][0], match[-1][0]
    crosses = bool(context.gap and context.gap[0] < ended and context.gap[1] > started)
    return {
        "shipment": context.id,
        "role": "完整" if context.complete else "部分",
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "span_hours": _hours(ended - started),
        "crosses_gap": crosses,
        "steps": [{"at": moment.isoformat(), "events": list(ids)} for moment, ids in match],
    }


def _cohort_summary(contexts):
    return {
        "shipments": len(contexts),
        "complete": sum(1 for c in contexts if c.complete),
        "partial": sum(1 for c in contexts if not c.complete),
        "returned": sum(1 for c in contexts if c.returned is True),
        "no_outcome": sum(1 for c in contexts if c.returned is None),
        "visible_events": sum(len(c.eligible) for c in contexts),
        "late_received_events": sum(c.late_received for c in contexts),
        "observed_hours": round(sum(c.observed_hours for c in contexts), 6),
    }


def build_report(catalog, overrides=None):
    parameters = resolve_parameters(overrides)
    contexts = build_contexts(catalog)
    ordered = sorted(contexts, key=lambda c: (c.start_at, c.id))
    cut = max(1, min(len(ordered) - 1, round(len(ordered) * parameters["discovery_fraction"])))
    discovery = ordered[:cut]
    validation = ordered[cut:]
    cohort_of = {c.id: ("discovery" if c in discovery else "validation") for c in ordered}
    timelines = {c.id: c.timeline() for c in ordered}
    by_id = {c.id: c for c in ordered}
    max_gap = parameters["max_gap_hours"]
    min_support = parameters["min_support"]

    discovery_timelines = {c.id: timelines[c.id] for c in discovery}
    candidates = _candidates(discovery_timelines, min_support, parameters["max_length"])

    kept = []
    rejected = 0
    for steps in candidates:
        support = 0
        for identifier, timeline in discovery_timelines.items():
            if find_matches(timeline, steps, max_gap, limit=1):
                support += 1
        if support >= min_support:
            kept.append(steps)
        else:
            rejected += 1

    patterns = []
    p_values = []
    for steps in kept:
        entry = {
            "display": _display(steps),
            "steps": [sorted(step) for step in steps],
            "step_count": len(steps),
            "event_count": sum(len(step) for step in steps),
            "max_gap_hours": max_gap,
        }
        per_cohort = {"discovery": [], "validation": []}
        for cohort_name, cohort_contexts in (("discovery", discovery), ("validation", validation)):
            for context in cohort_contexts:
                for match in find_matches(timelines[context.id], steps, max_gap):
                    per_cohort[cohort_name].append(_match_record(context, match))
        for cohort_name in ("discovery", "validation"):
            records = per_cohort[cohort_name]
            involved = {record["shipment"] for record in records}
            entry[cohort_name] = {
                "occurrences": len(records),
                "shipments": len(involved),
                "complete_shipments": sum(1 for s in involved if by_id[s].complete),
                "partial_shipments": sum(1 for s in involved if not by_id[s].complete),
            }
        entry["matches"] = per_cohort

        # 关联评估只用观察完整的验证货批；部分货批单独描述，不进入对照。
        complete_validation = [c for c in validation if c.complete and c.returned is not None]
        matched_ids = {record["shipment"] for record in per_cohort["validation"]}
        a = sum(1 for c in complete_validation if c.id in matched_ids and c.returned)
        b = sum(1 for c in complete_validation if c.id in matched_ids and not c.returned)
        c_ = sum(1 for c in complete_validation if c.id not in matched_ids and c.returned)
        d = sum(1 for c in complete_validation if c.id not in matched_ids and not c.returned)
        ratio, ratio_ci = _risk_ratio_ci(a, b, c_, d, parameters["alpha"])
        table = [[a, b], [c_, d]]
        p_value = fisher_exact(table)[1] if all(sum(row) > 0 for row in table) else 1.0
        p_values.append(p_value)
        entry["association"] = {
            "matched": a + b,
            "unmatched": c_ + d,
            "returned_matched": a,
            "returned_unmatched": c_,
            "risk_matched": a / (a + b) if a + b else None,
            "risk_unmatched": c_ / (c_ + d) if c_ + d else None,
            "risk_ratio": ratio,
            "risk_ratio_ci": ratio_ci,
            "fisher_p": p_value,
        }

        returned_group = [c for c in complete_validation if c.returned]
        other_group = [c for c in complete_validation if not c.returned]
        returned_ids = {c.id for c in returned_group}
        other_ids = {c.id for c in other_group}
        k1 = sum(1 for r in per_cohort["validation"] if r["shipment"] in returned_ids)
        k0 = sum(1 for r in per_cohort["validation"] if r["shipment"] in other_ids)
        t1 = sum(c.observed_hours for c in returned_group)
        t0 = sum(c.observed_hours for c in other_group)
        rate_ratio, rate_ci, poisson_p = _rate_ratio_ci(k1, t1, k0, t0, parameters["alpha"])
        entry["exposure"] = {
            "returned": {"occurrences": k1, "hours": round(t1, 6),
                         "per_100h": (k1 / t1 * 100) if t1 > 0 else None},
            "not_returned": {"occurrences": k0, "hours": round(t0, 6),
                             "per_100h": (k0 / t0 * 100) if t0 > 0 else None},
            "rate_ratio": rate_ratio,
            "rate_ratio_ci": rate_ci,
            "poisson_p": poisson_p,
            "median_hours_returned": median([c.observed_hours for c in returned_group]) if returned_group else None,
            "median_hours_not_returned": median([c.observed_hours for c in other_group]) if other_group else None,
        }

        partial = [c for c in validation if not c.complete]
        partial_matched = [c for c in partial if c.id in matched_ids]
        entry["partial_role"] = {
            "shipments": len(partial),
            "matched": len(partial_matched),
            "returned_matched": sum(1 for c in partial_matched if c.returned is True),
            "note": "部分货批未匹配不能视为未出现，不进入对照表，仅在此描述。",
        }
        patterns.append(entry)

    adjusted = _adjust(p_values, parameters["correction"])
    adjusted_key = {"bh": "q_value", "bonferroni": "adjusted_p", "none": "p_value"}[parameters["correction"]]
    order = sorted(range(len(patterns)),
                   key=lambda i: (patterns[i]["step_count"], patterns[i]["display"]))
    patterns = [patterns[i] for i in order]
    adjusted = [adjusted[i] for i in order]
    for index, entry in enumerate(patterns):
        entry["id"] = f"P{index + 1:03d}"
        entry["association"][adjusted_key] = adjusted[index]

    split_after = discovery[-1].start_at.isoformat() if discovery else None
    return {
        "kind": "时序组合发现报告",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_fingerprint": catalog.fingerprint,
        "parameters": parameters,
        "cohorts": {
            "split_rule": "按 start_at 升序取较早份额为发现集，其余为验证集；发现阶段不使用退货结果。",
            "split_after": split_after,
            "discovery": _cohort_summary(discovery),
            "validation": _cohort_summary(validation),
        },
        "shipment_roles": [
            {
                "id": c.id,
                "cohort": cohort_of[c.id],
                "role": "完整" if c.complete else "部分",
                "reasons": c.reasons,
                "visible_events": len(c.eligible),
                "late_received": c.late_received,
                "outside_window": c.outside_window,
                "observed_hours": round(c.observed_hours, 6),
                "returned": c.returned,
            }
            for c in ordered
        ],
        "patterns": patterns,
        "rejected_by_support": rejected,
        "multiple_testing": {
            "method": parameters["correction"],
            "tests": len(patterns),
            "alpha": parameters["alpha"],
            "adjusted_field": adjusted_key,
        },
        "method_notes": METHOD_NOTES,
    }
