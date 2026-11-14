"""Compare this run's metrics with the previous run of the same dataset.

A drift flag fires when a metric moves past its threshold. Every flag carries
the old value, the new value, the size of the move and the threshold, so a
reader can judge it without opening the history store.

Threshold modes:
  absolute  rates (0..1). 0.05 means "moved more than 5 percentage points"
  relative  counts and means. 0.20 means "moved more than 20% of the old value"
  increase  counts that should not grow. 0 means "any increase"
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .loader import DataHoundError
from .util import fmt_num, pct

DEFAULT_THRESHOLDS: dict[str, tuple[str, float]] = {
    "row_count": ("relative", 0.20),
    "duplicate_row_rate": ("absolute", 0.02),
    "rules_failed": ("increase", 0),
    "null_rate": ("absolute", 0.05),
    "empty_rate": ("absolute", 0.05),
    "invalid_rate": ("absolute", 0.02),
    "distinct_count": ("relative", 0.50),
    "mean": ("relative", 0.25),
}
TABLE_METRICS = ("row_count", "duplicate_row_rate", "rules_failed")
COLUMN_METRICS = ("null_rate", "empty_rate", "invalid_rate", "distinct_count", "mean")
RATE_METRICS = {"duplicate_row_rate", "null_rate", "empty_rate", "invalid_rate"}
TABLE = ""  # column name used for table-level metrics in the store


@dataclass
class DriftFlag:
    metric: str
    column: str | None
    old: object
    new: object
    change: str
    threshold: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def validate_drift_config(drift) -> None:
    if not isinstance(drift, dict):
        raise DataHoundError("rules config: 'drift' must be an object of metric thresholds")
    unknown = set(drift) - set(DEFAULT_THRESHOLDS)
    if unknown:
        raise DataHoundError(
            f"rules config: unknown drift metric(s): {', '.join(sorted(unknown))} "
            f"(allowed: {', '.join(DEFAULT_THRESHOLDS)})"
        )
    for metric, value in drift.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise DataHoundError(f"rules config: drift.{metric} must be a number of 0 or more")
        if DEFAULT_THRESHOLDS[metric][0] == "absolute" and value > 1:
            raise DataHoundError(f"rules config: drift.{metric} is a rate change and must be from 0 to 1")


def thresholds(config: dict | None) -> dict[str, tuple[str, float]]:
    merged = dict(DEFAULT_THRESHOLDS)
    for metric, value in ((config or {}).get("drift") or {}).items():
        merged[metric] = (merged[metric][0], float(value))
    return merged


def flatten_metrics(profile: dict, results) -> dict[tuple[str, str], object]:
    """Everything drift and trends need, as {(column, metric): value}. Types are text."""
    metrics: dict[tuple[str, str], object] = {
        (TABLE, "row_count"): profile["rows"],
        (TABLE, "column_count"): profile["column_count"],
        (TABLE, "duplicate_row_rate"): profile["duplicate_row_rate"],
        (TABLE, "rules_failed"): sum(1 for r in results if not r.passed),
        (TABLE, "rules_total"): len(results),
    }
    for col in profile["columns"]:
        name = col["name"]
        metrics[(name, "type")] = col["type"]
        metrics[(name, "null_rate")] = col["null_rate"]
        metrics[(name, "empty_rate")] = col["empty_rate"]
        metrics[(name, "invalid_rate")] = col["invalid_rate"]
        metrics[(name, "distinct_count")] = col["unique_count"]
        if col["mean"] is not None:
            metrics[(name, "mean")] = col["mean"]
    return metrics


def _show(metric: str, value) -> str:
    return pct(value) if metric in RATE_METRICS else fmt_num(value)


def _compare(metric: str, column: str | None, old, new, mode: str, limit: float) -> DriftFlag | None:
    old = float(old)
    new = float(new)
    delta = new - old
    if mode == "absolute":
        moved = abs(delta) > limit + 1e-12
        change = f"{delta * 100:+.1f} pts"
        threshold = f"{limit * 100:.1f} pts"
    elif mode == "relative":
        if old == 0:
            moved = new != 0
            change = "from zero"
        else:
            share = delta / abs(old)
            moved = abs(share) > limit + 1e-12
            change = f"{share * 100:+.1f}%"
        threshold = f"{limit * 100:.1f}%"
    else:  # increase
        moved = delta > limit
        change = f"{delta:+g}"
        threshold = f"+{fmt_num(limit)}"
    if not moved:
        return None
    where = f" on {column}" if column else ""
    old_text, new_text = _show(metric, old), _show(metric, new)
    return DriftFlag(
        metric=metric,
        column=column,
        old=old_text,
        new=new_text,
        change=change,
        threshold=threshold,
        message=f"{metric}{where}: {old_text} -> {new_text} ({change}), threshold {threshold}",
    )


def detect_drift(previous: dict | None, current: dict, config: dict | None = None) -> list[DriftFlag]:
    """previous and current are flatten_metrics() dicts. No previous run means no drift."""
    if not previous:
        return []
    limits = thresholds(config)
    flags: list[DriftFlag] = []
    for metric in TABLE_METRICS:
        key = (TABLE, metric)
        if key in previous and key in current:
            mode, limit = limits[metric]
            flag = _compare(metric, None, previous[key], current[key], mode, limit)
            if flag:
                flags.append(flag)

    old_cols = [c for (c, m) in previous if m == "type"]
    new_cols = [c for (c, m) in current if m == "type"]
    for name in new_cols:
        if name not in old_cols:
            flags.append(
                DriftFlag("schema", name, "absent", "present", "column added", "any change",
                          f"column {name} was added since the previous run")
            )
    for name in old_cols:
        if name not in new_cols:
            flags.append(
                DriftFlag("schema", name, "present", "absent", "column removed", "any change",
                          f"column {name} was removed since the previous run")
            )
    for name in new_cols:
        if name not in old_cols:
            continue
        old_type, new_type = previous[(name, "type")], current[(name, "type")]
        if old_type != new_type:
            flags.append(
                DriftFlag("type", name, old_type, new_type, "type changed", "any change",
                          f"type on {name}: {old_type} -> {new_type}, threshold any change")
            )
        for metric in COLUMN_METRICS:
            key = (name, metric)
            if key in previous and key in current:
                mode, limit = limits[metric]
                flag = _compare(metric, name, previous[key], current[key], mode, limit)
                if flag:
                    flags.append(flag)
    return flags
