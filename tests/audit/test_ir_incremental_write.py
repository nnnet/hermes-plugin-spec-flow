"""STAGE 14 extension (S14.6): ir.json is a WORKING artifact — written
incrementally and thread-safely, not a single end-of-run report (node I1,
plan 2026-07-04T00-45; user request 2026-07-06 "переворот").

Why: ir.json was dumped once, late (_write_ir after the whole tree is
realized), through workspace._write (path.write_text — NOT atomic). A reader
(the live dashboard) could catch a truncated file mid-write, and two leaves
realized in parallel could race the same file. To make the IR a live,
inspectable source the write must be (a) atomic — a reader never sees a
partial file — and (b) serialized under a lock across concurrent callers.

What is pinned here:
  * S14.6a `_write_ir` is guarded by an engine lock (`_ir_write_lock`) so
    concurrent callers cannot interleave a build+write;
  * S14.6b the on-disk ir.json is replaced ATOMICALLY (tmp + os.replace) —
    a concurrent reader always parses a complete JSON document, never an
    empty or half-written one;
  * S14.6c after each leaf is realized the engine re-dumps (an incremental
    hook), so ir.json grows during the run instead of appearing only at the
    end.

Deterministic-ish: the atomicity test hammers the writer with many concurrent
readers; without atomic replace a reader catches the truncated file.
"""
from __future__ import annotations

import json
import pathlib
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402


def _engine(tmp_path):
    return sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)


# ── S14.6a the writer holds a lock ──────────────────────────────────────────

def test_write_ir_has_a_lock(tmp_path):
    eng = _engine(tmp_path)
    assert isinstance(getattr(eng, "_ir_write_lock", None),
                      (type(threading.Lock()), type(threading.RLock()))), (
        "_write_ir must be guarded by an engine lock so parallel leaves do "
        "not race the dump")


# ── S14.6b concurrent readers never see a partial file ──────────────────────

def test_ir_json_is_written_atomically(tmp_path):
    eng = _engine(tmp_path)
    eng._write_ir("seed")                      # ensure the file exists first
    p = pathlib.Path(eng.workspace.root) / "ir.json"
    assert p.is_file(), "the seed dump must land"

    errors = []
    stop = threading.Event()

    def writer():
        for _ in range(200):
            if stop.is_set():
                return
            eng._write_ir("hammer")

    def reader():
        # the file already has content; once it exists a reader must ALWAYS
        # see a complete document. An empty read is the truncate window of a
        # non-atomic write_text('w'); a parse error is a partial write.
        for _ in range(400):
            if stop.is_set():
                return
            try:
                txt = p.read_text(encoding="utf-8")
                if txt == "":
                    errors.append("empty read (truncate window)")
                    return
                json.loads(txt)              # a partial write raises here
            except (ValueError, json.JSONDecodeError) as exc:
                errors.append(str(exc))
                return

    threads = [threading.Thread(target=writer) for _ in range(3)] + \
              [threading.Thread(target=reader) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    stop.set()
    assert not errors, (
        "a concurrent reader caught a truncated ir.json — the write is not "
        "atomic (tmp + os.replace): %r" % errors[:2])


# ── S14.6c an incremental hook exists ───────────────────────────────────────

def test_incremental_dump_hook_present(tmp_path):
    eng = _engine(tmp_path)
    assert hasattr(eng, "_write_ir_incremental"), (
        "the engine must expose an incremental dump hook called per realized "
        "leaf so ir.json grows during the run, not only at the end")
