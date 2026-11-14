"""Small formatting helpers shared by the rules, drift and report modules."""

from __future__ import annotations


def pct(share: float) -> str:
    """0.125 -> '12.5%'."""
    return f"{share * 100:.1f}%"


def rate(count: int, total: int, unit: str = "rows") -> str:
    """Every rate DataHound prints carries the counts behind it: '12.5% (5 of 40 rows)'."""
    share = count / total if total else 0.0
    return f"{pct(share)} ({count} of {total} {unit})"


def fmt_num(value) -> str:
    """Compact, stable rendering for stats: integers stay whole, floats get 2 decimals."""
    if value is None:
        return "n/a"
    if isinstance(value, str):
        return value
    value = float(value)
    if value != value:  # NaN guard, never expected but never crash on it
        return "n/a"
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    if abs(value) < 1:
        return f"{value:.4g}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def quote_value(value: str, limit: int = 40) -> str:
    """Show a cell value in a message, trimmed so one bad cell cannot flood the terminal."""
    text = value if len(value) <= limit else value[: limit - 3] + "..."
    return repr(text)
