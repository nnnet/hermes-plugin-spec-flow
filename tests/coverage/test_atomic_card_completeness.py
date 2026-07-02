"""#113 Slice 3 — the atomic card is the single source of truth: an exposing
leaf MUST carry acceptance criteria (else the tester invents its own — the v144
gap), and BOTH the coder and the tester read that card verbatim.

Pure/deterministic: no LLM, no HTTP call.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from harness import role_worker as rw  # noqa: E402


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._constitution = [
        "POST /notes takes {text} responds {id}. The entry src/app.py exposes"
        " wsgi_app."]
    eng._goal = "notes"
    return eng


# ── completeness gate ────────────────────────────────────────────────────────

def test_exposing_leaf_without_acceptance_is_flagged(tmp_path):
    eng = _engine(tmp_path)
    node = {"id": "notes_post", "title": "handle POST /notes",
            "requirement": "create a note via POST /notes"}
    gaps = eng._card_completeness_findings(node)
    assert gaps, "an exposing leaf with no acceptance must be flagged"
    assert "acceptance" in gaps[0].lower()


def test_exposing_leaf_with_acceptance_is_clean(tmp_path):
    eng = _engine(tmp_path)
    node = {"id": "notes_post", "title": "handle POST /notes",
            "acceptance": ["Given an empty store, when POST /notes {text:'hi'},"
                           " then the response is {id:1}"]}
    assert eng._card_completeness_findings(node) == []


def test_pure_edit_leaf_needs_no_card(tmp_path):
    eng = _engine(tmp_path)
    # a leaf that owns no route and declares no exposes → exposes nothing → the
    # gate stays inert (no false red on a housekeeping node).
    node = {"id": "tweak", "title": "polish the wording"}
    assert eng._card_completeness_findings(node) == []


def test_branch_node_needs_no_card(tmp_path):
    eng = _engine(tmp_path)
    node = {"id": "core", "title": "core", "children": [{"id": "x"}]}
    assert eng._card_completeness_findings(node) == []


def test_blank_acceptance_counts_as_missing(tmp_path):
    eng = _engine(tmp_path)
    node = {"id": "notes_post", "title": "handle POST /notes",
            "acceptance": ["", "   "]}
    assert eng._card_completeness_findings(node), "blank strings are not a card"


# ── both consumers read the card ─────────────────────────────────────────────

def test_card_block_renders_acceptance_and_examples():
    block = rw._card_block({
        "acceptance": ["Given X, when Y, then Z"],
        "examples": ["POST /notes {text:'hi'} -> {id:1}"]})
    assert "Given X, when Y, then Z" in block
    assert "POST /notes {text:'hi'} -> {id:1}" in block
    assert "do NOT invent" in block


def test_card_block_empty_without_a_card():
    assert rw._card_block({}) == ""
    assert rw._card_block({"acceptance": []}) == ""


def test_card_block_accepts_a_bare_string():
    block = rw._card_block({"acceptance": "Given X, when Y, then Z"})
    assert "Given X, when Y, then Z" in block
