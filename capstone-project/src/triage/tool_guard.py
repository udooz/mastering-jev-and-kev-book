# capstone-project/src/triage/tool_guard.py
from __future__ import annotations

import functools
import json
from typing import Any, Callable, Final, ParamSpec, TypeVar

from triage.jev_client import DecisionClient, DecisionError

P = ParamSpec("P")
R = TypeVar("R")

GUARD_QUESTIONS: Final = {
    "matches_user_goal": {"type": "noul",
                          "instructions": "Does calling `tool` with `args` directly serve `user_goal`?"},
    "harm": {"type": "score", "instructions": "What is the worst outcome if `tool` runs with `args`?",
             "criteria": ["Nothing changes outside this conversation",
                          "A reversible change to the user's own data",
                          "Money moves, messages are sent, or data is deleted"]},
}


class ToolCallBlocked(RuntimeError):
    pass


def guarded(client: DecisionClient, tool_name: str, get_goal: Callable[[], str],
            max_harm: float = 1.2, min_match: float = 0.8) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Wrap any tool function (plain, LangChain @tool body, MCP handler) with a Jev pre-check."""
    def deco(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            state = {"tool": tool_name, "args": json.loads(json.dumps(kwargs, default=str)),
                     "user_goal": get_goal()}
            try:
                r = client.decide(state, GUARD_QUESTIONS)
            except DecisionError as e:
                raise ToolCallBlocked(f"guard unavailable: {e}") from e
            harm, match = r.score("harm").score, r.noul("matches_user_goal").p_yes
            if harm > max_harm or match < min_match:
                raise ToolCallBlocked(f"{tool_name}: harm={harm:.2f} goal_match={match:.2f} -> needs approval")
            return fn(*args, **kwargs)
        return wrapper
    return deco
