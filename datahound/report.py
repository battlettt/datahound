"""Terminal summary and a self-contained HTML report (inline CSS and SVG, no external assets)."""

from __future__ import annotations

import html
import os

from .rules import worst_first
from .util import fmt_num, pct, rate

# ---------------------------------------------------------------- terminal

ANSI = {"red": "\033[31m", "yellow": "\033[33m", "green": "\033[32m", "cyan": "\033[36m", "bold": "\033[1m", "dim": "\033[2m"}
RESET = "\033[0m"


def use_color(stream) -> bool:
    return hasattr(stream, "isatty") and stream.isatty() and "NO_COLOR" not in os.environ


def terminal_summary(ctx: dict, color: bool = False, max_problems: int = 15) -> str:
    def paint(text, style):
        return f"{ANSI[style]}{text}{RESET}" if color else text

    profile = ctx["profile"]
    results = ctx["results"]
    failed = worst_first(results)
    flags = ctx["drift"]
    lines = []
    lines.append(
        paint("DataHound", "bold")
        + f"  {ctx['table_name']}  {profile['rows']} rows x {profile['column_count']} columns"
    )
    lines.append(paint(f"source: {ctx['source']}", "dim"))
    passed = len(results) - len(failed)
    errors = sum(1 for r in failed if r.severity == "error")
    warnings = len(failed) - errors
    verdict = paint("FAIL", "red") if errors else (paint("WARN", "yellow") if warnings else paint("PASS", "green"))
    prev = ctx.get("previous")
    against = f" vs run #{prev['id']} ({prev['created_at']})" if prev else " (first run, nothing to compare)"
    lines.append(
        f"{verdict}  rules: {len(failed)} failed ({errors} errors, {warnings} warnings), {passed} passed"
        f"   drift: {len(flags)} flag(s){against}"
    )
    lines.append("")

    if failed:
        lines.append(paint("Problems, worst first", "bold"))
        width = max(len(r.rule) for r in failed[:max_problems])
        for result in failed[:max_problems]:
            tag = paint("ERROR", "red") if result.severity == "error" else paint("WARN ", "yellow")
            where = result.column or "(table)"
            lines.append(f"  {tag}  {result.rule.ljust(width)}  {where}: {result.message}")
            if result.examples:
                lines.append(paint("         e.g. " + "; ".join(result.examples[:3]), "dim"))
        if len(failed) > max_problems:
            lines.append(f"  ... and {len(failed) - max_problems} more in the HTML report")
        lines.append("")
    else:
        lines.append(paint("No rule failures.", "green"))
        lines.append("")

    if flags:
        lines.append(paint("Drift since the previous run", "bold"))
        for flag in flags:
            lines.append(f"  {paint('DRIFT', 'cyan')}  {flag.message}")
        lines.append("")

    lines.append(paint("Columns", "bold"))
    names = [c["name"] for c in profile["columns"]]
    width = min(max(len(n) for n in names), 24)
    lines.append(f"  {'column'.ljust(width)}  {'type'.ljust(8)}  {'null'.rjust(7)}  {'empty'.rjust(7)}  {'unique'.rjust(7)}")
    for col in profile["columns"]:
        name = col["name"] if len(col["name"]) <= width else col["name"][: width - 3] + "..."
        lines.append(
            f"  {name.ljust(width)}  {col['type'].ljust(8)}  {pct(col['null_rate']).rjust(7)}  "
            f"{pct(col['empty_rate']).rjust(7)}  {str(col['unique_count']).rjust(7)}"
        )
    lines.append(
        f"  duplicate rows: {rate(profile['duplicate_row_count'], profile['rows'])}"
    )
    lines.append("")
    if ctx.get("report_path"):
        lines.append(f"report:  {ctx['report_path']}")
    if ctx.get("store_path"):
        lines.append(f"history: {ctx['store_path']} (run #{ctx['run_id']})")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- html

E = html.escape

CSS = """
:root{--bg:#f6f4ef;--panel:#fffdf8;--ink:#1d1b17;--muted:#6b665c;--line:#e2ddd2;--accent:#b4441c;
--err:#b42318;--err-bg:#fdecea;--warn:#9a6700;--warn-bg:#fff4d6;--ok:#1f7a3f;--ok-bg:#e6f4ea;
--drift:#1d5fa8;--drift-bg:#e7f0fb;--bar:#d9d3c6;--bar-fill:#b4441c;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#15140f;--panel:#1e1c17;--ink:#ece7dc;--muted:#a49d8f;
--line:#34312a;--accent:#f08a5d;--err:#ff8a80;--err-bg:#3a1714;--warn:#f2c14e;--warn-bg:#352a0e;
--ok:#7bd88f;--ok-bg:#12301c;--drift:#8ab8f0;--drift-bg:#142338;--bar:#3a372f;--bar-fill:#f08a5d}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--sans)}
main{max-width:1120px;margin:0 auto;padding:32px 16px 64px}
header{border-bottom:2px solid var(--ink);padding-bottom:16px;margin-bottom:24px}
h1{font-size:30px;margin:0 0 4px;letter-spacing:-.02em}
h1 span{color:var(--accent)}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.12em;margin:36px 0 12px;color:var(--muted)}
.sub{color:var(--muted);font-size:13px;overflow-wrap:anywhere}
.num,code,td.n{font-family:var(--mono);font-variant-numeric:tabular-nums}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.stat b{display:block;font:600 24px/1.2 var(--mono)}
.stat small{color:var(--muted);font-size:12px}
.verdict{display:inline-block;font:700 12px var(--mono);padding:2px 8px;border-radius:4px;margin-left:8px;vertical-align:middle}
.v-fail{background:var(--err-bg);color:var(--err)}.v-warn{background:var(--warn-bg);color:var(--warn)}.v-pass{background:var(--ok-bg);color:var(--ok)}
.table-wrap{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600}
tr:last-child td{border-bottom:0}
.sev{font:700 11px var(--mono);padding:1px 6px;border-radius:3px;white-space:nowrap}
.sev-error{background:var(--err-bg);color:var(--err)}.sev-warning{background:var(--warn-bg);color:var(--warn)}
.sev-drift{background:var(--drift-bg);color:var(--drift)}
ul.ex{margin:4px 0 0;padding-left:18px;color:var(--muted);font:12px/1.5 var(--mono)}
.trend{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.trend figure{margin:0;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.trend figcaption{font-size:12px;color:var(--muted);display:flex;justify-content:space-between}
svg.spark{display:block;width:100%;height:auto;margin-top:6px}
svg .ln{fill:none;stroke:var(--accent);stroke-width:2}
svg .dot{fill:var(--accent)}
svg .base{stroke:var(--line);stroke-width:1}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px 16px;border-top:4px solid var(--ok)}
.card.has-error{border-top-color:var(--err)}.card.has-warning{border-top-color:var(--warn)}
.card h3{margin:0;font:600 16px var(--mono);word-break:break-all}
.card .type{font:12px var(--mono);color:var(--muted)}
dl{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin:10px 0 0;font-size:13px}
dt{color:var(--muted)}dd{margin:0;font-family:var(--mono);word-break:break-word}
.meter{height:6px;background:var(--bar);border-radius:3px;margin-top:3px;overflow:hidden}
.meter i{display:block;height:100%;background:var(--bar-fill)}
.tops{margin:10px 0 0;padding:0;list-style:none;font:12px var(--mono)}
.tops li{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;margin-top:4px}
.tops li span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tops .meter{grid-column:1/-1;margin-top:0}
.badges{margin-top:8px;display:flex;flex-wrap:wrap;gap:4px}
details{margin-top:12px}summary{cursor:pointer;color:var(--muted);font-size:13px}
footer{margin-top:48px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:12px}
.empty{color:var(--muted);font-size:14px;background:var(--panel);border:1px dashed var(--line);border-radius:8px;padding:14px}
"""


def sparkline(values: list[float], label: str, fmt=fmt_num, width: int = 240, height: int = 48) -> str:
    """Inline SVG trend line. One point renders as a single dot."""
    values = [float(v) for v in values]
    pad = 5
    title = E(f"{label}: " + ", ".join(fmt(v) for v in values))
    if not values:
        return ""
    low, high = min(values), max(values)
    span = high - low

    def y(v):
        if span == 0:
            return height / 2
        return height - pad - (v - low) / span * (height - 2 * pad)

    if len(values) == 1:
        xs = [width / 2]
    else:
        xs = [pad + i * (width - 2 * pad) / (len(values) - 1) for i in range(len(values))]
    points = " ".join(f"{x:.1f},{y(v):.1f}" for x, v in zip(xs, values))
    line = f'<polyline class="ln" points="{points}"/>' if len(values) > 1 else ""
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" role="img" aria-label="{title}">'
        f"<title>{title}</title>"
        f'<line class="base" x1="0" y1="{height - 1}" x2="{width}" y2="{height - 1}"/>'
        f"{line}"
        f'<circle class="dot" cx="{xs[-1]:.1f}" cy="{y(values[-1]):.1f}" r="3.5"/>'
        f"</svg>"
    )


def _meter(share: float) -> str:
    width = max(0.0, min(1.0, share)) * 100
    return f'<div class="meter" aria-hidden="true"><i style="width:{width:.1f}%"></i></div>'


def _column_card(col: dict, failures: list, trend: list[dict]) -> str:
    worst = "has-error" if any(r.severity == "error" for r in failures) else ("has-warning" if failures else "")
    declared = " (declared)" if col["declared_type"] else ""
    rows = col["rows"]
    parts = [f'<article class="card {worst}">']
    parts.append(f'<h3>{E(col["name"])}</h3><div class="type">{E(col["type"])}{declared}</div>')
    parts.append("<dl>")
    parts.append(f"<dt>null</dt><dd>{E(rate(col['null_count'], rows))}{_meter(col['null_rate'])}</dd>")
    parts.append(f"<dt>empty</dt><dd>{E(rate(col['empty_count'], rows))}</dd>")
    parts.append(f"<dt>unique</dt><dd>{col['unique_count']} of {col['present_count']} values</dd>")
    if col["invalid_count"]:
        parts.append(
            f"<dt>invalid</dt><dd>{E(rate(col['invalid_count'], col['present_count'], 'values'))}</dd>"
        )
    if col["min"] is not None:
        parts.append(f"<dt>min</dt><dd>{E(fmt_num(col['min']))}</dd><dt>max</dt><dd>{E(fmt_num(col['max']))}</dd>")
    if col["mean"] is not None:
        parts.append(f"<dt>mean</dt><dd>{E(fmt_num(col['mean']))}</dd>")
    parts.append("</dl>")
    if col["top_values"]:
        parts.append('<ul class="tops" aria-label="top values">')
        for value, count in col["top_values"]:
            share = count / col["present_count"] if col["present_count"] else 0
            parts.append(
                f'<li><span title="{E(value)}">{E(value)}</span><b>{count}</b>{_meter(share)}</li>'
            )
        parts.append("</ul>")
    nulls = [run["metrics"].get((col["name"], "null_rate")) for run in trend]
    nulls = [v for v in nulls if v is not None]
    if len(nulls) > 1:
        parts.append(
            f'<div class="type" style="margin-top:10px">null rate over {len(nulls)} runs</div>'
            + sparkline(nulls, f"{col['name']} null rate", fmt=pct, height=32)
        )
    if failures:
        parts.append('<div class="badges">')
        for r in failures:
            parts.append(f'<span class="sev sev-{E(r.severity)}">{E(r.rule)}</span>')
        parts.append("</div>")
    parts.append("</article>")
    return "".join(parts)


def _rule_rows(results) -> str:
    out = []
    for r in results:
        examples = "".join(f"<li>{E(x)}</li>" for x in r.examples)
        ex_html = f'<ul class="ex">{examples}</ul>' if examples else ""
        sev = r.severity if not r.passed else "pass"
        badge = f'<span class="sev sev-{E(sev)}">{E(sev.upper())}</span>' if not r.passed else '<span class="sev v-pass">PASS</span>'
        out.append(
            f"<tr><td>{badge}</td><td><code>{E(r.rule)}</code></td><td><code>{E(r.column or '(table)')}</code></td>"
            f"<td>{E(r.message)}{ex_html}</td><td class=\"n\">{E(r.threshold)}</td></tr>"
        )
    return "".join(out)


def render_html(ctx: dict) -> str:
    profile = ctx["profile"]
    results = ctx["results"]
    failed = worst_first(results)
    flags = ctx["drift"]
    trend = ctx.get("trend") or []
    errors = sum(1 for r in failed if r.severity == "error")
    verdict = ("FAIL", "v-fail") if errors else (("WARN", "v-warn") if failed else ("PASS", "v-pass"))
    prev = ctx.get("previous")

    by_column: dict[str, list] = {}
    for r in failed:
        by_column.setdefault(r.column, []).append(r)

    h = []
    h.append("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">")
    h.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    h.append(f"<title>DataHound report: {E(ctx['table_name'])}</title><style>{CSS}</style></head><body><main>")
    h.append(
        f'<header><h1>Data<span>Hound</span> report <span class="verdict {verdict[1]}">{verdict[0]}</span></h1>'
        f'<div class="sub">{E(ctx["table_name"])} &middot; {E(ctx["source"])}</div>'
        f'<div class="sub">generated {E(ctx["created_at"])} &middot; run #{E(str(ctx.get("run_id", "-")))}'
        + (f" &middot; compared with run #{prev['id']} from {E(prev['created_at'])}" if prev else " &middot; first run, no comparison yet")
        + "</div></header>"
    )

    h.append('<section class="stats" aria-label="summary">')
    h.append(f'<div class="stat"><b>{profile["rows"]}</b><small>rows, {profile["column_count"]} columns</small></div>')
    h.append(f'<div class="stat"><b>{len(failed)}</b><small>rules failed of {len(results)} ({errors} errors)</small></div>')
    h.append(f'<div class="stat"><b>{len(flags)}</b><small>drift flags vs previous run</small></div>')
    h.append(
        f'<div class="stat"><b>{pct(profile["duplicate_row_rate"])}</b><small>duplicate rows '
        f'({profile["duplicate_row_count"]} of {profile["rows"]})</small></div>'
    )
    h.append("</section>")

    h.append("<h2>Trend</h2>")
    if trend:
        rows_series = [run["row_count"] for run in trend]
        fail_series = [run["rules_failed"] for run in trend]
        drift_series = [run["drift_flags"] for run in trend]
        h.append('<section class="trend">')
        for label, series in (("rows", rows_series), ("rules failed", fail_series), ("drift flags", drift_series)):
            h.append(
                f"<figure><figcaption><span>{label}</span><span class=\"num\">{E(fmt_num(series[-1]))} now"
                f"{', ' + E(fmt_num(series[0])) + ' first' if len(series) > 1 else ''}</span></figcaption>"
                f"{sparkline(series, label)}</figure>"
            )
        h.append("</section>")
        h.append(f'<p class="sub">{len(trend)} run(s) of this dataset in the history store, oldest on the left.</p>')
    else:
        h.append('<p class="empty">No history yet. Run again to start a trend.</p>')

    h.append("<h2>Failed rules, worst first</h2>")
    if failed:
        h.append('<div class="table-wrap"><table><thead><tr><th>severity</th><th>rule</th><th>column</th>'
                 "<th>finding and examples</th><th>threshold</th></tr></thead><tbody>")
        h.append(_rule_rows(failed))
        h.append("</tbody></table></div>")
    else:
        h.append('<p class="empty">Every rule passed.</p>')

    h.append("<h2>Drift since the previous run</h2>")
    if flags:
        h.append('<div class="table-wrap"><table><thead><tr><th></th><th>metric</th><th>column</th><th>old</th>'
                 "<th>new</th><th>change</th><th>threshold</th></tr></thead><tbody>")
        for f in flags:
            h.append(
                f'<tr><td><span class="sev sev-drift">DRIFT</span></td><td><code>{E(f.metric)}</code></td>'
                f"<td><code>{E(f.column or '(table)')}</code></td><td class=\"n\">{E(str(f.old))}</td>"
                f"<td class=\"n\">{E(str(f.new))}</td><td class=\"n\">{E(f.change)}</td>"
                f"<td class=\"n\">{E(f.threshold)}</td></tr>"
            )
        h.append("</tbody></table></div>")
    elif prev:
        h.append('<p class="empty">No metric moved past its threshold.</p>')
    else:
        h.append('<p class="empty">This is the first run of this dataset, so there is nothing to compare yet.</p>')

    table_level = by_column.get(None, [])
    h.append("<h2>Columns</h2>")
    h.append('<section class="cards">')
    for col in profile["columns"]:
        h.append(_column_card(col, by_column.get(col["name"], []), trend))
    h.append("</section>")
    if table_level:
        h.append('<p class="sub">Table-level failures: ' + ", ".join(E(r.rule) for r in table_level) + "</p>")

    passed = [r for r in results if r.passed]
    if passed:
        h.append(f"<details><summary>{len(passed)} passed rules</summary>")
        h.append('<div class="table-wrap"><table><tbody>' + _rule_rows(passed) + "</tbody></table></div></details>")

    h.append(
        "<footer>Generated locally by DataHound. Every number above is recomputable from the input; "
        "no data left this machine.</footer></main></body></html>"
    )
    return "".join(h)
