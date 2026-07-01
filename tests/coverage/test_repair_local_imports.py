"""Deterministic import repair — the general fix for the recurring root cause.

Across dozens of live runs no product reached an honest green because the weak
model wrote leaf bodies that import siblings by INVENTED module names (``db``,
``src.storage``) that do not exist. The pre-integrate gate DETECTS this, but it
fed every violation to an LLM rework round that (under provider throttle) never
converged — a different invented name each run.

``repair_local_imports`` closes the class deterministically: it re-points a
``from X import Y`` to the module that ACTUALLY defines Y, using the product's
own symbol table. No LLM, no provider cost, model-independent, and
MEDIUM-AGNOSTIC — a CLI / library / pipeline leaf is repaired exactly like a web
leaf, because the mechanism is "bind a real symbol to its real module", not
"wire an HTTP route".
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from harness import pytest_verifier as pv  # noqa: E402


def _write(root, rel, body):
    p = pathlib.Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_non_web_pipeline_import_is_repaired(tmp_path):
    # a data-pipeline CLI — NO HTTP anywhere. cli.py imports from an invented
    # module 'db'; the real symbols live in engine.py.
    _write(tmp_path, "src/engine.py",
           "def run_pipeline(rows):\n    return [r * 2 for r in rows]\n"
           "def load(path):\n    return [1, 2, 3]\n")
    _write(tmp_path, "src/cli.py",
           "from db import run_pipeline, load\n"
           "def main():\n    return run_pipeline(load('x'))\n")
    fixed = pv.repair_local_imports(str(tmp_path))
    assert fixed, "invented import should be repaired"
    cli = (tmp_path / "src" / "cli.py").read_text()
    assert "from engine import run_pipeline, load" in cli
    assert "from db import" not in cli


def test_dotted_missing_module_is_repaired(tmp_path):
    # v137's exact product-breaker: `from src.storage import ...` where the
    # symbols actually live in core.py.
    _write(tmp_path, "src/core.py",
           "def init_db():\n    return {}\n"
           "def save_note(t):\n    return 1\n")
    _write(tmp_path, "src/web_ui.py",
           "from src.storage import init_db, save_note\n"
           "def get_ui(p, q):\n    return (200, '<html>')\n")
    fixed = pv.repair_local_imports(str(tmp_path))
    assert fixed
    ui = (tmp_path / "src" / "web_ui.py").read_text()
    assert "from core import init_db, save_note" in ui
    assert "src.storage" not in ui


def test_real_module_that_provides_symbol_is_untouched(tmp_path):
    _write(tmp_path, "src/core.py", "def save(t):\n    return 1\n")
    _write(tmp_path, "src/api.py",
           "from core import save\n"
           "def h():\n    return save('x')\n")
    before = (tmp_path / "src" / "api.py").read_text()
    fixed = pv.repair_local_imports(str(tmp_path))
    assert fixed == []
    assert (tmp_path / "src" / "api.py").read_text() == before


def test_symbol_owned_by_nobody_stays_red(tmp_path):
    # 'ghost' exists in no module — repair must NOT invent it; the import is
    # left for the doctor and the gate still flags it.
    _write(tmp_path, "src/core.py", "def save(t):\n    return 1\n")
    _write(tmp_path, "src/api.py",
           "from db import ghost\n"
           "def h():\n    return ghost()\n")
    fixed = pv.repair_local_imports(str(tmp_path))
    assert fixed == []
    assert "from db import ghost" in (tmp_path / "src" / "api.py").read_text()


def test_ambiguous_symbol_is_not_repaired(tmp_path):
    # two modules define `handle` — the owner is ambiguous, so repair abstains.
    _write(tmp_path, "src/a.py", "def handle():\n    return 1\n")
    _write(tmp_path, "src/b.py", "def handle():\n    return 2\n")
    _write(tmp_path, "src/c.py",
           "from missing import handle\n"
           "def run():\n    return handle()\n")
    fixed = pv.repair_local_imports(str(tmp_path))
    assert fixed == []
    assert "from missing import handle" in (tmp_path / "src" / "c.py").read_text()


def test_multi_owner_import_splits(tmp_path):
    # one bad import whose symbols live in TWO different real modules → split.
    _write(tmp_path, "src/store.py", "def save(t):\n    return 1\n")
    _write(tmp_path, "src/render.py", "def html(x):\n    return '<p>'\n")
    _write(tmp_path, "src/ui.py",
           "from helpers import save, html\n"
           "def page():\n    return html(save('x'))\n")
    fixed = pv.repair_local_imports(str(tmp_path))
    assert fixed
    ui = (tmp_path / "src" / "ui.py").read_text()
    assert "from store import save" in ui
    assert "from render import html" in ui
    assert "helpers" not in ui


def test_stdlib_import_is_never_touched(tmp_path):
    _write(tmp_path, "src/core.py",
           "import json\n"
           "def dump(x):\n    return json.dumps(x)\n")
    before = (tmp_path / "src" / "core.py").read_text()
    fixed = pv.repair_local_imports(str(tmp_path))
    assert fixed == []
    assert (tmp_path / "src" / "core.py").read_text() == before
