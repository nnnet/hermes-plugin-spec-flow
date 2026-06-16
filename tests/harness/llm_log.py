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


def timed_ask(ask: Callable[[str], str], *, prompt: str, meta: Optional[dict] = None,
              **legacy: Any) -> str:
    """THE one door for logging an LLM call — universal, role-independent.

    Wrap any model invocation: it times the call and writes a ``call_start``
    (the request, with its full descriptive context) and a ``call_ok`` /
    ``call_error`` (the outcome). The descriptive context is OPEN SCHEMA — pass
    a ``meta`` dict with whatever characterises the call: who/role, why/purpose,
    node, specialty, mode (solo vs orchestra), attempt, model, … New keys need
    no change here or downstream; the analysis discovers them from the data.

    ``meta`` SHOULD carry at least ``role`` and ``model`` (the request marker
    every consumer keys on). Legacy callers may still pass role=/node=/depth=/
    model= as keywords; they are merged into meta for backward compatibility."""
    ctx = {k: v for k, v in (meta or {}).items() if k not in _BIG_META}
    for k, v in legacy.items():
        ctx.setdefault(k, v)
    ctx["prompt_chars"] = len(prompt)
    t0 = time.monotonic()
    log({"event": "call_start", **ctx})
    # the outcome echoes the identity keys (role/node) but NOT `model`, so the
    # call counts exactly once (the request, via call_start) — the outcome is a
    # completion record, excluded from request counts.
    ident = {k: ctx[k] for k in ("role", "node", "depth", "purpose", "mode")
             if k in ctx}
    try:
        reply = ask(prompt)
    except Exception as exc:  # noqa: BLE001
        log({"event": "call_error", **ident,
             "latency_s": round(time.monotonic() - t0, 2),
             "error": repr(exc)[:300]})
        raise
    log({"event": "call_ok", **ident,
         "latency_s": round(time.monotonic() - t0, 2),
         "reply_chars": len(reply)})
    return reply


def log_outcome(**fields: Any) -> None:
    """Log the parsed outcome of a call (leaf/branch + children, or impl files)."""
    log({"event": "outcome", **fields})
