"""STAGE 26 (S26): the HOT PATH reads the held IR accumulator, never a
fresh rebuild of it (node I3, plan 2026-07-04T00-45; the closing node of the
"переворот" — IR the single source).

Context: node I2 (S25) made ``self._ir`` the single in-memory accumulator and
made ``interface.json`` a projection of it. But three skeleton hot-path readers
still called ``spec_ir.build_ir(self)`` with NO ``sources`` argument, which
re-runs ``collect_ir_sources(self)`` and reads every raw datum method
(``_route_owners`` / ``_route_media_map`` / ``_route_request_fields`` /
``_module_names`` / env / pins) a SECOND time BEHIND the accumulator:

  * ``_ir_skeleton_for`` (write-door skeleton per leaf),
  * the late-route ``binds_route`` refresh that feeds ``_refresh_ir_skeletons``,
  * the IR-compiled conformance test writer's engine fragment.

That is exactly the pairwise-drift class the accumulator was built to remove:
a hot reader whose value diverges from the held IR when the accumulator is
updated. The behavioural proof below MUTATES the held accumulator and asserts
the hot reader REFLECTS the held structure — not a rebuild that ignores it.

  * S26a ``_ir_skeleton_for`` compiles from the HELD IR: an env access point
    injected into the held node's IR entry appears in the compiled skeleton;
    a rebuild from raw datums (which never saw the injection) would drop it.
  * S26b the held IR is used as-is (identity), not re-derived: the skeleton
    reader observes the SAME ``nodes`` object the accumulator holds.
  * S26c a text gate (secondary): no skeleton hot-path reader calls
    ``build_ir(self)`` without an explicit ``sources`` snapshot — the raw
    datums are upstream of the accumulator and are never queried behind it.

Deterministic: engine unit calls over a tmp workspace, no LLM.
"""
from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402
import spec_skeletons  # noqa: E402

_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
    "routes": [],
    "media": {"/notes": "json", "/health": "json"},
}
_NODE = {"id": "core", "title": "Product core", "files": ["src/core.py"],
         "requirement": "own POST /notes and GET /notes and GET /health"}

# an env access point NO raw datum knows about — it exists ONLY on the held
# accumulator, so it can appear in a hot-path read iff that read is served
# from the held IR rather than re-assembled from the datum methods.
_SENTINEL = "SPEC_FLOW_S26_HELD_ONLY"


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(_CONTRACT)
    eng._goal = "notes service"
    eng._constitution = ["stdlib only"]
    owned = eng._leaf_owned_routes(dict(_NODE))
    assert owned, "precondition: the leaf must own the declared routes"
    eng._write_ir("plan realized")
    assert isinstance(eng._ir, dict) and eng._ir.get("nodes"), (
        "precondition: the accumulator holds an IR after the first dump")
    return eng


# ── S26a: the skeleton reader compiles from the HELD accumulator ─────────────
def test_skeleton_reader_reflects_held_accumulator(tmp_path):
    eng = _engine(tmp_path)
    # mutate ONLY the held accumulator — no datum method changes.
    eng._ir["nodes"]["core"]["env"] = [
        {"name": _SENTINEL, "rule": "held-only marker"}]
    skel = eng._ir_skeleton_for("core", "src/core.py")
    assert skel, "precondition: the leaf compiles a skeleton"
    assert _SENTINEL in skel, (
        "the skeleton hot reader rebuilt the IR from the raw datums instead "
        "of compiling from the held accumulator — an accumulator update is "
        "invisible to the hot path, the pairwise-drift class I3 must remove. "
        "skeleton head=%r" % skel[:200])


# ── S26b: the reader uses the held nodes object, not a re-derived copy ───────
def test_skeleton_reader_uses_held_ir_identity(tmp_path):
    eng = _engine(tmp_path)
    captured = {}
    real_compile = spec_skeletons.compile_skeleton

    def _spy(ir, node_id):
        captured["ir"] = ir
        return real_compile(ir, node_id)

    spec_skeletons.compile_skeleton = _spy
    try:
        eng._ir_skeleton_for("core", "src/core.py")
    finally:
        spec_skeletons.compile_skeleton = real_compile
    assert captured.get("ir") is not None, "compile_skeleton was not reached"
    assert captured["ir"]["nodes"] is eng._ir["nodes"], (
        "the skeleton reader compiled a FRESHLY rebuilt IR, not the held "
        "accumulator — two independent assemblers can drift; the held IR "
        "must be the one source the hot path reads")


# ── S26c: text gate — no hot skeleton reader rebuilds the IR from datums ─────
def test_no_hot_skeleton_reader_rebuilds_ir_from_datums():
    runner = (pathlib.Path(__file__).resolve().parents[2]
              / "spec_flow_runner.py")
    src = runner.read_text(encoding="utf-8")
    # A build_ir(self) CALL with no explicit sources snapshot re-reads every
    # raw datum behind the accumulator. The only sanctioned rebuild is the
    # write door (collect_ir_sources -> build_ir(self, sources) under lock).
    # Match the call as an expression head (assigned or nested), so a bare
    # mention in a docstring/comment line is not counted as an offender.
    call = re.compile(r"[=(]\s*[\w.]*build_ir\(\s*self\s*\)")
    offenders = [ln for ln in src.splitlines()
                 if call.search(ln) and not ln.lstrip().startswith("#")]
    assert not offenders, (
        "a hot-path reader still rebuilds the IR from raw datums via a "
        "build_ir(self) call; read the held accumulator (self._ir / a "
        "held-IR helper) so datum reads stay upstream of the accumulator, "
        "never behind it. offenders=%r" % offenders)
