# DataHound

A local CLI that sniffs out data quality problems in a CSV or SQLite table and tracks them run over run, so bad data gets caught before it reaches a dashboard.

DataHound profiles every column (types, null rates, duplicates, ranges, top values), runs built-in and custom quality rules, saves each run to a local history store, and flags drift when a metric moves past a stated threshold. You get a terminal summary, worst problems first, plus a self-contained HTML report.

Python 3.10+, standard library only. No dependencies, no network access, nothing hosted. Your data and your history file stay on your machine.

Built with Claude Opus as portfolio project 27 (Data Engineering / Analytics).

## Why data quality fails silently

Bad data rarely crashes anything. A null rate creeps from 1% to 15% after an upstream form change, and the dashboard average quietly moves. A country column picks up `us` next to `US`, and a group-by splits one market into two. An export starts writing dates as `31/12/2026`, the parser turns them into nulls, and the chart has a gap nobody notices for a month. Every one of these produces valid-looking output, so the query succeeds and the problem reaches the dashboard.

DataHound checks the table itself before anyone trusts it. It tells you what is wrong now (rules) and what changed since last time (drift), and it shows the row counts and examples behind every finding, so you can confirm each one yourself.

## Run it

```bash
git clone https://github.com/battlettt/datahound.git
cd datahound

# Built-in messy sample, no input file needed
python3 -m datahound check --demo

# A CSV file
python3 -m datahound check data.csv

# A table (or view) inside a SQLite database
python3 -m datahound check warehouse.db --table orders

# Custom rules, a chosen history store and report path
python3 -m datahound check data.csv --rules rules.json --store history.db --out report.html
```

| Option | Meaning |
| --- | --- |
| `path` | CSV file, or SQLite database (recognised by its file header, not its extension) |
| `--table NAME` | Table or view to check inside a SQLite database. Required for SQLite, rejected for CSV |
| `--rules FILE` | JSON file with custom rules and thresholds (see below) |
| `--store FILE` | History store. Default: `datahound-history.db` next to the report |
| `--out FILE` | HTML report. Default: `datahound-report.html` in the current folder |
| `--demo` | Run against the built-in sample dataset |
| `--name NAME` | History key for this dataset. Default: the file's absolute path (plus `::table` for SQLite). Use it when the file moves or gets a dated name each day |
| `--delimiter C` | CSV delimiter. Default: comma, or tab for `.tsv` files |
| `--no-color` | Plain terminal output (colour is also off when piped or when `NO_COLOR` is set) |

Exit codes: `0` no error-level rule failed, `1` at least one did, `2` bad input or arguments. Warnings and drift flags do not change the exit code; they are reported for a person to judge.

DataHound refuses input it cannot judge fairly, with a message that says what to fix: a missing file, an empty file, a header-only file, ragged rows (with the line numbers and field counts), blank or duplicate header names, a non-UTF-8 file, a SQLite table that does not exist (with the list of tables that do), and an empty SQLite table.

## What it measures

**Nulls, empties and present values.** A CSV cell is *null* when it is bare empty (`a,,c`) or one of the tokens `NULL`, `None`, `NaN`, `N/A`, `#N/A` in any case. A cell that holds only whitespace is an *empty string*, reported separately, because "the field was never filled" and "someone typed a space" are different problems. In SQLite, `NULL` is null and `''` or whitespace-only text is an empty string. Everything else is *present*.

**Per column:** inferred type (integer, float, date, boolean, string), null rate, empty-string rate, unique count, invalid count (present values that do not parse as the column's type), min, max and mean for numbers, min and max for dates, top five values for text and boolean columns.

**Per table:** row count, column count, exact duplicate row rate.

Every rate is printed with the counts behind it, for example `12.7% (31 of 245 rows)`. Row numbers are data rows: row 1 is the first row after a CSV header.

Type inference takes the most specific type that at least 80% of present values parse as, so a date column with a few garbage values stays a date column, and the garbage is reported as unparseable instead of hiding the column's type. A column of whole numbers with a single `10.5` is a float column. Declare a type in the rules file to override inference.

## Built-in rules

These run on every check with no config. Each result is pass or fail, with the threshold, the counts and up to five offending examples.

| Rule | Fails when | Default | Severity |
| --- | --- | --- | --- |
| `null_rate` | a column's null rate is over its limit | 10% (0% for `required` columns) | error |
| `empty_strings` | blank text appears where a value is expected | 0% | warning |
| `duplicate_primary_key` | a key value repeats or a key is missing | key is the first column when it is named `id` or `*_id` | error |
| `unparseable_dates` | values in a date column do not parse as dates | 0 values | error |
| `non_numeric_values` | values in a number column are not numbers | 0 values | error |
| `invalid_booleans` | values in a boolean column are not true/false | 0 values | error |
| `inconsistent_casing` | a code column mixes cases (`US`, `us`, `Ca`) | auto on short, repeating, space-free text columns | warning |
| `duplicate_rows` | exact copies of an earlier row | 0% | warning |

These need a rules file because only you know the right values:

| Rule | Config key | Fails when |
| --- | --- | --- |
| `out_of_range` | `min`, `max` | a number or date falls outside the bounds |
| `unexpected_values` | `allowed` | a value is not in the allowed set |
| `pattern_mismatch` | `pattern` | a value does not fully match the regular expression |
| `duplicate_values` | `unique` | a non-key column repeats a value |
| `missing_column` | any column entry | a configured column is not in the table |

## Custom rules

Pass a JSON file with `--rules`. Every key is optional, and unknown keys are rejected with the list of allowed ones, so a typo never silently disables a check. A complete example lives in [`examples/rules.json`](examples/rules.json):

```json
{
  "primary_key": "order_id",
  "defaults": { "max_null_rate": 0.05, "max_empty_rate": 0.0 },
  "max_duplicate_row_rate": 0.0,
  "columns": {
    "order_id":       { "type": "integer", "required": true },
    "customer_email": { "max_null_rate": 0.02, "pattern": "[^@\\s]+@[^@\\s]+\\.[a-z]+" },
    "country":        { "code": true, "case": "upper", "allowed": ["US", "CA", "GB"] },
    "status":         { "allowed": ["paid", "pending", "refunded", "shipped"] },
    "amount":         { "type": "number", "min": 0, "max": 5000 },
    "order_date":     { "type": "date", "format": "%Y-%m-%d", "min": "2026-01-01" },
    "coupon_code":    { "max_null_rate": 1.0, "severity": "warning" }
  },
  "drift": { "row_count": 0.2, "null_rate": 0.05, "mean": 0.25 }
}
```

Top level:

- `primary_key`: a column name, a list of names for a composite key, or `null` to turn the key check off.
- `defaults`: `max_null_rate` and `max_empty_rate` (0 to 1) for every column, and `check_casing: false` to turn off automatic code-column detection.
- `max_duplicate_row_rate`: limit for exact duplicate rows (0 to 1).
- `drift`: thresholds for drift flags (next section).

Per column:

- `type`: `integer`, `number` (or `float`), `date`, `boolean`, `string`. Overrides inference.
- `format`: strptime format for a date column, such as `%Y-%m-%d`. Without it DataHound accepts ISO dates, `YYYY/MM/DD`, `MM/DD/YYYY` and `DD.MM.YYYY`.
- `required`: no nulls and no empty strings allowed.
- `max_null_rate`, `max_empty_rate`: per-column limits, 0 to 1.
- `min`, `max`: numbers for number columns, date strings for date columns.
- `allowed`: list of permitted values, compared exactly after trimming outer whitespace.
- `pattern`: regular expression every value must fully match.
- `unique`: no repeated values.
- `code`: `true` forces the casing check on, `false` turns it off. `case`: `upper` or `lower` sets the expected style (otherwise the majority style wins).
- `severity`: `error` or `warning` for every rule on this column. Only error failures make the exit code 1.

## History and drift

Each run appends to the history store, a plain SQLite file you own. Tables: `runs` (one row per check), `metrics` (every metric per column per run), `rule_results` (every rule outcome), and `meta`. Read it with any SQLite tool:

```bash
sqlite3 datahound-history.db "SELECT id, created_at, row_count, rules_failed, drift_flags FROM runs"
```

Runs are grouped by dataset key (the file's absolute path, or `--name`). Each run is compared with the previous run of the same dataset, and a drift flag fires when a metric moves *past* its threshold:

| Metric | Scope | Threshold mode | Default |
| --- | --- | --- | --- |
| `row_count` | table | relative change | 20% |
| `duplicate_row_rate` | table | absolute change | 2 percentage points |
| `rules_failed` | table | any increase | +0 |
| `null_rate` | column | absolute change | 5 percentage points |
| `empty_rate` | column | absolute change | 5 percentage points |
| `invalid_rate` | column | absolute change | 2 percentage points |
| `distinct_count` | column | relative change | 50% |
| `mean` | column | relative change | 25% |
| type, added or removed column | column | any change | always flagged |

Override any number in the `drift` section of the rules file. Rate thresholds are fractions (`0.05` = 5 points); relative thresholds are fractions of the old value (`0.2` = 20%).

Reading a flag:

```
DRIFT  null_rate on customer_email: 0.0% -> 12.7% (+12.7 pts), threshold 5.0 pts
```

That says: last run this column had no nulls, now 12.7% of rows are null, the move is 12.7 percentage points, and anything over 5 points is flagged. Treat a drift flag as a question to answer (did the source change? is this expected?), not as noise. If the change is expected, the next run compares against this one and the flag goes away.

## Reading the report

The terminal summary starts with a verdict (`FAIL` if any error-level rule failed, `WARN` for warnings only, `PASS` otherwise), then lists problems worst first: errors before warnings, then by the share of rows affected. Each problem names the rule, the column, the counts and the threshold, with examples on the next line. Drift flags follow, then a one-line-per-column profile.

The HTML report (`datahound-report.html` by default) is one file with inline CSS and SVG and no external assets, so it opens offline and can be attached to a ticket. It has:

- summary tiles: rows, failed rules, drift flags, duplicate rows;
- a trend view: inline SVG sparklines of row count, failed rules and drift flags across the last 20 runs;
- the failed rules table, worst first, with every example;
- the drift table with old value, new value, change and threshold;
- one card per column: type, null and empty rates with counts, unique count, min/max/mean or top values, a null-rate sparkline across runs, and badges for the rules it failed (red top border for errors, amber for warnings);
- the passed rules, folded away.

It follows your system light or dark theme and works at phone width.

## The demo

`python3 -m datahound check --demo` checks a built-in orders table (245 rows, 9 columns) with planted problems:

- 31 null customer emails, 12.7% of rows against a 5% limit (30 planted, one more copied by a duplicate row), and 10 blank customer names;
- country codes `us` and `Ca` next to `US` and `CA`;
- statuses `PAID`, `shipped?` and `cancelled` outside the allowed set;
- 5 out-of-range amounts (`-80.00`, `99999.00`) and 2 non-numeric ones (`12,50`, `abc`);
- 6 bad dates (`2026-02-30`, `31/12/2026`, `soon`);
- reused order ids and 5 exact duplicate rows.

The first demo run against a store also records a clean 200-row baseline as the previous run, so the drift table and trend view are populated right away (row count +22.5%, email null rate 0% to 12.7%, amount mean +325%, and more). Later demo runs compare with the run before them. The data comes from fixed seeds, so the numbers are the same every time. The demo exits with code 1 because error-level rules fail, as they should.

## Tests

```bash
python3 tests/run_tests.py          # all tests
python3 tests/run_tests.py drift    # only tests whose name contains "drift"
```

93 tests, plain asserts, no framework. They cover profiler stats against a fixture CSV with known planted problems, every built-in rule, config validation, drift detection across a two-run history, the history store, HTML and terminal report generation, the demo, and CLI argument and input errors. Edge fixtures include an empty file, a header-only file, ragged rows, a column that is all nulls, blank and duplicate headers, a non-UTF-8 file and an empty SQLite table. Fixtures are generated into `tests/fixtures/` on each run.

## Scope

DataHound checks one table at a time, when you run it. It is not a pipeline tool, a scheduler, a warehouse connector or a hosted dashboard, and it never makes a network call. It loads the table into memory, which is fine for files up to a few million cells; for larger tables, point it at a SQLite view that samples the rows you care about.

## Layout

```
datahound/
  cli.py        argument parsing and the check flow
  loader.py     CSV and SQLite loading, input errors
  profiler.py   type inference and per-column stats
  rules.py      built-in and custom rules, config validation
  drift.py      metric flattening and drift detection
  history.py    SQLite history store
  report.py     terminal summary and HTML report
  demo.py       built-in sample dataset with planted problems
tests/run_tests.py
examples/rules.json
```

## License

MIT
