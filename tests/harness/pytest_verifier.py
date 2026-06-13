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


def run_suite(root: str, include_smoke: bool,
              targets: Optional[list] = None) -> tuple[bool, str]:
    """Run the workspace tests for REAL. Returns (passed, output tail).

    #4: ``targets`` scopes a BRANCH integrate to its subtree's test files
    instead of the whole corpus — each non-root gate stays cheap as the
    suite grows. The ROOT integrate passes targets=None and runs everything
    (the honest full check is never skipped)."""
    if not (Path(root) / "tests").is_dir():
        return True, "(no tests yet)"
    scope = [t for t in (targets or []) if (Path(root) / t).is_file()]
    paths = scope or ["tests"]
    # --continue-on-collection-errors: ONE unimportable file (e.g. a weak
    # model wrote a non-ASCII char into Python) must not abort collection and
    # zero the whole corpus — pytest's default makes a single SyntaxError look
    # like total failure. With this flag the bad file is reported and isolated
    # while the other tests still run, so the verdict stays honest and repair
    # can target the one broken file.
    cmd = [sys.executable, "-m", "pytest", *paths, "-q", "--no-header",
           "-p", "no:cacheprovider", "--import-mode=importlib",
           "--continue-on-collection-errors"]
    if not scope and not include_smoke and (Path(root) / SMOKE_DIR).is_dir():
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
             "-p", "no:cacheprovider", "--import-mode=importlib"],
            capture_output=True, text=True, timeout=PYTEST_TIMEOUT, cwd=root)
    return proc.returncode in (0, 5)


def _run_suite_green(root: str, include_smoke: bool) -> bool:
    """Run the whole tests/ suite. True = green (exit 0 or 'no tests')."""
    cmd = [sys.executable, "-m", "pytest", "tests", "-q", "--no-header",
           "-p", "no:cacheprovider", "--import-mode=importlib"]
    if not include_smoke and (Path(root) / SMOKE_DIR).is_dir():
        cmd += ["--ignore", SMOKE_DIR]
    with llm_backend.PYTEST_LOCK:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=PYTEST_TIMEOUT, cwd=root)
    return proc.returncode in (0, 5)


def _run_without(root: str, rels: list, drop: str, include_smoke: bool) -> bool:
    """Run the suite with one file ignored. True = green without it."""
    cmd = [sys.executable, "-m", "pytest", "tests", "-q", "--no-header",
           "-p", "no:cacheprovider", "--import-mode=importlib", "--ignore", str(Path(root) / drop)]
    if not include_smoke and (Path(root) / SMOKE_DIR).is_dir():
        cmd += ["--ignore", SMOKE_DIR]
    with llm_backend.PYTEST_LOCK:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=PYTEST_TIMEOUT, cwd=root)
    return proc.returncode in (0, 5)


FLAKY_RERUNS = int(os.environ.get("SPEC_FLOW_FLAKY_RERUNS", "0"))


def detect_flaky(root: str, rel: str, runs: int) -> bool:
    """#3: a test file is FLAKY only if its isolated verdict is INCONSISTENT
    across ``runs`` repetitions — some green, some red. A consistently-red
    file is a GENUINE bug, never flaky; a consistently-green one isn't red to
    begin with. Proven non-determinism is the only trigger, so a real failure
    is never quarantined."""
    if runs < 2:
        return False
    seen = set()
    for _ in range(runs):
        seen.add(_run_one(root, rel))
        if len(seen) > 1:            # already both green and red → flaky
            return True
    return False


def quarantine_flaky(root: str, include_smoke: bool, runs: int) -> dict:
    """When the suite is red, find which red test FILES are flaky (proven
    non-deterministic) vs stably red (real bugs). Returns
    {flaky: [rel], stable_red: [rel]}. A flaky file is RECORDED, never
    silently dropped — the caller excludes it from the gate verdict and notes
    it in the output, so the root stays honest about real failures while a
    coin-flip test can't redden it. Bounded: only runs on a red suite, only
    re-checks the files that were red alone."""
    if runs < 2:
        return {"flaky": [], "stable_red": []}
    tdir = Path(root) / "tests"
    if not tdir.is_dir():
        return {"flaky": [], "stable_red": []}
    files = sorted(str(p.relative_to(root)) for p in tdir.glob("test_*.py"))
    flaky, stable_red = [], []
    for rel in files:
        # check flakiness DIRECTLY — a single _run_one can't pre-screen a
        # flaky file (its one run may land green and wrongly skip it)
        if detect_flaky(root, rel, runs):
            flaky.append(rel)
        elif not _run_one(root, rel):
            stable_red.append(rel)   # consistently red → a real bug
        # else consistently green → not a red source
    return {"flaky": flaky, "stable_red": stable_red}


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


# a pytest one-line summary with a reason: "FAILED tests/x.py::t - <reason>"
_SUMMARY_RE = re.compile(r"^(?:FAILED|ERROR)\s+\S+\s+-\s+(.+)$", re.M)
# ANY failed/error summary line, incl. class-based ids and reason-less ones
# ("FAILED tests/x.py::Class::test", "ERROR tests/x.py::Class::test") — pytest
# omits the ' - reason' when the repr is empty/multi-line, so counting only
# _SUMMARY_RE undercounts the cascade.
_FAIL_LINE_RE = re.compile(r"^(?:FAILED|ERROR)\s+\S+", re.M)
# a traceback exception line: "E   sqlite3.OperationalError: near \"EXISTS\": ..."
_EXC_RE = re.compile(r"^E\s+([\w.]*(?:Error|Exception)\b[^\n]*)$", re.M)
_DB_HINT_RE = re.compile(
    r"OperationalError|no such table|no such column|syntax error|EXISTS", re.I)


def _norm_error(msg: str) -> str:
    """Normalize a failure message to a signature for clustering: keep the
    exception type + a short, value-stripped head so '500 == 201' and
    '500 == 200' fall in one bucket but distinct exception types stay apart."""
    s = re.sub(r"\d+", "N", msg.strip())
    return s[:80]


def _dominant_error(output: str) -> Optional[tuple]:
    """The single error signature shared by many failures — the cascade
    poisoner an exception in ONE shared module (DB schema/bootstrap, a common
    import, a shared fixture) produces. Bisection misses this class: when a
    shared module breaks EVERY test, no file is 'green alone', so there is
    nothing to bisect. Prefer real exception signatures over assertion
    mismatches (the assert is usually a downstream symptom of the exception).

    Returns (signature, count, total_failures, looks_db) or None when no
    single cause dominates (genuine independent per-file bugs)."""
    summary = _SUMMARY_RE.findall(output)
    # total counts EVERY failed/error line (class-based + reason-less), so the
    # cascade fraction is honest even when pytest prints no per-line reason.
    total = len(_FAIL_LINE_RE.findall(output))
    if total < 3:
        return None
    exc_sigs: dict[str, int] = {}
    for m in summary + _EXC_RE.findall(output):
        sig = _norm_error(m)
        is_exc = bool(re.search(r"(?:Error|Exception)\b", sig))
        # weight true exceptions over bare asserts so the root, not the
        # symptom, wins the bucket
        exc_sigs[sig] = exc_sigs.get(sig, 0) + (3 if is_exc else 1)
    if not exc_sigs:
        return None
    sig, score = max(exc_sigs.items(), key=lambda kv: kv[1])
    # a real cascade: the top signature is an exception covering many failures
    if "Error" not in sig and "Exception" not in sig:
        return None
    count = min(max(1, score // 3), total)
    if count < 3 or count < max(2, total // 3):
        return None
    return (sig, count, total, bool(_DB_HINT_RE.search(sig)))


def _schema_module_paths(root: str) -> list:
    """Writable src modules that register a DB schema — when a schema error
    cascades, the culprit DDL lives in one of these, not in the victim tests
    the pytest output names."""
    out = []
    sdir = Path(root) / "src"
    if not sdir.is_dir():
        return out
    for p in sorted(sdir.glob("*.py")):
        rel = str(p.relative_to(root))
        if not _safe_rel(rel):
            continue
        try:
            if "register_schema" in p.read_text(encoding="utf-8"):
                out.append(rel)
        except OSError:
            continue
    return out


_DEFINE_TABLE_RE = re.compile(r"""define_table\(\s*['"]([A-Za-z_]\w*)['"]""")


def _duplicate_table_owners(root: str) -> dict:
    """Tables declared via db.define_table in MORE THAN ONE module — a design
    conflict: the module-global registry keeps the last definition, so the
    other module's columns vanish in the integrated corpus while it passes
    alone. Deterministic (no LLM) and the exact class that reddened a live run.
    Returns {table: [module_rel, ...]} for tables owned by >1 writable module."""
    owners: dict[str, list] = {}
    sdir = Path(root) / "src"
    if not sdir.is_dir():
        return {}
    for p in sorted(sdir.glob("*.py")):
        rel = str(p.relative_to(root))
        if not _safe_rel(rel):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for tbl in set(_DEFINE_TABLE_RE.findall(text)):
            owners.setdefault(tbl, [])
            if rel not in owners[tbl]:
                owners[tbl].append(rel)
    return {t: m for t, m in owners.items() if len(m) > 1}


def _non_ascii_offenders(root: str) -> list:
    """Python files that fail to compile because a weak model wrote a non-ASCII
    character into the source (e.g. '…' U+2026 instead of '...', or smart
    quotes). Deterministic: pytest's collection abort hides WHICH char/line,
    so report file:line:char precisely for repair. Returns a list of
    (rel_path, line_no, char, codepoint)."""
    out = []
    base = Path(root)
    for sub in ("src", "tests"):
        d = base / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.py")):
            rel = str(p.relative_to(root))
            try:
                text = p.read_text(encoding="utf-8")
                compile(text, rel, "exec")
            except SyntaxError as e:                       # noqa: PERF203
                # find the first non-ASCII char (the usual culprit)
                bad = next((ch for ch in text if ord(ch) > 127), "")
                if bad:
                    line = text[:text.index(bad)].count("\n") + 1
                    out.append((rel, line, bad, f"U+{ord(bad):04X}"))
                elif e.lineno:
                    out.append((rel, e.lineno, "", "syntax"))
            except OSError:
                continue
    return out


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
        # #4: a branch integrate runs only its subtree's tests (ctx.test_targets);
        # the root (L0) passes none and runs the whole corpus. Repair re-runs and
        # diagnostics below stay full-suite — a scoped green still owes the root.
        targets = None if include_smoke else (ctx.get("test_targets") or None)
        passed, out = run_suite(root, include_smoke, targets)
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
            # CASCADE ROOT-CAUSE: one exception in a SHARED module (DB schema /
            # bootstrap, a common import, a shared fixture) reddens many tests
            # at once. Bisection can't catch it — when the shared break hits
            # EVERY file, none is 'green alone'. Detect the dominant error
            # signature and tell the repair it is ONE shared bug, not N — and,
            # for a DB-schema error, surface the schema-registering modules
            # (the culprit DDL lives there, not in the victim tests the output
            # names). Observed live: a single 'ALTER TABLE ... ADD COLUMN IF
            # NOT EXISTS' (unsupported by SQLite) aborted connect() and broke
            # 127 of 160 corpus tests through 6 fruitless per-test repairs.
            dom = _dominant_error(out)
            if dom:
                sig, cnt, tot, looks_db = dom
                note = (f"\n\nROOT-CAUSE HINT: {cnt} of {tot} failures share ONE "
                        f"error signature: `{sig}`. This is almost certainly a "
                        "SINGLE shared-cause bug (a common module — DB schema/"
                        "bootstrap, a shared import, or a fixture) breaking many "
                        "tests at once, NOT independent bugs. Find the one shared "
                        "origin and fix it THERE; do not rewrite the victim "
                        "modules.")
                if looks_db:
                    mods = _schema_module_paths(root)
                    note += ("\nThis is a DB-schema error. SQLite does NOT support "
                             "`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` nor "
                             "`CREATE INDEX IF NOT EXISTS ... IF NOT EXISTS`; a bad "
                             "statement in one schema aborts the shared connect() "
                             "and leaves every later table uncreated. Put new "
                             "columns in the CREATE TABLE, or add them "
                             "unconditionally once. The culprit DDL is registered "
                             "in one of: " + (", ".join(mods) or "(none found)")
                             + ".")
                    out += "\n" + "\n".join(mods)   # _FILE_RE picks these up
                out += note
                llm_log.log({"event": "cascade_root_cause", "role": "verifier",
                             "node": str(ctx.get("node")), "signature": sig,
                             "count": cnt, "total": tot, "db": looks_db})
            # TABLE-OWNERSHIP CONFLICT: deterministic (no LLM) — two modules
            # define the same table differently; the registry keeps the last,
            # so the other's columns vanish in the corpus (green alone, red
            # together). Name the conflict and the owning modules so repair
            # consolidates to ONE owner instead of chasing missing-column
            # symptoms across victim tests.
            dupes = _duplicate_table_owners(root)
            if dupes:
                lines = "; ".join(f"'{t}' in {', '.join(m)}"
                                  for t, m in sorted(dupes.items()))
                out += ("\n\nROOT-CAUSE HINT: the same DB table is defined by "
                        "MORE THAN ONE module: " + lines + ". db.define_table "
                        "keeps only the last definition, so the other module's "
                        "columns disappear in the assembled corpus (each passes "
                        "alone, they fail together). Make ONE module own each "
                        "table and declare its FULL schema; the others must use "
                        "db.insert/select/update/delete and NOT call "
                        "define_table on it.\n" + "\n".join(
                            sorted({m for ms in dupes.values() for m in ms})))
                llm_log.log({"event": "table_ownership_conflict",
                             "role": "verifier", "node": str(ctx.get("node")),
                             "tables": sorted(dupes)})
            # NON-ASCII SYNTAX: a weak model writes '…'/smart-quotes into
            # Python -> SyntaxError -> the file won't import. Deterministic and
            # precise (pytest's collection abort hides the char/line).
            offenders = _non_ascii_offenders(root)
            if offenders:
                lines = "; ".join(
                    f"{f} line {ln}: invalid character {repr(ch)} ({cp})"
                    if ch else f"{f} line {ln}: syntax error ({cp})"
                    for f, ln, ch, cp in offenders)
                out += ("\n\nROOT-CAUSE HINT: these files do not compile — a "
                        "non-ASCII character slipped into Python source: "
                        + lines + ". Replace each with its ASCII equivalent "
                        "('…'→'...', smart quotes → straight quotes) and never "
                        "put a literal '…(truncated)' placeholder in code.\n"
                        + "\n".join(sorted({f for f, _, _, _ in offenders})))
                llm_log.log({"event": "non_ascii_syntax", "role": "verifier",
                             "node": str(ctx.get("node")),
                             "files": sorted({f for f, _, _, _ in offenders})})
            # #3: a FLAKY acceptance test (proven non-deterministic) must not
            # redden the root. Re-run each red file FLAKY_RERUNS times; a file
            # that flips green↔red is quarantined — recorded, never silent —
            # and the verdict is re-taken with flaky files excluded. A stably
            # red file is a real bug and stays red.
            if FLAKY_RERUNS >= 2:
                try:
                    q = quarantine_flaky(root, include_smoke, FLAKY_RERUNS)
                except Exception:  # noqa: BLE001 — never breaks the verdict
                    q = {}
                if q.get("flaky") and not q.get("stable_red"):
                    # every red source was flaky → re-run excluding them
                    cmd = [sys.executable, "-m", "pytest", "tests", "-q",
                           "--no-header", "-p", "no:cacheprovider",
                           "--import-mode=importlib"]
                    for rel in q["flaky"]:
                        cmd += ["--ignore", str(Path(root) / rel)]
                    if not include_smoke and (Path(root) / SMOKE_DIR).is_dir():
                        cmd += ["--ignore", SMOKE_DIR]
                    with llm_backend.PYTEST_LOCK:
                        pr = subprocess.run(cmd, capture_output=True, text=True,
                                            timeout=PYTEST_TIMEOUT, cwd=root)
                    if pr.returncode in (0, 5):
                        passed = True
                        out += ("\n\nQUARANTINE: these test files are FLAKY "
                                "(green↔red across reruns) and were excluded "
                                "from the verdict — NOT a product failure: "
                                + ", ".join(q["flaky"]))
                    llm_log.log({"event": "flaky_quarantine",
                                 "role": "verifier", "node": str(ctx.get("node")),
                                 "flaky": q["flaky"], "passed_after": passed})
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
