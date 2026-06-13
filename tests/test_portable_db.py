"""The platform-seeded db.py is a PORTABLE data layer: features describe
tables with define_table() and read/write with insert/select/update/delete,
and the SQL is produced by a swappable backend adapter. This guards the
skeleton shipped in the p4 case's seed_files (a SQLite-specific 'ALTER TABLE
... ADD COLUMN IF NOT EXISTS' once aborted the whole bootstrap and reddened
127 corpus tests — the portable API makes that class unwritable)."""
import importlib.util
import os
import pathlib
import tempfile

import yaml

CASE = pathlib.Path(__file__).resolve().parent / "scenarios" / "p4_b2b_marketplace.yaml"


def _load_seeded_db(tmp):
    src = yaml.safe_load(CASE.read_text())["seed_files"]["src/db.py"]
    p = pathlib.Path(tmp) / "db.py"
    p.write_text(src)
    os.environ["MARKETPLACE_DB"] = str(pathlib.Path(tmp) / "t.db")
    os.environ.pop("MARKETPLACE_DB_BACKEND", None)
    spec = importlib.util.spec_from_file_location("seeded_db", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_seeded_db_is_portable():
    with tempfile.TemporaryDirectory() as tmp:
        db = _load_seeded_db(tmp)
        # describe a table portably — no raw SQL in feature code
        db.define_table("products", {"id": "id", "name": "text",
                                     "price": "real", "in_stock": "bool"})
        rid = db.insert("products", name="ThinkPad", price=749.0, in_stock=1)
        db.insert("products", name="MacBook", price=689.0, in_stock=1)
        assert rid == 1
        rows = db.select("products", order_by="price")
        assert [r["name"] for r in rows] == ["MacBook", "ThinkPad"]
        assert db.select("products", where={"name": "MacBook"})[0]["price"] == 689.0
        assert db.update("products", {"name": "ThinkPad"}, price=700.0) == 1
        assert db.delete("products", name="MacBook") == 1
        assert len(db.select("products")) == 1


def test_no_dialect_specific_ddl_in_skeleton():
    src = yaml.safe_load(CASE.read_text())["seed_files"]["src/db.py"]
    # the exact statement that broke the live run must not be the pattern
    assert "ADD COLUMN IF NOT EXISTS" not in src
    assert "def define_table" in src and "_ADAPTERS" in src


def test_backend_is_pluggable():
    with tempfile.TemporaryDirectory() as tmp:
        db = _load_seeded_db(tmp)
        # the dialect lives in an adapter chosen by env — swapping needs no
        # feature change
        assert "sqlite" in db._ADAPTERS
        a = db._adapter()
        sql = a.create_table_sql("t", {"id": "id", "name": "text"})
        assert sql.startswith("CREATE TABLE IF NOT EXISTS t (")
        assert a.PARAM == "?"


def test_legacy_register_schema_still_works():
    with tempfile.TemporaryDirectory() as tmp:
        db = _load_seeded_db(tmp)
        db.register_schema(
            "CREATE TABLE IF NOT EXISTS legacy (id INTEGER PRIMARY KEY, x TEXT)")
        db.execute("INSERT INTO legacy (x) VALUES (?)", ("hi",))
        assert db.execute("SELECT x FROM legacy")[0]["x"] == "hi"
