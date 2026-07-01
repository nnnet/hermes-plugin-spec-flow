"""C3+C4 — the engine must build ANY service shape, not only a web app.

C3: infer the project SHAPE (web|cli|lib) deterministically and synthesise the
matching entry — for a library that means re-exporting the public API, the
non-web analogue of the WSGI router.
C4: acceptance = run the described BEHAVIOUR (import the entry, the capability is
callable), not curl an HTTP route.

Both are pure/deterministic and make NO HTTP assumption.
"""

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import spec_flow_runner as sfr  # noqa: E402


def test_kind_web_from_routes():
    assert sfr._project_kind(["The service exposes GET /health and POST /notes"]) \
        == "web"


def test_kind_cli_from_wording():
    assert sfr._project_kind(["A command-line tool that counts words via argv"]) \
        == "cli"


def test_kind_lib_from_wording():
    assert sfr._project_kind(
        ["A reusable Python library that exposes a parse() function"]) == "lib"


def test_kind_unknown_is_empty():
    assert sfr._project_kind(["Do something vague"]) == ""


def test_synth_lib_entry_reexports_public_api(tmp_path):
    # build a two-module library, synthesise the entry, import it.
    src = tmp_path / "src"
    src.mkdir()
    (src / "tokenize.py").write_text("def split_words(s):\n    return s.split()\n")
    (src / "count.py").write_text("def word_count(s):\n    return len(s.split())\n")
    entry = sfr._synthesize_lib_entry(
        {"split_words": "tokenize", "word_count": "count"})
    (src / "wordlib.py").write_text(entry)
    probe = ("import wordlib\n"
             "assert wordlib.word_count('a b c') == 3\n"
             "assert wordlib.split_words('a b') == ['a', 'b']\n"
             "print('OK')\n")
    r = subprocess.run([sys.executable, "-c", probe], cwd=str(src),
                       capture_output=True, text=True)
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr


def test_capability_probe_passes_and_fails(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("def run():\n    return 42\n")
    ok = sfr._capability_probe_src("app", "run()")
    r = subprocess.run([sys.executable, "-c", ok], cwd=str(src),
                       capture_output=True, text=True)
    assert r.returncode == 0 and "CAPABILITY_OK" in r.stdout, r.stderr
    # a missing capability must FAIL the probe (honest RED)
    bad = sfr._capability_probe_src("app", "missing_thing")
    r2 = subprocess.run([sys.executable, "-c", bad], cwd=str(src),
                        capture_output=True, text=True)
    assert r2.returncode != 0
