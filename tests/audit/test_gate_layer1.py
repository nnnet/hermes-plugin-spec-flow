"""Layer-1 audit gates: known-answer tests on the real v150 run + a clean
synthetic run that must produce zero findings (false-positive guard).

The v150 run directory lives under tests/runs-out/ (gitignored, kept on
disk). When it is absent the known-answer tests skip — the gate must not
depend on an unsaved run.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

AUDIT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AUDIT_DIR.parents[1]
V150_RUN = REPO_ROOT / "tests" / "runs-out" / "2026-07-02T20-18-57__v150__p6-micro-notes"

sys.path.insert(0, str(AUDIT_DIR))

import consistency  # noqa: E402
import journal_invariants  # noqa: E402

needs_v150 = pytest.mark.skipif(
    not V150_RUN.is_dir(), reason="v150 run dir not present on this checkout"
)


# --------------------------------------------------------------------------
# known-answer: the real v150 run
# --------------------------------------------------------------------------

@needs_v150
def test_v150_consistency_finds_unowned_db_module():
    """core spec talks about src/db.py, but no tree node owns a db module."""
    findings = consistency.audit(V150_RUN)
    unowned = [
        f
        for f in findings
        if f.kind == "spec_mentions_unowned_file"
        and f.file.endswith("core.md")
        and "src/db.py" in f.message
    ]
    assert unowned, f"expected core.md src/db.py ownership finding, got: {findings}"


@needs_v150
def test_v150_consistency_finds_metrics_scope_contradiction():
    """core spec metrics say modules=1 while its own Scope plans 2 src files."""
    findings = consistency.audit(V150_RUN)
    contradictions = [
        f
        for f in findings
        if f.kind == "spec_metrics_scope_contradiction" and f.file.endswith("core.md")
    ]
    assert contradictions, f"expected core.md modules/Scope contradiction, got: {findings}"
    assert "modules=1" in contradictions[0].message
    assert "2 src file(s)" in contradictions[0].message


@needs_v150
def test_v150_consistency_meta_matches_product_results():
    """meta.json FAILED <-> PRODUCT-RESULTS 'NOT READY' agree (already fixed)."""
    findings = consistency.audit(V150_RUN)
    assert not [f for f in findings if f.kind == "meta_results_status_mismatch"]


@needs_v150
def test_v150_consistency_no_orphan_src_files():
    """Every built src/*.py in v150 has an owner node or the declared entry."""
    findings = consistency.audit(V150_RUN)
    assert not [f for f in findings if f.kind == "orphan_src_file"]


@needs_v150
def test_v150_journal_finds_collapse_without_spec_rewrite():
    """The base collapse to one module is never followed by a spec rewrite
    event — the KNOWN v150 engine gap this rule is designed to keep red."""
    findings = journal_invariants.audit(V150_RUN)
    kinds = {f.kind for f in findings}
    assert "collapse_without_spec_rewrite" in kinds, f"got: {findings}"
    # No other invariant may be violated on this run — pin the exact answer.
    assert kinds == {"collapse_without_spec_rewrite"}, f"got: {findings}"


@needs_v150
def test_v150_cli_exit_codes_signal_findings():
    """Both CLIs return exit code 1 on the v150 run (findings present)."""
    for script in ("consistency.py", "journal_invariants.py"):
        proc = subprocess.run(
            [sys.executable, str(AUDIT_DIR / script), str(V150_RUN)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 1, f"{script}: {proc.stdout}\n{proc.stderr}"
        assert "finding(s)" in proc.stdout


# --------------------------------------------------------------------------
# false-positive guard: a clean synthetic run must produce zero findings
# --------------------------------------------------------------------------

@pytest.fixture()
def clean_run(tmp_path: Path) -> Path:
    """Minimal internally-consistent run directory."""
    run = tmp_path / "run"
    (run / "workspace" / "specs").mkdir(parents=True)
    (run / "workspace" / "src").mkdir(parents=True)
    (run / "workspace" / "tests").mkdir(parents=True)

    tree = {
        "id": "L0",
        "metrics": {"modules": 1},
        "children": [
            {
                "id": "core",
                "metrics": {"modules": 1},
                "exposes": [
                    "post_notes(payload, query)",
                    "get_notes(payload, query)",
                    "get_health(payload, query)",
                ],
            },
            {
                "id": "product_entry",
                "code_target": "src/app.py",
                "metrics": {"modules": 1},
            },
        ],
    }
    (run / "tree.json").write_text(json.dumps(tree), encoding="utf-8")
    (run / "meta.json").write_text(json.dumps({"status": "PASSED"}), encoding="utf-8")

    (run / "workspace" / "specs" / "core.md").write_text(
        textwrap.dedent(
            """\
            # Product core

            ## Size estimate (leaf_check input)

            | metric | value |
            |---|---|
            | modules | 1 |
            | tasks | 2 |

            ## Requirements
            REQ-1: POST /notes stores a note in src/core.py and returns 201.

            ## Scope
            In:
            - src/core.py: storage plus all route handlers
            - JSON request parsing and response formatting

            Out:
            - Authentication

            ## Acceptance criteria
            AC-1: POST /notes returns 201 with an integer id.
            """
        ),
        encoding="utf-8",
    )

    (run / "workspace" / "src" / "core.py").write_text(
        "# feature module owned by node 'core'\n", encoding="utf-8"
    )
    (run / "workspace" / "src" / "app.py").write_text(
        "# entry module owned via product_entry code_target\n", encoding="utf-8"
    )

    (run / "workspace" / "tests" / "test_core.py").write_text(
        textwrap.dedent(
            """\
            # synthetic contract-conformant test file
            def _call(method, path, body=b""):
                return 0, {}


            def test_post_note_created():
                code, body = _call("POST", "/notes", b'{"text": "hi"}')
                assert code == 201


            def test_health_ok():
                code, body = _call("GET", "/health")
                assert code == 200


            def test_missing_text_rejected():
                code, body = _call("POST", "/notes", b"{}")
                assert code == 400
            """
        ),
        encoding="utf-8",
    )

    (run / "workspace" / "PRODUCT-RESULTS.md").write_text(
        "# Product readiness\n\nStatus: ✅ READY\n", encoding="utf-8"
    )

    events = [
        {
            "task": "core",
            "phase": "decompose",
            "action": "leaf_check",
            "detail": "within all thresholds",
            "verdict": "leaf",
            "level": 1,
        },
        {
            "task": "core",
            "phase": "review",
            "action": "card completeness: leaf card is complete",
            "detail": "surface + acceptance present",
            "verdict": "PASS",
            "level": 1,
        },
        {
            "task": "core",
            "phase": "lifecycle",
            "action": "to_done",
            "detail": "state=DONE",
            "verdict": "",
            "level": 2,
        },
        {
            "task": "product",
            "phase": "product",
            "action": "build+run product against acceptance spec",
            "detail": "all checks green",
            "verdict": "READY",
            "level": 1,
        },
    ]
    (run / "trace.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
        encoding="utf-8",
    )
    return run


def test_clean_run_consistency_has_no_findings(clean_run: Path):
    assert consistency.audit(clean_run) == []


def test_clean_run_journal_has_no_findings(clean_run: Path):
    assert journal_invariants.audit(clean_run) == []


def test_clean_run_cli_exit_zero(clean_run: Path):
    for script in ("consistency.py", "journal_invariants.py"):
        proc = subprocess.run(
            [sys.executable, str(AUDIT_DIR / script), str(clean_run)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"{script}: {proc.stdout}\n{proc.stderr}"


# --------------------------------------------------------------------------
# known-answer: the real v152 run (frozen artifacts — engine fixes S10.13/14/15
# only help FUTURE runs; checker fixes must remove the checker-noise findings)
# --------------------------------------------------------------------------

V152_RUN = (REPO_ROOT / "tests" / "runs-out"
            / "2026-07-03T06-31-32__v152__p6-micro-notes")

needs_v152 = pytest.mark.skipif(
    not V152_RUN.is_dir(), reason="v152 run dir not present on this checkout"
)


@needs_v152
def test_v152_consistency_exact_findings():
    """Artifact-frozen: the two spec_mentions_unowned_file findings stay (the
    old engine never purged l0.md / product_entry.md — S10.14 fixes future
    runs). Checker-noise: status_contract_mismatch must be GONE — the assert
    at tests/test_core.py:87 belongs to the delete_notes call at line 86 (an
    unknown handler-shaped call), not to post_notes at line 85."""
    findings = consistency.audit(V152_RUN)
    kinds = {f.kind for f in findings}
    assert kinds == {"spec_mentions_unowned_file"}, f"got: {findings}"
    files = {f.file for f in findings}
    assert any(f.endswith("l0.md") for f in files), files
    assert any(f.endswith("product_entry.md") for f in files), files


@needs_v152
def test_v152_journal_exact_finding_kinds():
    """Artifact-frozen kinds (counts not pinned): the old engine emitted
    neither the collapse rewrite event (S10.14) nor the card-gate PASS
    (S10.15), so both findings honestly stay on this run dir."""
    findings = journal_invariants.audit(V152_RUN)
    kinds = {f.kind for f in findings}
    assert kinds == {"collapse_without_spec_rewrite",
                     "unresolved_milestone_fail"}, f"got: {findings}"


# --------------------------------------------------------------------------
# status-assert binding: nearest PRECEDING call in source order; an unknown
# handler-shaped call between a recognized call and the assert must SKIP the
# assert, never blame the earlier route (the v152 line-87 misbinding)
# --------------------------------------------------------------------------

def _binding_run(tmp_path: Path, test_src: str, iface: dict | None = None) -> Path:
    run = tmp_path / "run"
    (run / "workspace" / "tests").mkdir(parents=True)
    tree = {
        "id": "L0",
        "children": [
            {"id": "core",
             "exposes": ["post_notes(payload, query)",
                         "get_notes(payload, query)",
                         "get_health(payload, query)"]},
            {"id": "delete_note", "code_target": "src/core.py"},
        ],
    }
    (run / "tree.json").write_text(json.dumps(tree), encoding="utf-8")
    (run / "workspace" / "tests" / "test_core.py").write_text(
        textwrap.dedent(test_src), encoding="utf-8")
    if iface is not None:
        cdir = run / "workspace" / "contracts"
        cdir.mkdir(parents=True)
        (cdir / "interface.json").write_text(json.dumps(iface),
                                             encoding="utf-8")
    return run


_V152_SHAPE = """\
    from core import post_notes, delete_notes


    def test_delete_note_removes_from_list():
        _, body = post_notes({"text": "x"}, {})
        status, _ = delete_notes({}, {"id": str(body["id"])})
        assert status == 200
"""


def test_assert_after_unknown_call_is_skipped(tmp_path):
    run = _binding_run(tmp_path, _V152_SHAPE)
    findings = [f for f in consistency.audit(run)
                if f.kind in ("status_contract_mismatch",
                              "success_on_undeclared_route")]
    assert not findings, (
        "the 200 at line 7 answers delete_notes (line 6, unknown to the"
        " contract) — binding it to post_notes two lines up is the v152"
        f" misbinding: {findings}")


def test_real_mismatch_after_recognized_call_still_fires(tmp_path):
    run = _binding_run(tmp_path, """\
        from core import post_notes


        def test_post_note():
            status, _ = post_notes({"text": "x"}, {})
            assert status == 200
    """)
    findings = [f for f in consistency.audit(run)
                if f.kind == "status_contract_mismatch"]
    assert findings, "a genuine 200-vs-201 mismatch must still be caught"


_IFACE_WITH_DELETE = {
    "format": "spec-flow interface contract v1",
    "routes": [
        {"method": "POST", "path": "/notes", "handler": "post_notes",
         "success_status": 201},
        {"method": "GET", "path": "/notes", "handler": "get_notes",
         "success_status": 200},
        {"method": "DELETE", "path": "/notes", "handler": "delete_notes",
         "success_status": 200},
    ],
}


def test_interface_handler_binds_grown_route(tmp_path):
    """After S10.13 the machine contract carries the grown DELETE route with
    its handler name — the checker must READ it: delete_notes binds to
    DELETE /notes, assert 200 == contracted 200, nothing to report."""
    run = _binding_run(tmp_path, _V152_SHAPE, iface=_IFACE_WITH_DELETE)
    findings = [f for f in consistency.audit(run)
                if f.kind in ("status_contract_mismatch",
                              "success_on_undeclared_route")]
    assert not findings, f"got: {findings}"


def test_interface_handler_mismatch_still_fires(tmp_path):
    run = _binding_run(tmp_path, """\
        from core import delete_notes


        def test_delete_wrong_status():
            status, _ = delete_notes({}, {"id": "1"})
            assert status == 201
    """, iface=_IFACE_WITH_DELETE)
    findings = [f for f in consistency.audit(run)
                if f.kind == "status_contract_mismatch"]
    assert findings, (
        "a wrong status against an interface-declared route must still red")


# --------------------------------------------------------------------------
# collapse respec: the S10.14 purge event satisfies the invariant; a collapse
# with NO cleanup event stays red (the v150 known-answer above pins that)
# --------------------------------------------------------------------------

def _journal_run(tmp_path: Path, events: list[dict]) -> Path:
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    (run / "trace.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return run


_COLLAPSE = {"task": "L0", "phase": "decompose",
             "action": "small product → single core leaf (root stays branch)",
             "detail": "base collapsed to one module", "verdict": "branch",
             "level": 1}
_TERMINAL = {"task": "product", "phase": "product", "action": "final",
             "detail": "", "verdict": "READY", "level": 1}


def test_collapse_purge_event_satisfies_respec(tmp_path):
    purge = {"task": "L0", "phase": "decompose",
             "action": "collapse purged dropped-module references",
             "detail": "retargeted at src/core.py in: specs/l0.md",
             "verdict": "", "level": 1}
    run = _journal_run(tmp_path, [_COLLAPSE, purge, _TERMINAL])
    kinds = {f.kind for f in journal_invariants.audit(run)}
    assert "collapse_without_spec_rewrite" not in kinds, f"got: {kinds}"


def test_collapse_rewrite_event_satisfies_respec(tmp_path):
    rewrite = {"task": "core", "phase": "decompose",
               "action": "collapsed spec rewritten for the single core module",
               "detail": "engine-rewritten scope", "verdict": "", "level": 2}
    run = _journal_run(tmp_path, [_COLLAPSE, rewrite, _TERMINAL])
    kinds = {f.kind for f in journal_invariants.audit(run)}
    assert "collapse_without_spec_rewrite" not in kinds, f"got: {kinds}"


def test_collapse_without_any_cleanup_event_stays_red(tmp_path):
    run = _journal_run(tmp_path, [_COLLAPSE, _TERMINAL])
    kinds = {f.kind for f in journal_invariants.audit(run)}
    assert "collapse_without_spec_rewrite" in kinds, f"got: {kinds}"
