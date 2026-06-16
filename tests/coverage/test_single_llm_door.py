"""SINGLE LLM DOOR — a structural invariant, verified over real source code.

The rule the user set: there is exactly ONE place that reaches an LLM and emits
the LLM-call telemetry — ``llm_backend``. This test reads the harness source
(no monkeypatch, no stub, no smoke — it asserts on the actual files) and fails
if any other module emits an LLM-call event. It is the deterministic guard that
the consolidation does not silently regress when someone adds a new worker.
"""
import pathlib
import re

_HARNESS = pathlib.Path(__file__).resolve().parents[1] / "harness"

# Events that describe a CALL TO A MODEL (request / response / per-attempt /
# fallback / token accounting). These must originate ONLY from llm_backend —
# the single door. NOT in this set: outcome/ws_write/commit_queue/gate events,
# which are engine/stage telemetry written by many modules on purpose.
_LLM_CALL_EVENTS = (
    "call_start", "call_ok", "call_error",
    "llm_attempt", "llm_fallback", "quota_wait", "error_round", "token_usage",
)


def _emitters(event: str) -> set:
    """Files under harness/ that emit ``{"event": "<event>"}``, excluding the
    single door itself."""
    pat = re.compile(r'"event":\s*"' + re.escape(event) + r'"')
    hits = set()
    for py in _HARNESS.glob("*.py"):
        if py.name == "llm_backend.py":
            continue
        if pat.search(py.read_text(encoding="utf-8")):
            hits.add(py.name)
    return hits


def test_llm_call_events_emit_only_from_the_single_door():
    leaks = {ev: sorted(_emitters(ev)) for ev in _LLM_CALL_EVENTS}
    leaks = {ev: who for ev, who in leaks.items() if who}
    assert not leaks, (
        "LLM-call telemetry must emit ONLY from llm_backend (the single door); "
        f"found emitters elsewhere: {leaks}")


def test_no_timed_ask_wrapper_remains():
    # the old llm_log.timed_ask wrapped a model call and emitted call_* OUTSIDE
    # the door — it was removed; assert it is not reintroduced.
    llm_log = (_HARNESS / "llm_log.py").read_text(encoding="utf-8")
    assert "def timed_ask(" not in llm_log, \
        "llm_log.timed_ask reintroduced — call_* must live only in llm_backend"
