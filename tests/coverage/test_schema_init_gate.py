"""Deterministic gate for the 'sqlite no such table' class: a module that
defines a CREATE-TABLE-only initialiser but never calls it, while separately
running SQL, ships a fresh-DB OperationalError. Regression for p6 notes_api
(init_db defined, never invoked; create_note INSERTs -> no such table)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from spec_flow_tools import schema_init_uninvoked as siu  # noqa: E402

NOTES_API_BUG = '''
import sqlite3, os
def init_db():
    conn = sqlite3.connect(os.environ.get("NOTES_DB", "notes.db"))
    conn.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, text TEXT)")
def create_note(payload, q):
    conn = sqlite3.connect("notes.db")
    conn.execute("INSERT INTO notes (text) VALUES (?)", (payload["text"],))
    return 200, {"id": 1}
'''

INIT_CALLED = '''
import sqlite3
def init_db():
    sqlite3.connect("d.db").execute("CREATE TABLE IF NOT EXISTS t (id INT)")
def create(p):
    init_db()
    sqlite3.connect("d.db").execute("INSERT INTO t VALUES (1)")
'''

SELF_CONTAINED = '''
import sqlite3
def create(p):
    c = sqlite3.connect("d.db")
    c.execute("CREATE TABLE IF NOT EXISTS t (id INT)")
    c.execute("INSERT INTO t VALUES (1)")
'''

TOPLEVEL_CREATE = '''
import sqlite3
_c = sqlite3.connect("d.db")
_c.execute("CREATE TABLE IF NOT EXISTS t (id INT)")
def create(p):
    _c.execute("INSERT INTO t VALUES (1)")
'''

NO_SQL = '''
def add(a, b):
    return a + b
'''


def test_flags_uninvoked_initialiser():
    why = siu(NOTES_API_BUG)
    assert why and "init_db" in why and "no such table" in why


def test_initialiser_called_is_clean():
    assert siu(INIT_CALLED) is None


def test_self_contained_function_is_clean():
    assert siu(SELF_CONTAINED) is None


def test_toplevel_create_is_clean():
    assert siu(TOPLEVEL_CREATE) is None


def test_non_sql_module_is_clean():
    assert siu(NO_SQL) is None


def test_syntax_error_is_ignored():
    assert siu("def broken(:\n  pass") is None
