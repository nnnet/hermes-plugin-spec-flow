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


def realness_violations(root: str, modules=None) -> list:
    """Combined 'no fake product' check for a LEAF or BRANCH at REVIEW time:
    stub bodies + mocks of a local module + smoke-only tests + unbounded WSGI
    body reads. When ``modules`` is given (a set of src stems, e.g. the node's
    own file), only violations in those files are returned — so a per-node
    review flags ONLY that node's hollow code. Used by the leaf gate; the full
    set runs at integrate."""
    v = (stub_bodies(root) + mocks_local_module(root) + smoke_only_tests(root)
         + wsgi_body_read_violations(root))
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
                              timeout=PYTEST_TIMEOUT, cwd=root)
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
_BOOT_PROBE = r'''
import glob, importlib, io, json, os, sys, tempfile
from pathlib import Path

ws = Path(sys.argv[1]).resolve()
want_notes = sys.argv[2] == "1"
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

# minimum contract: GET /health -> 200, GET /ui returns HTML
st, _ = call("GET", "/health")
if st != 200:
    fail("GET /health -> %s (expected 200)" % st)

st, raw = call("GET", "/ui")
text = (raw or b"").decode("utf-8", "replace").lower()
if st != 200 or ("<" not in text):
    fail("GET /ui -> %s / not HTML" % st)

if want_notes:
    st, raw = call("POST", "/notes", {"text": "bootgate"})
    if st not in (200, 201):
        fail("POST /notes -> %s" % st)
    st, raw = call("GET", "/notes")
    if st != 200:
        fail("GET /notes -> %s" % st)
    try:
        data = json.loads(raw or b"{}")
        items = data.get("items", data if isinstance(data, list) else [])
        texts = " ".join(str(i.get("text", "")) for i in items
                         if isinstance(i, dict))
    except Exception as exc:  # noqa: BLE001
        fail("GET /notes body not JSON: %r" % (exc,))
    if "bootgate" not in texts:
        fail("POST then GET /notes did not round-trip the note")

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
    tmo = timeout if timeout is not None else PYTEST_TIMEOUT
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _BOOT_PROBE, str(root), want_notes],
            capture_output=True, text=True, timeout=tmo)
    except subprocess.TimeoutExpired:
        return False, f"boot-gate TIMED OUT after {tmo}s assembling {entry}"
    except OSError as exc:  # noqa: BLE001
        return False, f"boot-gate could not start: {exc}"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if "BOOTGATE_OK" in out:
        return True, "boot-gate green: entry assembled, endpoints answered"
    # surface the probe's own reason + a tail of any traceback as the hint
    return False, "boot-gate RED: " + out[-600:]
