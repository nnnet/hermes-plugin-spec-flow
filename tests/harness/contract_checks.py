"""Pre-integrate CONTRACT checks — pure static/mechanical, NO LLM.

Why: a weak model produces a HOLLOW-GREEN integrate — handlers do
``import registry`` / ``registry.register(...)`` while no ``registry.py`` is
ever built, and the tests paper over the gap with
``sys.modules["registry"] = MagicMock()`` so nothing actually fails. The
real pytest run stays green over a product that cannot be assembled.

What: a small family of deterministic checks over a workspace ``root``,
each returning a list of human-readable violation strings. They catch the
un-buildable / self-mocked / stubbed-out classes BEFORE the (expensive)
full pytest run, so the repair worker gets a precise, cheap-to-produce hint.

Test: ``tests/harness/test_contract_checks.py`` drives every function on
synthetic ``src/``/``tests/`` trees (positive + negative) under ``tmp_path``.

These functions are intentionally tiny, well-typed and independently
unit-testable. Cross-module / duplicate-table / non-ASCII classes already
have detectors in :mod:`pytest_verifier`; this module REUSES them rather
than duplicating, and adds the import-but-unbuilt / mock-local / stub-body
/ collectability checks the existing detectors do not cover.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Reuse the existing AST/detector machinery — do not duplicate it.
from .pytest_verifier import (
    PYTEST_TIMEOUT,
    hermetic_env,
    _module_exports,
    _safe_rel,
    _cross_module_import_violations,
    _duplicate_table_owners,
    _non_ascii_offenders,
)

# stdlib top-level module names we must never flag as "a LOCAL module that is
# missing" — only siblings inside src/ count as local product modules.
_STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {
    "app", "wsgiref", "sqlite3", "json", "os", "sys", "io", "re", "ast",
    "pathlib", "typing", "functools", "itertools", "collections", "datetime",
    "tempfile", "subprocess", "threading", "unittest", "pytest", "math",
    "hashlib", "base64", "urllib", "http", "html", "uuid", "time", "logging",
}


def _local_modules(root: str) -> set:
    """Set of LOCAL product module names = the file stems under ``src/``.

    Why: only a sibling in ``src/`` is a module the product is expected to
    build; importing it without the file existing is the unbuilt-glue bug.
    What: returns {"app", "db", ...} for every ``src/*.py``.
    Test: a tree with src/db.py + src/api.py returns {"db", "api"}.
    """
    sdir = Path(root) / "src"
    if not sdir.is_dir():
        return set()
    return {p.stem for p in sdir.glob("*.py")}


def _iter_py(root: str, subs=("src", "tests")) -> list:
    """All safe-to-read .py files under the given subdirs as (rel, Path).

    Why: every check walks the same src+tests surface; centralize the walk.
    What: yields (rel_path, Path) for files that pass ``_safe_rel``.
    Test: a tree with src/a.py + tests/test_a.py yields both, skips ../ paths.
    """
    base = Path(root)
    out = []
    for sub in subs:
        d = base / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.py")):
            rel = str(p.relative_to(root))
            if _safe_rel(rel):
                out.append((rel, p))
    return out


def _registered_module_names(tree: ast.AST) -> set:
    """Module names referenced via ``registry.register("X", ...)`` /
    ``registry.register(X, ...)`` / ``import registry``.

    Why: hollow glue often is ``registry.register(...)`` against a registry
    module that was never built — surface the registry dependency itself.
    What: returns the set of plain module names the code expects to import
    or register against (here just the registry-style names).
    Test: code ``import registry`` yields {"registry"}.
    """
    names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                for a in node.names:
                    names.add(a.name.split(".")[0])
            elif node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def imported_but_unbuilt(root: str) -> list:
    """Imports of a LOCAL (sibling-in-src) module that has no file.

    Why: catches the canonical hollow-green — ``import registry`` /
    ``registry.register(...)`` while ``src/registry.py`` was never built, so
    the assembled product cannot import. The mock in the test hides it.
    What: for every src/test file, any top-level import of a name that looks
    local (not stdlib, not an existing src module, not the file's own stem)
    AND is referenced as glue (registry-style or a bare local import that
    resolves to no file) ⇒ a violation string.
    Test: tree where tests/test_h.py does ``import registry`` but no
    src/registry.py exists ⇒ one violation; add src/registry.py ⇒ none.
    """
    local = _local_modules(root)
    viol: list = []
    for rel, p in _iter_py(root):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        stem = Path(rel).stem
        for name in sorted(_registered_module_names(tree)):
            if name in _STDLIB or name in local or name == stem:
                continue
            # a lower_snake bare name imported as a sibling that does not
            # exist as a file is product glue that was never built.
            if name.isidentifier() and name.islower() and "." not in name:
                viol.append(
                    f"{rel} imports local module '{name}' but src/{name}.py "
                    f"does not exist (unbuilt glue)")
    return sorted(set(viol))


def _patch_targets(tree: ast.AST) -> list:
    """All string targets of ``@patch(...)`` / ``patch(...)`` /
    ``sys.modules["X"] = ...`` and the names assigned a bare MagicMock().

    Why: a TEST that mocks the product's OWN glue makes the suite green over
    a non-existent module — ban mocking local modules.
    What: returns (kind, target) tuples where kind is 'patch'|'sysmod'|'mock'.
    Test: ``@patch("registry.register")`` → ('patch','registry.register');
    ``sys.modules["registry"]=MagicMock()`` → both ('sysmod','registry') and
    ('mock','registry').
    """
    out: list = []
    for node in ast.walk(tree):
        # @patch("X...") / patch("X...")
        if isinstance(node, ast.Call):
            fn = node.func
            fname = ""
            if isinstance(fn, ast.Attribute):
                fname = fn.attr
            elif isinstance(fn, ast.Name):
                fname = fn.id
            if fname in ("patch", "patch.object") and node.args:
                a0 = node.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                    out.append(("patch", a0.value))
        # sys.modules["X"] = ...
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Subscript)
                        and isinstance(t.value, ast.Attribute)
                        and t.value.attr == "modules"
                        and isinstance(t.value.value, ast.Name)
                        and t.value.value.id == "sys"):
                    key = t.slice
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        out.append(("sysmod", key.value))
                        rhs = node.value
                        # sys.modules["X"] = MagicMock()
                        if _is_magicmock(rhs):
                            out.append(("mock", key.value))
    return out


def _is_magicmock(node: ast.AST) -> bool:
    """True if the expression is a ``MagicMock(...)`` / ``Mock(...)`` call.

    Why: the assigned RHS being a bare mock is the tell of self-mocked glue.
    What: matches Name/Attribute call whose final name is MagicMock or Mock.
    Test: parse ``MagicMock()`` → True; ``foo()`` → False.
    """
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
    return name in ("MagicMock", "Mock")


def mocks_local_module(root: str) -> list:
    """A TEST that mocks a LOCAL product module (its own glue).

    Why: ``sys.modules["registry"]=MagicMock()`` or ``@patch("registry...")``
    makes the suite green while the real module is never imported/built.
    What: for every tests/*.py, any patch/sys.modules target whose head name
    is a local src module (or a not-yet-built sibling referenced as glue)
    ⇒ a violation. Local product modules must never be mocked.
    Test: a test mocking ``registry`` fires; a test patching stdlib
    ``time.time`` does not.
    """
    local = _local_modules(root)
    # also treat names that other files import as local glue (registry) — a
    # module mocked AND imported-but-unbuilt is still your own glue.
    unbuilt = {v.split("'")[1] for v in imported_but_unbuilt(root)
               if "'" in v}
    candidates = local | unbuilt
    viol: list = []
    for rel, p in _iter_py(root, subs=("tests",)):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for kind, target in _patch_targets(tree):
            head = target.split(".")[0]
            if head in candidates:
                viol.append(
                    f"{rel} mocks LOCAL product module '{head}' "
                    f"(via {kind} '{target}') — never mock your own glue")
    return sorted(set(viol))


def _is_stub_body(body: list) -> bool:
    """True if a function/method body is ONLY a placeholder.

    Why: a public handler whose body is just ``pass`` / ``...`` /
    ``raise NotImplementedError`` / a lone docstring / a sole TODO comment is
    not built — it green-lights nothing.
    What: returns True when, after dropping a leading docstring, the body is
    empty/pass/.../bare-raise-NotImplementedError.
    Test: ``def f(): pass`` → True; ``def f(): return 1`` → False.
    """
    stmts = list(body)
    # drop a leading docstring
    if (stmts and isinstance(stmts[0], ast.Expr)
            and isinstance(stmts[0].value, ast.Constant)
            and isinstance(stmts[0].value.value, str)):
        stmts = stmts[1:]
    if not stmts:
        return True
    for s in stmts:
        if isinstance(s, ast.Pass):
            continue
        if (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                and s.value.value is Ellipsis):
            continue
        if isinstance(s, ast.Raise):
            exc = s.exc
            nm = ""
            if isinstance(exc, ast.Call):
                exc = exc.func
            if isinstance(exc, ast.Name):
                nm = exc.id
            elif isinstance(exc, ast.Attribute):
                nm = exc.attr
            if nm == "NotImplementedError":
                continue
        return False
    return True


def stub_bodies(root: str) -> list:
    """Public src functions/methods whose body is only a stub.

    Why: a stubbed public handler ships a non-product — the assembled app
    answers nothing. Catch it before integrate.
    What: walks every ``src/*.py``, flags each public (no leading underscore)
    FunctionDef/AsyncFunctionDef whose body is a placeholder (see
    ``_is_stub_body``). A sole ``# TODO`` body parses to an empty body and is
    therefore caught too.
    Test: src with ``def handle(): pass`` ⇒ one violation; a real body ⇒ none.
    """
    viol: list = []
    for rel, p in _iter_py(root, subs=("src",)):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue
                if _is_stub_body(node.body):
                    viol.append(
                        f"{rel}:{node.lineno} public '{node.name}' is an "
                        f"empty stub (pass / ... / NotImplementedError / "
                        f"docstring only) — not built")
    return sorted(set(viol))


def _asserts_nothing(node) -> bool:
    """True if a test function body never really asserts behaviour.

    A test is 'smoke only' when, after dropping a docstring, it contains no
    ``assert`` (and no ``pytest.raises``/``self.assert*`` call) OR its only
    assertion is a constant-true (``assert True`` / ``assert 1``). Such a test
    is green by construction and proves nothing."""
    real = False
    for n in ast.walk(node):
        if isinstance(n, ast.Assert):
            t = n.test
            # assert True / assert 1 / assert "x" — a constant — proves nothing
            if isinstance(t, ast.Constant) and bool(t.value):
                continue
            real = True
        elif isinstance(n, ast.Call):
            f = n.func
            nm = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if nm.startswith("assert") or nm in ("raises", "fail", "approx"):
                real = True
    return not real


def smoke_only_tests(root: str) -> list:
    """Test functions that assert NOTHING real (smoke-only / always-green).

    Why: a leaf can look green while its test only does ``assert True`` or just
    imports the module — that is a fake proof, forbidden. Catch it like a stub.
    What: walks ``tests/**`` (and any ``test_*`` under src), flags each
    ``test_*`` function whose body never asserts real behaviour
    (see ``_asserts_nothing``).
    Test: a test with ``assert add(2,2)==4`` ⇒ []; one with only ``assert True``
    or no assert ⇒ one violation.
    """
    viol: list = []
    for rel, p in _iter_py(root, subs=("tests", "src")):
        if "test" not in Path(rel).name:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name.startswith("test")):
                if _asserts_nothing(node):
                    viol.append(
                        f"{rel}:{node.lineno} test '{node.name}' asserts nothing "
                        f"real (smoke-only / always green) — not a proof")
    return sorted(set(viol))


_WSGI_BARE_READ = re.compile(r"\.read\(\s*\)")


def wsgi_body_read_violations(root: str) -> list:
    """Flag a WSGI handler that reads the request body UNBOUNDED — a real
    hang-bug, not a style nit.

    Why: ``environ['wsgi.input'].read()`` with no length blocks waiting for an
    EOF that never comes on a real (keep-alive) server, so every POST HANGS —
    the leaf's own unit test passes (its fake environ returns a BytesIO that
    EOFs), but the assembled product times out at the boot gate. A recurring
    weak-model error class; catch it deterministically, model-independently.
    What: for each src module that touches ``wsgi.input``, flag a bare
    ``.read()`` (no length arg) unless that line bounds it via CONTENT_LENGTH.
    Test: a handler doing ``wsgi.input.read()`` ⇒ one violation; one doing
    ``read(int(environ['CONTENT_LENGTH']))`` ⇒ none.
    """
    out: list = []
    srcdir = Path(root) / "src"
    if not srcdir.is_dir():
        return out
    for p in sorted(srcdir.glob("*.py")):
        try:
            src = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "wsgi.input" not in src:
            continue
        for i, line in enumerate(src.splitlines(), 1):
            if _WSGI_BARE_READ.search(line) and "CONTENT_LENGTH" not in line:
                out.append(
                    f"src/{p.name}:{i} reads the WSGI request body UNBOUNDED "
                    "(wsgi.input.read() with no length) — this HANGS every POST "
                    "under a real server; read exactly "
                    "int(environ.get('CONTENT_LENGTH') or 0) bytes")
    return out


# --- env-resource cached in a module global (per-call contract) -------------
# Known db-path env keys + a generic *_DB / *_DB_PATH suffix rule so the check
# is not hardcoded to one product's variable name.
_DB_ENV_KEYS = ("NOTES_DB", "NOTES_DB_PATH", "MARKETPLACE_DB", "APP_DB",
                "DB_PATH")


def _const_str(node) -> Optional[str]:
    """The string value of a Constant (unwrapping a py<3.9 Index), else None."""
    if isinstance(node, ast.Index):  # pragma: no cover - legacy py
        node = node.value
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_db_key(key: str) -> bool:
    """True for a db-path env var: a known key, or any *_DB / *DB_PATH name."""
    k = key.upper()
    return k in _DB_ENV_KEYS or k.endswith("_DB") or k.endswith("DB_PATH")


def _reads_db_env(tree) -> bool:
    """True if a subtree reads a db-path from the environment:
    ``os.environ['NOTES_DB']`` / ``os.environ.get('NOTES_DB')`` /
    ``os.getenv('NOTES_DB')`` (key matched by :func:`_is_db_key`)."""
    for n in ast.walk(tree):
        key = None
        if isinstance(n, ast.Subscript):
            v = n.value
            if isinstance(v, ast.Attribute) and v.attr == "environ":
                key = _const_str(n.slice)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            f = n.func
            is_env = (
                (f.attr == "getenv" and isinstance(f.value, ast.Name)
                 and f.value.id == "os")
                or (f.attr == "get" and isinstance(f.value, ast.Attribute)
                    and f.value.attr == "environ"))
            if is_env and n.args:
                key = _const_str(n.args[0])
        if key and _is_db_key(key):
            return True
    return False


def _is_connect_call(node) -> bool:
    """True if a call opens a connection: ``*.connect(...)`` / ``Connection(...)``
    / any callable whose name ends in 'connect'."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    name = f.attr if isinstance(f, ast.Attribute) else (
        f.id if isinstance(f, ast.Name) else "")
    return name in ("connect", "Connection") or name.lower().endswith("connect")


def _names_tested_falsy(test) -> set:
    """Names checked as 'not yet set' in an ``if`` guard: ``X is None`` /
    ``not X`` / either side of an ``and``/``or``."""
    out: set = set()
    if (isinstance(test, ast.Compare) and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Is)
            and isinstance(test.left, ast.Name)
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None):
        out.add(test.left.id)
    elif (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
          and isinstance(test.operand, ast.Name)):
        out.add(test.operand.id)
    elif isinstance(test, ast.BoolOp):
        for v in test.values:
            out |= _names_tested_falsy(v)
    return out


def _connection_cache_global(fn) -> Optional[str]:
    """If a function caches a CONNECTION in one of its ``global`` names behind
    an ``is None`` guard, return that name (the singleton), else None."""
    declared = {nm for n in ast.walk(fn) if isinstance(n, ast.Global)
                for nm in n.names}
    if not declared:
        return None
    cached: set = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and any(_is_connect_call(c)
                                             for c in ast.walk(n.value)):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id in declared:
                    cached.add(t.id)
    if not cached:
        return None
    for n in ast.walk(fn):
        if isinstance(n, ast.If):
            for nm in _names_tested_falsy(n.test):
                if nm in cached:
                    return nm
    return None


def db_connection_singleton_violations(root: str) -> list:
    """Flag a module that opens its db ONCE and caches the handle — a real
    cross-test corruption bug, not a style nit.

    Why: a weak model 'reuses' the connection in a module global
    (``_conn = None; if _conn is None: _conn = sqlite3.connect(os.environ[...]))``
    or opens it at import time. The env db-path is then read ONCE, so the leaf's
    own test passes, but every SIBLING test sets a FRESH ``NOTES_DB`` and gets
    the stale handle — whose tables live in the first db — so they die with
    'no such table'. The boot-gate (one process, one db) cannot see this; catch
    it statically, model-independently.
    What: in any src module that reads a db-path env var, flag (a) a module-level
    connect bound to a global (import-time singleton) and (b) a function caching
    a connection in a global guarded by ``is None``.
    Test: a module caching ``_conn`` from ``os.environ['NOTES_DB']`` ⇒ one
    violation; one that connects fresh per call ⇒ none.
    """
    out: list = []
    srcdir = Path(root) / "src"
    if not srcdir.is_dir():
        return out
    for p in sorted(srcdir.glob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        if not _reads_db_env(tree):
            continue
        for n in tree.body:  # import-time singleton at module scope
            if isinstance(n, ast.Assign) and any(_is_connect_call(c)
                                                 for c in ast.walk(n.value)):
                out.append(
                    f"src/{p.name}:{n.lineno}: this module opens the db connection "
                    "at IMPORT time (a module-level connect). The NOTES_DB env var "
                    "is then read exactly ONCE — at import — so when a sibling test "
                    "sets a fresh NOTES_DB and imports this module, it gets the "
                    "STALE connection whose tables live in the FIRST db, and dies "
                    "with sqlite3.OperationalError: no such table. FIX: do NOT open "
                    "at module scope; move the connect INTO a function that re-reads "
                    "os.environ['NOTES_DB'] and returns a fresh "
                    "sqlite3.connect(...) on EVERY call.")
        for fn in ast.walk(tree):  # function-level cached singleton
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nm = _connection_cache_global(fn)
                if nm:
                    out.append(
                        f"src/{p.name}:{fn.lineno}: this function CACHES the db "
                        f"connection in the module global '{nm}' (the "
                        f"`if {nm} is None:` guard means it opens once and reuses "
                        "forever). NOTES_DB is therefore read only on the FIRST "
                        "call; every later caller — a sibling test with its OWN "
                        "fresh NOTES_DB — gets the stale handle whose tables live "
                        "in the first db, and dies with "
                        "sqlite3.OperationalError: no such table. FIX: remove the "
                        f"'{nm}' cache; make connect() re-read "
                        "os.environ['NOTES_DB'] and return a NEW "
                        "sqlite3.connect(...) on EVERY call (open per call, close "
                        "when done).")
    return sorted(set(out))


# --- query a table without ensuring its schema ------------------------------
_SQL_VERB = re.compile(r"(?i)\b(?:select|insert|update|delete|create)\b")
_RE_CREATE = re.compile(r"(?i)\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?"
                        r"[\"'`\[]?(\w+)")
_RE_QUERY = re.compile(r"(?i)\b(?:from|into|update|join)\s+[\"'`\[]?(\w+)")


def _sql_literals(tree) -> list:
    """String constants in a module that look like SQL (contain a verb)."""
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and _SQL_VERB.search(n.value)]


def _local_imports(tree, stems: set) -> set:
    """Project-local module stems this module imports (``import db_connect`` /
    ``from db_connect import connect`` / ``from . import db_connect``)."""
    out: set = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                b = a.name.split(".")[0]
                if b in stems:
                    out.add(b)
        elif isinstance(n, ast.ImportFrom):
            if n.module and n.module.split(".")[0] in stems:
                out.add(n.module.split(".")[0])
            for a in n.names:
                if a.name in stems:
                    out.add(a.name)
    return out


def schema_guarantee_violations(root: str) -> list:
    """Flag a self-connecting module that QUERIES a table it never ensures.

    Why: the decomposer often splits the db into connect/insert/read leaves, but
    a sibling (e.g. the WSGI endpoints) then opens its OWN connection and runs
    ``SELECT/INSERT ... FROM <t>`` without going through the schema-initialising
    connect() and without a ``CREATE TABLE IF NOT EXISTS`` of its own. On a fresh
    NOTES_DB that is 'no such table'. The leaf's own test may pass (it seeds the
    row first), but the assembled product on an empty db fails — a recurring
    cross-module class, distinct from the cached-connection one.
    What: for each src module that self-connects (reads a db env or opens a
    connection) and queries table T, require it to either CREATE T itself or
    import a local module that creates T; else flag.
    Test: an endpoints module that ``sqlite3.connect``s and selects ``notes``
    while only db_connect creates it ⇒ one violation; the same module importing
    db_connect ⇒ none.
    """
    srcdir = Path(root) / "src"
    if not srcdir.is_dir():
        return []
    files = {p.stem: p for p in srcdir.glob("*.py")}
    stems = set(files)
    created: dict = {}
    queried: dict = {}
    imports: dict = {}
    selfconn: dict = {}
    for stem, p in files.items():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        cr: set = set()
        q: set = set()
        for s in _sql_literals(tree):
            cr |= {m.group(1).lower() for m in _RE_CREATE.finditer(s)}
            q |= {m.group(1).lower() for m in _RE_QUERY.finditer(s)}
        created[stem] = cr
        queried[stem] = q - cr
        imports[stem] = _local_imports(tree, stems)
        selfconn[stem] = (_reads_db_env(tree)
                          or any(_is_connect_call(n) for n in ast.walk(tree)))
    creators: dict = {}
    for stem, cr in created.items():
        for t in cr:
            creators.setdefault(t, set()).add(stem)
    out: list = []
    for stem, q in queried.items():
        if not selfconn.get(stem):          # a DI consumer (gets a conn) is fine
            continue
        for t in q:
            if any(imp in creators.get(t, set()) for imp in imports.get(stem, ())):
                continue
            owner = sorted(creators.get(t, set()))
            # Maximally descriptive rework feedback: this string is handed
            # VERBATIM to the builder on rework, so it states the exact symptom,
            # why the leaf's own test hides it, and a concrete fix that names the
            # real schema-owning module when one exists.
            if owner:
                fix = (f"(a) REUSE the schema-owning module: `import {owner[0]}` "
                       f"then `conn = {owner[0]}.connect()` (it already runs "
                       f"CREATE TABLE IF NOT EXISTS {t}); or ")
            else:
                fix = ("(a) add a CREATE TABLE IF NOT EXISTS for it in a shared "
                       "db module and import that; or ")
            out.append(
                f"src/{stem}.py: this module opens its OWN db connection and runs "
                f"SQL against table '{t}', but nothing guarantees '{t}' exists at "
                f"that point. On the ASSEMBLED product the database starts EMPTY, "
                f"so the first query raises "
                f"sqlite3.OperationalError: no such table: {t}. Your own unit test "
                f"can pass (it seeds rows in a db it set up), but the product on a "
                f"fresh NOTES_DB cannot. FIX — pick ONE: {fix}"
                f"(b) CREATE the schema yourself before ANY query: "
                f"conn.execute('CREATE TABLE IF NOT EXISTS {t} (...)'). Never rely "
                f"on another leaf's test having run first — every connection to "
                f"NOTES_DB is fresh and independent.")
    return sorted(set(out))


def realness_violations(root: str, modules=None) -> list:
    """Combined 'no fake product' check for a LEAF or BRANCH at REVIEW time:
    stub bodies + mocks of a local module + smoke-only tests + unbounded WSGI
    body reads + db-connection singletons. When ``modules`` is given (a set of
    src stems, e.g. the node's own file), only violations in those files are
    returned — so a per-node review flags ONLY that node's hollow code. Used by
    the leaf gate; the full set runs at integrate."""
    v = (stub_bodies(root) + mocks_local_module(root) + smoke_only_tests(root)
         + wsgi_body_read_violations(root)
         + db_connection_singleton_violations(root)
         + schema_guarantee_violations(root))
    if modules:
        keep = []
        for line in v:
            rel = line.split(":", 1)[0]
            stem = Path(rel).name
            stem = stem[5:] if stem.startswith("test_") else stem
            stem = stem[:-3] if stem.endswith(".py") else stem
            if stem in modules:
                keep.append(line)
        return keep
    return v


def tests_collect(root: str) -> list:
    """Run ``pytest --collect-only`` — a collection error is a violation.

    Why: the cheapest proof the corpus even imports; an ImportError at
    collection (e.g. a missing local module the mocks did NOT cover) zeroes
    the verdict's honesty.
    What: runs collect-only in the workspace; on non-zero exit returns one
    violation carrying the error tail. Exit 5 (nothing collected) is NOT a
    failure here.
    Test: a collectable tree ⇒ []; a tree with a syntactically broken test
    ⇒ one violation with the tail.
    """
    if not (Path(root) / "tests").is_dir():
        return []
    cmd = [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q",
           "--no-header", "-p", "no:cacheprovider", "--import-mode=importlib"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=PYTEST_TIMEOUT, cwd=root,
                              env=hermetic_env(root))
    except (subprocess.TimeoutExpired, OSError) as exc:  # noqa: BLE001
        return [f"pytest --collect-only failed to run: {exc}"]
    if proc.returncode in (0, 5):
        return []
    out = ((proc.stdout or "") + (proc.stderr or ""))[-800:]
    return [f"pytest collection failed (exit {proc.returncode}): {out}"]


def run_all(root: str) -> list:
    """Run every contract check; return the combined violation list.

    Why: one entry point the pre-integrate gate calls; reuses the existing
    cross-module / duplicate-table / non-ASCII detectors so the gate covers
    every known hollow-green class in one place.
    What: concatenates imported_but_unbuilt, mocks_local_module, stub_bodies,
    tests_collect, plus the reused detectors (formatted to strings).
    Test: a clean assembled tree ⇒ []; the hollow-green tree ⇒ non-empty.
    """
    viol: list = []
    viol += imported_but_unbuilt(root)
    viol += mocks_local_module(root)
    viol += stub_bodies(root)
    viol += smoke_only_tests(root)        # forbid always-green / no-assert tests
    viol += wsgi_body_read_violations(root)   # forbid POST-hanging body reads
    viol += db_connection_singleton_violations(root)  # forbid cached-db handle
    viol += schema_guarantee_violations(root)  # forbid query-without-schema
    # reuse existing deterministic detectors (don't duplicate them)
    for f, mod, name in _cross_module_import_violations(root):
        viol.append(f"{f} imports '{name}' from '{mod}' but src/{mod}.py "
                    f"does not define it")
    for tbl, mods in sorted(_duplicate_table_owners(root).items()):
        viol.append(f"table '{tbl}' defined by >1 module: {', '.join(mods)}")
    for f, ln, ch, cp in _non_ascii_offenders(root):
        viol.append(f"{f}:{ln} non-ASCII/syntax: {ch!r} ({cp})")
    # collectability last (most expensive — a subprocess)
    viol += tests_collect(root)
    return viol


# ---------------------------------------------------------------------------
# B2 boot-gate: the un-mockable assembly smoke (engine-owned, ROOT only).
# ---------------------------------------------------------------------------

# constitution rules that DECLARE a product entry: the boot-gate only runs
# when the constitution asks for a WSGI app at src/app.py exposing wsgi_app.
def constitution_declares_entry(constitution) -> Optional[str]:
    """Return the declared entry module rel-path if the constitution asks for
    one, else None (skip the boot-gate — keeps p4/p5 unaffected).

    Why: B2 must stay constitution-keyed — no entry declared ⇒ no boot-gate.
    What: scans the constitution rule strings for a WSGI/app.py + wsgi_app
    declaration; returns "src/app.py" when found.
    Test: a constitution mentioning "src/app.py exposes wsgi_app" ⇒
    "src/app.py"; an empty constitution ⇒ None.
    """
    blob = " ".join(str(r) for r in (constitution or [])).lower()
    if "wsgi_app" in blob and ("app.py" in blob or "src/app" in blob):
        return "src/app.py"
    return None


def goal_wants_notes(constitution, goal: str = "") -> bool:
    """True if the goal/constitution implies a /notes round-trip.

    Why: the boot-gate exercises /notes only when the product is a notes app.
    What: substring check over goal + constitution for "/notes".
    Test: a p6 constitution mentioning "POST /notes" ⇒ True; p4 ⇒ False.
    """
    blob = (str(goal) + " " + " ".join(str(r) for r in (constitution or []))).lower()
    return "/notes" in blob


# The probe SCRIPT run in a FRESH python subprocess so that worker-test
# sys.modules mocks and conftest fixtures CANNOT leak into the assembly —
# this is the whole point of B2: import the real product entry, no mocking.
def _derive_boot_routes(constitution, goal: str = ""):
    """Derive the boot-gate routes from the case WORDING — never hardcode a path.

    Scans the goal + constitution for ``VERB /path`` mentions and classifies a
    liveness GET (health/status/ready/live/ping), the JSON round-trip path (one
    path with both POST and GET), and an HTML page route (a GET path described
    near page/form/html/render wording). Any of the three may be "" when the
    case does not declare it — the probe then skips that check. Pure text,
    model-independent. (A late HITL page like /ui is NOT in the base goal, so it
    is checked by its own node, not smuggled in here as a literal.)"""
    parts = constitution if isinstance(constitution, (list, tuple)) \
        else [str(constitution or "")]
    text = (goal or "") + "\n" + "\n".join(str(p) for p in parts)
    low = text.lower()
    methods: dict = {}
    for m in re.finditer(r"\b(GET|POST|PUT|DELETE|PATCH)\s+(/[A-Za-z0-9_./-]+)",
                         text, re.I):
        p = m.group(2).rstrip("/.,;:)\"'")
        methods.setdefault(p, set()).add(m.group(1).upper())
    ok_route = html_route = notes_route = ""
    for p, ms in methods.items():
        pl = p.lower()
        if not ok_route and any(k in pl for k in
                                ("health", "status", "ready", "live", "ping")):
            ok_route = p
        if not notes_route and "POST" in ms and "GET" in ms:
            notes_route = p
    for p, ms in methods.items():
        if "GET" in ms and p not in (ok_route, notes_route):
            i = low.find(p.lower())
            if i >= 0 and any(k in low[max(0, i - 90):i + 90]
                              for k in ("html", "page", "browser", "form",
                                        "render")):
                html_route = p
                break
    return ok_route, html_route, notes_route


_BOOT_PROBE = r'''
import glob, importlib, io, json, os, sys, tempfile
from pathlib import Path

ws = Path(sys.argv[1]).resolve()
want_notes = sys.argv[2] == "1"
# routes are DERIVED from the case wording and passed in — no hardcoded paths.
# Any empty route means the case did not declare it; that check is skipped.
ok_route = sys.argv[3] if len(sys.argv) > 3 else ""
html_route = sys.argv[4] if len(sys.argv) > 4 else ""
notes_route = sys.argv[5] if len(sys.argv) > 5 else "/notes"
sys.path.insert(0, str(ws / "src"))
# give the product a private, throwaway sqlite db if it reads these env vars
db = os.path.join(tempfile.mkdtemp(prefix="bootgate-"), "boot.db")
for k in ("MARKETPLACE_DB", "NOTES_DB", "APP_DB", "DB_PATH"):
    os.environ.setdefault(k, db)

def fail(msg):
    print("BOOTGATE_FAIL " + msg)
    raise SystemExit(0)

# Find the WSGI callable. Prefer the constitution's declared entry (src/app.py
# exposing wsgi_app), but FALL BACK to discovering any assembled module under
# src/ that exposes a WSGI callable — the product is judged on whether it BOOTS
# and answers, not on a filename. This mirrors how serve-product.sh boots it.
cands = []
if (ws / "src" / "app.py").exists():
    cands.append("app")
for f in sorted(glob.glob(str(ws / "src" / "*.py"))):
    stem = Path(f).stem
    if stem != "__init__" and stem not in cands:
        cands.append(stem)
wsgi = None
last_err = ""
for name in cands:
    try:
        m = importlib.import_module(name)  # real product module — NO mocking
    except Exception as exc:  # noqa: BLE001
        last_err = "import %s: %r" % (name, exc)
        continue
    for attr in ("wsgi_app", "application", "app"):
        c = getattr(m, attr, None)
        if callable(c):
            wsgi = c
            break
    if wsgi is not None:
        break
if wsgi is None:
    fail("no module under src/ exposes a callable wsgi_app/application/app"
         + ((" (last import error: " + last_err + ")") if last_err else ""))

def call(method, path, payload=None, query=""):
    body = json.dumps(payload).encode() if payload is not None else b""
    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path,
        "QUERY_STRING": query, "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body), "wsgi.errors": sys.stderr,
        "SERVER_NAME": "boot", "SERVER_PORT": "0",
        "wsgi.url_scheme": "http",
    }
    cap = {}
    def start_response(status, headers, exc_info=None):
        cap["status"] = int(str(status).split()[0])
    chunks = wsgi(environ, start_response)
    raw = b"".join(chunks if chunks else [])
    return cap.get("status", 0), raw

# minimum contract — every route DERIVED from the case wording (no hardcode);
# an empty route means the case did not declare it, so that check is skipped.
if ok_route:
    st, _ = call("GET", ok_route)
    if st != 200:
        fail("GET %s -> %s (expected 200)" % (ok_route, st))

if html_route:
    st, raw = call("GET", html_route)
    text = (raw or b"").decode("utf-8", "replace").lower()
    if st != 200 or ("<" not in text):
        fail("GET %s -> %s / not HTML" % (html_route, st))

if want_notes and notes_route:
    st, raw = call("POST", notes_route, {"text": "bootgate"})
    if st not in (200, 201):
        fail("POST %s -> %s" % (notes_route, st))
    st, raw = call("GET", notes_route)
    if st != 200:
        fail("GET %s -> %s" % (notes_route, st))
    try:
        data = json.loads(raw or b"{}")
        items = data.get("items", data if isinstance(data, list) else [])
        texts = " ".join(str(i.get("text", "")) for i in items
                         if isinstance(i, dict))
    except Exception as exc:  # noqa: BLE001
        fail("GET %s body not JSON: %r" % (notes_route, exc))
    if "bootgate" not in texts:
        fail("POST then GET %s did not round-trip the note" % notes_route)

print("BOOTGATE_OK")
'''


def boot_gate(root: str, constitution, goal: str = "",
              timeout: Optional[int] = None) -> tuple:
    """The un-mockable assembly smoke. Returns (passed, detail).

    Why: tests can mock away the missing glue; a FRESH subprocess that
    imports the real ``src/app.py`` and drives ``wsgi_app`` over real WSGI
    calls cannot — if the product does not assemble, this is RED. This is
    the engine-owned hard floor under B2.
    What: only runs when the constitution declares an entry; spawns a
    separate python process running ``_BOOT_PROBE`` (so worker-test
    sys.modules mocks / conftest do not leak), parses its single-line
    verdict. Robust: timeout + captured output become the failure hint.
    Test: an assembled app with /health + /ui ⇒ (True, ...); a missing
    src/app.py ⇒ (False, "...cannot import...").
    """
    entry = constitution_declares_entry(constitution)
    if entry is None:
        return True, "(no entry declared — boot-gate skipped)"
    want_notes = "1" if goal_wants_notes(constitution, goal) else "0"
    ok_route, html_route, notes_route = _derive_boot_routes(constitution, goal)
    tmo = timeout if timeout is not None else PYTEST_TIMEOUT
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _BOOT_PROBE, str(root), want_notes,
             ok_route, html_route, notes_route],
            capture_output=True, text=True, timeout=tmo,
            env=hermetic_env(root))
    except subprocess.TimeoutExpired:
        return False, f"boot-gate TIMED OUT after {tmo}s assembling {entry}"
    except OSError as exc:  # noqa: BLE001
        return False, f"boot-gate could not start: {exc}"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if "BOOTGATE_OK" in out:
        return True, "boot-gate green: entry assembled, endpoints answered"
    # surface the probe's own reason + a tail of any traceback as the hint
    return False, "boot-gate RED: " + out[-600:]
