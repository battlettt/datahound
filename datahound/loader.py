"""Load a CSV file or a SQLite table into memory as rows of text cells.

Every cell becomes either None (a null) or a string. Keeping one representation
for both inputs means the profiler and the rules never care where data came from.

CSV null convention: a bare empty field (`a,,c`) and the tokens NULL, None, NaN,
N/A and #N/A (any case) are nulls. A field that holds only whitespace is an
*empty string*, which DataHound reports separately. SQLite NULL is a null and
SQLite '' or whitespace-only text is an empty string; SQLite text is never
reinterpreted as a null token.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

CSV_NULL_TOKENS = frozenset({"null", "none", "nan", "n/a", "#n/a"})
SQLITE_MAGIC = b"SQLite format 3\x00"
MAX_RAGGED_SHOWN = 5


class DataHoundError(Exception):
    """A problem the user can fix (bad path, bad file, bad config). Printed without a traceback."""


@dataclass
class Table:
    name: str  # short display name
    source: str  # where it came from, shown in reports
    dataset: str  # stable key that ties runs together in the history store
    kind: str  # "csv", "sqlite" or "demo"
    columns: list[str]
    rows: list[list[str | None]]

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def column_values(self, index: int) -> list[str | None]:
        return [row[index] for row in self.rows]


def _check_file(path: Path) -> None:
    if not path.exists():
        raise DataHoundError(f"file not found: {path}")
    if path.is_dir():
        raise DataHoundError(f"{path} is a directory, not a file")


def is_sqlite_file(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def csv_cell(raw: str) -> str | None:
    if raw == "" or raw.strip().lower() in CSV_NULL_TOKENS:
        return None
    return raw


def _validate_header(header: list[str], path: Path) -> None:
    for position, name in enumerate(header, start=1):
        if name == "":
            raise DataHoundError(f"{path}: column {position} has a blank header name")
    seen: set[str] = set()
    for name in header:
        if name in seen:
            raise DataHoundError(f"{path}: duplicate column name {name!r} in the header")
        seen.add(name)


def load_csv(path: str | Path, delimiter: str | None = None) -> Table:
    path = Path(path)
    _check_file(path)
    if delimiter is None:
        delimiter = "\t" if path.suffix.lower() in (".tsv", ".tab") else ","
    header: list[str] | None = None
    rows: list[list[str | None]] = []
    ragged: list[tuple[int, int]] = []
    line = 0
    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle, delimiter=delimiter)
            for record in reader:
                line = reader.line_num
                if not record:  # a truly blank line
                    continue
                if header is None:
                    header = [name.strip() for name in record]
                    continue
                if len(record) != len(header):
                    ragged.append((line, len(record)))
                    continue
                rows.append([csv_cell(cell) for cell in record])
    except UnicodeDecodeError:
        raise DataHoundError(f"{path} is not UTF-8 text; DataHound reads UTF-8 CSV files") from None
    except csv.Error as exc:
        raise DataHoundError(f"{path} could not be parsed as CSV near line {line + 1}: {exc}") from None

    if header is None:
        raise DataHoundError(f"{path} is empty: no header row and no data rows")
    _validate_header(header, path)
    if ragged:
        shown = ", ".join(f"line {ln} has {n}" for ln, n in ragged[:MAX_RAGGED_SHOWN])
        more = f", and {len(ragged) - MAX_RAGGED_SHOWN} more" if len(ragged) > MAX_RAGGED_SHOWN else ""
        raise DataHoundError(
            f"{path} has ragged rows: {len(ragged)} row(s) do not have the {len(header)} fields "
            f"the header declares ({shown}{more}). Fix those rows, or quote fields that contain "
            f"the delimiter."
        )
    if not rows:
        raise DataHoundError(f"{path} has a header row but no data rows")
    resolved = path.resolve()
    return Table(
        name=path.name,
        source=str(resolved),
        dataset=str(resolved),
        kind="csv",
        columns=header,
        rows=rows,
    )


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sqlite_cell(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return f"<blob {len(value)} bytes>"
    return str(value)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def list_sqlite_tables(path: str | Path) -> list[str]:
    path = Path(path)
    conn = _connect_readonly(path)
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
    except sqlite3.DatabaseError as exc:
        raise DataHoundError(f"{path} is not a readable SQLite database: {exc}") from None
    finally:
        conn.close()


def load_sqlite(path: str | Path, table: str) -> Table:
    path = Path(path)
    _check_file(path)
    names = list_sqlite_tables(path)
    if table not in names:
        available = ", ".join(names) if names else "none, the database has no tables"
        raise DataHoundError(f"table {table!r} does not exist in {path} (available: {available})")
    conn = _connect_readonly(path)
    try:
        cursor = conn.execute(f"SELECT * FROM {quote_ident(table)}")
        columns = [desc[0] for desc in cursor.description]
        rows = [[sqlite_cell(value) for value in row] for row in cursor]
    except sqlite3.DatabaseError as exc:
        raise DataHoundError(f"could not read table {table!r} from {path}: {exc}") from None
    finally:
        conn.close()
    if not rows:
        raise DataHoundError(f"table {table!r} in {path} has no rows")
    resolved = path.resolve()
    return Table(
        name=table,
        source=f"{resolved} (table {table})",
        dataset=f"{resolved}::{table}",
        kind="sqlite",
        columns=columns,
        rows=rows,
    )


def load(path: str | Path, table: str | None = None, delimiter: str | None = None) -> Table:
    """Pick the right loader: SQLite files are recognised by their header bytes, not the extension."""
    path = Path(path)
    _check_file(path)
    if is_sqlite_file(path):
        if not table:
            names = list_sqlite_tables(path)
            listing = ", ".join(names) if names else "none"
            raise DataHoundError(
                f"{path} is a SQLite database; pass --table NAME to pick a table (tables: {listing})"
            )
        return load_sqlite(path, table)
    if table:
        raise DataHoundError(f"--table only applies to SQLite databases, and {path} is not one")
    return load_csv(path, delimiter=delimiter)
