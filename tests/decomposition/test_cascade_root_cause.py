"""Cascade root-cause detection: one exception in a shared module (DB
schema/bootstrap, a common import, a fixture) reddens many tests at once.
Bisection misses it (nothing is green alone), so the verifier clusters
failures by error signature and flags the single dominant cause so repair
fixes the origin, not the victims. Real incident: a SQLite-illegal
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` broke 127/160 corpus tests."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import pytest_verifier as pv  # noqa: E402


# a corpus run where ONE schema error cascades into many "no such table" /
# 500 victims across unrelated modules
_CASCADE = """\
FAILED tests/test_seller_kyc.py::test_post_kyc_valid - assert 500 == 201
FAILED tests/test_seller_kyc.py::test_schema_exists - sqlite3.OperationalError: near "EXISTS": syntax error
FAILED tests/test_seller_profile.py::test_create_profile_success - assert 500 == 201
FAILED tests/test_seller_profile.py::test_schema_registration - sqlite3.OperationalError: near "EXISTS": syntax error
ERROR tests/test_web_ui.py::test_catalog_page - sqlite3.OperationalError: near "EXISTS": syntax error
ERROR tests/test_web_ui.py::test_payouts_page - sqlite3.OperationalError: near "EXISTS": syntax error
FAILED tests/test_seller_directory.py::test_returns_items - sqlite3.OperationalError: near "EXISTS": syntax error
127 failed, 33 passed in 1.67s
"""

# genuinely independent failures: distinct errors, no single dominant cause
_INDEPENDENT = """\
FAILED tests/test_a.py::t1 - AssertionError: expected foo
FAILED tests/test_b.py::t2 - KeyError: 'bar'
FAILED tests/test_c.py::t3 - ValueError: bad input
3 failed, 1 passed in 0.20s
"""


def test_detects_dominant_db_cascade():
    dom = pv._dominant_error(_CASCADE)
    assert dom is not None
    sig, count, total, looks_db = dom
    assert "OperationalError" in sig
    assert looks_db is True
    assert count >= 3           # many victims share the one exception
    assert total == 7


def test_prefers_exception_over_assert_symptom():
    # the 'assert 500 == N' lines are downstream of the OperationalError;
    # the signature must name the exception, not the assert
    sig = pv._dominant_error(_CASCADE)[0]
    assert "assert" not in sig.lower()


# class-based ids and reason-less FAILED/ERROR lines — pytest omits ' - reason'
# when the repr is empty/multi-line; the traceback still carries the exception
_CLASS_FORMAT = """\
FAILED tests/test_payouts_schema.py::TestPayouts::test_ac1_structure
FAILED tests/test_payouts_schema.py::TestPayouts::test_ac2_not_null
ERROR tests/test_admin_payout.py::TestApproval::test_setup
ERROR tests/test_web_ui.py::TestCatalog::test_page
E   sqlite3.OperationalError: no such table: payouts
E   sqlite3.OperationalError: no such table: payouts
E   sqlite3.OperationalError: no such table: payouts
E   sqlite3.OperationalError: no such table: payouts
33 failed, 60 passed in 1.45s
"""


def test_detects_cascade_in_class_based_reasonless_format():
    dom = pv._dominant_error(_CLASS_FORMAT)
    assert dom is not None, "must catch a cascade even without ' - reason' lines"
    sig, count, total, looks_db = dom
    assert "OperationalError" in sig and looks_db is True
    assert total == 4           # all FAILED + ERROR lines counted


def test_no_false_positive_on_independent_bugs():
    # three different exception types, none dominant → no cascade
    assert pv._dominant_error(_INDEPENDENT) is None


def test_quiet_below_threshold():
    assert pv._dominant_error("1 failed in 0.1s\nFAILED tests/x.py::t - boom") \
        is None


def test_duplicate_table_owners(tmp_path):
    # two modules define 'products', three define 'orders' — the registry keeps
    # the last, dropping the others' columns in the corpus (green alone, red
    # together). The deterministic detector names the conflict for repair.
    src = tmp_path / "src"
    src.mkdir()
    (src / "catalog.py").write_text(
        "import db\ndb.define_table('products', {'id': 'id', 'name': 'text'})")
    (src / "product_schema.py").write_text(
        'import db\ndb.define_table("products", {"id": "id", "price": "real"})')
    (src / "orders_a.py").write_text("db.define_table('orders', {'id': 'id'})")
    (src / "orders_b.py").write_text("db.define_table('orders', {'id': 'id'})")
    (src / "orders_c.py").write_text('db.define_table("orders", {"id": "id"})')
    (src / "clean.py").write_text("db.insert('products', name='x')")
    dupes = pv._duplicate_table_owners(str(tmp_path))
    assert set(dupes) == {"products", "orders"}
    assert len(dupes["orders"]) == 3
    assert "src/clean.py" not in dupes.get("products", [])


def test_no_duplicate_owners_when_single(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("db.define_table('products', {'id': 'id'})")
    (src / "b.py").write_text("db.insert('products', name='x')")
    assert pv._duplicate_table_owners(str(tmp_path)) == {}


def test_norm_error_buckets_numeric_variants():
    assert pv._norm_error("assert 500 == 201") == pv._norm_error("assert 500 == 200")


def test_schema_module_paths_finds_registrars(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "db.py").write_text("def register_schema(s): ...")
    (src / "categories.py").write_text("import db\ndb.register_schema('CREATE ...')")
    (src / "plain.py").write_text("x = 1")
    got = set(pv._schema_module_paths(str(tmp_path)))
    assert "src/categories.py" in got
    assert "src/plain.py" not in got


# ── non-ASCII syntax offender (weak-model class) ──────────────────────────

def test_non_ascii_offender_detected(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    tdir = tmp_path / "tests"; tdir.mkdir()
    # a weak model wrote a U+2026 ellipsis into Python instead of '...'
    (tdir / "test_cart.py").write_text(
        "def test_x():\n    items = [1, 2, 3]\n    assert items  # …(truncated)\n"
        "    x = …\n", encoding="utf-8")
    (src / "clean.py").write_text("x = 1\n")
    bad = pv._non_ascii_offenders(str(tmp_path))
    assert len(bad) == 1
    rel, line, ch, cp = bad[0]
    assert rel == "tests/test_cart.py" and ch == "…" and cp == "U+2026"


def test_no_offender_when_pure_ascii(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    (src / "ok.py").write_text("def f():\n    return '...'\n")
    assert pv._non_ascii_offenders(str(tmp_path)) == []
