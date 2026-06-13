# Session state — spec-flow (2026-06-13)

Branch: `feat/roadmap-phase-1` (repo nnnet/hermes-plugin-spec-flow).
Head: `69eb9c5`. Tests: 605 green. Dashboard live on :8092 (one only;
NEVER touch 8507/8508; kill only by exact PID).

## Plan list — ALL DONE this block (each = tests+commit+push)
- **П2** (#32) — wave journal wired into the engine (waves.jsonl, one
  node_commit wave per node). Commit 5e5044e.
- **П15** — pluggable ClaimStore: memory default + sqlite-per-run WAL with
  RESTORE on reopen (stop/restart persistence). Commit 6577125.
- **F** (#37, MANDATORY) — leaf worktree isolation + merge-tree pre-flight
  (`isolation: worktree`); conflict REFUSED, never forced. Commit a8ca84c.
- **П1** (#35) — cooperative stop + --resume / --stop: STOP sentinel at
  node boundary, partial result, run.pid SIGTERM. Commit f1bec2d.
- **П3** (#33) — closed MEASURE-ONLY: phase overlap already delivered by
  parallel siblings (#28). phase_overlap.py proved on v023: peak conc 6,
  2235s vs 7446s = ~70% saved. Commit 33376bb.

## Quality levers — ALSO DONE (beyond the plan)
- **П7** (#38) — reviewer judges with ARTIFACTS: repo map (existing public
  surface) + node refusal history → catch collisions at review, not
  integrate. Commit 32375da.
- **П4-bis** (#39) — poisoner bisection in verifier: 'green alone, red
  together' → names the poisoner so repair targets it, not victims.
  Cost-guarded (≤16 files, once per red, off via SPEC_FLOW_BISECT=0).
  Commit 504e294.
- **#40** — cyclic primary rotation (`cycle_models`) spreads load across a
  role's chain; ENDLESS-ROTATION GUARD: quota waits unbounded, but
  CONSECUTIVE non-quota ERROR rounds capped by `max_error_rounds` (reset by
  a quota round). haiku leads reviewer+verifier, in every chain. Commit
  69eb9c5.

## Active mission — task #21 (cron a8680732, every 13 min)
Loop: watch newest `tests/runs-out/*p4*/` → after ≥2 `integrate_verify`
gates, inject BOTH late requirements + seed their protected smoke tests:
  - web_ui: `cp /tmp/claude/web_ui_requirement/REQUIREMENT.md <run>/hitl/requirements/web_ui/`
  - seller_directory: same from `/tmp/claude/seller_directory_requirement/`
  - smoke: `cp /tmp/claude/*/test_*.py <run>/workspace/tests/smoke/`
Answer worker questions in `<run>/hitl/answer.md` (consumed+unlinked when
non-empty); review-exhausted ask → reply "record" if spec tolerable. On
finish: both ATTACHED, src/web_ui*.py + src/seller_directory*.py exist,
independent `python3 -m pytest tests -q` in workspace (outside sandbox),
GREEN root WITH web UI = close #21. FAIL → diagnose class, fix, commit+push,
restart next vNNN.

Current run: v023 (`2026-06-13T12-19-53__v023`, pid 587059, ~56min).
9 integrate gates, 18 leaves done. web_ui + seller_directory INJECTED
(13:14) + smoke seeded; seller_consent question answered+consumed. Run
progressing (tick 323). Next cron check: verify web_ui/seller_directory
materialize + root PASS. Launch cmd for vNNN:
`SPEC_FLOW_INTEGRATE_MAX_REPAIR=6 python3 tests/run_cases.py --case p4 --workers real --depth execute --hitl auto > /tmp/specflow_industrial_p4_vNNN.log 2>&1`

## Hard rules
- Free OpenRouter only + haiku/subscription fallback; paid NEVER.
- Comments/strings English; brief Russian replies, no jargon.
- Commit+push after each block. Plans in `<project>/.claude/plans/`.
- Navigation via Serena; codebase-memory only for dead code.
- Board mechanism MUST support stop/restart (persist + restore) — done П15.
- Dashboard one on :8092; NEVER touch 8507/8508; kill only by exact PID.
