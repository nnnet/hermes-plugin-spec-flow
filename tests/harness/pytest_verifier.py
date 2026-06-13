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

from . import llm_backend, llm_log, memory

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

Fix the code (and only if genuinely wrong, the tests). A test that imports
or calls an API the module no longer provides is STALE: either restore the
API in the module or update that test to the current API — the suite must
COLLECT and pass as a whole; an ImportError in any test file fails
everything. Conventions: code in src/, tests in tests/, standard library
only, test files insert ../src into sys.path and import modules by name.
Reply with ONLY a JSON object mapping EVERY file you change to its FULL new
text (no prose, no fence):
{{"files": {{"src/x.py": "...", "tests/test_x.py": "..."}}}}"""


def run_suite(root: str, include_smoke: bool) -> tuple[bool, str]:
    """Run the workspace tests for REAL. Returns (passed, output tail)."""
    if not (Path(root) / "tests").is_dir():
        return True, "(no tests yet)"
    cmd = [sys.executable, "-m", "pytest", "tests", "-q", "--no-header",
           "-p", "no:cacheprovider"]
    if not include_smoke and (Path(root) / SMOKE_DIR).is_dir():
        cmd += ["--ignore", SMOKE_DIR]
    with llm_backend.PYTEST_LOCK:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=PYTEST_TIMEOUT, cwd=root)
    out = (proc.stdout or "") + (proc.stderr or "")
    # pytest exit 5 = no tests collected — nothing to verify is not a failure
    return proc.returncode in (0, 5), out[-2000:]


def protected_files() -> set[str]:
    """The platform-seeded skeleton is IMMUTABLE for workers — features are
    built INTO it, never over it (a worker once rewrote src/app.py as its
    own monolith and broke the assembled product)."""
    raw = os.environ.get("SPEC_FLOW_PROTECTED_FILES", "")
    try:
        return set(json.loads(raw)) if raw else set()
    except json.JSONDecodeError:
        return set()


def _safe_rel(rel: str) -> bool:
    p = Path(rel)
    return (not p.is_absolute() and ".." not in p.parts
            and p.suffix == ".py"
            and p.parts and p.parts[0] in ("src", "tests")
            and rel not in protected_files())


# worker code must never reach into platform internals: a leaf test once
# did db._SCHEMAS.clear() to 'isolate' itself and silently destroyed every
# sibling's schema for the rest of the pytest session
_FORBIDDEN_CONTENT = re.compile(r"_SCHEMAS|ROUTES\s*\.\s*clear\s*\(")


def content_allowed(body: str) -> bool:
    return not _FORBIDDEN_CONTENT.search(body or "")


def _implicated_files(root: str, output: str) -> dict[str, str]:
    """The failing files (from the pytest output) and their src counterparts.

    When the failure names no WRITABLE file (e.g. the protected smoke suite
    dies on a feature handler — the traceback shows no module path), fall
    back to ALL writable src modules: the bug lives in one of them and the
    repair model needs their text to find it."""
    found: dict[str, str] = {}
    rels = list(dict.fromkeys(_FILE_RE.findall(output)))
    for rel in rels:
        m = re.match(r"tests/test_(\w+)\.py$", rel)
        if m:
            rels.append(f"src/{m.group(1)}.py")
    if not any(_safe_rel(r) for r in rels):
        rels += sorted(
            str(p.relative_to(root)) for p in (Path(root) / "src").glob("*.py")
        ) if (Path(root) / "src").is_dir() else []
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


def _readonly_context(root: str, output: str) -> str:
    """Protected files named by the failure (e.g. the smoke suite) — the
    repair model must SEE the expected API even though it cannot write it."""
    blocks = []
    budget = INLINE_LIMIT
    for rel in dict.fromkeys(_FILE_RE.findall(output)):
        if rel not in protected_files():
            continue
        f = Path(root) / rel
        if f.is_file() and budget > 0:
            text = f.read_text(encoding="utf-8")[:INLINE_LIMIT // 2]
            budget -= len(text)
            blocks.append(f"--- {rel} (READ-ONLY platform file — satisfy it,"
                          f" never edit it) ---\n{text}")
    return "\n".join(blocks)


def _write_files(root: str, files: dict) -> tuple[bool, dict]:
    """Write the model's files; returns (wrote_any, snapshot) where snapshot
    maps each touched path to its previous content (None = did not exist),
    so a round that made things WORSE can be rolled back."""
    wrote = False
    snapshot: dict[str, Optional[str]] = {}
    for rel, body in (files or {}).items():
        if not (_safe_rel(str(rel)) and isinstance(body, str) and body.strip()):
            continue
        if not content_allowed(body):
            llm_log.log({"event": "write_refused", "role": "verifier",
                         "path": str(rel),
                         "reason": "touches platform internals"})
            continue
        f = Path(root) / rel
        snapshot[str(rel)] = (f.read_text(encoding="utf-8")
                              if f.is_file() else None)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body if body.endswith("\n") else body + "\n",
                     encoding="utf-8")
        wrote = True
    return wrote, snapshot


def _rollback(root: str, snapshot: dict) -> None:
    for rel, old in snapshot.items():
        f = Path(root) / rel
        if old is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(old, encoding="utf-8")


_FAIL_RE = re.compile(r"(\d+) failed")
_ERR_RE = re.compile(r"(\d+) error")


def _badness(passed: bool, output: str) -> int:
    """Comparable failure score: 0 = green; collection break is worst."""
    if passed:
        return 0
    score = 1
    m = _FAIL_RE.search(output)
    if m:
        score += int(m.group(1))
    m = _ERR_RE.search(output)
    if m:
        score += 10 * int(m.group(1))
    if "Interrupted" in output or "errors during collection" in output:
        score += 100
    return score


BISECT_MAX_FILES = int(os.environ.get("SPEC_FLOW_BISECT_MAX_FILES", "16"))


def _run_one(root: str, rel: str) -> bool:
    """Run a SINGLE test file in its own pytest process. True = green."""
    with llm_backend.PYTEST_LOCK:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", rel, "-q", "--no-header",
             "-p", "no:cacheprovider"],
            capture_output=True, text=True, timeout=PYTEST_TIMEOUT, cwd=root)
    return proc.returncode in (0, 5)


def _run_suite_green(root: str, include_smoke: bool) -> bool:
    """Run the whole tests/ suite. True = green (exit 0 or 'no tests')."""
    cmd = [sys.executable, "-m", "pytest", "tests", "-q", "--no-header",
           "-p", "no:cacheprovider"]
    if not include_smoke and (Path(root) / SMOKE_DIR).is_dir():
        cmd += ["--ignore", SMOKE_DIR]
    with llm_backend.PYTEST_LOCK:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=PYTEST_TIMEOUT, cwd=root)
    return proc.returncode in (0, 5)


def _run_without(root: str, rels: list, drop: str, include_smoke: bool) -> bool:
    """Run the suite with one file ignored. True = green without it."""
    cmd = [sys.executable, "-m", "pytest", "tests", "-q", "--no-header",
           "-p", "no:cacheprovider", "--ignore", str(Path(root) / drop)]
    if not include_smoke and (Path(root) / SMOKE_DIR).is_dir():
        cmd += ["--ignore", SMOKE_DIR]
    with llm_backend.PYTEST_LOCK:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=PYTEST_TIMEOUT, cwd=root)
    return proc.returncode in (0, 5)


def bisect_poisoners(root: str, include_smoke: bool) -> dict:
    """П4-bis: diagnose the 'green alone, red together' class. When the whole
    suite is red but individual test files pass on their own, one file is
    POISONING shared state for the others. Find it by elimination: for each
    file that is green alone, re-run the suite WITHOUT it — if that turns the
    suite green, that file is the poisoner. Returns
    {poisoners: [rel], green_alone: [rel], victims: [rel]} (empty when the
    failure is genuine per-file bugs, not cross-test interference).

    Cost is bounded: skipped entirely above BISECT_MAX_FILES test files, and
    it only runs once per red integrate, never on a green suite."""
    tdir = Path(root) / "tests"
    if not tdir.is_dir():
        return {}
    files = sorted(str(p.relative_to(root)) for p in tdir.glob("test_*.py"))
    if not (2 <= len(files) <= BISECT_MAX_FILES):
        return {}
    # only a RED suite has a poisoner — a green suite has nothing to bisect
    try:
        if _run_suite_green(root, include_smoke):
            return {"poisoners": [], "green_alone": files, "victims": []}
    except Exception:  # noqa: BLE001
        return {}
    green_alone, red_alone = [], []
    for rel in files:
        (green_alone if _run_one(root, rel) else red_alone).append(rel)
    # interference requires victims: files green alone but red in the suite
    if not green_alone or not red_alone and len(green_alone) == len(files):
        # everyone green alone but suite red → a green file poisons the rest;
        # fall through. If some are red alone, those are genuine bugs.
        pass
    if not green_alone:
        return {"poisoners": [], "green_alone": [], "victims": red_alone}
    poisoners = []
    for cand in green_alone:
        try:
            if _run_without(root, files, cand, include_smoke):
                poisoners.append(cand)
        except Exception:  # noqa: BLE001 — bisection never breaks the verdict
            continue
    return {"poisoners": poisoners, "green_alone": green_alone,
            "victims": red_alone}


def make_verifier(model: Optional[str] = None,
                  max_repair: int = MAX_REPAIR,
                  channel: Optional[object] = None) -> Callable[[dict], dict]:
    model = model or llm_backend.model_for("verifier")

    def verify(ctx: dict) -> dict:
        root = ctx["workspace_root"]
        if channel is not None:
            # standing-requirement acceptance tests land in the smoke suite
            # (root gate): a late human requirement becomes ENFORCEABLE here
            synced = channel.sync_requirements(root)
            if synced:
                llm_log.log({"event": "requirements_synced",
                             "role": "verifier", "paths": synced})
        include_smoke = str(ctx.get("node")) == "L0"
        passed, out = run_suite(root, include_smoke)
        first_red = ""
        if not passed:
            # the FIRST red output is the diagnosis — keep it on record
            first_red = out[-600:]
            llm_log.log({"event": "integrate_red", "role": "verifier",
                         "node": str(ctx.get("node")),
                         "test_output": first_red})
            # П4-bis: 'green alone, red together' is a poisoning class the
            # repair worker cannot see from the suite output. Bisect once and
            # name the poisoner so the repair gets a PRECISE target instead of
            # blindly rewriting innocent files.
            if str(os.environ.get("SPEC_FLOW_BISECT", "1")) != "0":
                try:
                    diag = bisect_poisoners(root, include_smoke)
                except Exception:  # noqa: BLE001 — never breaks the verdict
                    diag = {}
                if diag.get("poisoners"):
                    note = ("\n\nPOISONER DIAGNOSIS (bisection): the suite is "
                            "red but these files PASS alone — removing them "
                            "turns the suite green, so they corrupt shared "
                            "state for the others: "
                            + ", ".join(diag["poisoners"])
                            + ". Fix the poisoner's test isolation (teardown / "
                            "fixture scope / global state), do NOT rewrite the "
                            "victims.")
                    out += note
                    llm_log.log({"event": "poisoner_found", "role": "verifier",
                                 "node": str(ctx.get("node")),
                                 "poisoners": diag["poisoners"],
                                 "green_alone": diag.get("green_alone", [])})
        rounds = 0
        while not passed and rounds < max_repair:
            rounds += 1
            llm_log.log({"event": "call_start", "role": "verifier",
                         "worker": True, "node": str(ctx.get("node")),
                         "depth": int(ctx.get("depth", -1)),
                         "model": model})
            files = _implicated_files(root, out)
            files_block = "\n".join(
                f"--- {rel} ---\n{text}" for rel, text in files.items()) \
                or "(none located)"
            ro = _readonly_context(root, out)
            if ro:
                files_block += "\n" + ro
                files_block += ("\nIf the failure says 'no route METHOD"
                                " /path', the FEATURE IS MISSING — author a"
                                " NEW src module registering that route per"
                                " the platform conventions.")
            from . import repo_map
            rmap = repo_map.build_map(root, budget=repo_map.MAP_BUDGET // 2)
            if rmap:
                files_block += ("\n\nREPOSITORY MAP (public surface of every"
                                " module — keep your fix consistent with"
                                " it):\n" + rmap)
            try:
                from . import role_worker
                raw = llm_backend.ask(
                    _REPAIR_TASK.format(output=out, files_block=files_block),
                    model=model,
                    system=role_worker._with_language(
                        "You are the integration repair worker."))
                m = re.search(r"\{.*\}", raw, re.S)
                reply = json.loads(m.group(0)) if m else {}
            except Exception as exc:  # noqa: BLE001 — verdict stays honest
                llm_log.log_outcome(role="verifier", worker=True,
                                    node=str(ctx.get("node")), depth=-1,
                                    model=model, ok=False, error=str(exc)[:200])
                break
            # GIT TRANSACTION: write + re-verify + rollback is ONE atomic,
            # attributable commit — interleaved with a leaf's write it
            # once produced a module/test pair no single writer ever
            # wrote (v17). Rollback is exact, incl. files the repair
            # CREATED (the old snapshot restored only overwritten ones).
            from . import ws_tx
            with ws_tx.transaction(
                    root, f"verifier:{ctx.get('node')}",
                    f"repair round {rounds}") as tx:
                before = _badness(passed, out)
                wrote, _snapshot = _write_files(root, reply.get("files"))
                if wrote:
                    llm_log.log({"event": "ws_write", "writer": "verifier",
                                 "node": str(ctx.get("node")),
                                 "round": rounds,
                                 "paths": sorted((reply.get("files") or {}))})
                    passed, out = run_suite(root, include_smoke)
                    rolled_back = False
                    if _badness(passed, out) > before:
                        # the round made the suite WORSE — a repair never
                        # leaves the tree worse than it found it
                        tx.rollback()
                        passed, out = run_suite(root, include_smoke)
                        rolled_back = True
            if not wrote:
                break
            llm_log.log_outcome(role="verifier", worker=True,
                                node=str(ctx.get("node")), depth=-1,
                                model=model, ok=True, tests_passed=passed,
                                repair_round=rounds, rolled_back=rolled_back)
        node = str(ctx.get("node"))
        if not passed:
            # the verifier's craft is failure CLASSES: what integration
            # breaks look like and which ones resist repair
            memory.retain_role(
                "verifier",
                f"Integration FAIL at node '{node}' after {rounds} repair"
                f" round(s); red tail: {out[-300:]}",
                context="integrate fail", tags=["fail"])
        elif rounds:
            memory.retain_role(
                "verifier",
                f"Integration at node '{node}' went green after {rounds}"
                f" repair round(s); first diagnosis was: {first_red[-300:]}",
                context="integrate repaired", tags=["repair"])
        return {"status": "PASS" if passed else "FAIL",
                "detail": ("green pytest run" if passed else out[-300:])
                + (f" (after {rounds} repair round(s))" if rounds else "")}

    return verify
