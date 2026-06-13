"""Intention board (variants B + C of the anti-duplication axis).

Variant A gives every node a UNIQUE module name, so two leaves never
overwrite the same src file. But duplication also happens LOGICALLY: two
leaves implement the SAME functionality in DIFFERENT modules — paying two
LLM implementations for one feature. The board catches that:

  * C — each leaf declares its INTENT; the board keys claims on a stable
    content-hash of that intent (title + normalised requirement).
  * B — before implementing, a leaf claims its intent-hash. If an EARLIER
    leaf already finished the SAME intent, this leaf is a cache-hit: it
    re-exports the owner's module instead of re-implementing (no second
    LLM call). A fresh intent is granted and built normally.

Concurrency stance (deadlock-free): a hash seen as still IN-PROGRESS by
another node is treated as fresh (proceed) rather than blocking — better
to occasionally double-build than to wedge the thread pool. Only a DONE
owner triggers the cache-hit. Single-writer via commit_queue; every
decision is logged (event=claim).
"""
from __future__ import annotations

import hashlib
import re
import threading
from typing import Callable, Optional

from . import commit_queue


def intent_hash(title: str, requirement: str) -> str:
    """Stable, language-tolerant hash of a leaf's intent: lowercased,
    punctuation-stripped, whitespace-collapsed title + requirement."""
    raw = f"{title}\n{requirement}".lower()
    raw = re.sub(r"[^\w\s]", " ", raw, flags=re.UNICODE)
    raw = re.sub(r"\s+", " ", raw).strip()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class ClaimBoard:
    """Single-writer board of intent claims for one run."""

    def __init__(self, sink: Optional[Callable] = None):
        # hash -> {"owner": nid, "module": fn, "state": in_progress|done}
        self._claims: dict[str, dict] = {}
        self._sink = sink
        self._lock = threading.RLock()

    def _log(self, **kw) -> None:
        try:
            from . import llm_log
            llm_log.log({"event": "claim", **kw})
        except Exception:            # noqa: BLE001 — logging never blocks work
            pass

    def claim(self, nid: str, module: str, title: str,
              requirement: str) -> dict:
        """Reserve this leaf's intent. Returns a verdict dict:
          {"verdict": "granted", "hash": h}            → build normally
          {"verdict": "duplicate", "owner": ..,        → cache-hit, do not
           "owner_module": .., "hash": h}                re-implement
        A claim by the SAME node id is idempotent (re-visit/rework)."""
        h = intent_hash(title, requirement)
        with commit_queue.exclusive(f"claim:{nid}", "intent claim"):
            with self._lock:
                cur = self._claims.get(h)
                if cur is not None and cur["owner"] != nid:
                    if cur["state"] == "done":
                        self._log(node=nid, hash=h, verdict="DUPLICATE",
                                  owner=cur["owner"], module=module)
                        return {"verdict": "duplicate", "owner": cur["owner"],
                                "owner_module": cur["module"], "hash": h}
                    # in-progress by another node → proceed (deadlock-free)
                    self._log(node=nid, hash=h, verdict="CONCURRENT",
                              owner=cur["owner"])
                    return {"verdict": "granted", "hash": h}
                # fresh intent (or our own re-visit): own it
                self._claims[h] = {"owner": nid, "module": module,
                                   "state": "in_progress"}
                self._log(node=nid, hash=h, verdict="GRANTED", module=module)
                return {"verdict": "granted", "hash": h}

    def complete(self, nid: str, h: str) -> None:
        """Mark this node's claim done — later identical intents cache-hit."""
        with self._lock:
            cur = self._claims.get(h)
            if cur is not None and cur["owner"] == nid:
                cur["state"] = "done"
                self._log(node=nid, hash=h, verdict="DONE")

    def snapshot(self) -> dict:
        with self._lock:
            return {h: dict(v) for h, v in self._claims.items()}


# run-scoped singleton (mirrors memory.MANAGER): the implementer consults
# it, run_cases configures/resets it per run. None = board off (dedup
# disabled — every leaf implements independently).
BOARD: Optional[ClaimBoard] = None


def configure(enabled: bool = True, sink: Optional[Callable] = None) -> None:
    global BOARD
    BOARD = ClaimBoard(sink=sink) if enabled else None
