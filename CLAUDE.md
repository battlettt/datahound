# DataHound

Local CLI that profiles one CSV file or SQLite table, runs quality rules, saves each run to a SQLite history store, flags drift against the previous run, and writes a terminal summary plus a self-contained HTML report.

## Hard constraints

- Python 3.10+, standard library only. No dependencies, ever.
- No network calls of any kind, including in tests and reports (no CDN fonts, scripts or images).
- Local CLI by design: no cloud warehouses, hosted dashboards, schedulers or pipelines. One table per run.
- Not an ETL tool and not a model-drift tool (that is Driftwatch). Scope is data quality and drift on a table.
- Push only to battlettt/datahound.

## Commands

```bash
python3 -m datahound check --demo                 # built-in messy sample
python3 -m datahound check data.csv --rules examples/rules.json
python3 -m datahound check db.sqlite --table orders
python3 tests/run_tests.py [name-filter]          # plain-assert suite, must stay all green
```

## Layout

- `datahound/loader.py` loads input into `Table` (cells are `str` or `None`); raises `DataHoundError` for user-fixable problems.
- `datahound/profiler.py` type inference (80% majority) and per-column stats.
- `datahound/rules.py` built-in and config rules, config validation, `worst_first` ordering.
- `datahound/drift.py` `flatten_metrics` and `detect_drift` with absolute / relative / increase thresholds.
- `datahound/history.py` SQLite store (`meta`, `runs`, `metrics`, `rule_results`).
- `datahound/report.py` terminal summary and HTML (inline CSS + SVG, light and dark).
- `datahound/demo.py` seeded demo data; `PLANTED` lists what is planted.
- `datahound/cli.py` argparse and `run_check`, the single code path for every run.

## Conventions

- Every rate shown to a user carries its counts: use `util.rate(count, total)`.
- Every rule result has pass/fail, threshold, counts and up to 5 examples. Every drift flag has old, new, change and threshold.
- Null vs empty: null is missing, empty is whitespace-only text. Keep them separate.
- Row numbers are 1-based data rows (row 1 is the first row after a CSV header).
- Exit codes: 0 clean, 1 error-level rule failed, 2 bad input or arguments.
- Config keys are whitelisted; add new keys to the allowed sets in `rules.py` and document them in the README.
- HTML output must escape every cell value and contain no em-dashes or external assets.
- Tests generate their fixtures into `tests/fixtures/` (gitignored). Add a test for every new rule, metric or error path.
