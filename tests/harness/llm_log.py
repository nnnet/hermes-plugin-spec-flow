"""Debug logging for the live LLM adapters (decomposer / implementer).

During live calibration we must SEE every model call, not guess: this records
one JSONL line per call (node, depth, latency, return code, reply size, parse
outcome) to the file named by ``SPEC_FLOW_LLM_LOG``. Off when the env var is
unset, so production/tests pay nothing.

Analyse a run with: ``python3 tests/lib/analyze_llm_log.py <log.jsonl>``.
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
    """Append one structured event to the LLM debug log (no-op when unset).

    Each record carries TWO clocks: ``t`` is run-relative monotonic seconds
    (resume-safe, used for ordering/latency), ``wall`` is the real wall-clock
    EPOCH second. The trace stamps its rows in epoch too, so ``wall`` lets a
    consumer correlate an LLM call to the run timeline WITHOUT heuristics — the
    implement-stage milestones are written to the trace in a burst (one shared
    millisecond) after the call returns, so the trace gaps do not reflect when
    the model was actually working; ``wall`` on the call records does."""
    p = log_path()
    if p is None:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"t": round(time.monotonic() - _T0, 3),
           "wall": round(time.time(), 3), **event}
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_events(kinds: Any = None, *, node: Any = None) -> list:
    """Read back structured events from the LLM log (П7). Optional filters:
    ``kinds`` (an event name or iterable of names) and ``node``. Returns []
    when the log is unset or unreadable — a reader never breaks a run."""
    p = log_path()
    if p is None or not p.exists():
        return []
    if isinstance(kinds, str):
        kinds = {kinds}
    elif kinds is not None:
        kinds = set(kinds)
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if kinds is not None and rec.get("event") not in kinds:
                continue
            if node is not None and rec.get("node") != node:
                continue
            out.append(rec)
    except OSError:
        return []
    return out


#: keys whose value is large/free-text and must NOT bloat every call record
_BIG_META = {"prompt", "system", "text", "reply"}


# NOTE: the old `timed_ask` wrapper (which emitted call_start/call_ok/call_error
# around a model call) was REMOVED. Those events now emit from the SINGLE door —
# llm_backend.ask — so each LLM-call event has exactly one emission site. This
# module's `log`/`log_outcome` remain the generic structured-event sink used by
# the engine/stage telemetry (commits, file writes, gates, parsed outcomes),
# which is a different concern from the LLM call boundary.


def log_outcome(**fields: Any) -> None:
    """Log the parsed outcome of a call (leaf/branch + children, or impl files)."""
    log({"event": "outcome", **fields})
