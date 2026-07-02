"""Audit rule (revision P6, v150, spec_flow_runner.py ~7368/~7406): the root
capability/boot probes must be HERMETIC — host PYTHONPATH / user-site .pth
files must never satisfy a workspace import (the v149 phantom-import class).

v150: ``_nonweb_capability_boots`` and ``_assembled_product_boots`` launched
``python3 -c probe`` with the inherited environment, so a module absent in the
workspace could resolve into an unrelated host install and turn an honest
ModuleNotFoundError into a false green boot verdict.

Contract enforced here:
  * a probe over an entry that imports a module ONLY present on the host
    PYTHONPATH goes RED (the import must not resolve);
  * a self-contained workspace still probes green (the isolation flag must
    not break legitimate src-only imports).

Deterministic: real subprocess probes over tmp workspaces, no LLM.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

_WSGI_ENTRY = '''\
import foreign_dep


def wsgi_app(environ, start_response):
    start_response("200 OK", [("Content-Type", "application/json")])
    return [b'{"status": "ok"}']


application = wsgi_app
'''


def _engine(tmp_path):
    return sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)


def _host_pollution(tmp_path, monkeypatch):
    host = tmp_path / "host-site"
    host.mkdir()
    (host / "foreign_dep.py").write_text("VALUE = 'host leak'\n")
    monkeypatch.setenv("PYTHONPATH", str(host))
    return host


def test_capability_probe_rejects_host_pythonpath(tmp_path, monkeypatch):
    _host_pollution(tmp_path, monkeypatch)
    eng = _engine(tmp_path)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "tool.py").write_text(
        "import foreign_dep\n\n\ndef run(x):\n    return x\n")
    ok, detail = eng._nonweb_capability_boots(
        {"kind": "lib", "entry": "src/tool.py", "exposes": ["run"]})
    assert not ok, (
        "capability probe satisfied a workspace import from the HOST "
        "PYTHONPATH — the sterile-oracle boundary is broken: " + detail)


def test_capability_probe_still_green_on_self_contained_src(tmp_path):
    eng = _engine(tmp_path)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "helper.py").write_text("def add(a, b):\n    return a + b\n")
    (src / "tool.py").write_text(
        "from helper import add\n\n\ndef run(x):\n    return add(x, 1)\n")
    ok, detail = eng._nonweb_capability_boots(
        {"kind": "lib", "entry": "src/tool.py", "exposes": ["run"]})
    assert ok, "isolation must not break legitimate src-only imports: " + detail


def test_root_boot_probe_rejects_host_pythonpath(tmp_path, monkeypatch):
    _host_pollution(tmp_path, monkeypatch)
    eng = _engine(tmp_path)
    eng._constitution = ["Serve GET /health.",
                        "The entry src/app.py exposes wsgi_app."]
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "app.py").write_text(_WSGI_ENTRY)
    ok, detail = eng._assembled_product_boots()
    assert not ok, (
        "root boot probe satisfied a workspace import from the HOST "
        "PYTHONPATH — the sterile-oracle boundary is broken: " + detail)


def test_root_boot_probe_still_green_on_self_contained_src(tmp_path):
    eng = _engine(tmp_path)
    eng._constitution = ["Serve GET /health.",
                        "The entry src/app.py exposes wsgi_app."]
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "app.py").write_text(_WSGI_ENTRY.replace("import foreign_dep\n", ""))
    ok, detail = eng._assembled_product_boots()
    assert ok, "isolation must not break a legitimate boot: " + detail
