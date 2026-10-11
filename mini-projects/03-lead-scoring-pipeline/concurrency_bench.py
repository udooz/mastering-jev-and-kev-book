# mini-projects/03-lead-scoring-pipeline/concurrency_bench.py
from __future__ import annotations

import asyncio
import time
from typing import Any

from typesafe_sdk import AsyncTypeSafeClient

from composite_scorer import LEAD_QUESTIONS


async def one_call(c: AsyncTypeSafeClient, state: dict[str, Any]) -> tuple[float, int]:
    t0 = time.perf_counter()
    r = await c.system_one(state=state, questions=LEAD_QUESTIONS)
    return (time.perf_counter() - t0) * 1000, r.usage.input_tokens or 0


async def concurrent_calls(c: AsyncTypeSafeClient, state: dict[str, Any]) -> tuple[float, int]:
    t0 = time.perf_counter()
    rs = await asyncio.gather(*(c.system_one(state=state, questions={k: v})
                                for k, v in LEAD_QUESTIONS.items()))
    return (time.perf_counter() - t0) * 1000, sum(r.usage.input_tokens or 0 for r in rs)


async def main() -> None:
    state = {"email": {"body": "We're a 300-person fintech; need a quote to start this month."},
             "company": {"industry": "fintech", "employees_band": "200_500"},
             "ideal_customer": "Fintech or payments companies, 100-2000 employees"}
    async with AsyncTypeSafeClient() as c:
        for name, fn in (("fan-out", one_call), ("concurrent", concurrent_calls)):
            ms, tok = await fn(c, state)
            print(f"{name:10} {ms:7.0f} ms  {tok:5d} input tokens")


if __name__ == "__main__":
    asyncio.run(main())