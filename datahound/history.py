"""Run history in a plain SQLite file the user owns.

Tables:
  meta          key/value, holds schema_version so a foreign database is refused
  runs          one row per check: dataset key, time, row and rule counts
  metrics       (run_id, column_name, metric) -> value or text_value
  rule_results  every rule outcome for the run, pass or fail

Inspect it with any SQLite client, for example:
  sqlite3 datahound-history.db "SELECT id, created_at, row_count, rules_failed FROM runs"
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .loader import DataHoundError, is_sqlite_file

SCHEMA_VERSION = "1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    column_count INTEGER NOT NULL,
    rules_failed INTEGER NOT NULL,
    rules_total INTEGER NOT NULL,
    drift_flags INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_dataset ON runs (dataset, id);
CREATE TABLE IF NOT EXISTS metrics (
    run_id INTEGER NOT NULL REFERENCES runs (id),
    column_name TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    text_value TEXT,
    PRIMARY KEY (run_id, column_name, metric)
);
CREATE TABLE IF NOT EXISTS rule_results (
    run_id INTEGER NOT NULL REFERENCES runs (id),
    rule TEXT NOT NULL,
    column_name TEXT,
    passed INTEGER NOT NULL,
    severity TEXT NOT NULL,
    failing INTEGER NOT NULL,
    total INTEGER NOT NULL,
    message TEXT NOT NULL
);
"""


class HistoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.exists():
            if self.path.is_dir():
                raise DataHoundError(f"history store {self.path} is a directory, not a file")
            if self.path.stat().st_size and not is_sqlite_file(self.path):
                raise DataHoundError(f"history store {self.path} exists but is not a SQLite file")
        elif not self.path.parent.exists():
            raise DataHoundError(f"history store folder does not exist: {self.path.parent}")
        self.conn = sqlite3.connect(self.path)
        try:
            tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "meta" not in tables:
                raise DataHoundError(
                    f"history store {self.path} is a SQLite file that DataHound did not create; "
                    f"pass a different --store path"
                )
            self.conn.executescript(SCHEMA)
            self.conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
            )
            self.conn.commit()
        except sqlite3.DatabaseError as exc:
            self.conn.close()
            raise DataHoundError(f"history store {self.path} could not be opened: {exc}") from None
        except DataHoundError:
            self.conn.close()
            raise

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def run_count(self, dataset: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM runs WHERE dataset = ?", (dataset,)).fetchone()[0]

    def save_run(self, dataset, source, created_at, metrics: dict, results, drift_count: int) -> int:
        failed = sum(1 for r in results if not r.passed)
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO runs (dataset, source, created_at, row_count, column_count, rules_failed, "
                "rules_total, drift_flags) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    dataset,
                    source,
                    created_at,
                    int(metrics[("", "row_count")]),
                    int(metrics[("", "column_count")]),
                    failed,
                    len(results),
                    drift_count,
                ),
            )
            run_id = cursor.lastrowid
            self.conn.executemany(
                "INSERT INTO metrics (run_id, column_name, metric, value, text_value) VALUES (?, ?, ?, ?, ?)",
                [
                    (run_id, col, metric, None if isinstance(v, str) else float(v), v if isinstance(v, str) else None)
                    for (col, metric), v in metrics.items()
                ],
            )
            self.conn.executemany(
                "INSERT INTO rule_results (run_id, rule, column_name, passed, severity, failing, total, message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (run_id, r.rule, r.column, int(r.passed), r.severity, r.failing, r.total, r.message)
                    for r in results
                ],
            )
        return run_id

    def _metrics(self, run_id: int) -> dict:
        return {
            (col, metric): (text if text is not None else value)
            for col, metric, value, text in self.conn.execute(
                "SELECT column_name, metric, value, text_value FROM metrics WHERE run_id = ?", (run_id,)
            )
        }

    def previous_run(self, dataset: str, before_id: int | None = None) -> dict | None:
        """Latest run of this dataset (optionally before a given run id), with its metrics."""
        sql = "SELECT id, created_at, row_count, rules_failed FROM runs WHERE dataset = ?"
        params: list = [dataset]
        if before_id is not None:
            sql += " AND id < ?"
            params.append(before_id)
        row = self.conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "created_at": row[1],
            "row_count": row[2],
            "rules_failed": row[3],
            "metrics": self._metrics(row[0]),
        }

    def trend(self, dataset: str, limit: int = 20) -> list[dict]:
        """Last `limit` runs of this dataset, oldest first, each with its metrics."""
        rows = self.conn.execute(
            "SELECT id, created_at, row_count, rules_failed, drift_flags FROM runs WHERE dataset = ? "
            "ORDER BY id DESC LIMIT ?",
            (dataset, limit),
        ).fetchall()
        return [
            {
                "id": r[0],
                "created_at": r[1],
                "row_count": r[2],
                "rules_failed": r[3],
                "drift_flags": r[4],
                "metrics": self._metrics(r[0]),
            }
            for r in reversed(rows)
        ]
