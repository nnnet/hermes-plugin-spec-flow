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

Persistence (П15): the board keeps its claims in a pluggable ClaimStore.
The default is in-memory (lost on exit). A durable backend (sqlite, one
db per run) lets a STOPPED run restore its board on --resume so an
already-finished intent stays a cache-hit across the restart. The store
is chosen like memory.make_provider — by the case YAML ``claims:`` block.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import commit_queue


def intent_hash(title: str, requirement: str) -> str:
    """Stable, language-tolerant hash of a leaf's intent: lowercased,
    punctuation-stripped, whitespace-collapsed title + requirement."""
    raw = f"{title}\n{requirement}".lower()
    raw = re.sub(r"[^\w\s]", " ", raw, flags=re.UNICODE)
    raw = re.sub(r"\s+", " ", raw).strip()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ─── pluggable persistence (П15) ──────────────────────────────────────
# A store maps an intent-hash to {"owner": nid, "module": fn,
# "state": in_progress|done}. The board owns the LOGIC; the store owns
# only WHERE the records live, so a durable backend survives a restart.

class ClaimStore:
    """In-memory claim store (default). Lost when the process exits."""

    def __init__(self) -> None:
        self._d: dict[str, dict] = {}

    def get(self, h: str) -> Optional[dict]:
        rec = self._d.get(h)
        return dict(rec) if rec is not None else None

    def put(self, h: str, rec: dict) -> None:
        self._d[h] = {"owner": rec["owner"], "module": rec["module"],
                      "state": rec["state"]}

    def items(self) -> Iterable[tuple]:
        return [(h, dict(r)) for h, r in self._d.items()]

    def close(self) -> None:
        pass


class SqliteClaimStore(ClaimStore):
    """Durable claim store: one sqlite db per run (WAL). Reopening the
    same db RESTORES the board — an intent finished before a stop is
    still a cache-hit after --resume."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the board serialises writes under its own
        # RLock + commit_queue, so the connection is safe to share.
        self._cx = sqlite3.connect(str(self.path), check_same_thread=False)
        self._cx.execute("PRAGMA journal_mode=WAL")
        self._cx.execute("PRAGMA synchronous=NORMAL")
        self._cx.execute(
            "CREATE TABLE IF NOT EXISTS claims ("
            "hash TEXT PRIMARY KEY, owner TEXT, module TEXT, state TEXT)")
        self._cx.commit()

    def get(self, h: str) -> Optional[dict]:
        row = self._cx.execute(
            "SELECT owner, module, state FROM claims WHERE hash=?",
            (h,)).fetchone()
        if row is None:
            return None
        return {"owner": row[0], "module": row[1], "state": row[2]}

    def put(self, h: str, rec: dict) -> None:
        self._cx.execute(
            "INSERT INTO claims(hash, owner, module, state) VALUES(?,?,?,?) "
            "ON CONFLICT(hash) DO UPDATE SET owner=excluded.owner, "
            "module=excluded.module, state=excluded.state",
            (h, rec["owner"], rec["module"], rec["state"]))
        self._cx.commit()

    def items(self) -> Iterable[tuple]:
        rows = self._cx.execute(
            "SELECT hash, owner, module, state FROM claims").fetchall()
        return [(r[0], {"owner": r[1], "module": r[2], "state": r[3]})
                for r in rows]

    def close(self) -> None:
        try:
            self._cx.close()
        except Exception:  # noqa: BLE001
            pass


def make_store(cfg: Optional[object] = None,
               run_dir: Optional[str] = None) -> ClaimStore:
    """Store from the case YAML ``claims:`` block (or a bare backend name):

    claims: {backend: memory}                 # default, non-durable
    claims: {backend: sqlite}                 # <run_dir>/claims.db (WAL)
    claims: {backend: sqlite, path: "/x.db"}  # explicit db path

    A future durable backend (beads/redis) plugs in here without touching
    the board logic.
    """
    if isinstance(cfg, dict):
        kind = str(cfg.get("backend", "memory")).lower()
        explicit = cfg.get("path")
    else:
        kind = str(cfg or "memory").lower()
        explicit = None
    if kind == "memory":
        return ClaimStore()
    if kind == "sqlite":
        path = explicit or str(Path(run_dir or ".") / "claims.db")
        return SqliteClaimStore(path)
    raise ValueError(f"unknown claims backend {kind!r} (memory|sqlite)")


class ClaimBoard:
    """Single-writer board of intent claims for one run."""

    def __init__(self, sink: Optional[Callable] = None,
                 store: Optional[ClaimStore] = None):
        self._store = store if store is not None else ClaimStore()
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
                cur = self._store.get(h)
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
                self._store.put(h, {"owner": nid, "module": module,
                                    "state": "in_progress"})
                self._log(node=nid, hash=h, verdict="GRANTED", module=module)
                return {"verdict": "granted", "hash": h}

    def complete(self, nid: str, h: str) -> None:
        """Mark this node's claim done — later identical intents cache-hit."""
        with self._lock:
            cur = self._store.get(h)
            if cur is not None and cur["owner"] == nid:
                cur["state"] = "done"
                self._store.put(h, cur)
                self._log(node=nid, hash=h, verdict="DONE")

    def snapshot(self) -> dict:
        with self._lock:
            return {h: dict(v) for h, v in self._store.items()}

    def close(self) -> None:
        self._store.close()


# run-scoped singleton (mirrors memory.MANAGER): the implementer consults
# it, run_cases configures/resets it per run. None = board off (dedup
# disabled — every leaf implements independently).
BOARD: Optional[ClaimBoard] = None


def configure(enabled: bool = True, sink: Optional[Callable] = None,
              store: Optional[ClaimStore] = None) -> None:
    global BOARD
    if not enabled:
        if BOARD is not None:
            BOARD.close()
        BOARD = None
        return
    BOARD = ClaimBoard(sink=sink, store=store)
