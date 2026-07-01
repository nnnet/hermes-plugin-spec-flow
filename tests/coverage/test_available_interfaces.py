"""#113: publish the producer's REAL interface to the consumer coder.

The recurring root of a red product is a CONSUMER leaf that invents the
PRODUCER's symbols (v137: web_ui imported ``save_note``/``init_db`` while core
exposes ``create_note``). No repair can map an invented NAME back without
guessing — the only sound lever is to hand the coder the producer's real surface
as DATA, so it imports by the actual names. This is medium-agnostic: the surface
is the same for a web, CLI, or library leaf.

Two units:
- ``Engine._available_interfaces`` — deterministic AST scan of built src modules.
- ``role_worker._interfaces_block`` — the coder-prompt instruction built from it.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import spec_flow_runner as sfr  # noqa: E402
from harness import role_worker as rw  # noqa: E402


def _engine(tmp_path):
    return sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)


def test_available_interfaces_lists_symbols_with_signatures(tmp_path):
    eng = _engine(tmp_path)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "core.py").write_text(
        "NOTES = []\n"
        "def create_note(text):\n    return 1\n"
        "def list_notes():\n    return NOTES\n"
        "def _private():\n    return 0\n"
        "class Store:\n    pass\n")
    ifaces = eng._available_interfaces()
    assert "core" in ifaces
    syms = ifaces["core"]
    assert "create_note(text)" in syms
    assert "list_notes()" in syms
    assert "class Store" in syms
    assert "NOTES" in syms
    assert not any("_private" in s for s in syms)   # privates excluded


def test_available_interfaces_excludes_the_leaf_being_written(tmp_path):
    eng = _engine(tmp_path)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "core.py").write_text("def save(t):\n    return 1\n")
    (src / "web_ui.py").write_text("def get_ui(p, q):\n    return (200, '')\n")
    ifaces = eng._available_interfaces(exclude="web_ui")
    assert "core" in ifaces and "web_ui" not in ifaces


def test_interfaces_block_names_the_real_producer_symbols(tmp_path):
    ctx = {"available_interfaces": {
        "core": ["create_note(text)", "list_notes()"]}}
    block = rw._interfaces_block(ctx)
    assert "core exposes: create_note(text), list_notes()" in block
    # the instruction must forbid inventing names
    assert "do NOT" in block or "does not exist" in block


def test_interfaces_block_empty_when_nothing_built(tmp_path):
    assert rw._interfaces_block({}) == ""
    assert rw._interfaces_block({"available_interfaces": {}}) == ""
