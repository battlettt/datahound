"""DataHound test suite. Plain asserts, no framework.

    python3 tests/run_tests.py            run everything
    python3 tests/run_tests.py drift      run tests whose name contains "drift"

Fixtures are written to tests/fixtures/ on every run (the folder is gitignored),
so the planted problems below are the single source of truth for what each
fixture contains.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from datahound import demo  # noqa: E402
from datahound.cli import main, run_check  # noqa: E402
from datahound.drift import detect_drift, flatten_metrics  # noqa: E402
from datahound.history import HistoryStore  # noqa: E402
from datahound.loader import DataHoundError, Table, load, load_csv, load_sqlite  # noqa: E402
from datahound.profiler import infer_type, profile_column, profile_table  # noqa: E402
from datahound.report import render_html, sparkline, terminal_summary  # noqa: E402
from datahound.rules import (  # noqa: E402
    load_config,
    resolve_primary_key,
    run_rules,
    validate_config,
    worst_first,
)

FIX = ROOT / "tests" / "fixtures"
TMP = Path(tempfile.mkdtemp(prefix="datahound-tests-"))

# ---------------------------------------------------------------- fixtures

# planted.csv: 20 data rows with known problems
#   id         row 20 repeats id 5 (duplicate primary key)
#   email      4 nulls: bare empty (rows 3, 7), NULL token (row 11), N/A token (row 15)
#   country    codes, lowercase 'us' on rows 2 and 9, mixed 'Ca' on row 4
#   status     'cancelled' on row 6 is outside the allowed set
#   amount     -5 on row 8, 9999 on row 12 (out of 0..1000), 'abc' on row 13
#   signup     '2026-02-30' row 10, 'yesterday' row 14 (unparseable dates)
#   note       whitespace-only on rows 1 and 2 (empty strings)
#   row 19 is an exact copy of row 18
PLANTED_ROWS = [
    ["1", "a@x.com", "US", "paid", "10.5", "2026-01-01", " "],
    ["2", "b@x.com", "us", "paid", "20", "2026-01-02", "  "],
    ["3", "", "CA", "pending", "30", "2026-01-03", "ok"],
    ["4", "d@x.com", "Ca", "paid", "40", "2026-01-04", "ok"],
    ["5", "e@x.com", "GB", "refunded", "50", "2026-01-05", "ok"],
    ["6", "f@x.com", "US", "cancelled", "60", "2026-01-06", "ok"],
    ["7", "", "US", "paid", "70", "2026-01-07", "ok"],
    ["8", "h@x.com", "GB", "paid", "-5", "2026-01-08", "ok"],
    ["9", "i@x.com", "us", "paid", "90", "2026-01-09", "ok"],
    ["10", "j@x.com", "CA", "paid", "100", "2026-02-30", "ok"],
    ["11", "NULL", "US", "paid", "110", "2026-01-11", "ok"],
    ["12", "l@x.com", "US", "paid", "9999", "2026-01-12", "ok"],
    ["13", "m@x.com", "GB", "paid", "abc", "2026-01-13", "ok"],
    ["14", "n@x.com", "US", "pending", "140", "yesterday", "ok"],
    ["15", "N/A", "CA", "paid", "150", "2026-01-15", "ok"],
    ["16", "p@x.com", "US", "paid", "160", "2026-01-16", "ok"],
    ["17", "q@x.com", "GB", "paid", "170", "2026-01-17", "ok"],
    ["18", "r@x.com", "US", "paid", "180", "2026-01-18", "ok"],
    ["18", "r@x.com", "US", "paid", "180", "2026-01-18", "ok"],
    ["5", "t@x.com", "US", "paid", "200", "2026-01-20", "ok"],
]
PLANTED_HEADER = ["id", "email", "country", "status", "amount", "signup", "note"]


def write_csv(name: str, text: str) -> Path:
    path = FIX / name
    path.write_text(text, encoding="utf-8")
    return path


def build_fixtures() -> None:
    if FIX.exists():
        shutil.rmtree(FIX)
    FIX.mkdir(parents=True)
    lines = [",".join(PLANTED_HEADER)] + [",".join(r) for r in PLANTED_ROWS]
    write_csv("planted.csv", "\n".join(lines) + "\n")
    write_csv("empty.csv", "")
    write_csv("blank_lines.csv", "\n\n\n")
    write_csv("header_only.csv", "id,name,score\n")
    write_csv("ragged.csv", "id,name,score\n1,ann,3\n2,bob\n3,cy,4,extra\n4,di,5\n")
    write_csv("all_null.csv", "id,name,middle\n1,ann,\n2,bob,NULL\n3,cy,\n4,di,none\n")
    write_csv("blank_header.csv", "id,,score\n1,a,2\n")
    write_csv("dup_header.csv", "id,name,name\n1,a,b\n")
    write_csv("clean.csv", "id,name,score\n1,ann,3\n2,bob,4\n3,cy,5\n4,di,6\n")
    write_csv("types.tsv", "n\tx\n1\t1.5\n2\t2.5\n")
    (FIX / "bom.csv").write_bytes("﻿id,name\n1,a\n2,b\n".encode("utf-8"))
    (FIX / "latin1.csv").write_bytes("id,name\n1,caf\xe9\n".encode("latin-1"))
    (FIX / "not_sqlite.db").write_text("hello", encoding="utf-8")
    conn = sqlite3.connect(FIX / "sample.db")
    conn.execute("CREATE TABLE people (id INTEGER, name TEXT, age INTEGER, code TEXT)")
    conn.executemany(
        "INSERT INTO people VALUES (?, ?, ?, ?)",
        [(1, "ann", 30, "AA"), (2, "bob", None, "AA"), (3, "", 41, "BB"), (3, "dee", 52, "bb"),
         (4, "eve", 28, "AA"), (5, "fay", 33, "BB")],
    )
    conn.execute('CREATE TABLE "odd ""name" (v TEXT)')
    conn.execute('INSERT INTO "odd ""name" VALUES (\'x\')')
    conn.execute("CREATE TABLE no_rows (v TEXT)")
    conn.commit()
    conn.close()


def planted():
    table = load(FIX / "planted.csv")
    return table, profile_table(table)


def col(profile, name):
    return next(c for c in profile["columns"] if c["name"] == name)


def find(results, rule, column=None):
    hits = [r for r in results if r.rule == rule and (column is None or r.column == column)]
    assert hits, f"no {rule} result for {column}"
    return hits[0]


def expect_error(fn, *fragments):
    try:
        fn()
    except DataHoundError as exc:
        text = str(exc)
        for fragment in fragments:
            assert fragment in text, f"{fragment!r} not in {text!r}"
        return text
    raise AssertionError("expected DataHoundError")


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def fresh(name: str) -> Path:
    path = TMP / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir()
    return path


def table_of(columns, rows, name="t"):
    return Table(name, name, name, "csv", list(columns), [list(r) for r in rows])


NOW = dt.datetime(2026, 11, 13, 12, 0, tzinfo=dt.timezone.utc)

# ---------------------------------------------------------------- loader


def test_csv_loads_header_and_rows():
    table = load(FIX / "planted.csv")
    assert table.columns == PLANTED_HEADER
    assert table.row_count == 20
    assert table.kind == "csv"


def test_csv_null_tokens_become_none():
    table = load(FIX / "planted.csv")
    emails = table.column_values(1)
    assert emails[2] is None and emails[10] is None and emails[14] is None


def test_csv_whitespace_is_kept_as_empty_string_not_null():
    table = load(FIX / "planted.csv")
    assert table.rows[0][6] == " "


def test_empty_file_errors():
    expect_error(lambda: load(FIX / "empty.csv"), "is empty")


def test_blank_lines_only_file_errors_as_empty():
    expect_error(lambda: load(FIX / "blank_lines.csv"), "is empty")


def test_header_only_file_errors():
    expect_error(lambda: load(FIX / "header_only.csv"), "header row but no data rows")


def test_ragged_rows_error_names_lines():
    msg = expect_error(lambda: load(FIX / "ragged.csv"), "ragged rows", "2 row(s)", "3 fields")
    assert "line 3 has 2" in msg and "line 4 has 4" in msg


def test_missing_file_errors():
    expect_error(lambda: load(FIX / "nope.csv"), "file not found")


def test_directory_errors():
    expect_error(lambda: load(FIX), "is a directory")


def test_blank_header_errors():
    expect_error(lambda: load(FIX / "blank_header.csv"), "column 2 has a blank header")


def test_duplicate_header_errors():
    expect_error(lambda: load(FIX / "dup_header.csv"), "duplicate column name 'name'")


def test_non_utf8_errors():
    expect_error(lambda: load(FIX / "latin1.csv"), "not UTF-8")


def test_bom_is_stripped_from_header():
    assert load(FIX / "bom.csv").columns == ["id", "name"]


def test_tsv_uses_tab_delimiter():
    table = load(FIX / "types.tsv")
    assert table.columns == ["n", "x"] and table.rows[1] == ["2", "2.5"]


def test_sqlite_table_loads_with_nulls_and_empty():
    table = load(FIX / "sample.db", table="people")
    assert table.columns == ["id", "name", "age", "code"]
    assert table.row_count == 6
    assert table.rows[1][2] is None and table.rows[2][1] == ""
    assert table.dataset.endswith("::people")


def test_sqlite_quoted_table_name():
    assert load_sqlite(FIX / "sample.db", 'odd "name').rows == [["x"]]


def test_sqlite_missing_table_lists_available():
    expect_error(lambda: load(FIX / "sample.db", table="ghost"), "'ghost' does not exist", "people")


def test_sqlite_without_table_flag_errors():
    expect_error(lambda: load(FIX / "sample.db"), "pass --table", "people")


def test_sqlite_empty_table_errors():
    expect_error(lambda: load(FIX / "sample.db", table="no_rows"), "has no rows")


def test_table_flag_on_csv_errors():
    expect_error(lambda: load(FIX / "clean.csv", table="x"), "only applies to SQLite")


# ---------------------------------------------------------------- profiler


def test_infer_types():
    assert infer_type(["1", "2", "-3"]) == "integer"
    assert infer_type(["1.5", "2", "3e2"]) == "float"
    assert infer_type(["1", "2", "3", "4", "10.5"]) == "float"  # one fraction makes it float
    assert infer_type(["2026-01-01", "2026/02/03"]) == "date"
    assert infer_type(["yes", "no", "TRUE"]) == "boolean"
    assert infer_type(["a", "b"]) == "string"
    assert infer_type([]) == "unknown"


def test_type_inference_tolerates_minority_of_bad_values():
    # 9 of 10 parse, above the 80% majority, so the column is still a date column
    assert infer_type(["2026-01-01"] * 9 + ["soon"]) == "date"
    assert infer_type(["2026-01-01"] * 7 + ["x", "y", "z"]) == "string"


def test_profile_null_rate_with_counts():
    _, prof = planted()
    email = col(prof, "email")
    assert email["null_count"] == 4 and email["rows"] == 20
    assert abs(email["null_rate"] - 0.2) < 1e-9
    assert email["null_rows"] == [3, 7, 11, 15]


def test_profile_empty_string_rate():
    _, prof = planted()
    note = col(prof, "note")
    assert note["empty_count"] == 2 and note["empty_rows"] == [1, 2]
    assert note["null_count"] == 0


def test_profile_numeric_stats_skip_invalid():
    _, prof = planted()
    amount = col(prof, "amount")
    assert amount["type"] == "float"
    assert amount["min"] == -5 and amount["max"] == 9999
    assert amount["invalid_count"] == 1 and amount["invalid_examples"] == [(13, "abc")]
    values = [float(r[4]) for r in PLANTED_ROWS if r[4] != "abc"]
    assert abs(amount["mean"] - sum(values) / len(values)) < 1e-9


def test_profile_unique_count():
    _, prof = planted()
    assert col(prof, "id")["unique_count"] == 18  # 20 rows, ids 5 and 18 repeat
    assert col(prof, "country")["unique_count"] == 5  # US us CA Ca GB


def test_profile_top_values_for_categoricals():
    _, prof = planted()
    tops = col(prof, "status")["top_values"]
    assert tops[0] == ("paid", 16)
    assert ("cancelled", 1) in tops
    assert col(prof, "amount")["top_values"] == []


def test_profile_date_column_min_max_and_invalid():
    _, prof = planted()
    signup = col(prof, "signup")
    assert signup["type"] == "date"
    assert signup["invalid_count"] == 2
    assert signup["min"] == "2026-01-01" and signup["max"] == "2026-01-20"


def test_profile_duplicate_rows():
    _, prof = planted()
    assert prof["duplicate_row_count"] == 1
    assert prof["duplicate_examples"] == [(19, 18)]
    assert abs(prof["duplicate_row_rate"] - 0.05) < 1e-9


def test_all_null_column_profile():
    table = load(FIX / "all_null.csv")
    middle = col(profile_table(table), "middle")
    assert middle["null_count"] == 4 and middle["null_rate"] == 1.0
    assert middle["type"] == "unknown" and middle["unique_count"] == 0
    assert middle["mean"] is None and middle["top_values"] == []


def test_declared_type_overrides_inference():
    prof = profile_column("code", ["001", "002", "003", "004", "A05"], declared_type="string")
    assert prof["type"] == "string" and prof["inferred_type"] == "integer" and prof["invalid_count"] == 0
    prof = profile_column("d", ["01/02/2026", "2026-01-02"], declared_type="date", date_format="%d/%m/%Y")
    assert prof["invalid_count"] == 1


# ---------------------------------------------------------------- rules

PLANTED_RULES = {
    "primary_key": "id",
    "columns": {
        "country": {"code": True, "case": "upper", "allowed": ["US", "CA", "GB"]},
        "status": {"allowed": ["paid", "pending", "refunded"]},
        "amount": {"type": "number", "min": 0, "max": 1000},
        "email": {"max_null_rate": 0.25, "pattern": r"[^@]+@[^@]+\.[a-z]+"},
    },
}


def planted_results(config=None):
    table = load(FIX / "planted.csv")
    config = validate_config(json.loads(json.dumps(config if config is not None else PLANTED_RULES)))
    prof = profile_table(table, config)
    return run_rules(table, prof, config)


def test_rule_null_rate_fails_over_default_threshold():
    result = find(planted_results({}), "null_rate", "email")
    assert not result.passed and result.failing == 4 and result.total == 20
    assert "20.0% (4 of 20 rows)" in result.message and result.threshold == "10.0%"
    assert result.examples[:2] == ["row 3", "row 7"]


def test_rule_null_rate_custom_threshold_passes():
    result = find(planted_results(), "null_rate", "email")
    assert result.passed and result.threshold == "25.0%"


def test_rule_duplicate_primary_key():
    result = find(planted_results(), "duplicate_primary_key", "id")
    assert not result.passed and result.failing == 2
    assert any("'5' appears 2 times (rows 5, 20)" in e for e in result.examples)
    assert any("'18' appears 2 times" in e for e in result.examples)


def test_rule_primary_key_auto_detected_from_id_column():
    assert resolve_primary_key(["id", "x"], {}) == ["id"]
    assert resolve_primary_key(["order_id", "x"], {}) == ["order_id"]
    assert resolve_primary_key(["name", "id"], {}) is None
    assert resolve_primary_key(["id"], {"primary_key": None}) is None
    assert find(planted_results({}), "duplicate_primary_key", "id").failing == 2


def test_rule_composite_primary_key():
    table = table_of(["a", "b"], [["1", "x"], ["1", "y"], ["1", "x"], [None, "z"]])
    results = run_rules(table, profile_table(table), {"primary_key": ["a", "b"]})
    result = find(results, "duplicate_primary_key", "a + b")
    assert result.failing == 2  # one repeat plus one missing key
    assert any("missing key in row 4" in e for e in result.examples)


def test_rule_out_of_range():
    result = find(planted_results(), "out_of_range", "amount")
    assert not result.passed and result.failing == 2 and result.total == 19
    assert result.examples == ["row 8: '-5'", "row 12: '9999'"]
    assert result.threshold == "min 0, max 1000"


def test_rule_out_of_range_dates():
    table = table_of(["d"], [["2026-01-01"], ["2025-12-31"], ["2027-01-01"]])
    config = validate_config({"columns": {"d": {"type": "date", "min": "2026-01-01", "max": "2026-12-31"}}})
    result = find(run_rules(table, profile_table(table, config), config), "out_of_range", "d")
    assert result.failing == 2


def test_rule_range_on_text_column_is_a_config_error():
    table = table_of(["s"], [["a"], ["b"]])
    config = validate_config({"columns": {"s": {"min": 1}}})
    expect_error(lambda: run_rules(table, profile_table(table, config), config), "min/max", "string column")


def test_rule_unexpected_categorical_values():
    result = find(planted_results(), "unexpected_values", "status")
    assert not result.passed and result.failing == 1 and result.examples == ["'cancelled' x1"]
    country = find(planted_results(), "unexpected_values", "country")
    assert country.failing == 3


def test_rule_unparseable_dates():
    result = find(planted_results(), "unparseable_dates", "signup")
    assert not result.passed and result.failing == 2 and result.total == 20
    assert result.examples == ["row 10: '2026-02-30'", "row 14: 'yesterday'"]


def test_rule_non_numeric_values():
    result = find(planted_results(), "non_numeric_values", "amount")
    assert result.failing == 1 and result.examples == ["row 13: 'abc'"]


def test_rule_empty_strings_where_value_expected():
    result = find(planted_results(), "empty_strings", "note")
    assert not result.passed and result.failing == 2 and result.severity == "warning"
    assert result.examples == ["row 1", "row 2"]


def test_rule_inconsistent_casing_configured_upper():
    result = find(planted_results(), "inconsistent_casing", "country")
    assert not result.passed and result.failing == 3
    assert result.examples[0].startswith("same code spelled")


def test_rule_inconsistent_casing_auto_detects_code_columns():
    result = find(planted_results({}), "inconsistent_casing", "country")
    assert not result.passed and "majority upper-case" in result.message
    assert not any(r.rule == "inconsistent_casing" and r.column == "email" for r in planted_results({}))


def test_rule_pattern_mismatch():
    table = table_of(["e"], [["a@b.com"], ["nope"], ["c@d.org"]])
    config = validate_config({"columns": {"e": {"pattern": r"[^@]+@[^@]+\.[a-z]+"}}})
    result = find(run_rules(table, profile_table(table, config), config), "pattern_mismatch", "e")
    assert result.failing == 1 and result.examples == ["row 2: 'nope'"]


def test_rule_duplicate_rows_table_level():
    result = find(planted_results(), "duplicate_rows")
    assert result.column is None and result.failing == 1
    assert result.examples == ["row 19 repeats row 18"]


def test_rule_missing_column_from_config():
    result = find(planted_results({"columns": {"ghost": {"required": True}}}), "missing_column", "ghost")
    assert not result.passed and result.severity == "error"


def test_rule_required_means_zero_nulls():
    table = table_of(["x"], [["a"], [None], ["b"], ["c"], ["d"], ["e"], ["f"], ["g"], ["h"], ["i"], ["j"]])
    config = validate_config({"columns": {"x": {"required": True}}})
    assert not find(run_rules(table, profile_table(table, config), config), "null_rate", "x").passed
    assert find(run_rules(table, profile_table(table), {}), "null_rate", "x").passed  # 9.1% under 10%


def test_rule_unique_column():
    table = table_of(["id", "email"], [["1", "a"], ["2", "a"], ["3", "b"]])
    config = validate_config({"columns": {"email": {"unique": True}}})
    result = find(run_rules(table, profile_table(table), config), "duplicate_values", "email")
    assert result.failing == 1


def test_rule_severity_override():
    result = find(planted_results({"columns": {"note": {"severity": "error"}}}), "empty_strings", "note")
    assert result.severity == "error"


def test_clean_file_passes_every_rule():
    table = load(FIX / "clean.csv")
    results = run_rules(table, profile_table(table), {})
    assert results and all(r.passed for r in results)


def test_all_null_column_fails_null_rate():
    table = load(FIX / "all_null.csv")
    result = find(run_rules(table, profile_table(table), {}), "null_rate", "middle")
    assert not result.passed and "100.0% (4 of 4 rows)" in result.message


def test_worst_first_orders_errors_then_share():
    ordered = worst_first(planted_results())
    severities = [r.severity for r in ordered]
    assert severities == sorted(severities, key=lambda s: 0 if s == "error" else 1)
    errors = [r for r in ordered if r.severity == "error"]
    rates = [r.failing_rate for r in errors]
    assert rates == sorted(rates, reverse=True)


def test_config_unknown_keys_rejected():
    expect_error(lambda: validate_config({"colums": {}}), "unknown key", "colums")
    expect_error(lambda: validate_config({"columns": {"a": {"maxnull": 1}}}), "'a'", "maxnull")


def test_config_bad_values_rejected():
    expect_error(lambda: validate_config({"columns": {"a": {"max_null_rate": 2}}}), "0 to 1")
    expect_error(lambda: validate_config({"columns": {"a": {"type": "money"}}}), "type must be")
    expect_error(lambda: validate_config({"columns": {"a": {"pattern": "("}}}), "not a valid regular expression")
    expect_error(lambda: validate_config({"columns": {"a": {"case": "title"}}}), "upper' or 'lower")
    expect_error(lambda: validate_config({"drift": {"null_rate": 3}}), "from 0 to 1")
    expect_error(lambda: validate_config({"drift": {"nul_rate": 0.1}}), "unknown drift metric")
    expect_error(lambda: validate_config([1]), "JSON object")


def test_config_file_loading_and_errors():
    good = FIX / "rules.json"
    good.write_text(json.dumps(PLANTED_RULES), encoding="utf-8")
    assert load_config(good)["primary_key"] == "id"
    bad = FIX / "bad_rules.json"
    bad.write_text('{"columns": {,}}', encoding="utf-8")
    expect_error(lambda: load_config(bad), "not valid JSON", "line 1")
    expect_error(lambda: load_config(FIX / "missing.json"), "rules file not found")


def test_example_rules_file_is_valid_and_matches_demo():
    config = load_config(ROOT / "examples" / "rules.json")
    table = demo.messy_table()
    results = run_rules(table, profile_table(table, config), config)
    assert not any(r.rule == "missing_column" for r in results)
    assert find(results, "null_rate", "coupon_code").severity == "warning"


# ---------------------------------------------------------------- history and drift


def two_runs(first_rows, second_rows, config=None, columns=("id", "v")):
    store_path = fresh("drift") / "h.db"
    out = store_path.parent / "r.html"
    config = config or {}
    a = table_of(columns, first_rows, name="ds")
    b = table_of(columns, second_rows, name="ds")
    run_check(a, config, store_path, out, now=NOW)
    return run_check(b, config, store_path, out, now=NOW + dt.timedelta(hours=1)), store_path


def test_history_saves_runs_and_finds_previous():
    ctx, store_path = two_runs([["1", "a"], ["2", "b"]], [["1", "a"], ["2", "b"]])
    with HistoryStore(store_path) as store:
        assert store.run_count("ds") == 2
        prev = store.previous_run("ds")
        assert prev["id"] == ctx["run_id"] and prev["metrics"][("", "row_count")] == 2
        assert store.previous_run("ds", before_id=ctx["run_id"])["id"] == ctx["run_id"] - 1
        assert store.previous_run("other") is None
    assert ctx["previous"]["created_at"] == "2026-11-13 12:00:00 UTC"


def test_history_store_is_plain_sqlite():
    _, store_path = two_runs([["1", "a"]], [["1", "a"]])
    conn = sqlite3.connect(store_path)
    rows = conn.execute("SELECT dataset, row_count FROM runs ORDER BY id").fetchall()
    rules = conn.execute("SELECT COUNT(*) FROM rule_results").fetchone()[0]
    conn.close()
    assert rows == [("ds", 1), ("ds", 1)] and rules > 0


def test_history_refuses_foreign_files():
    path = fresh("foreign") / "x.db"
    path.write_text("not a db", encoding="utf-8")
    expect_error(lambda: HistoryStore(path), "not a SQLite file")
    other = path.parent / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE stuff (a)")
    conn.commit()
    conn.close()
    expect_error(lambda: HistoryStore(other), "did not create")


def test_history_trend_oldest_first_and_per_dataset():
    path = fresh("trend") / "h.db"
    out = path.parent / "r.html"
    for n in (1, 2, 3):
        run_check(table_of(["id"], [[str(i)] for i in range(n)], name="t1"), {}, path, out, now=NOW)
    run_check(table_of(["id"], [["1"]], name="t2"), {}, path, out, now=NOW)
    with HistoryStore(path) as store:
        assert [r["row_count"] for r in store.trend("t1")] == [1, 2, 3]
        assert len(store.trend("t2")) == 1


def test_first_run_has_no_drift():
    path = fresh("first") / "h.db"
    ctx = run_check(table_of(["id"], [["1"]], name="solo"), {}, path, path.parent / "r.html", now=NOW)
    assert ctx["drift"] == [] and ctx["previous"] is None


def test_drift_null_rate_flag_shows_old_new_threshold():
    first = [[str(i), "x"] for i in range(20)]
    second = [[str(i), None if i < 4 else "x"] for i in range(20)]
    ctx, _ = two_runs(first, second)
    flag = next(f for f in ctx["drift"] if f.metric == "null_rate")
    assert flag.column == "v" and flag.old == "0.0%" and flag.new == "20.0%"
    assert flag.change == "+20.0 pts" and flag.threshold == "5.0 pts"
    assert flag.message == "null_rate on v: 0.0% -> 20.0% (+20.0 pts), threshold 5.0 pts"


def test_drift_under_threshold_is_not_flagged():
    first = [[str(i), "x"] for i in range(50)]
    second = [[str(i), None if i < 2 else "x"] for i in range(50)]  # 4% null, under 5 pts
    ctx, _ = two_runs(first, second)
    assert not any(f.metric == "null_rate" for f in ctx["drift"])


def test_drift_row_count_relative():
    ctx, _ = two_runs([[str(i), "x"] for i in range(10)], [[str(i), "x"] for i in range(13)])
    flag = next(f for f in ctx["drift"] if f.metric == "row_count")
    assert flag.old == "10" and flag.new == "13" and flag.change == "+30.0%" and flag.threshold == "20.0%"
    ctx, _ = two_runs([[str(i), "x"] for i in range(10)], [[str(i), "x"] for i in range(11)])
    assert not any(f.metric == "row_count" for f in ctx["drift"])


def test_drift_threshold_from_config():
    first = [[str(i), "x"] for i in range(10)]
    second = [[str(i), "x"] for i in range(11)]
    ctx, _ = two_runs(first, second, config={"drift": {"row_count": 0.05}})
    flag = next(f for f in ctx["drift"] if f.metric == "row_count")
    assert flag.threshold == "5.0%"


def test_drift_mean_and_type_change():
    first = [[str(i), "10"] for i in range(10)]
    second = [[str(i), "20"] for i in range(10)]
    ctx, _ = two_runs(first, second)
    assert any(f.metric == "mean" and f.change == "+100.0%" for f in ctx["drift"])
    third = [[str(i), "word"] for i in range(10)]
    ctx, _ = two_runs(first, third)
    flag = next(f for f in ctx["drift"] if f.metric == "type")
    assert flag.old == "integer" and flag.new == "string"


def test_drift_schema_added_and_removed():
    prev = {("", "row_count"): 5, ("a", "type"): "string", ("b", "type"): "string"}
    cur = {("", "row_count"): 5, ("a", "type"): "string", ("c", "type"): "integer"}
    flags = detect_drift(prev, cur)
    changes = {(f.column, f.change) for f in flags}
    assert ("c", "column added") in changes and ("b", "column removed") in changes


def test_drift_rules_failed_increase_and_from_zero():
    prev = {("", "rules_failed"): 0, ("a", "type"): "integer", ("a", "mean"): 0.0}
    cur = {("", "rules_failed"): 2, ("a", "type"): "integer", ("a", "mean"): 4.0}
    flags = {f.metric: f for f in detect_drift(prev, cur)}
    assert flags["rules_failed"].change == "+2" and flags["mean"].change == "from zero"


def test_flatten_metrics_covers_every_column():
    table, prof = planted()
    metrics = flatten_metrics(prof, run_rules(table, prof, {}))
    for name in PLANTED_HEADER:
        assert (name, "null_rate") in metrics and (name, "type") in metrics
    assert metrics[("", "row_count")] == 20


# ---------------------------------------------------------------- reports


def demo_ctx():
    path = fresh("report") / "h.db"
    config = validate_config(demo.DEMO_RULES)
    with HistoryStore(path) as store:
        from datahound.cli import record_baseline

        record_baseline(store, demo.clean_table(), config, NOW - dt.timedelta(days=1))
    return run_check(demo.messy_table(), config, path, path.parent / "report.html", now=NOW)


def test_html_report_written_with_column_cards():
    ctx = demo_ctx()
    page = Path(ctx["report_path"]).read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert page.count('<article class="card') == len(demo.COLUMNS)
    for name in demo.COLUMNS:
        assert f"<h3>{name}</h3>" in page


def test_html_report_lists_failed_rules_with_examples():
    page = Path(demo_ctx()["report_path"]).read_text(encoding="utf-8")
    assert "Failed rules, worst first" in page
    assert "duplicate_primary_key" in page and "unparseable_dates" in page
    assert "&#x27;soon&#x27;" in page  # example value, HTML-escaped


def test_html_report_drift_table_and_inline_svg_trend():
    page = Path(demo_ctx()["report_path"]).read_text(encoding="utf-8")
    assert "Drift since the previous run" in page and "sev-drift" in page
    assert page.count("<svg") >= 3 and "<polyline" in page
    assert "2 run(s) of this dataset" in page


def test_html_report_has_no_external_assets():
    page = Path(demo_ctx()["report_path"]).read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", page)
    assert "@import" not in page and "<script" not in page and "<link" not in page


def test_html_report_escapes_cell_values():
    table = table_of(["id", "s"], [["1", "<script>alert(1)</script>"], ["2", "x"]])
    ctx = run_check(table, {"columns": {"s": {"allowed": ["x"]}}}, fresh("esc") / "h.db",
                    TMP / "esc" / "r.html", now=NOW)
    page = Path(ctx["report_path"]).read_text(encoding="utf-8")
    assert "<script>alert" not in page and "&lt;script&gt;" in page


def test_html_report_dark_mode_and_no_em_dash():
    page = Path(demo_ctx()["report_path"]).read_text(encoding="utf-8")
    assert "prefers-color-scheme:dark" in page
    assert "\u2014" not in page


def test_sparkline_single_point_and_flat():
    one = sparkline([5], "x")
    assert "<circle" in one and "<polyline" not in one
    flat = sparkline([3, 3, 3], "x")
    assert "<polyline" in flat


def test_terminal_summary_worst_first_and_drift():
    ctx = demo_ctx()
    text = terminal_summary(ctx, color=False)
    lines = text.splitlines()
    assert lines[0].startswith("DataHound  orders (demo)  245 rows x 9 columns")
    problems = [ln for ln in lines if ln.startswith("  ERROR") or ln.startswith("  WARN")]
    first_warn = next(i for i, ln in enumerate(problems) if "WARN" in ln)
    assert all("ERROR" in ln for ln in problems[:first_warn])
    assert all("WARN" in ln for ln in problems[first_warn:])
    assert "null_rate" in problems[0] and "customer_email" in problems[0]
    assert "DRIFT  row_count: 200 -> 245 (+22.5%), threshold 20.0%" in text
    assert "\033[" not in text


def test_terminal_summary_clean_run():
    path = fresh("clean") / "h.db"
    ctx = run_check(load(FIX / "clean.csv"), {}, path, path.parent / "r.html", now=NOW)
    text = terminal_summary(ctx)
    assert "PASS" in text and "No rule failures." in text and "first run" in text


# ---------------------------------------------------------------- demo


def test_demo_plants_known_problems():
    table = demo.messy_table()
    config = validate_config(demo.DEMO_RULES)
    prof = profile_table(table, config)
    results = run_rules(table, prof, config)
    assert find(results, "null_rate", "customer_email").failing >= demo.PLANTED["customer_email_nulls"]
    assert find(results, "unparseable_dates", "order_date").failing == demo.PLANTED["order_date_unparseable"]
    assert find(results, "non_numeric_values", "amount").failing == demo.PLANTED["amount_non_numeric"]
    assert find(results, "out_of_range", "amount").failing == demo.PLANTED["amount_out_of_range"]
    assert find(results, "empty_strings", "customer_name").failing >= demo.PLANTED["customer_name_blank"]
    assert find(results, "duplicate_rows").failing == demo.PLANTED["exact_duplicate_rows"]
    assert not find(results, "duplicate_primary_key", "order_id").passed
    assert not find(results, "inconsistent_casing", "country").passed


def test_demo_baseline_is_clean_and_deterministic():
    base = demo.clean_table()
    config = validate_config(demo.DEMO_RULES)
    results = run_rules(base, profile_table(base, config), config)
    assert all(r.passed for r in results), [r.message for r in results if not r.passed]
    assert demo.messy_table().rows == demo.messy_table().rows


# ---------------------------------------------------------------- cli


def test_cli_demo_end_to_end():
    work = fresh("cli_demo")
    out = work / "report.html"
    code, stdout, _ = cli("check", "--demo", "--out", str(out), "--no-color")
    assert code == 1  # error-level rules failed
    assert out.exists() and (work / "datahound-history.db").exists()
    assert "Problems, worst first" in stdout and "DRIFT" in stdout
    with HistoryStore(work / "datahound-history.db") as store:
        assert store.run_count(demo.DATASET) == 2  # baseline + this run
    cli("check", "--demo", "--out", str(out), "--no-color")
    with HistoryStore(work / "datahound-history.db") as store:
        assert store.run_count(demo.DATASET) == 3  # baseline recorded only once


def test_cli_clean_csv_exit_zero_and_custom_store():
    work = fresh("cli_clean")
    store = work / "custom.db"
    code, stdout, _ = cli("check", str(FIX / "clean.csv"), "--out", str(work / "r.html"), "--store", str(store))
    assert code == 0 and store.exists() and not (work / "datahound-history.db").exists()
    code, stdout, _ = cli("check", str(FIX / "clean.csv"), "--out", str(work / "r.html"), "--store", str(store))
    assert "drift: 0 flag(s) vs run #1" in stdout


def test_cli_rules_and_sqlite_table():
    work = fresh("cli_sqlite")
    rules = work / "rules.json"
    rules.write_text(json.dumps({"columns": {"age": {"type": "integer", "min": 0, "max": 50}}}), encoding="utf-8")
    code, stdout, _ = cli("check", str(FIX / "sample.db"), "--table", "people", "--rules", str(rules),
                          "--out", str(work / "r.html"), "--no-color")
    assert code == 1
    assert "out_of_range" in stdout and "duplicate_primary_key" in stdout and "inconsistent_casing" in stdout


def test_cli_missing_file():
    code, _, err = cli("check", str(FIX / "nope.csv"), "--out", str(fresh("e1") / "r.html"))
    assert code == 2 and "datahound: error: file not found" in err


def test_cli_empty_and_header_only_files():
    work = fresh("e2")
    code, _, err = cli("check", str(FIX / "empty.csv"), "--out", str(work / "r.html"))
    assert code == 2 and "is empty" in err
    code, _, err = cli("check", str(FIX / "header_only.csv"), "--out", str(work / "r.html"))
    assert code == 2 and "no data rows" in err
    assert not (work / "r.html").exists()


def test_cli_ragged_rows():
    code, _, err = cli("check", str(FIX / "ragged.csv"), "--out", str(fresh("e3") / "r.html"))
    assert code == 2 and "ragged rows" in err


def test_cli_sqlite_table_does_not_exist():
    code, _, err = cli("check", str(FIX / "sample.db"), "--table", "ghost", "--out", str(fresh("e4") / "r.html"))
    assert code == 2 and "'ghost' does not exist" in err


def test_cli_argument_errors():
    code, _, err = cli("check", "--out", str(fresh("e5") / "r.html"))
    assert code == 2 and "or use --demo" in err
    code, _, err = cli("check", str(FIX / "clean.csv"), "--demo")
    assert code == 2 and "drop the file path" in err
    code, _, err = cli("check", "--demo", "--table", "x")
    assert code == 2 and "--table does not apply" in err
    code, _, err = cli("check", str(FIX / "clean.csv"), "--out", str(TMP / "no" / "such" / "r.html"))
    assert code == 2 and "report folder does not exist" in err


def test_cli_bad_rules_file():
    work = fresh("e6")
    code, _, err = cli("check", str(FIX / "clean.csv"), "--rules", str(work / "none.json"), "--out", str(work / "r.html"))
    assert code == 2 and "rules file not found" in err
    bad = work / "bad.json"
    bad.write_text('{"columns": {"score": {"max": "lots"}}}', encoding="utf-8")
    code, _, err = cli("check", str(FIX / "clean.csv"), "--rules", str(bad), "--out", str(work / "r.html"))
    assert code == 2 and "max must be a number" in err


def test_cli_usage_errors_from_argparse():
    with contextlib.redirect_stderr(io.StringIO()):
        assert main([]) == 2
        assert main(["frobnicate"]) == 2
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["--help"]) == 0


def test_cli_name_flag_keys_history():
    work = fresh("cli_name")
    for _ in range(2):
        cli("check", str(FIX / "clean.csv"), "--out", str(work / "r.html"), "--name", "scores")
    with HistoryStore(work / "datahound-history.db") as store:
        assert store.run_count("scores") == 2


# ---------------------------------------------------------------- runner


def run(selected: str | None = None) -> int:
    build_fixtures()
    tests = [(name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)]
    if selected:
        tests = [(n, f) for n, f in tests if selected in n]
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    shutil.rmtree(TMP, ignore_errors=True)
    passed = len(tests) - failures
    print(f"\n{passed} passed, {failures} failed, {len(tests)} total")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1] if len(sys.argv) > 1 else None))
