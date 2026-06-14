"""#9 / П2: derive an API contract from a smoke test's AST — endpoints +
asserted facts — so the contract can't drift from the executable truth."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import contract_from_smoke as cfs  # noqa: E402


_SMOKE = '''
import io


def get(path, query=""):
    return 200, "text/html", "<html></html>"


def test_catalog_page_is_html():
    status, ctype, html = get("/ui/catalog")
    assert status == 200
    assert ctype.startswith("text/html")


def test_directory_returns_items():
    status, ctype, body = get("/sellers/directory")
    assert status == 200
    data = {"items": []}
    assert "items" in data
'''


def test_endpoints_extracted():
    clauses = cfs.derive_contract(_SMOKE)
    eps = {e for c in clauses for e in c["endpoints"]}
    assert ("GET", "/ui/catalog") in eps
    assert ("GET", "/sellers/directory") in eps


def test_status_and_content_type_facts():
    clauses = cfs.derive_contract(_SMOKE)
    catalog = next(c for c in clauses if c["test"] == "test_catalog_page_is_html")
    assert "status 200" in catalog["facts"]
    assert "content-type text/html" in catalog["facts"]


def test_json_key_fact():
    clauses = cfs.derive_contract(_SMOKE)
    directory = next(c for c in clauses
                     if c["test"] == "test_directory_returns_items")
    assert any("items" in f for f in directory["facts"])


def test_only_test_functions_are_read():
    clauses = cfs.derive_contract(_SMOKE)
    names = {c["test"] for c in clauses}
    assert names == {"test_catalog_page_is_html", "test_directory_returns_items"}
    assert "get" not in names


def test_broken_source_yields_empty():
    assert cfs.derive_contract("def test_x(:\n  pass") == []


def test_render_contract_markdown():
    md = cfs.render_contract(cfs.derive_contract(_SMOKE))
    assert "## API contract" in md
    assert "`GET /ui/catalog`" in md
    assert "test_catalog_page_is_html" in md


def test_render_empty_is_blank():
    assert cfs.render_contract([]) == ""


def test_contract_from_file(tmp_path):
    p = tmp_path / "test_smoke.py"
    p.write_text(_SMOKE, encoding="utf-8")
    md = cfs.contract_from_file(str(p))
    assert "`GET /sellers/directory`" in md
    assert cfs.contract_from_file(str(tmp_path / "ghost.py")) == ""
