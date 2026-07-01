"""C3+C4 wiring — the NON-WEB integration path end-to-end (no LLM).

The pure cores (`_project_kind`, `_synthesize_lib_entry`, `_capability_probe_src`)
are covered in ``test_medium_agnostic_entry``. Here we assert they are actually
WIRED into the engine so a library/CLI project reaches an honest READY:

  * ``_product_contract`` derives a medium-agnostic contract (kind + entry +
    exposes, NO routes) from a project that describes NO HTTP;
  * ``_try_synthesize_entry`` branches to ``_try_synthesize_lib_entry`` and
    writes an entry that re-exports the public API from its owning modules;
  * ``_assembled_product_boots`` branches to the capability probe and judges the
    product by BEHAVIOUR — green when the capability runs, RED when it is absent;
  * ``_assembly_node`` builds NO LLM assembly leaf for a non-web product.

Fully offline: pure AST + a hermetic subprocess, no model call.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402


def _engine(tmp_path):
    return sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)


# A project described ENTIRELY in human text, with NO HTTP route anywhere.
_LIB_GOAL = (
    "A reusable Python library for word statistics. The package entry "
    "src/wordlib.py exposes word_count and it also exposes split_words. "
    "It is imported by other code, not served over HTTP.")


def test_contract_is_medium_agnostic_for_a_library(tmp_path):
    eng = _engine(tmp_path)
    eng._goal = _LIB_GOAL
    eng._constitution = [_LIB_GOAL]
    c = eng._product_contract()
    assert c.get("kind") == "lib"
    assert c["entry"] == "src/wordlib.py"
    assert set(c["exposes"]) == {"word_count", "split_words"}
    # a non-web contract carries NO routes (the HTTP readers stay no-op)
    assert not c.get("routes") and not c.get("boot")


def test_no_llm_assembly_leaf_for_a_library(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_PRE_GATE", "1")
    eng = _engine(tmp_path)
    eng._goal = _LIB_GOAL
    eng._constitution = [_LIB_GOAL]
    # the engine synthesises the entry deterministically → no coder assembly leaf
    assert eng._assembly_node(force=True) is None


def test_library_entry_synthesised_and_capability_green(tmp_path):
    eng = _engine(tmp_path)
    eng._goal = _LIB_GOAL
    eng._constitution = [_LIB_GOAL]
    eng.workspace.enabled = True
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    # two built leaf modules owning the exposed capabilities
    (src / "counter.py").write_text(
        "def word_count(s):\n    return len(s.split())\n")
    (src / "tokenizer.py").write_text(
        "def split_words(s):\n    return s.split()\n")
    # entry is synthesised from the AST interface registry — no model glue
    assert eng._try_synthesize_entry() is True
    assert (src / "wordlib.py").is_file()
    # behaviour-acceptance: the library boots and the capabilities run
    ok, detail = eng._assembled_product_boots()
    assert ok, detail


def test_missing_capability_is_honest_red(tmp_path):
    eng = _engine(tmp_path)
    eng._goal = _LIB_GOAL
    eng._constitution = [_LIB_GOAL]
    eng.workspace.enabled = True
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    # only ONE of the two exposed capabilities is built
    (src / "counter.py").write_text(
        "def word_count(s):\n    return len(s.split())\n")
    # synthesis declines (split_words has no owner) → LLM stays responsible
    assert eng._try_synthesize_entry() is False
    assert not (src / "wordlib.py").is_file()
    # and the boot-gate is honestly RED (entry not assembled)
    ok, detail = eng._assembled_product_boots()
    assert not ok and "wordlib" in detail


def test_p7_scenario_wording_derives_a_library_contract(tmp_path):
    # guard the C5 scenario against wording drift: the p7 goal + constitution,
    # fed verbatim, MUST derive a non-web library contract (kind=lib, the two
    # declared capabilities, no routes). If someone edits the yaml into web
    # phrasing or drops the `exposes` markers, this fails loudly.
    import yaml  # noqa: PLC0415
    p7 = (pathlib.Path(__file__).resolve().parents[1]
          / "scenarios" / "p7_word_stats_lib.yaml")
    spec = yaml.safe_load(p7.read_text())
    eng = _engine(tmp_path)
    eng._goal = spec.get("goal", "")
    eng._constitution = list(spec.get("constitution", []))
    c = eng._product_contract()
    assert c.get("kind") == "lib", c
    assert c["entry"] == "src/wordlib.py"
    assert {"word_count", "split_words"} <= set(c["exposes"])
    assert not c.get("routes")


def test_broken_capability_body_is_honest_red(tmp_path):
    # both capabilities present by NAME, but one raises on import → probe RED.
    eng = _engine(tmp_path)
    eng._goal = _LIB_GOAL
    eng._constitution = [_LIB_GOAL]
    eng.workspace.enabled = True
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "counter.py").write_text(
        "def word_count(s):\n    return len(s.split())\n")
    (src / "tokenizer.py").write_text(
        "raise RuntimeError('boom')\n"
        "def split_words(s):\n    return s.split()\n")
    assert eng._try_synthesize_entry() is True    # names resolve
    ok, _ = eng._assembled_product_boots()        # but import blows up
    assert not ok
