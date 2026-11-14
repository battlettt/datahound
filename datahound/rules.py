"""Built-in quality rules plus custom per-column rules from a JSON config.

Every rule produces a RuleResult with a clear pass/fail, the counts behind it,
the threshold it was judged against, and up to five offending examples.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .loader import DataHoundError
from .profiler import EXAMPLES, MISS, TYPES, normalize_type, parse_date, parser_for
from .util import fmt_num, pct, quote_value, rate

DEFAULT_MAX_NULL_RATE = 0.10
DEFAULT_MAX_EMPTY_RATE = 0.0
DEFAULT_MAX_DUPLICATE_ROW_RATE = 0.0
CODE_MAX_LENGTH = 12
CODE_MAX_DISTINCT = 50

TOP_KEYS = {"defaults", "primary_key", "columns", "drift", "max_duplicate_row_rate"}
DEFAULT_KEYS = {"max_null_rate", "max_empty_rate", "check_casing"}
COLUMN_KEYS = {
    "type",
    "format",
    "required",
    "max_null_rate",
    "max_empty_rate",
    "min",
    "max",
    "allowed",
    "pattern",
    "unique",
    "code",
    "case",
    "severity",
}
SEVERITIES = ("error", "warning")
SEVERITY_RANK = {"error": 0, "warning": 1}


@dataclass
class RuleResult:
    rule: str
    column: str | None
    passed: bool
    severity: str
    message: str
    failing: int = 0
    total: int = 0
    threshold: str = ""
    examples: list[str] = field(default_factory=list)

    @property
    def failing_rate(self) -> float:
        return self.failing / self.total if self.total else 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- config


def _is_rate(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1


def validate_config(config) -> dict:
    """Raise DataHoundError with a precise message for anything malformed."""
    if not isinstance(config, dict):
        raise DataHoundError("rules config must be a JSON object at the top level")
    unknown = set(config) - TOP_KEYS
    if unknown:
        raise DataHoundError(
            f"rules config has unknown key(s): {', '.join(sorted(unknown))} "
            f"(allowed: {', '.join(sorted(TOP_KEYS))})"
        )
    defaults = config.get("defaults", {})
    if not isinstance(defaults, dict):
        raise DataHoundError("rules config: 'defaults' must be an object")
    unknown = set(defaults) - DEFAULT_KEYS
    if unknown:
        raise DataHoundError(f"rules config: unknown key(s) in defaults: {', '.join(sorted(unknown))}")
    for key in ("max_null_rate", "max_empty_rate"):
        if key in defaults and not _is_rate(defaults[key]):
            raise DataHoundError(f"rules config: defaults.{key} must be a number from 0 to 1")
    if "max_duplicate_row_rate" in config and not _is_rate(config["max_duplicate_row_rate"]):
        raise DataHoundError("rules config: max_duplicate_row_rate must be a number from 0 to 1")
    key = config.get("primary_key")
    if key is not None and not (
        isinstance(key, str) or (isinstance(key, list) and key and all(isinstance(k, str) for k in key))
    ):
        raise DataHoundError("rules config: primary_key must be a column name, a list of names, or null")
    columns = config.get("columns", {})
    if not isinstance(columns, dict):
        raise DataHoundError("rules config: 'columns' must be an object keyed by column name")
    for name, cfg in columns.items():
        _validate_column(name, cfg)
    if "drift" in config:
        from .drift import validate_drift_config

        validate_drift_config(config["drift"])
    return config


def _validate_column(name: str, cfg) -> None:
    where = f"rules config: column {name!r}"
    if not isinstance(cfg, dict):
        raise DataHoundError(f"{where} must map to an object")
    unknown = set(cfg) - COLUMN_KEYS
    if unknown:
        raise DataHoundError(
            f"{where} has unknown key(s): {', '.join(sorted(unknown))} "
            f"(allowed: {', '.join(sorted(COLUMN_KEYS))})"
        )
    if "type" in cfg and normalize_type(cfg["type"]) not in TYPES:
        raise DataHoundError(f"{where}: type must be one of {', '.join(TYPES)} (or number)")
    for key in ("max_null_rate", "max_empty_rate"):
        if key in cfg and not _is_rate(cfg[key]):
            raise DataHoundError(f"{where}: {key} must be a number from 0 to 1")
    for key in ("required", "unique", "code"):
        if key in cfg and not isinstance(cfg[key], bool):
            raise DataHoundError(f"{where}: {key} must be true or false")
    if "allowed" in cfg and not (
        isinstance(cfg["allowed"], list) and all(isinstance(v, (str, int, float)) for v in cfg["allowed"])
    ):
        raise DataHoundError(f"{where}: allowed must be a list of values")
    if "case" in cfg and cfg["case"] not in ("upper", "lower"):
        raise DataHoundError(f"{where}: case must be 'upper' or 'lower'")
    if "severity" in cfg and cfg["severity"] not in SEVERITIES:
        raise DataHoundError(f"{where}: severity must be 'error' or 'warning'")
    if "format" in cfg and not isinstance(cfg["format"], str):
        raise DataHoundError(f"{where}: format must be a strptime string such as '%Y-%m-%d'")
    if "pattern" in cfg:
        try:
            re.compile(cfg["pattern"])
        except (re.error, TypeError) as exc:
            raise DataHoundError(f"{where}: pattern is not a valid regular expression ({exc})") from None
    for key in ("min", "max"):
        if key in cfg and (isinstance(cfg[key], bool) or not isinstance(cfg[key], (int, float, str))):
            raise DataHoundError(f"{where}: {key} must be a number (or a date string for date columns)")


def load_config(path: str | Path | None) -> dict:
    if path is None:
        return {}
    path = Path(path)
    if not path.exists():
        raise DataHoundError(f"rules file not found: {path}")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DataHoundError(
            f"rules file {path} is not valid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}"
        ) from None
    except UnicodeDecodeError:
        raise DataHoundError(f"rules file {path} is not UTF-8 text") from None
    return validate_config(config)


# ---------------------------------------------------------------- helpers


def _sev(cfg: dict, default: str) -> str:
    return cfg.get("severity", default)


def _row_examples(rows: list[int]) -> list[str]:
    return [f"row {r}" for r in rows[:EXAMPLES]]


def _value_counts(counter: Counter) -> list[str]:
    ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return [f"{quote_value(v)} x{n}" for v, n in ranked[:EXAMPLES]]


def resolve_primary_key(columns: list[str], config: dict) -> list[str] | None:
    """Configured key, or the first column when it is named id / *_id. null disables the check."""
    if "primary_key" in config:
        key = config["primary_key"]
        if key is None:
            return None
        return [key] if isinstance(key, str) else list(key)
    first = columns[0] if columns else ""
    low = first.lower()
    if low == "id" or low.endswith("_id") or low.endswith(" id"):
        return [first]
    return None


def looks_like_code(profile: dict, values: list[str]) -> bool:
    """Short, space-free, repeating text values: country codes, status codes, SKUs."""
    if profile["type"] != "string" or not values:
        return False
    # Codes repeat: at most CODE_MAX_DISTINCT distinct values, each used twice on average.
    if profile["unique_count"] > CODE_MAX_DISTINCT or profile["unique_count"] * 2 > len(values):
        return False
    for value in values:
        if len(value) > CODE_MAX_LENGTH or any(ch.isspace() for ch in value):
            return False
    return any(ch.isalpha() for value in values for ch in value)


def _case_style(value: str) -> str | None:
    if not any(ch.isalpha() for ch in value):
        return None
    if value == value.upper():
        return "upper"
    if value == value.lower():
        return "lower"
    return "mixed"


# ---------------------------------------------------------------- checks


def check_nulls(prof: dict, cfg: dict, defaults: dict) -> RuleResult:
    fallback = 0.0 if cfg.get("required") else defaults.get("max_null_rate", DEFAULT_MAX_NULL_RATE)
    limit = cfg.get("max_null_rate", fallback)
    passed = prof["null_rate"] <= limit
    detail = rate(prof["null_count"], prof["rows"])
    verdict = "within" if passed else "over"
    return RuleResult(
        rule="null_rate",
        column=prof["name"],
        passed=passed,
        severity=_sev(cfg, "error"),
        message=f"{detail} null, {verdict} the {pct(limit)} limit",
        failing=prof["null_count"],
        total=prof["rows"],
        threshold=pct(limit),
        examples=[] if passed else _row_examples(prof["null_rows"]),
    )


def check_empty(prof: dict, cfg: dict, defaults: dict) -> RuleResult:
    fallback = 0.0 if cfg.get("required") else defaults.get("max_empty_rate", DEFAULT_MAX_EMPTY_RATE)
    limit = cfg.get("max_empty_rate", fallback)
    passed = prof["empty_rate"] <= limit
    detail = rate(prof["empty_count"], prof["rows"])
    return RuleResult(
        rule="empty_strings",
        column=prof["name"],
        passed=passed,
        severity=_sev(cfg, "warning"),
        message=f"{detail} are blank text where a value is expected (limit {pct(limit)})"
        if not passed
        else f"{detail} blank text, within the {pct(limit)} limit",
        failing=prof["empty_count"],
        total=prof["rows"],
        threshold=pct(limit),
        examples=[] if passed else _row_examples(prof["empty_rows"]),
    )


TYPE_RULES = {
    "date": ("unparseable_dates", "do not parse as dates"),
    "integer": ("non_numeric_values", "are not whole numbers"),
    "float": ("non_numeric_values", "are not numbers"),
    "boolean": ("invalid_booleans", "are not true/false values"),
}


def check_type(prof: dict, cfg: dict) -> RuleResult | None:
    if prof["type"] not in TYPE_RULES:
        return None
    rule, phrase = TYPE_RULES[prof["type"]]
    bad = prof["invalid_count"]
    total = prof["present_count"]
    passed = bad == 0
    detail = rate(bad, total, "values")
    fmt = f" (format {cfg['format']})" if cfg.get("format") else ""
    return RuleResult(
        rule=rule,
        column=prof["name"],
        passed=passed,
        severity=_sev(cfg, "error"),
        message=f"{detail} {phrase}{fmt}" if not passed else f"all {total} values parse as {prof['type']}{fmt}",
        failing=bad,
        total=total,
        threshold="0 values",
        examples=[f"row {r}: {quote_value(v)}" for r, v in prof["invalid_examples"]],
    )


def check_range(prof: dict, cfg: dict, values: list[str | None]) -> RuleResult:
    ctype = prof["type"]
    name = prof["name"]
    if ctype in ("integer", "float"):
        parse = parser_for("float")

        def bound(key):
            raw = cfg[key]
            if isinstance(raw, str):
                raw = parse(raw)
                if raw is MISS:
                    raise DataHoundError(f"rules config: column {name!r}: {key} must be a number")
            return float(raw)

        show = fmt_num
    elif ctype == "date":
        fmt = cfg.get("format")
        parse = parser_for("date", fmt)

        def bound(key):
            parsed = parse_date(str(cfg[key]), fmt)
            if parsed is MISS:
                raise DataHoundError(
                    f"rules config: column {name!r}: {key} {cfg[key]!r} is not a date"
                    + (f" in format {fmt}" if fmt else "")
                )
            return parsed

        def show(value):
            return str(value)

    else:
        raise DataHoundError(
            f"rules config: column {name!r} has min/max but is a {ctype} column; "
            f"declare \"type\": \"number\" or \"date\" if it should be compared"
        )
    low = bound("min") if "min" in cfg else None
    high = bound("max") if "max" in cfg else None
    checked = 0
    offenders: list[tuple[int, str]] = []
    for row, value in enumerate(values, start=1):
        if value is None or value.strip() == "":
            continue
        parsed = parse(value)
        if parsed is MISS:
            continue  # reported by the type rule
        checked += 1
        if (low is not None and parsed < low) or (high is not None and parsed > high):
            offenders.append((row, value))
    limits = []
    if "min" in cfg:
        limits.append(f"min {show(cfg['min'])}")
    if "max" in cfg:
        limits.append(f"max {show(cfg['max'])}")
    limit_text = ", ".join(limits)
    passed = not offenders
    detail = rate(len(offenders), checked, "values")
    return RuleResult(
        rule="out_of_range",
        column=name,
        passed=passed,
        severity=_sev(cfg, "error"),
        message=f"{detail} fall outside {limit_text}" if not passed else f"all {checked} values within {limit_text}",
        failing=len(offenders),
        total=checked,
        threshold=limit_text,
        examples=[f"row {r}: {quote_value(v)}" for r, v in offenders[:EXAMPLES]],
    )


def check_allowed(prof: dict, cfg: dict, values: list[str | None]) -> RuleResult:
    allowed = {str(v) for v in cfg["allowed"]}
    bad = Counter(v.strip() for v in values if v is not None and v.strip() != "" and v.strip() not in allowed)
    failing = sum(bad.values())
    total = prof["present_count"]
    passed = failing == 0
    detail = rate(failing, total, "values")
    shown = ", ".join(sorted(allowed)[:8]) + (", ..." if len(allowed) > 8 else "")
    return RuleResult(
        rule="unexpected_values",
        column=prof["name"],
        passed=passed,
        severity=_sev(cfg, "error"),
        message=f"{detail} are not in the allowed set ({shown})"
        if not passed
        else f"all {total} values are in the allowed set",
        failing=failing,
        total=total,
        threshold=f"{len(allowed)} allowed values",
        examples=_value_counts(bad),
    )


def check_pattern(prof: dict, cfg: dict, values: list[str | None]) -> RuleResult:
    regex = re.compile(cfg["pattern"])
    offenders = [
        (row, v) for row, v in enumerate(values, start=1) if v is not None and v.strip() and not regex.fullmatch(v.strip())
    ]
    total = prof["present_count"]
    passed = not offenders
    detail = rate(len(offenders), total, "values")
    return RuleResult(
        rule="pattern_mismatch",
        column=prof["name"],
        passed=passed,
        severity=_sev(cfg, "error"),
        message=f"{detail} do not match /{cfg['pattern']}/" if not passed else f"all {total} values match /{cfg['pattern']}/",
        failing=len(offenders),
        total=total,
        threshold=f"/{cfg['pattern']}/",
        examples=[f"row {r}: {quote_value(v)}" for r, v in offenders[:EXAMPLES]],
    )


def check_casing(prof: dict, cfg: dict, values: list[str | None]) -> RuleResult:
    present = [v.strip() for v in values if v is not None and v.strip()]
    styles = Counter(s for s in (_case_style(v) for v in present) if s)
    expected = cfg.get("case") or (styles.most_common(1)[0][0] if styles else "upper")
    bad = Counter(v for v in present if _case_style(v) not in (None, expected))
    # Also catch the same code spelled several ways, e.g. CA / ca / Ca.
    spellings: dict[str, set[str]] = defaultdict(set)
    for v in present:
        spellings[v.lower()].add(v)
    variants = sorted(sorted(group) for group in spellings.values() if len(group) > 1)
    failing = sum(bad.values())
    total = len(present)
    passed = failing == 0
    examples = _value_counts(bad)
    if variants and not passed:
        examples.insert(0, "same code spelled " + " / ".join(variants[0]))
        examples = examples[:EXAMPLES]
    origin = "configured" if cfg.get("case") else "majority"
    detail = rate(failing, total, "values")
    return RuleResult(
        rule="inconsistent_casing",
        column=prof["name"],
        passed=passed,
        severity=_sev(cfg, "warning"),
        message=f"{detail} break the {origin} {expected}-case style of this code column"
        if not passed
        else f"all {total} codes use {expected} case",
        failing=failing,
        total=total,
        threshold=f"{expected} case",
        examples=examples,
    )


def check_key(table, key: list[str], rule: str = "duplicate_primary_key", severity: str = "error") -> RuleResult:
    indexes = [table.columns.index(k) for k in key]
    positions: dict[tuple, list[int]] = defaultdict(list)
    missing: list[int] = []
    for number, row in enumerate(table.rows, start=1):
        values = tuple(row[i] for i in indexes)
        if any(v is None or v.strip() == "" for v in values):
            missing.append(number)
            continue
        positions[tuple(v.strip() for v in values)].append(number)
    dupes = {k: rows for k, rows in positions.items() if len(rows) > 1}
    extra = sum(len(rows) - 1 for rows in dupes.values())
    failing = extra + len(missing)
    label = " + ".join(key)
    examples = []
    for values, rows in sorted(dupes.items(), key=lambda item: item[1][0])[:EXAMPLES]:
        shown = ", ".join(str(r) for r in rows[:4]) + (", ..." if len(rows) > 4 else "")
        examples.append(f"{quote_value(' + '.join(values))} appears {len(rows)} times (rows {shown})")
    if missing and len(examples) < EXAMPLES:
        examples.append("missing key in " + ", ".join(f"row {r}" for r in missing[:3]))
    passed = failing == 0
    parts = []
    if dupes:
        parts.append(f"{len(dupes)} key value(s) repeated, {extra} extra row(s)")
    if missing:
        parts.append(f"{len(missing)} row(s) with no key")
    detail = rate(failing, table.row_count)
    return RuleResult(
        rule=rule,
        column=label,
        passed=passed,
        severity=severity,
        message=f"{detail} break uniqueness: " + "; ".join(parts) if not passed else f"all {table.row_count} keys are unique",
        failing=failing,
        total=table.row_count,
        threshold="0 duplicates",
        examples=examples,
    )


def check_duplicate_rows(profile: dict, config: dict) -> RuleResult:
    limit = config.get("max_duplicate_row_rate", DEFAULT_MAX_DUPLICATE_ROW_RATE)
    passed = profile["duplicate_row_rate"] <= limit
    detail = rate(profile["duplicate_row_count"], profile["rows"])
    return RuleResult(
        rule="duplicate_rows",
        column=None,
        passed=passed,
        severity="warning",
        message=f"{detail} are exact copies of an earlier row (limit {pct(limit)})"
        if not passed
        else f"{detail} exact duplicate rows, within {pct(limit)}",
        failing=profile["duplicate_row_count"],
        total=profile["rows"],
        threshold=pct(limit),
        examples=[f"row {d} repeats row {f}" for d, f in profile["duplicate_examples"]],
    )


# ---------------------------------------------------------------- driver


def run_rules(table, profile: dict, config: dict | None = None) -> list[RuleResult]:
    config = config or {}
    columns_cfg = config.get("columns", {})
    defaults = config.get("defaults", {})
    by_name = {col["name"]: col for col in profile["columns"]}
    index = {name: i for i, name in enumerate(table.columns)}
    results: list[RuleResult] = []

    for name in columns_cfg:
        if name not in index:
            results.append(
                RuleResult(
                    rule="missing_column",
                    column=name,
                    passed=False,
                    severity="error",
                    message=f"column {name!r} is in the rules config but not in the table",
                    failing=1,
                    total=1,
                    threshold="column present",
                    examples=["table columns: " + ", ".join(table.columns[:10])],
                )
            )

    results.append(check_duplicate_rows(profile, config))

    key = resolve_primary_key(table.columns, config)
    if key:
        missing = [k for k in key if k not in index]
        if missing:
            raise DataHoundError(f"primary_key column(s) not in the table: {', '.join(missing)}")
        results.append(check_key(table, key))

    check_casing_default = defaults.get("check_casing", True)
    for name in table.columns:
        prof = by_name[name]
        cfg = columns_cfg.get(name, {})
        values = table.column_values(index[name])
        results.append(check_nulls(prof, cfg, defaults))
        results.append(check_empty(prof, cfg, defaults))
        typed = check_type(prof, cfg)
        if typed:
            results.append(typed)
        if "min" in cfg or "max" in cfg:
            results.append(check_range(prof, cfg, values))
        if "allowed" in cfg:
            results.append(check_allowed(prof, cfg, values))
        if "pattern" in cfg:
            results.append(check_pattern(prof, cfg, values))
        if cfg.get("unique") and key != [name]:
            results.append(check_key(table, [name], rule="duplicate_values", severity=_sev(cfg, "error")))
        present = [v.strip() for v in values if v is not None and v.strip()]
        wants_casing = cfg.get("code")
        if wants_casing is None:
            wants_casing = bool(cfg.get("case")) or (check_casing_default and looks_like_code(prof, present))
        if wants_casing and present:
            results.append(check_casing(prof, cfg, values))
    return results


def worst_first(results: list[RuleResult]) -> list[RuleResult]:
    """Failures only, errors before warnings, then by share of rows affected, then by count."""
    failed = [r for r in results if not r.passed]
    return sorted(
        failed,
        key=lambda r: (SEVERITY_RANK.get(r.severity, 9), -r.failing_rate, -r.failing, r.rule, r.column or ""),
    )
