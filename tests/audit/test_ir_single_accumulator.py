"""STAGE 25 (S25): ONE in-memory IR accumulator is the single source; the
datum projections (interface.json rows: routes / media / request_fields) are
DERIVED from it, not re-assembled independently from the raw datums (node I2,
plan 2026-07-04T00-45; user request 2026-07-06 "переворот, сердце").

Why (the drift class this closes): before I2 there were TWO independent
assemblers reading the SAME raw engine datums:
  * `spec_ir.build_ir(engine)` merged `_route_owners` / `_route_media_map` /
    `_route_request_fields` / `_route_success_status` into the IR;
  * `Engine._write_interface_contract` re-called `_route_media_map()` and
    `_route_request_fields()` and re-derived the very same route rows for
    contracts/interface.json.
Two computations of one projection with no shared held structure — refactor
either side and they silently diverge (the pairwise-drift class the whole
IR rearchitecture exists to kill). There was no ONE structure held in memory:
build_ir rebuilt from scratch on every call, so nothing was a "dump".

What is pinned here (all RED before I2, GREEN after):
  * S25a the engine HOLDS one IR accumulator in memory (`self._ir`) once it
    has been written under the I1 lock — build_ir's result is retained, not
    thrown away and rebuilt on the next reader;
  * S25b `_write_interface_contract` reads its route media / request-fields
    projection FROM that accumulator, NOT by re-calling the raw datums. Proof
    of direction: after the accumulator is built we mutate the RAW datum
    (monkeypatch `_route_media_map`); a re-derived interface would pick the
    mutated datum up (two directions), a derived-from-accumulator interface
    ignores it (one direction: IR is the source, the datum is upstream of it
    but never read a SECOND time behind the accumulator's back);
  * S25c the held IR is a DUMP/snapshot: reading it after a raw-datum mutation
    returns the SAME held structure, not a fresh rebuild that could drift
    between two reads under changing datums.

Deterministic: engine unit calls over a tmp workspace, no LLM.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402
import spec_ir  # noqa: E402

_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
    "routes": [],
    "media": {"/notes": "json", "/health": "json"},
}
_NODE = {"id": "core", "title": "Product core",
         "requirement": "own POST /notes and GET /notes and GET /health"}


def _engine(tmp_path, contract=_CONTRACT):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(contract)
    eng._goal = "notes service"
    eng._constitution = ["stdlib only"]
    # record the ownership datum the same way the pipeline does
    owned = eng._leaf_owned_routes(dict(_NODE))
    assert owned, "precondition: the leaf must own the declared routes"
    return eng


# ── S25a a single accumulator is held in memory ─────────────────────────────

def test_engine_holds_one_ir_accumulator(tmp_path):
    eng = _engine(tmp_path)
    eng._write_ir_locked("seed")
    held = getattr(eng, "_ir", None)
    assert isinstance(held, dict) and held.get("format") == spec_ir.IR_FORMAT, (
        "after a write under the I1 lock the engine must HOLD one IR structure "
        "in memory (self._ir) — build_ir's result is the single source, not a "
        "throwaway rebuilt on every reader")


# ── S25b interface.json is DERIVED from the accumulator, not re-assembled ────

def test_interface_reads_media_from_accumulator_not_raw_datum(tmp_path):
    eng = _engine(tmp_path)
    eng._write_ir_locked("seed")

    # After the accumulator is built, corrupt the RAW datum. A projection that
    # re-derives interface.json straight from the datum will pick this up and
    # diverge from the IR the accumulator holds; a projection that reads the
    # accumulator ignores it. This is the one-direction proof.
    eng._route_media_map = lambda: {"/notes": "html", "/health": "html"}

    eng._write_interface_contract()
    ip = pathlib.Path(eng.workspace.root) / "contracts" / "interface.json"
    assert ip.is_file(), "interface.json must be written"
    rows = {(_r["method"], _r["path"]): _r
            for _r in json.loads(ip.read_text(encoding="utf-8"))["routes"]}

    # the accumulator was built with media=json for /notes; the corrupted raw
    # datum says html. interface.json must follow the ACCUMULATOR (json).
    notes = rows[("POST", "/notes")]
    assert notes.get("media") == "application/json", (
        "interface.json took its media from the mutated RAW datum (html) "
        "instead of the held IR accumulator (json) — two independent "
        "assemblers, the pairwise-drift class I2 must remove. media=%r"
        % notes.get("media"))


# ── S25c the held IR is a DUMP of the accumulator, not a live rebuild ────────

def test_held_ir_is_a_dump_not_a_live_rebuild(tmp_path):
    eng = _engine(tmp_path)
    eng._write_ir_locked("seed")
    held = getattr(eng, "_ir", None)
    assert isinstance(held, dict)
    before = json.dumps(held, sort_keys=True)

    # mutate a raw datum AFTER the accumulator exists; reading the held IR must
    # not silently re-derive behind the reader's back on the next snapshot.
    eng._route_media_map = lambda: {"/notes": "html", "/health": "html"}
    snap = eng._ir_snapshot() if hasattr(eng, "_ir_snapshot") else eng._ir
    assert json.dumps(snap, sort_keys=True) == before, (
        "reading the IR after a raw-datum mutation must return the SAME held "
        "structure (a dump/snapshot), not a fresh rebuild that drifts — the "
        "accumulator is the one source, the datum is upstream of it, never "
        "read a second time behind it")
