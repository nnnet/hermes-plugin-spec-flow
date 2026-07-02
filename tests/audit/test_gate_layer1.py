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
