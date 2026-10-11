"""Black-box probes for any System One backend (Jev, Kev, adapter) behind triage.DecisionClient.

Each probe turns an architectural claim from Appendix A into a test you can run on YOUR
backend, model version and region before you trust a threshold:

  order_sensitivity  - does reversing the option list move the winner's probability?
  sibling_isolation  - can one question see text placed in another question? (it shouldn't)
  irrelevant_option  - does adding an unrelated option change the odds between two others?
  fanout_latency     - is latency roughly flat as you add questions to one request?

DECISION_BACKEND=kev uv run python -m triage.probes
"""
from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from triage.jev_client import DecisionClient

TICKET = {"ticket": "The export button crashes the settings page in Safari. Works in Chrome."}
OPTIONS: dict[str, str] = {
    "technical": "Bugs, crashes, integration problems",
    "billing": "Charges, invoices, refunds",
    "sales": "Pricing, upgrades, new accounts",
    "other": "None of the above",
}


@dataclass(frozen=True, slots=True)
class ProbeResult:
    name: str
    value: float
    verdict: str
    detail: str


def choice_confidence(probs: Sequence[float]) -> float:
    """Confidence as reported for the official adapter: (p_max - 1/K) / (1 - 1/K).

    It measures how far the winner sits above uniform. It is NOT a learned estimate of correctness.
    """
    k = len(probs)
    if k <= 1:
        return 1.0
    return (max(probs) - 1 / k) / (1 - 1 / k)


def _choice(instr: str, criteria: Mapping[str, str | None]) -> dict[str, Any]:
    return {"type": "choice", "instructions": instr, "criteria": dict(criteria)}


def order_sensitivity(client: DecisionClient, repeats: int = 3, threshold: float = 0.9) -> ProbeResult:
    """Same evidence, options reversed. A shift across your threshold means the action can flip."""
    fwd, rev = [], []
    reversed_opts = dict(reversed(list(OPTIONS.items())))
    for _ in range(repeats):
        fwd.append(client.decide(TICKET, {"q": _choice("Which team should handle `ticket`?", OPTIONS)})
                   .choice("q").probabilities["technical"])
        rev.append(client.decide(TICKET, {"q": _choice("Which team should handle `ticket`?", reversed_opts)})
                   .choice("q").probabilities["technical"])
    a, b = statistics.fmean(fwd), statistics.fmean(rev)
    crosses = (a >= threshold) != (b >= threshold)
    return ProbeResult("order_sensitivity", round(abs(a - b), 3),
                       "RISK: crosses threshold" if crosses else "ok",
                       f"p(technical) forward={a:.3f} reversed={b:.3f} threshold={threshold}")


def sibling_isolation(client: DecisionClient) -> ProbeResult:
    """Put a secret in question A; ask question B about it. Isolated branches can't see it."""
    secret_q = {"type": "noul", "instructions": "The access code is PELICAN-42. Is the sky blue?"}
    ask = _choice("What is the access code mentioned anywhere in this request?",
                  {"PELICAN-42": None, "HERON-17": None, "not_stated": None})
    with_sibling = client.decide({"note": "routine check"}, {"a": secret_q, "b": ask}).choice("b")
    in_state = client.decide({"note": "The access code is PELICAN-42."}, {"b": ask}).choice("b")
    leak = with_sibling.probabilities.get("PELICAN-42", 0.0)
    return ProbeResult("sibling_isolation", round(leak, 3),
                       "ok: isolated" if leak < 0.5 else "UNEXPECTED: sibling visible",
                       f"p(code | sibling)={leak:.2f}  p(code | state)={in_state.probabilities.get('PELICAN-42', 0):.2f}")


def irrelevant_option(client: DecisionClient) -> ProbeResult:
    """Independent per-option scores would keep log-odds(technical:billing) fixed when a 5th option appears."""
    def log_odds(opts: Mapping[str, str]) -> float:
        p = client.decide(TICKET, {"q": _choice("Which team should handle `ticket`?", opts)}).choice("q").probabilities
        return math.log(max(p["technical"], 1e-6) / max(p["billing"], 1e-6))
    base = log_odds(OPTIONS)
    extra = log_odds({**OPTIONS, "facilities": "Office buildings, parking, cleaning"})
    return ProbeResult("irrelevant_option", round(extra - base, 3),
                       "options interact" if abs(extra - base) > 0.05 else "independent",
                       f"log-odds technical:billing {base:+.2f} -> {extra:+.2f}")


def fanout_latency(client: DecisionClient, sizes: Sequence[int] = (1, 10, 50, 100), repeats: int = 3) -> ProbeResult:
    """Questions are evaluated in parallel over a shared state: latency should stay nearly flat."""
    med: dict[int, float] = {}
    for n in sizes:
        qs = {f"q{i}": {"type": "noul", "instructions": f"Does `ticket` mention item number {i}?"} for i in range(n)}
        runs = []
        for _ in range(repeats):
            t0 = time.perf_counter()
            client.decide(TICKET, qs)
            runs.append((time.perf_counter() - t0) * 1000)
        med[n] = statistics.median(runs)
    ratio = med[sizes[-1]] / max(med[sizes[0]], 1.0)       # 1 ms floor: sub-ms fakes are noise
    return ProbeResult("fanout_latency", round(ratio, 2),
                       "flat-ish" if ratio < 3 else "grows with questions",
                       "  ".join(f"{n}q={ms:.0f}ms" for n, ms in med.items()))


def run_all(client: DecisionClient) -> list[ProbeResult]:
    return [order_sensitivity(client), sibling_isolation(client), irrelevant_option(client), fanout_latency(client)]


if __name__ == "__main__":
    from triage.jev_client import client_from_env

    c = client_from_env()
    print(f"backend={c.backend}")
    for r in run_all(c):
        print(f"{r.name:18} {r.value:>7}  {r.verdict:24} {r.detail}")
