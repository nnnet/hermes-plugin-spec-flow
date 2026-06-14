"""Diff-based repair (#1 / П3): SEARCH/REPLACE blocks applied surgically to
the current file; a non-applicable block (text gone or ambiguous) is refused
WITHOUT a write, so a stale diff never clobbers working code."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import diff_repair as dr  # noqa: E402


# ─── parsing ──────────────────────────────────────────────────────────

def test_parse_single_block():
    text = ("FILE: src/cart.py\n"
            "<<<<<<< SEARCH\n"
            "def add(payload):\n"
            "=======\n"
            "def add(payload, query):\n"
            ">>>>>>> REPLACE\n")
    blocks = dr.parse_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["file"] == "src/cart.py"
    assert blocks[0]["search"] == "def add(payload):"
    assert blocks[0]["replace"] == "def add(payload, query):"


def test_parse_multiple_blocks():
    text = ("FILE: src/a.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"
            "FILE: tests/test_a.py\n<<<<<<< SEARCH\nassert x==1\n=======\nassert x==2\n>>>>>>> REPLACE\n")
    blocks = dr.parse_blocks(text)
    assert [b["file"] for b in blocks] == ["src/a.py", "tests/test_a.py"]


def test_parse_prose_yields_nothing():
    assert dr.parse_blocks("I would fix it by changing the signature.") == []
    assert dr.parse_blocks("") == []


# ─── applicability ────────────────────────────────────────────────────

def test_apply_block_unique_match():
    ok, res = dr.apply_block("a\nb\nc\n", "b", "B")
    assert ok and res == "a\nB\nc\n"


def test_apply_block_not_found_is_refused():
    ok, res = dr.apply_block("a\nb\n", "zzz", "Z")
    assert not ok and "not found" in res


def test_apply_block_ambiguous_is_refused():
    ok, res = dr.apply_block("x\nx\n", "x", "y")
    assert not ok and "ambiguous" in res


def test_empty_search_replaces_whole_file():
    ok, res = dr.apply_block("old content\n", "", "brand new\n")
    assert ok and res == "brand new\n"


# ─── apply_repair over a virtual filesystem ───────────────────────────

def test_apply_repair_applies_and_refuses():
    files = {"src/cart.py": "def add(p):\n    return 0\n"}
    blocks = [
        {"file": "src/cart.py", "search": "return 0", "replace": "return 1"},
        {"file": "src/cart.py", "search": "NONEXISTENT", "replace": "x"},
    ]
    res = dr.apply_repair(lambda rel: files.get(rel, ""), blocks)
    assert res["applied"] == ["src/cart.py"]
    assert res["refused"] == [("src/cart.py", "search text not found")]
    assert res["files"]["src/cart.py"] == "def add(p):\n    return 1\n"


def test_apply_repair_composes_two_edits_to_one_file():
    files = {"src/a.py": "a = 1\nb = 2\n"}
    blocks = [
        {"file": "src/a.py", "search": "a = 1", "replace": "a = 10"},
        {"file": "src/a.py", "search": "b = 2", "replace": "b = 20"},
    ]
    res = dr.apply_repair(lambda rel: files.get(rel, ""), blocks)
    assert res["files"]["src/a.py"] == "a = 10\nb = 20\n"


def test_apply_repair_honors_allowed_set():
    blocks = [{"file": "src/secret.py", "search": "", "replace": "x"}]
    res = dr.apply_repair(lambda rel: "", blocks, allowed={"src/cart.py"})
    assert res["files"] == {}
    assert res["refused"] == [("src/secret.py", "path not permitted")]
