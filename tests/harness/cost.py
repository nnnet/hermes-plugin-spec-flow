"""Cost meter for live runs (plan step 1.2).

Wraps the injectable LLM agents (decomposer / implementer) so a run records how
many model calls it made and — best-effort — how many tokens. Call counts are
the reliable cost proxy (one call ≈ one node / one leaf); token totals are
filled only when the backend reports usage. ``write_cost_md`` renders the
COST.md that lands inside the run workspace.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable


class Meter:
    """Counts agent calls (and tokens if available) per role."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.tokens: dict[str, int] = {}

    def wrap(self, role: str, fn: Callable) -> Callable:
        """Return ``fn`` wrapped so every invocation bumps the counter for
        ``role``. A returned dict/object carrying ``_tokens`` is summed in."""
        self.calls.setdefault(role, 0)
        self.tokens.setdefault(role, 0)

        def wrapped(*args: Any, **kw: Any):
            self.calls[role] += 1
            out = fn(*args, **kw)
            tok = None
            if isinstance(out, dict):
                tok = out.get("_tokens")
            tok = tok if isinstance(tok, int) else getattr(out, "_tokens", None)
            if isinstance(tok, int):
                self.tokens[role] += tok
            return out

        return wrapped

    def total_calls(self) -> int:
        return sum(self.calls.values())

    def total_tokens(self) -> int:
        return sum(self.tokens.values())

    def report(self) -> dict:
        return {"calls": dict(self.calls), "tokens": dict(self.tokens),
                "total_calls": self.total_calls(),
                "total_tokens": self.total_tokens()}


def write_cost_md(meter: Meter, path: str, *, model: str = "", case: str = "") -> None:
    """Render the per-run cost report from a Meter."""
    rep = meter.report()
    has_tokens = rep["total_tokens"] > 0
    lines = ["# Cost — live run", ""]
    if case:
        lines.append(f"**Case:** {case}")
    if model:
        lines.append(f"**Model:** `{model}`")
    lines += ["", "| Role | LLM calls" + (" | Tokens" if has_tokens else "") + " |",
              "|---|---:" + ("|---:" if has_tokens else "") + "|"]
    for role in sorted(set(rep["calls"]) | set(rep["tokens"])):
        row = f"| {role} | {rep['calls'].get(role, 0)}"
        if has_tokens:
            row += f" | {rep['tokens'].get(role, 0)}"
        lines.append(row + " |")
    total = f"| **total** | **{rep['total_calls']}**"
    if has_tokens:
        total += f" | **{rep['total_tokens']}**"
    lines += [total + " |", ""]
    if not has_tokens:
        lines += ["> Token usage not reported by the backend — call count is the "
                  "cost proxy (≈ one call per tree node / per leaf).", ""]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    Path(path).with_suffix(".json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
