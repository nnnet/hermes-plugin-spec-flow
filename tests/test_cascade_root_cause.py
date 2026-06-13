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


def test_no_false_positive_on_independent_bugs():
    # three different exception types, none dominant → no cascade
    assert pv._dominant_error(_INDEPENDENT) is None


def test_quiet_below_threshold():
    assert pv._dominant_error("1 failed in 0.1s\nFAILED tests/x.py::t - boom") \
        is None


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
