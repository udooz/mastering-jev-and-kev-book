"""Static, dependency-free HTML dashboard for the SmartPlant benchmark."""
from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from smartplant.benchmark_metrics import EngineMetrics
from smartplant.scenario_runner import ScenarioResult

_CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#656d76;--line:#d0d7de;--ok:#1a7f37;--bad:#cf222e;--warn:#9a6700;--bar:#0969da}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--line:#30363d;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--bar:#4493f8}}
body{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px;max-width:1100px;margin-inline:auto}
h1{font-size:22px;margin:0 0 4px}p.sub{color:var(--muted);margin:0 0 24px}
table{border-collapse:collapse;width:100%;margin:12px 0 28px;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600}.ok{color:var(--ok)}.bad{color:var(--bad);font-weight:600}.warn{color:var(--warn)}
.wrap{overflow-x:auto}small{color:var(--muted)}
"""

_COLUMNS = [("agreement", "Agreement ↑", True), ("critical_miss_rate", "Critical miss ↓", False),
            ("unnecessary_intervention_rate", "Unnecessary intervention ↓", False),
            ("response_validity", "Valid responses ↑", True), ("escalation_quality", "Escalation quality ↑", True),
            ("fallback_success", "Fallback success ↑", True), ("audit_completeness", "Audit complete ↑", True)]


def _bar(value: float | None, good_high: bool) -> str:
    if value is None:
        return '<small>n/a</small>'
    w = int(max(0.0, min(1.0, value)) * 80)
    cls = "ok" if (value >= 0.9 if good_high else value <= 0.05) else "bad" if (value < 0.6 if good_high else value > 0.2) else "warn"
    return (f'<svg width="84" height="10" role="img" aria-label="{value:.0%}"><rect width="80" height="8" y="1" rx="2" '
            f'fill="currentColor" opacity=".12"/><rect width="{w}" height="8" y="1" rx="2" fill="var(--bar)"/></svg> '
            f'<span class="{cls}">{value:.0%}</span>')


def render(metrics: Sequence[EngineMetrics], results: Sequence[ScenarioResult],
           scenarios: Sequence[Mapping[str, Any]], title: str = "SmartPlant decision benchmark") -> str:
    engines = [m.engine for m in metrics]
    head = "".join(f"<th>{h}</th>" for _, h, _ in _COLUMNS)
    rows = "".join(
        f"<tr><td><b>{html.escape(m.engine)}</b><br><small>n={m.n}</small></td>"
        + "".join(f"<td>{_bar(getattr(m, k), up)}</td>" for k, _, up in _COLUMNS)
        + f"<td>{m.p50_ms:.0f} / {m.p95_ms:.0f} ms</td><td>${m.cost_per_state_usd:.6f}</td></tr>"
        for m in metrics)
    cell = {(r.scenario_id, r.engine): r for r in results}
    grid = ""
    for s in scenarios:
        exp = s["expected"]
        tds = ""
        for e in engines:
            r = cell.get((s["id"], e))
            if r is None:
                tds += "<td>–</td>"
                continue
            rec = r.recommendation
            hit = rec.action in exp["actions"] and rec.urgency in exp["urgencies"]
            miss = exp["critical"] and rec.urgency not in ("URGENT", "CRITICAL")
            cls = "bad" if miss else "ok" if hit else "warn"
            tds += (f'<td class="{cls}">{rec.action}<br><small>{rec.urgency} · sev {rec.severity} · '
                    f'{rec.route.replace("_", " ")} · {rec.source}</small></td>')
        grid += (f"<tr><td><b>{s['id']}</b> {html.escape(s['name'])}<br><small>expect "
                 f"{'/'.join(exp['actions'])} @ {'/'.join(exp['urgencies'])}</small></td>{tds}</tr>")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>{_CSS}</style></head><body>
<h1>{html.escape(title)}</h1><p class="sub">Generated {stamp}. Same states, same contract, same policy for every engine.
Measured results only; an offline stand-in measures the pipeline, not a model.</p>
<h2>Engine metrics</h2><div class="wrap"><table><tr><th>Engine</th>{head}<th>p50 / p95</th><th>Cost / state</th></tr>{rows}</table></div>
<h2>Scenario matrix</h2><p class="sub"><span class="ok">green</span> = matches expert label,
<span class="warn">amber</span> = disagrees with the expert label, <span class="bad">red</span> = critical case under-escalated.</p>
<div class="wrap"><table><tr><th>Scenario</th>{''.join(f'<th>{html.escape(e)}</th>' for e in engines)}</tr>{grid}</table></div>
</body></html>"""


def write(path: str | Path, metrics: Sequence[EngineMetrics], results: Sequence[ScenarioResult],
          scenarios: Sequence[Mapping[str, Any]]) -> Path:
    p = Path(path)
    p.write_text(render(metrics, results, scenarios), encoding="utf-8")
    return p
