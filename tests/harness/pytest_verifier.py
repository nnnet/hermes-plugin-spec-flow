"""Deterministic integrate verifier — the test suite IS the verdict.

The engine consults ``agents["verifier"]`` at every branch's Integrate &
verify node. This verifier runs the workspace's REAL test suite with
pytest and answers PASS/FAIL from the exit code — an LLM opinion never
substitutes for a green run.

Repair loop: on a red suite it asks the implementer-grade model (free
pool, same llm_backend rules) to fix the implicated files, writes the
returned files back (path-safe) and re-runs — up to ``max_repair``
rounds. The FINAL verdict is still the real pytest exit code.

The smoke suite (``tests/smoke/``) exercises the assembled product
end-to-end, so it only gates the ROOT integrate — partial trees skip it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from . import llm_backend, llm_log

PYTEST_TIMEOUT = int(os.environ.get("SPEC_FLOW_PYTEST_TIMEOUT", "180"))
MAX_REPAIR = int(os.environ.get("SPEC_FLOW_INTEGRATE_MAX_REPAIR", "2"))
INLINE_LIMIT = int(os.environ.get("SPEC_FLOW_INLINE_FILE_LIMIT", "8000"))
SMOKE_DIR = "tests/smoke"

_FILE_RE = re.compile(r"((?:tests|src)/[\w/]+\.py)")

_REPAIR_TASK = """You are the integration repair worker of a Spec-Driven
Development run. The workspace test suite FAILED. pytest output (tail):
---
{output}
---

Current content of the implicated files:
{files_block}

Fix the code (and only if genuinely wrong, the tests). Conventions: code in
src/, tests in tests/, standard library only, test files insert ../src into
sys.path and import modules by name. Reply with ONLY a JSON object mapping
EVERY file you change to its FULL new text (no prose, no fence):
{{"files": {{"src/x.py": "...", "tests/test_x.py": "..."}}}}"""


def run_suite(root: str, include_smoke: bool) -> tuple[bool, str]:
    """Run the workspace tests for REAL. Returns (passed, output tail)."""
    if not (Path(root) / "tests").is_dir():
        return True, "(no tests yet)"
    cmd = [sys.executable, "-m", "pytest", "tests", "-q", "--no-header",
           "-p", "no:cacheprovider"]
    if not include_smoke and (Path(root) / SMOKE_DIR).is_dir():
        cmd += ["--ignore", SMOKE_DIR]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=PYTEST_TIMEOUT, cwd=root)
    out = (proc.stdout or "") + (proc.stderr or "")
    # pytest exit 5 = no tests collected — nothing to verify is not a failure
    return proc.returncode in (0, 5), out[-2000:]


def _safe_rel(rel: str) -> bool:
    p = Path(rel)
    return (not p.is_absolute() and ".." not in p.parts
            and p.suffix == ".py"
            and p.parts and p.parts[0] in ("src", "tests"))


def _implicated_files(root: str, output: str) -> dict[str, str]:
    """The failing files (from the pytest output) and their src counterparts."""
    found: dict[str, str] = {}
    rels = list(dict.fromkeys(_FILE_RE.findall(output)))
    for rel in rels:
        m = re.match(r"tests/test_(\w+)\.py$", rel)
        if m:
            rels.append(f"src/{m.group(1)}.py")
    budget = INLINE_LIMIT * 2
    for rel in dict.fromkeys(rels):
        if not _safe_rel(rel):
            continue
        f = Path(root) / rel
        if f.is_file() and budget > 0:
            text = f.read_text(encoding="utf-8")[:INLINE_LIMIT]
            budget -= len(text)
            found[rel] = text
    return found


def _write_files(root: str, files: dict) -> bool:
    wrote = False
    for rel, body in (files or {}).items():
        if not (_safe_rel(str(rel)) and isinstance(body, str) and body.strip()):
            continue
        f = Path(root) / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body if body.endswith("\n") else body + "\n",
                     encoding="utf-8")
        wrote = True
    return wrote


def make_verifier(model: Optional[str] = None,
                  max_repair: int = MAX_REPAIR) -> Callable[[dict], dict]:
    model = model or llm_backend.DEFAULT_FREE_MODEL

    def verify(ctx: dict) -> dict:
        root = ctx["workspace_root"]
        include_smoke = str(ctx.get("node")) == "L0"
        passed, out = run_suite(root, include_smoke)
        rounds = 0
        while not passed and rounds < max_repair:
            rounds += 1
            llm_log.log({"event": "call_start", "role": "verifier",
                         "worker": True, "node": str(ctx.get("node")),
                         "depth": -1, "model": model})
            files = _implicated_files(root, out)
            files_block = "\n".join(
                f"--- {rel} ---\n{text}" for rel, text in files.items()) \
                or "(none located)"
            try:
                raw = llm_backend.ask(
                    _REPAIR_TASK.format(output=out, files_block=files_block),
                    model=model)
                m = re.search(r"\{.*\}", raw, re.S)
                reply = json.loads(m.group(0)) if m else {}
            except Exception as exc:  # noqa: BLE001 — verdict stays honest
                llm_log.log_outcome(role="verifier", worker=True,
                                    node=str(ctx.get("node")), depth=-1,
                                    model=model, ok=False, error=str(exc)[:200])
                break
            if not _write_files(root, reply.get("files")):
                break
            passed, out = run_suite(root, include_smoke)
            llm_log.log_outcome(role="verifier", worker=True,
                                node=str(ctx.get("node")), depth=-1,
                                model=model, ok=True, tests_passed=passed,
                                repair_round=rounds)
        return {"status": "PASS" if passed else "FAIL",
                "detail": ("green pytest run" if passed else out[-300:])
                + (f" (after {rounds} repair round(s))" if rounds else "")}

    return verify
