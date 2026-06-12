"""Commit queue: ONE writer mutates the shared workspace at a time.

v17 post-mortem: under parallel subtrees the workspace had concurrent
writers — leaf write+bar, integrate repair write+suite+rollback, and the
CLI implementer with raw fs access. Interleavings produced a module/test
pair no single writer ever wrote (src/stripe_integration.py exporting
handle_stripe_webhook while its test imported webhook_stripe), and every
repair of the broken pair rolled back. The queue makes each commit
ATOMIC: baseline capture, write and verification happen under one lock.

Every pass through the queue is journaled (event=commit_queue in
llm-log.jsonl) with wait and hold times — when the safeguard matters,
the log shows it (wait_s > 0 means a prevented interleaving).
"""
from __future__ import annotations

import threading
import time

# re-entrant: a holder may run nested helpers that also enter the queue
_LOCK = threading.RLock()
_WAIT_LOG_MIN_S = 0.05      # don't spam the journal with free acquisitions


class exclusive:
    """``with exclusive("leaf:cart_api", "write+bar"):`` — one commit."""

    def __init__(self, holder: str, why: str):
        self.holder, self.why = holder, why
        self._t_wait = 0.0
        self._t_hold = 0.0

    def __enter__(self):
        t0 = time.time()
        _LOCK.acquire()
        self._t_wait = time.time() - t0
        self._t_hold = time.time()
        if self._t_wait >= _WAIT_LOG_MIN_S:
            _log({"op": "acquired_after_wait", "holder": self.holder,
                  "why": self.why, "wait_s": round(self._t_wait, 2)})
        return self

    def __exit__(self, exc_type, exc, tb):
        held = time.time() - self._t_hold
        _LOCK.release()
        _log({"op": "released", "holder": self.holder, "why": self.why,
              "wait_s": round(self._t_wait, 2), "held_s": round(held, 2),
              "error": bool(exc_type)})
        return False


def _log(payload: dict) -> None:
    try:
        from . import llm_log
        llm_log.log({"event": "commit_queue", **payload})
    except Exception:               # noqa: BLE001 — logging never blocks work
        pass
