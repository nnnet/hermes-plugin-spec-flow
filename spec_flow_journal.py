"""Run journal — parallelization stage 0 (single writer, waves).

Why: parallel workers cannot share mutable run state; every fixation
(node result, requirement arrival, repair outcome) must pass through ONE
writer and become visible to readers atomically. The unit of visibility
is a WAVE: a batch of entries committed together with a monotonically
increasing sequence number. "Late" under parallel execution is defined
against waves: an event is late for a worker iff it was committed AFTER
the wave the worker started from.

What: ``RunJournal`` — an append-only JSONL file, one line per wave,
guarded by an OS-level exclusive lock (``fcntl``) held by the single
writer for the journal's whole lifetime. Readers need no lock: a wave
line is written with a trailing newline and fsynced before the commit
returns, and a torn final line (crash mid-write) is skipped on read.

The engine does NOT call this module yet — it lands first with its own
offline tests, the engine switches over in a later stage.

Test: tests/test_run_journal.py.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any, Iterator, Optional


class JournalLocked(RuntimeError):
    """A second writer tried to open the journal."""


class RunJournal:
    """Append-only wave journal with a single exclusive writer.

    Writer:  ``with RunJournal(path).open() as j: j.commit_wave([...])``
    Readers: ``RunJournal(path).waves()`` / ``last_wave()`` — lock-free.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fh = None          # writer handle, holds the lock
        self._seq = 0

    # -- writer ----------------------------------------------------------
    def open(self) -> "RunJournal":
        """Become THE writer: take the exclusive lock, recover the last
        committed sequence number. Raises JournalLocked when another
        writer holds the journal."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise JournalLocked(
                f"another writer holds {self.path}") from exc
        self._fh = fh
        self._truncate_torn_tail()
        last = self.last_wave()
        self._seq = last["wave"] if last else 0
        return self

    def _truncate_torn_tail(self) -> None:
        """Cut everything after the last fully committed line — a torn
        tail left by a crash would otherwise glue itself to the next
        commit and corrupt BOTH (the new wave would be unreadable)."""
        good = 0
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.endswith("\n"):
                    break
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    break
                good += len(line.encode("utf-8"))
        if good < self.path.stat().st_size:
            os.truncate(self.path, good)
            self._fh.seek(0, os.SEEK_END)

    def close(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "RunJournal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def commit_wave(self, entries: list[dict],
                    meta: Optional[dict] = None) -> int:
        """Commit one wave atomically; returns its sequence number.

        The line is fully serialized, written with a trailing newline and
        fsynced BEFORE the new sequence number is returned — a reader
        either sees the whole wave or (after a crash) a torn tail that
        ``waves()`` skips."""
        if self._fh is None:
            raise RuntimeError("journal is not open for writing")
        if not isinstance(entries, list):
            raise TypeError("entries must be a list of dicts")
        self._seq += 1
        record = {"wave": self._seq, "t": time.time(),
                  "entries": entries, **(meta or {})}
        line = json.dumps(record, ensure_ascii=False)
        self._fh.write(line + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        return self._seq

    # -- readers (lock-free) ----------------------------------------------
    def waves(self, since: int = 0) -> Iterator[dict]:
        """Committed waves with wave > since, in order. A torn last line
        (crash mid-write) is silently skipped — it was never committed."""
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.endswith("\n"):
                    return            # torn tail — not committed
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    return            # corrupt tail — stop, never guess
                if rec.get("wave", 0) > since:
                    yield rec

    def last_wave(self) -> Optional[dict]:
        last = None
        for rec in self.waves():
            last = rec
        return last

    def entries(self, *, kind: Optional[str] = None,
                since: int = 0) -> Iterator[dict]:
        """Flat view over wave entries, each annotated with its wave seq;
        ``kind`` filters on the entry's 'kind' field."""
        for rec in self.waves(since=since):
            for e in rec.get("entries", []):
                if kind is None or e.get("kind") == kind:
                    yield {**e, "wave": rec["wave"]}
