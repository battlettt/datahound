"""Per-column and table-level statistics.

Vocabulary used everywhere in DataHound:
  null     the cell has no value (CSV empty field or null token, SQLite NULL)
  empty    the cell holds text that is blank or only whitespace
  present  every other cell; types, uniques and stats are computed over these
  invalid  a present value that does not parse as the column's type

Row numbers are 1-based data rows: row 1 is the first row after a CSV header.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter

INT_RE = re.compile(r"^[+-]?\d+$")
FLOAT_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%m/%d/%Y",
    "%d.%m.%Y",
)
BOOL_TRUE = frozenset({"true", "yes", "y", "t"})
BOOL_FALSE = frozenset({"false", "no", "n", "f"})

TYPES = ("integer", "float", "date", "boolean", "string")
TYPE_ALIASES = {"number": "float", "int": "integer", "bool": "boolean", "text": "string"}
# A column is typed as integer/float/date/boolean when at least this share of its
# present values parse that way. The rest become "invalid" and the rules report them.
TYPE_MAJORITY = 0.8
TOP_N = 5
EXAMPLES = 5

MISS = object()  # parse failure sentinel; False and 0 are valid parse results


def parse_int(text: str):
    text = text.strip()
    return int(text) if INT_RE.match(text) else MISS


def parse_float(text: str):
    text = text.strip()
    return float(text) if FLOAT_RE.match(text) else MISS


def parse_bool(text: str):
    low = text.strip().lower()
    if low in BOOL_TRUE:
        return True
    if low in BOOL_FALSE:
        return False
    return MISS


def parse_date(text: str, fmt: str | None = None):
    text = text.strip()
    for candidate in (fmt,) if fmt else DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, candidate)
        except ValueError:
            continue
    return MISS


def parser_for(ctype: str, date_format: str | None = None):
    if ctype == "integer":
        return parse_int
    if ctype == "float":
        return parse_float
    if ctype == "boolean":
        return parse_bool
    if ctype == "date":
        return lambda text: parse_date(text, date_format)
    return None


def normalize_type(name: str) -> str:
    return TYPE_ALIASES.get(name, name)


def is_empty(value: str | None) -> bool:
    return value is not None and value.strip() == ""


def infer_type(values: list[str]) -> str:
    """Most specific type that at least TYPE_MAJORITY of the present values parse as."""
    if not values:
        return "unknown"
    total = len(values)
    numeric = [value for value in values if parse_float(value) is not MISS]
    if len(numeric) / total >= TYPE_MAJORITY:
        # Integer only when every numeric value is whole; one 10.5 makes it a float column.
        return "integer" if all(parse_int(value) is not MISS for value in numeric) else "float"
    for ctype in ("date", "boolean"):
        parse = parser_for(ctype)
        ok = sum(1 for value in values if parse(value) is not MISS)
        if ok / total >= TYPE_MAJORITY:
            return ctype
    return "string"


def _date_text(value: dt.datetime) -> str:
    if value.time() == dt.time(0, 0):
        return value.date().isoformat()
    return value.isoformat(sep=" ")


def profile_column(
    name: str,
    values: list[str | None],
    declared_type: str | None = None,
    date_format: str | None = None,
) -> dict:
    rows = len(values)
    null_rows = [i for i, v in enumerate(values, start=1) if v is None]
    empty_rows = [i for i, v in enumerate(values, start=1) if is_empty(v)]
    present = [(i, v) for i, v in enumerate(values, start=1) if v is not None and v.strip() != ""]
    present_values = [v for _, v in present]

    inferred = infer_type(present_values)
    ctype = normalize_type(declared_type) if declared_type else inferred

    parsed: list = []
    invalid: list[tuple[int, str]] = []
    parse = parser_for(ctype, date_format)
    if parse is not None:
        for row, value in present:
            result = parse(value)
            if result is MISS:
                invalid.append((row, value))
            else:
                parsed.append(result)

    counts = Counter(v.strip() for v in present_values)
    profile = {
        "name": name,
        "type": ctype,
        "inferred_type": inferred,
        "declared_type": normalize_type(declared_type) if declared_type else None,
        "rows": rows,
        "null_count": len(null_rows),
        "null_rate": len(null_rows) / rows if rows else 0.0,
        "null_rows": null_rows[:EXAMPLES],
        "empty_count": len(empty_rows),
        "empty_rate": len(empty_rows) / rows if rows else 0.0,
        "empty_rows": empty_rows[:EXAMPLES],
        "present_count": len(present),
        "unique_count": len(counts),
        "invalid_count": len(invalid),
        "invalid_rate": len(invalid) / len(present) if present else 0.0,
        "invalid_examples": invalid[:EXAMPLES],
        "min": None,
        "max": None,
        "mean": None,
        "top_values": [],
    }
    if ctype in ("integer", "float") and parsed:
        profile["min"] = min(parsed)
        profile["max"] = max(parsed)
        profile["mean"] = sum(parsed) / len(parsed)
    elif ctype == "date" and parsed:
        profile["min"] = _date_text(min(parsed))
        profile["max"] = _date_text(max(parsed))
    if ctype in ("string", "boolean") and counts:
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        profile["top_values"] = ranked[:TOP_N]
    return profile


def profile_table(table, config: dict | None = None) -> dict:
    """Profile every column. Types declared in the rules config win over inference."""
    columns_cfg = (config or {}).get("columns", {})
    rows = table.row_count
    seen: dict[tuple, int] = {}
    duplicates: list[tuple[int, int]] = []  # (duplicate row, first row it repeats)
    for number, row in enumerate(table.rows, start=1):
        key = tuple(row)
        if key in seen:
            duplicates.append((number, seen[key]))
        else:
            seen[key] = number
    columns = []
    for index, name in enumerate(table.columns):
        cfg = columns_cfg.get(name, {})
        columns.append(
            profile_column(
                name,
                table.column_values(index),
                declared_type=cfg.get("type"),
                date_format=cfg.get("format"),
            )
        )
    return {
        "name": table.name,
        "source": table.source,
        "rows": rows,
        "column_count": len(table.columns),
        "duplicate_row_count": len(duplicates),
        "duplicate_row_rate": len(duplicates) / rows if rows else 0.0,
        "duplicate_examples": duplicates[:EXAMPLES],
        "columns": columns,
    }
