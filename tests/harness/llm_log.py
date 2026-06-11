"""Debug logging for the live LLM adapters (decomposer / implementer).

During live calibration we must SEE every model call, not guess: this records
one JSONL line per call (node, depth, latency, return code, reply size, parse
outcome) to the file named by ``SPEC_FLOW_LLM_LOG``. Off when the env var is
unset, so production/tests pay nothing.

Analyse a run with: ``python3 tests/analyze_llm_log.py <log.jsonl>``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

# process start, so timestamps are seconds-since-run (monotonic, resume-safe)
_T0 = time.monotonic()


def log_path() -> Optional[Path]:
    p = os.environ.get("SPEC_FLOW_LLM_LOG")
    return Path(p) if p else None


def log(event: dict) -> None:
    """Append one structured event to the LLM debug log (no-op when unset)."""
    p = log_path()
    if p is None:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"t": round(time.monotonic() - _T0, 3), **event}
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def timed_ask(ask: Callable[[str], str], *, role: str, node: str, depth: Any,
              model: str, prompt: str) -> str:
    """Run ``ask(prompt)``, timing it and logging the call (and any failure).

    Logs BEFORE returning so a hang/slow call is visible the moment it ends;
    on exception logs the error and re-raises."""
    t0 = time.monotonic()
    log({"event": "call_start", "role": role, "node": node, "depth": depth,
         "model": model, "prompt_chars": len(prompt)})
    try:
        reply = ask(prompt)
    except Exception as exc:  # noqa: BLE001
        log({"event": "call_error", "role": role, "node": node, "depth": depth,
             "latency_s": round(time.monotonic() - t0, 2), "error": repr(exc)[:300]})
        raise
    log({"event": "call_ok", "role": role, "node": node, "depth": depth,
         "latency_s": round(time.monotonic() - t0, 2), "reply_chars": len(reply)})
    return reply


def log_outcome(**fields: Any) -> None:
    """Log the parsed outcome of a call (leaf/branch + children, or impl files)."""
    log({"event": "outcome", **fields})
