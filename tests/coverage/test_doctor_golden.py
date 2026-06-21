"""Golden / characterization tests for the doctor's default decisions:

  1. enabled=false by default => the doctor is inert (backward-compat: a run
     behaves byte-identically to the pre-doctor engine).
  2. the 15 factory causes each map to their documented FIRST remedy (rung 0) —
     pins the cause→policy table so an accidental edit is caught.
  3. every factory ladder TERMINATES: walking the same (cause, place) repeatedly
     walks distinct rungs and then escalates the level (never an infinite loop).

Pure config + pure contour math — offline, no engine, no LLM.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_doctor as doc          # noqa: E402
import spec_flow_remedies as rem        # noqa: E402


# the documented first action per cause (base-error-causes.md / DEFAULT_CAUSES)
GOLDEN_RUNG0 = {
    "hidden_deps": "order_by_deps",
    "garbage_accumulation": "find_root",
    "empty_delta": "reject_empty",
    "context_loss": "reload_memory",
    "vague_spec": "add_contract",
    "task_too_large": "split",
    "task_check_mismatch": "reconcile_check",
    "goal_disconnect": "goal_coverage",
    "weak_implementer": "rework",
    "rewrite_loops": "patch_diff",
    "excess_input": "trim_input",
    "review_cant_block": "enable_block",
    "inconsistent_checks": "unify_verdict",
    "answer_nondeterminism": "n_vote",
    "silent_truncation": "restore_dropped",
}


def test_doctor_off_by_default():
    # backward-compat: zero-config => doctor inert, run unchanged
    assert rem.doctor_config().get("enabled") is False
    assert doc.Doctor(project={}).enabled is False


def test_all_15_causes_present():
    assert set(rem.DEFAULT_CAUSES) == set(GOLDEN_RUNG0), \
        set(rem.DEFAULT_CAUSES) ^ set(GOLDEN_RUNG0)


def test_first_remedy_matches_policy():
    causes = rem.causes_config()
    for cause, expected in GOLDEN_RUNG0.items():
        ladder = doc.ladder_for(cause, "spec_review", causes,
                                rem.DEFAULT_DOCTOR.get("default_ladder", []))
        assert ladder and ladder[0] == expected, (cause, ladder[:2], expected)


def test_every_ladder_terminates():
    # walking the same place must consume DISTINCT rungs then escalate (no rung
    # repeats, no infinite loop) — the run can never hang on a cause.
    causes = rem.causes_config()
    default = rem.DEFAULT_DOCTOR.get("default_ladder", [])
    for cause in GOLDEN_RUNG0:
        ladder = doc.ladder_for(cause, "spec_review", causes, default)
        state = doc.new_state()
        seen = []
        for _ in range(len(ladder) + 2):
            remedy, rung = doc.ladder_step(cause, "n:spec_review", state, ladder)
            seen.append((remedy, rung))
            if rung >= len(ladder) - 1:
                break
        rungs = [r for _, r in seen]
        assert rungs == sorted(set(rungs)), (cause, seen)   # strictly increasing
