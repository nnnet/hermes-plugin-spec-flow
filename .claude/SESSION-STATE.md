# Session state — spec-flow (snapshot before compact, 2026-06-13)

Branch: `feat/roadmap-phase-1` (repo nnnet/hermes-plugin-spec-flow).
Head: `537f940`. Tests: 556 green. Dashboard live on :8092 (one only;
NEVER touch 8507/8508; kill only by exact PID).

## Active mission — task #21 (cron cycle p4+web)
Loop: watch newest `tests/runs-out/*p4-b2b*/` → after ≥2 completed
`integrate_verify` gates, inject BOTH late requirements:
  - web_ui (no scope): `cp /tmp/claude/web_ui_requirement/* <run>/hitl/requirements/web_ui/`
  - seller_directory (scoped): REQUIREMENT.md first line `@scope: <an open L1 branch>`,
    payload from `/tmp/claude/seller_directory_requirement/`
Answer worker questions in `<run>/hitl/answer.md`; review-exhausted ask
gate → reply "record" if spec is tolerable. On finish: both ATTACHED,
src/web_ui*.py + src/seller_directory*.py exist, independent
`python3 -m pytest tests -q` in workspace (outside sandbox), GREEN root
WITH web UI = close #21. FAIL → diagnose class, fix, commit+push, restart
next vNNN.

Current run: v022 (`2026-06-13T10-32-25__v022__p4-b2b-marketplace`),
WEDGED on free-pool cooldown ~30min at tick 233 (category_management
integrate), 0 completed integrations, 25 quota_waits. Alive, not dead.
NEXT RUN (v023) picks up new config: verifier LEADS claude/haiku, so
integration won't stall on free pool. Consider killing v022 by exact PID
and launching v023 once free pool / morning throttle context is clear.

Launch cmd: `SPEC_FLOW_INTEGRATE_MAX_REPAIR=6 python3 tests/run_cases.py --case p4 --workers real --depth execute --hitl auto > /tmp/specflow_industrial_p4_v23.log 2>&1` (background, unsandboxed).

## Plan feature list — "гони по списку" (autonomous, each = tests+commit+push)
Order (dependency-driven):
1. **П2** (task #32) — wire wave journal (spec_flow_journal.py RunJournal)
   into the engine: write a wave on each node commit. Foundation for resume.
2. **П15 sqlite ClaimStore** — pluggable board backend (memory default,
   sqlite-per-project `<run>/claims.db` WAL). Durable write per
   claim/complete. Plan §П15 + §П15-bis (persistence/restore contract).
   Pluggable like MemoryProvider: memory/sqlite/beads/redis.
3. **П1** (task #35) — stop/resume: STOP file + SIGTERM, `--resume <dir>`,
   `--stop <dir>`; restore wave journal + claim board from disk; in-flight
   nodes re-run idempotently. Depends on П2 + П15-sqlite.
4. **F** (task #37, MANDATORY) — worktree-per-leaf + `git merge-tree`
   pre-flight. Physical isolation axis (separate from anti-dup).
5. **П3** (task #33) — phase pipeline (review of one child while another
   implements); measure via compare_runs.

## DONE this block (anti-dup axis #36 complete)
- A static namespace partition (unique module per node, `_module_for`).
- B+C intention board + content-hash (duplicate intent → cache-hit
  re-export, no 2nd LLM call). `tests/harness/claims.py` (in-memory
  BOARD singleton, configure()), wired in role_worker.implement.
- Language pinned in prompts (SPEC_FLOW_LANG/workers.language, default
  English) + auto-responder language-agnostic (neutral code anchors).
- Dashboard: compare-runs tab (filters/sort/tooltips), role→model map,
  collapsible/resizable left panel, sticky tabs, flow bottom-up lanes.
- Resilience: quota-wait (not death), jitter, cross-provider fallback
  rotation (`fallback_after_rounds`), hung-CLI retriable, honest red
  root (no fake ✅), leaf time ceiling, commit_queue + git ws_tx.
- p4 config: verifier leads haiku, implementer cyclic 3-model chain.

## Hard rules
- Free OpenRouter only + haiku/subscription fallback; paid NEVER.
- Comments/strings English; brief Russian replies, no jargon.
- Commit+push after each block. Plans in `<project>/.claude/plans/`.
- Navigation via Serena; codebase-memory only for dead code.
- Board mechanism MUST support stop/restart (persist + restore) — П15-bis.
