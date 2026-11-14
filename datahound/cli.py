"""Command line entry point: python3 -m datahound check ..."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from . import __version__, demo
from .drift import detect_drift, flatten_metrics
from .history import HistoryStore
from .loader import DataHoundError, load
from .profiler import profile_table
from .report import render_html, terminal_summary, use_color
from .rules import load_config, run_rules, validate_config

DEFAULT_REPORT = "datahound-report.html"
DEFAULT_STORE = "datahound-history.db"

EXIT_OK = 0
EXIT_RULES_FAILED = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m datahound",
        description="Sniff out data quality problems in a CSV or SQLite table and track them run over run.",
    )
    parser.add_argument("--version", action="version", version=f"datahound {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True
    check = sub.add_parser(
        "check",
        help="profile a table, run quality rules, record history and write a report",
        description="Profile a CSV file or SQLite table, run quality rules, compare with the previous run, "
        "and write a terminal summary plus an HTML report. Exit code 0 means no error-level rule failed, "
        "1 means at least one did, 2 means bad input or arguments.",
    )
    check.add_argument("path", nargs="?", help="CSV file, or SQLite database with --table")
    check.add_argument("--table", help="table or view to check inside a SQLite database")
    check.add_argument("--rules", metavar="RULES.json", help="JSON file with custom rules and thresholds")
    check.add_argument(
        "--store",
        metavar="HISTORY.db",
        help=f"SQLite history store (default: {DEFAULT_STORE} next to the report)",
    )
    check.add_argument("--out", metavar="REPORT.html", help=f"HTML report path (default: {DEFAULT_REPORT})")
    check.add_argument("--demo", action="store_true", help="run against the built-in messy sample dataset")
    check.add_argument(
        "--name",
        help="dataset name for history (default: the file's absolute path, plus ::table for SQLite)",
    )
    check.add_argument("--delimiter", help="CSV delimiter (default: comma, or tab for .tsv files)")
    check.add_argument("--no-color", action="store_true", help="plain terminal output")
    return parser


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _stamp(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def record_baseline(store: HistoryStore, table, config: dict, moment: dt.datetime) -> int:
    """Save a run without writing a report. Used for the demo's clean baseline."""
    profile = profile_table(table, config)
    results = run_rules(table, profile, config)
    metrics = flatten_metrics(profile, results)
    return store.save_run(table.dataset, table.source, _stamp(moment), metrics, results, 0)


def run_check(table, config: dict, store_path: Path, out_path: Path, now: dt.datetime | None = None) -> dict:
    """Profile, run rules, compare with the previous run, save history and write the HTML report."""
    moment = now or _now()
    profile = profile_table(table, config)
    results = run_rules(table, profile, config)
    metrics = flatten_metrics(profile, results)
    with HistoryStore(store_path) as store:
        previous = store.previous_run(table.dataset)
        flags = detect_drift(previous["metrics"] if previous else None, metrics, config)
        run_id = store.save_run(table.dataset, table.source, _stamp(moment), metrics, results, len(flags))
        trend = store.trend(table.dataset)
    ctx = {
        "table_name": table.name,
        "source": table.source,
        "dataset": table.dataset,
        "created_at": _stamp(moment),
        "profile": profile,
        "results": results,
        "drift": flags,
        "previous": previous,
        "trend": trend,
        "run_id": run_id,
        "report_path": str(out_path),
        "store_path": str(store_path),
    }
    try:
        out_path.write_text(render_html(ctx), encoding="utf-8")
    except OSError as exc:
        raise DataHoundError(f"could not write the report to {out_path}: {exc.strerror}") from None
    return ctx


def _check(args) -> int:
    if args.demo and args.path:
        raise DataHoundError("--demo uses the built-in sample dataset; drop the file path or drop --demo")
    if args.demo and args.table:
        raise DataHoundError("--table does not apply to --demo")
    if not args.demo and not args.path:
        raise DataHoundError("give a CSV file or SQLite database to check, or use --demo")
    if args.delimiter is not None and len(args.delimiter.encode("utf-8").decode("unicode_escape")) != 1:
        raise DataHoundError("--delimiter must be a single character (use \\t for tab)")

    out_path = Path(args.out or DEFAULT_REPORT)
    if not out_path.parent.exists():
        raise DataHoundError(f"report folder does not exist: {out_path.parent}")
    if out_path.is_dir():
        raise DataHoundError(f"--out {out_path} is a directory; give a file name such as report.html")
    store_path = Path(args.store) if args.store else out_path.parent / DEFAULT_STORE

    config = load_config(args.rules) if args.rules else None
    moment = _now()
    if args.demo:
        if config is None:
            config = validate_config(demo.DEMO_RULES)
        table = demo.messy_table()
        with HistoryStore(store_path) as store:
            if store.run_count(demo.DATASET) == 0:
                record_baseline(store, demo.clean_table(), config, moment - dt.timedelta(days=1))
    else:
        delimiter = args.delimiter.encode("utf-8").decode("unicode_escape") if args.delimiter else None
        table = load(args.path, table=args.table, delimiter=delimiter)
        config = config or {}
    if args.name:
        table.dataset = args.name

    ctx = run_check(table, config, store_path, out_path, now=moment)
    color = use_color(sys.stdout) and not args.no_color
    sys.stdout.write(terminal_summary(ctx, color=color))
    errors = [r for r in ctx["results"] if not r.passed and r.severity == "error"]
    return EXIT_RULES_FAILED if errors else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already printed usage
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    try:
        return _check(args)
    except DataHoundError as exc:
        sys.stderr.write(f"datahound: error: {exc}\n")
        return EXIT_USAGE
    except KeyboardInterrupt:
        sys.stderr.write("datahound: interrupted\n")
        return 130
