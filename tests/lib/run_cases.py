#!/usr/bin/env python3
"""Run every scenario case as a REAL production run of the plugin.

For each case a separate, timestamped workspace folder is created:

    tests/runs-out/<YYYY-MM-DDTHH-MM-SS>__<case>/

EVERYTHING the run produces lands inside that folder:

    workspace/         materialised run artifacts (constitution, specs/,
                       contracts/, MANIFEST.json; deeper depths add src/,
                       tests/, COMMITS.md, TEST-RESULTS.md)
    trace.jsonl        raw event stream (full detail) — the report's source
    log.txt            readable execution log (text)
    report.md          footprints + methodology audit, built by the PLUGIN
                       (build_run_report) from trace.jsonl
    policy-report.md   policy_gate catching the imprecise goal variant
                       (only for cases that carry imprecise/resolved)
    SUMMARY.md         what ran, at which depth, verdicts, file map

Cases live in tests/scenarios/*.yaml. A case with a ``tree`` gets a full
engine run; a case with ``imprecise``/``resolved`` gets the policy pipeline;
p4 has both.

Usage:
    python3 tests/run_cases.py                          # all cases, depth=spec
    python3 tests/run_cases.py --depth scaffold         # deeper run
    python3 tests/run_cases.py --case p4 --depth verify # one case, deeper
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path


def _stop_run(run_dir: Path) -> None:
    """П1: ask a live run rooted at RUN_DIR to stop. Drops the STOP sentinel
    the engine checks at every node boundary, then SIGTERMs the run's pid
    (from run.pid) so a blocked worker wakes promptly. The run still exits
    cleanly with a partial result a later --resume continues."""
    import signal
    from harness import run_engine as _eng
    ws = run_dir / "workspace"
    sentinel = _eng.request_stop(str(ws))
    print(f"stop requested → {sentinel}")
    pidfile = run_dir / "run.pid"
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        print("no run.pid — sentinel dropped; the run stops at its next node")
        return
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"signalled pid {pid} (SIGTERM)")
    except ProcessLookupError:
        print(f"pid {pid} not running — sentinel left for a resumed run")


def _next_run_no(case_name: str) -> int:
    """Sequential run number for the case. The counter file is the source
    of truth — it tracks the OPERATOR's run sequence (v10, v11, ...) and
    survives runs-out cleanups; legacy unnumbered dirs never inflate it.
    Fallback (no counter yet): the highest v-number among existing dirs."""
    import re as _re
    marker = OUT_DIR / f".run-counter-{case_name}"
    try:
        last = int(marker.read_text().strip())
    except (OSError, ValueError):
        last = 0
        for d in OUT_DIR.glob(f"*__v*__{case_name}"):
            m = _re.search(r"__v(\d+)__", d.name)
            if m:
                last = max(last, int(m.group(1)))
    nxt = last + 1
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(nxt))
    return nxt


def _worker_models() -> dict:
    """Resolved role -> model map for the run's meta.json."""
    from harness import llm_backend
    return {r: llm_backend.model_for(r)
            for r in ("decomposer", "reviewer", "implementer", "verifier")}


import yaml

HERE = Path(__file__).resolve().parent.parent
PLUGIN_DIR = HERE.parent
SCENARIOS_DIR = HERE / "scenarios"
OUT_DIR = HERE / "runs-out"
sys.path.insert(0, str(HERE))
from harness import config as _cfg  # noqa: E402
_cfg.load_test_env()               # seed the config floor before harness reads it
from harness import auto_implementer  # noqa: E402
from harness import run_engine as eng  # noqa: E402
from harness import scenarios as scn  # noqa: E402


def _load_tools():
    """Load the plugin standalone (the same registry/toolsets stubs the tests
    use) and return its tools module."""
    reg_tools = {}

    class _Reg:
        def register(self, **kw):
            reg_tools[kw["name"]] = kw

    tools_pkg = types.ModuleType("tools")
    reg_mod = types.ModuleType("tools.registry")
    reg_mod.registry = _Reg()
    reg_mod.tool_error = lambda m: json.dumps({"error": m})
    tools_pkg.registry = reg_mod
    sys.modules["tools"] = tools_pkg
    sys.modules["tools.registry"] = reg_mod
    ts = types.ModuleType("toolsets")
    ts.TOOLSETS = {"kanban": {"tools": []}}
    sys.modules["toolsets"] = ts

    spec = importlib.util.spec_from_file_location(
        "spec_flow_cases", PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["spec_flow_cases"] = pkg
    spec.loader.exec_module(pkg)
    pkg.register(object())
    return sys.modules["spec_flow_cases.spec_flow_tools"]


def _run_full(case: dict, case_dir: Path, depth: str, tools,
              decomposer: str = "llm", implementer: str = "auto",
              meter=None, model: str = "", workers: str = "sim",
              hitl: str = "auto", resume: bool = False,
              replan: bool = False) -> dict:
    """The real production run: mandatory workspace inside the case folder,
    disk sink at full detail, the plugin's own report built from the trace.

    ``decomposer`` 'case' replays the tree, 'llm' drops it and the plugin builds
    it from the goal (live). ``implementer`` 'auto' uses the deterministic
    stand-in, 'llm' uses the live model implementer. A ``meter`` (cost.Meter)
    wraps the LLM agents to record call/token cost into COST.md."""
    tools.CONTRACT_VALIDATORS["openapi"] = [
        "python3", str(eng.OPENAPI_DIFF), "{contract}", "{code}"]
    # live agents log every model call here for post-hoc analysis (no guessing)
    live = decomposer == "llm" or implementer == "llm" or workers == "real"
    if live:
        os.environ["SPEC_FLOW_LLM_LOG"] = str(case_dir / "llm-log.jsonl")
        # claude -p is a full agent: its incidental file writes land here, not
        # in the plugin root (the engine's REAL artifacts go to workspace/)
        os.environ["SPEC_FLOW_LLM_CWD"] = str(case_dir / "agent-scratch")
    # B1/B2: opt-in pre-integrate contract gate + un-mockable boot-gate. Default
    # OFF (env unset) so p4/p5 are unchanged; a case turns it on with
    # ``integrate: {pre_gate: true}``. This is the honest hard floor — with it
    # on, a green L0 means the ASSEMBLED product actually boots and answers its
    # acceptance endpoints, not merely that the per-module pytest suite passed.
    _integ_cfg = case.get("integrate") or {}
    if _integ_cfg.get("pre_gate"):
        os.environ["SPEC_FLOW_PRE_GATE"] = "1"
    elif "SPEC_FLOW_PRE_GATE" in os.environ and not os.environ.get("SPEC_FLOW_PRE_GATE_STICKY"):
        # a case that does NOT opt in must not inherit a stray gate from a
        # previous in-process run (keeps p4/p5 deterministic in batch mode)
        del os.environ["SPEC_FLOW_PRE_GATE"]
    # Late-injection routing: a case that injects mid-run HITL requirements opts
    # the _amend matcher in so a refinement of an existing surface folds INTO the
    # owning node instead of forking a parallel one. Default OFF so non-injecting
    # cases are unchanged. ``llm_route: true`` (under injections) additionally
    # arms the strong-model fallback for refinements no deterministic signal
    # caught; without it routing stays purely deterministic.
    _inj_cfg = case.get("injections")
    if _inj_cfg:
        os.environ["SPEC_FLOW_REQ_AMEND"] = "1"
        _llm_route = (isinstance(_inj_cfg, dict) and _inj_cfg.get("llm_route"))
        if _llm_route:
            os.environ["SPEC_FLOW_AMEND_LLM"] = "1"
        elif "SPEC_FLOW_AMEND_LLM" in os.environ:
            del os.environ["SPEC_FLOW_AMEND_LLM"]
    elif "SPEC_FLOW_REQ_AMEND" in os.environ:
        del os.environ["SPEC_FLOW_REQ_AMEND"]
        os.environ.pop("SPEC_FLOW_AMEND_LLM", None)
    trace = case_dir / "trace.jsonl"
    sink = eng.LogSink(path=str(trace), level=eng.L_DETAIL, fmt="jsonl", enabled=True)
    # the plugin ALWAYS builds the tree itself by calling a decomposer — the
    # case `blueprint` never drives the engine directly (it is execution INPUT
    # for the deterministic decomposer; the analysis reference is the `oracle`
    # block). 'blueprint' = deterministic, offline, no quota; 'llm' = live model.
    agents = {}
    # HITL levels come from the CASE itself (hitl: block) — the CLI --hitl
    # console only switches WHO approves, not whether the dialogue exists.
    #   approve_required:     true = a checkpoint NEEDS a real human;
    #                         without one it REJECTS (default false =
    #                         auto-approve with an honest note)
    #   worker_may_ask_human: a blocked worker may ask and gets the answer
    #   human_may_intervene:  operator notes reach the next worker, which
    #                         must comply or defend
    hitl_cfg = case.get("hitl") or {}
    hitl_approve_required = bool(hitl_cfg.get("approve_required", False))
    hitl_questions = bool(hitl_cfg.get("worker_may_ask_human", True))
    hitl_notes = bool(hitl_cfg.get("human_may_intervene", True))
    channel = None
    if workers == "real" and (hitl_questions or hitl_notes):
        from harness import hitl as hitl_mod
        channel = hitl_mod.HumanChannel(case_dir / "hitl")
        print(f"  HITL channel: {channel.root}  "
              f"(inbox.md <- operator notes; answer.md <- reply to a worker "
              f"question; outbox.md -> comply/defend audit)")
    if workers == "real":
        # Industrial mode without Hermes: real worker sessions from the
        # plugin's own SKILL.md + profile tool policy, per role.
        from harness import llm_backend, memory as memory_mod, role_worker
        # per-role provider/model from the case YAML (env vars override);
        # MUST happen before the factories capture their models
        llm_backend.configure_workers(case.get("workers"))
        # PREFLIGHT (architecture rule): refuse to start unless EVERY provider
        # routes through Bifrost, and the claude -> Bifrost(anthropic) -> Meridian
        # -> subscription chain is LIVE (not just configured). Fails loud HERE
        # instead of mid-run — v074/v075 burned whole runs on a dead Meridian
        # behind a healthy-looking Bifrost.
        llm_backend.assert_provider_chain()
        # memory tiers (role craft / project decisions) + start-of-run
        # modes (fresh/resume/readonly/off) from the case YAML
        memory_mod.configure(case.get("memory"), case.get("name", ""))
        # intent board (anti-duplication B+C): on unless the case opts out
        # with `dedup: false`. The board's persistence backend is pluggable
        # (П15): default in-memory; `claims: {backend: sqlite}` keeps the
        # board in <workspace>/claims.db so a --resume restores cache-hits.
        from harness import claims as claims_mod
        ws_dir = str(case_dir / "workspace")
        _dedup_on = case.get("dedup", True) is not False
        _claims_store = (claims_mod.make_store(case.get("claims"), run_dir=ws_dir)
                         if _dedup_on and case.get("claims") else None)
        claims_mod.configure(_dedup_on, store=_claims_store)
        q_chan = channel if hitl_questions or hitl_notes else None
        dec_fn = role_worker.make_decomposer(workspace_dir=ws_dir, channel=q_chan)
        rev_fn = role_worker.make_reviewer()
        res_fn = role_worker.make_researcher()
        if meter is not None:
            dec_fn = meter.wrap("decomposer", dec_fn)
            rev_fn = meter.wrap("reviewer", rev_fn)
            res_fn = meter.wrap("researcher", res_fn)
        agents["decomposer"] = dec_fn
        agents["reviewer"] = rev_fn
        agents["researcher"] = res_fn
        if depth in ("execute", "product"):
            impl_fn = role_worker.make_implementer(channel=q_chan)
            if meter is not None:
                impl_fn = meter.wrap("implementer", impl_fn)
            agents["implementer"] = impl_fn
            # the integrate verdict is a REAL pytest run, never an opinion;
            # the smoke suite (tests/smoke/) gates only the root integrate
            from harness import pytest_verifier
            agents["verifier"] = pytest_verifier.make_verifier(channel=q_chan)
    if hitl == "console":
        from harness import hitl as hitl_mod
        agents["approver"] = hitl_mod.console_approver
    elif hitl_approve_required:
        # the case explicitly demands real sign-off: a checkpoint without a
        # human attached is a REJECT, not a silent pass
        def _strict_approver(ctx):
            return {"approved": False,
                    "reason": "case demands human sign-off "
                              "(hitl.approve_required: true) and no human "
                              "is attached — rejected"}
        agents["approver"] = _strict_approver
    if "implementer" not in agents and depth in ("execute", "product"):
        if implementer == "llm":
            from harness import llm_implementer
            impl_fn = llm_implementer.implement
            if meter is not None:
                impl_fn = meter.wrap("implementer", impl_fn)
            agents["implementer"] = impl_fn
        else:
            agents["implementer"] = auto_implementer.implement
    max_calls = eng.MAX_DECOMPOSE_CALLS
    if workers == "real":
        # live tree building: same blueprint/revision drop as llm mode
        exec_case = {k: v for k, v in case.items()
                     if k not in ("blueprint", "revisions", "revision", "hitl")}
        max_calls = int((case.get("workers") or {}).get("max_decompose_calls")
                        or _cfg.env("MAX_DECOMPOSE_CALLS", int))
    elif decomposer == "llm":
        from harness import llm_decomposer
        dec_fn = llm_decomposer.decompose
        if meter is not None:
            dec_fn = meter.wrap("decomposer", dec_fn)
        agents["decomposer"] = dec_fn
        # the live LLM builds its own ids → the blueprint and the id-bound
        # revisions are dropped; the oracle still checks the realized run
        exec_case = {k: v for k, v in case.items() if k not in ("blueprint", "revisions", "revision")}
        # live budget: case workers block overrides the .test.env floor so a
        # "flexible atomicity" run can widen the bounds (the engine still dies
        # loudly on non-convergence)
        max_calls = int((case.get("workers") or {}).get("max_decompose_calls")
                        or _cfg.env("MAX_DECOMPOSE_CALLS", int))
    else:
        from harness import blueprint_decomposer
        agents["decomposer"] = blueprint_decomposer.make(case["blueprint"])
        exec_case = {k: v for k, v in case.items() if k != "blueprint"}
    # run config from the CASE itself (as Hermes would pass it): lifecycle engine
    node_engine = case.get("node_engine", "inline")
    exec_case.pop("node_engine", None)
    exec_case.pop("workers", None)
    exec_case.pop("memory", None)
    review_policy = case.get("review") or None
    exec_case.pop("review", None)
    # Case-level scaffolding is intentionally NOT consumed: a case must not
    # hand the workers a ready-made skeleton (seed_files) or pre-solve its
    # known failure classes via extra rules (constitution_platform). That
    # fakes the result — the test must measure real, unaided capability. The
    # keys are stripped so a stale one in a YAML has zero effect; the engine's
    # own seed_files API stays for legitimate platform use (see test_seed_files).
    exec_case.pop("seed_files", None)
    exec_case.pop("constitution_platform", None)
    # Autonomous HITL: a declarative `injections:` block lets a dispatcher
    # materialise late requirements and answer worker questions on its own,
    # writing the same files a human would (no babysitting). Only meaningful
    # for a real-worker run with a live channel.
    dispatcher = None
    if workers == "real" and channel is not None and case.get("injections"):
        try:
            import hitl_dispatcher
            dispatcher = hitl_dispatcher.Dispatcher(case_dir, case.get("injections"))
            if dispatcher.enabled():
                dispatcher.start()
                print(f"  HITL dispatcher: autonomous injections active "
                      f"({len(dispatcher.requirements)} requirement(s))")
            else:
                dispatcher = None
        except Exception as exc:  # noqa: BLE001 — dispatcher never blocks a run
            print(f"  HITL dispatcher failed to start: {exc}")
            dispatcher = None
    # resume revalidation: inject the deterministic realness gate so a resumed
    # run re-checks each cached leaf against the CURRENT gates — a tightened
    # plugin gate re-runs ONLY the now-failing leaf, not the whole tree.
    try:
        from harness import contract_checks as _ccheck
        agents["_realness_check"] = _ccheck.realness_violations
    except Exception:  # noqa: BLE001
        pass
    def _write_terminal_meta(status, failed=None):
        """Plan Шаг 4 — stamp the run's CANONICAL outcome into meta.json so the
        result is read from one field, not guessed from a trace tail. Merges
        onto the meta written at start (never clobbers case/workers/etc.).
        v111 reached "NOT READY" yet meta.status stayed None — this closes that
        observability hole; an aborted/killed run is recorded as ABORTED."""
        import time as _time
        mp = case_dir / "meta.json"
        try:
            meta = json.loads(mp.read_text(encoding="utf-8")) if mp.is_file() else {}
        except Exception:        # noqa: BLE001 — a corrupt meta never blocks the stamp
            meta = {}
        meta["status"] = status
        meta["finished"] = _time.strftime("%Y-%m-%dT%H:%M:%S")
        if failed is not None:
            meta["product_failed"] = list(failed)
        mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                      encoding="utf-8")

    try:
        res = eng.run_project(exec_case, workspace=str(case_dir / "workspace"), depth=depth,
                              tools=tools, agents=agents or None,
                              contracts_dir=str(eng.CONTRACTS), sink=sink,
                              max_decompose_calls=max_calls, node_engine=node_engine,
                              review_policy=review_policy, resume=resume,
                              replan=replan,
                              standing_requirements=getattr(
                                  channel, "standing_requirements", None),
                              human_ask=getattr(channel, "ask", None))
    except BaseException:
        # an aborted run (crash, kill via KeyboardInterrupt) still distils
        # whatever the workers retained DURING the run into mental models —
        # otherwise a run that never reaches the post-run sweep leaves the
        # banks with raw experience and zero models (the live gap the user
        # spotted in Hindsight)
        _write_terminal_meta("ABORTED")
        try:
            from harness import memory as _mem
            if _mem.MANAGER is not None:
                _mem.finalize_run()
        except Exception:        # noqa: BLE001 — memory never masks the abort
            pass
        raise
    finally:
        if dispatcher is not None:
            dispatcher.stop()
    # persist the REALIZED task tree the plugin built (parent->children), so the
    # dashboard / offline review can walk the exact structure node by node
    (case_dir / "tree.json").write_text(
        json.dumps(res.project.get("tree", {}), ensure_ascii=False, indent=2),
        encoding="utf-8")
    # Plan Шаг 4 — canonical terminal status. READY/NOT READY come from the
    # product acceptance gate; a run that completed below product depth (no
    # acceptance asserted) is DONE, not a failure.
    _term = {"READY": "READY", "NOT READY": "FAILED"}.get(
        getattr(res, "product_status", None), "DONE")
    _write_terminal_meta(_term, failed=getattr(res, "product_failed", []))
    # memory learning sweep: engine-side lessons the workers can't see
    # (demotions, crashes), then distil the banks into mental models
    try:
        from harness import memory as _mem
        if _mem.MANAGER is not None:
            for e in res.events:
                if e.action == "childless branch demoted to leaf":
                    _mem.retain_role(
                        "decomposer",
                        f"Split of '{e.task}' proposed branch-sized metrics"
                        " but NO children — the engine demoted it to a leaf."
                        " A split must either stay atomic or name its parts.",
                        context="failed split", tags=["demotion"])
            for lp in res.loops:
                if lp.get("type") == "implementer-crash":
                    _mem.retain_role(
                        "implementer",
                        f"Implementer crashed at '{lp.get('task')}':"
                        f" {str(lp.get('detail'))[:200]}",
                        context="crash", tags=["fail"])
            models = _mem.finalize_run()
            print(f"  memory: mental models refreshed: "
                  f"{sum(len(v) for v in models.values())} across"
                  f" {len(models)} bank(s)")
    except Exception as exc:  # noqa: BLE001 — memory must not fail a run
        print(f"  memory sweep failed: {exc}")
    if meter is not None:
        from harness import cost as _cost
        _cost.write_cost_md(meter, str(case_dir / "COST.md"),
                            model=model, case=case.get("name", ""))
    # turn the raw LLM call log into an analysis (tree shape, latency, errors)
    log_file = case_dir / "llm-log.jsonl"
    if live and log_file.is_file():
        try:
            import analyze_llm_log as _an
            (case_dir / "llm-analysis.md").write_text(
                _an.render(_an.analyse(_an._load(log_file))), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — analysis must not fail a run
            (case_dir / "llm-analysis.md").write_text(f"analysis failed: {exc}\n",
                                                      encoding="utf-8")

    widths = eng._column_widths(res.events)
    (case_dir / "log.txt").write_text(
        "\n".join(eng.event_line(e, widths) for e in res.events) + "\n", encoding="utf-8")
    # the workflow view: goal/task tree (versions, ↻ re-runs, episode tags) +
    # the execution log + the loops table — where the run cycled and why
    (case_dir / "workflow.md").write_text(
        eng.render_report(res, level=eng.L_DETAIL), encoding="utf-8")
    events = tools._load_trace(str(trace))
    report = tools.build_run_report(events, title=f"{case['name']} (depth={depth})")
    (case_dir / "report.md").write_text(report, encoding="utf-8")

    findings = tools.audit_methodology(events)
    summary = tools.summarize_trace(events)

    # smart oracle report — built by PLUGIN code (tools.build_oracle_report)
    # from the realized run + the case's declared `oracle:` block, NOT from a
    # 1:1 tree comparison. Written into the run folder like every other report.
    oracle_spec = case.get("oracle")
    oracle_ok = None
    if oracle_spec:
        oracle_rep = tools.check_oracle(res, oracle_spec, summary)
        oracle_ok = oracle_rep.ok
        (case_dir / "oracle-report.md").write_text(
            tools.build_oracle_report(res, oracle_spec, summary,
                                      title=f"{case['name']} — oracle (depth={depth})"),
            encoding="utf-8")

    # product readiness report (depth=product) is written by the engine itself
    # into workspace/PRODUCT-RESULTS.md; surface its verdict in the summary.
    product_verdict = None
    pr_path = case_dir / "workspace" / "PRODUCT-RESULTS.md"
    if pr_path.exists():
        head = pr_path.read_text(encoding="utf-8")[:400]
        product_verdict = ("READY" if "✅" in head and "READY" in head
                           else "NOT READY" if "NOT READY" in head else "—")

    return {
        "oracle_ok": oracle_ok,
        "product": product_verdict,
        "skills": f"{len(res.skills_used)}/{len(eng.ALL_SKILLS)}",
        "profiles": f"{len(res.profiles_used)}/{len(eng.ALL_PROFILES)}",
        "tasks": len(res.tasks),
        "loops": {l["type"] for l in res.loops},
        "gate_calls": res.gate_calls,
        "audit_errors": sum(1 for f in findings if f["severity"] == "error"),
        "audit_warns": sum(1 for f in findings if f["severity"] == "warn"),
        # 'complete' is a VERDICT, not a progress bar: a run whose ROOT
        # gate recorded a FAIL can never present itself as ✅ — v17 did,
        # and that deception must be structurally impossible
        "complete": summary["complete"] and not _root_red(res),
        "root_red": _root_red(res),
    }


def _root_red(res) -> bool:
    """The ROOT integrate recorded a FAIL (record-and-continue policy):
    the run reached the end, but the assembled product is NOT green."""
    root_id = str((res.project.get("tree") or {}).get("id"))
    return any(lp.get("type") == "integrate-fail"
               and str(lp.get("task")) == root_id
               for lp in res.loops)


def _run_policy(path: Path, case_dir: Path, tools) -> dict:
    """The policy surface: the plugin (not a human) catches the imprecise goal."""
    sc = scn.load_scenario(path)
    (case_dir / "policy-report.md").write_text(
        scn.render_scenario(tools, sc) + "\n", encoding="utf-8")
    imprecise = scn.run_pipeline(tools, sc.imprecise)
    resolved = scn.run_pipeline(tools, sc.resolved)
    return {"imprecise": imprecise["policy_verdict"], "resolved": resolved["policy_verdict"]}


def _summary_md(name: str, goal: str, depth: str, full: dict | None,
                policy: dict | None) -> str:
    lines = [f"# {name} — итог прогона", "",
             f"**Цель:** {goal}", f"**Глубина:** `{depth}`", ""]
    if policy:
        lines += ["## Policy-разрез (плагин ловит неточность)",
                  f"- размытая постановка → **{policy['imprecise']}** (не декомпозируется)",
                  f"- уточнённая постановка → **{policy['resolved']}**", ""]
    if full:
        loops = ", ".join(sorted(full["loops"])) or "—"
        gates = ", ".join(f"{k}×{v}" for k, v in full["gate_calls"].items())
        audit = ("✅ нарушений нет" if not full["audit_errors"] and not full["audit_warns"]
                 else f"❌ ошибок {full['audit_errors']}, предупреждений {full['audit_warns']}")
        oracle = ("—" if full["oracle_ok"] is None
                  else "✅ пройден" if full["oracle_ok"] else "❌ не пройден")
        product = full["product"] or "— (глубина ниже product)"
        lines += ["## Полный прогон",
                  f"- скиллы: **{full['skills']}** · профили: **{full['profiles']}** · задач: **{full['tasks']}**",
                  f"- циклы: {loops}",
                  f"- вызовы гейтов: {gates}",
                  f"- завершён: {'✅' if full['complete'] else '❌'}",
                  # a RED root recorded by the integrate policy must shout —
                  # v17 looked '✅ завершён' while the final suite never
                  # went green (record-and-continue hid it)
                  f"- корневой гейт: "
                  f"{'🟥 КРАСНЫЙ — финальный набор тестов не зелёный (политика record)' if full.get('root_red') else '🟩 зелёный'}",
                  f"- методологический аудит: {audit}",
                  f"- умный оракул (опорные точки/глубина/эпизоды): {oracle}",
                  f"- готовность продукта: {product}", ""]
    lines += ["## Файлы",
              "- `workspace/` — артефакты прогона (конституция, спеки, контракты, MANIFEST)",
              "- `workspace/PRODUCT-RESULTS.md` — вердикт готовности продукта (глубина `product`)",
              "- `workflow.md` — **дерево целей/задач** (версии, ↻ повторы, эпизоды) + журнал + таблица циклов",
              "- `trace.jsonl` — сырой событийный поток (источник отчётов)",
              "- `log.txt` — журнал исполнения",
              "- `report.md` — footprint + методологический аудит (строит код плагина)",
              "- `oracle-report.md` — умная проверка исхода (строит код плагина: `build_oracle_report`)",
              "- `policy-report.md` — ловля размытой постановки (если есть policy-разрез)", ""]
    return "\n".join(lines)


def _parse_ovr_value(v: str):
    """Parse a --doctor-set value: JSON first, then [a,b] lists, bool/int, else str."""
    import json as _json
    s = str(v).strip()
    try:
        return _json.loads(s)
    except Exception:  # noqa: BLE001
        pass
    if s.startswith("[") and s.endswith("]"):
        return [x.strip() for x in s[1:-1].split(",") if x.strip()]
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s


def _apply_doctor_overrides(args) -> None:
    """Turn --doctor-enabled / --doctor-set K=V into layer-5 doctor overrides
    (highest priority) so a run can be tuned without editing the case YAML."""
    over: dict = {}
    if getattr(args, "doctor_enabled", None) is not None:
        over.setdefault("doctor", {})["enabled"] = bool(args.doctor_enabled)
    for item in getattr(args, "doctor_set", []) or []:
        if "=" not in item:
            continue
        path, _, val = item.partition("=")
        keys = [k for k in path.strip().split(".") if k]
        if not keys:
            continue
        ns = keys[0] if keys[0] in ("doctor", "causes", "evaluator", "tiers",
                                    "complexity_to_tier") else "doctor"
        rest = keys[1:] if keys[0] == ns else keys
        node = over.setdefault(ns, {})
        for k in rest[:-1]:
            node = node.setdefault(k, {})
        if rest:
            node[rest[-1]] = _parse_ovr_value(val)
    if not over:
        return
    try:
        try:
            import spec_flow_remedies as _rem  # type: ignore
        except Exception:  # noqa: BLE001
            from harness import run_engine  # noqa: F401  (ensures path)
            import spec_flow_remedies as _rem  # type: ignore
        _rem.set_overrides(over)
        print(f"[doctor] overrides applied: {over}")
    except Exception as exc:  # noqa: BLE001
        print(f"[doctor] could not apply overrides: {exc}")


def main() -> int:
    # Verb sugar over the flags (ACTION style, like the project Makefiles), kept
    # back-compatible: a leading non-dash token is a verb; `run` falls through to
    # the flag parser, the control verbs map to their one-shot flags. An unknown
    # leading token is left as-is so argparse errors helpfully.
    _argv = sys.argv[1:]
    if _argv and not _argv[0].startswith("-"):
        _verb, _rest = _argv[0], _argv[1:]
        _ctl = {"stop": "--stop", "checkpoint": "--checkpoint",
                "checkpoints": "--list-checkpoints"}
        if _verb in _ctl and _rest:
            sys.argv = [sys.argv[0], _ctl[_verb], _rest[0]] + _rest[1:]
        elif _verb == "run":
            sys.argv = [sys.argv[0]] + _rest
    # Opt-in detailed doctor logging (calls + LLM replies + decisions) to stderr,
    # captured by the run's log file. SPEC_FLOW_DOCTOR_LOG=DEBUG for even more.
    _dl = os.environ.get("SPEC_FLOW_DOCTOR_LOG", "").strip().upper()
    if _dl:
        import logging as _lg
        _lg.basicConfig(level=getattr(_lg, _dl, _lg.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
        _lg.getLogger("spec_flow.doctor").setLevel(getattr(_lg, _dl, _lg.INFO))
    ap = argparse.ArgumentParser(description="Run scenario cases as real plugin runs")
    ap.add_argument("--depth", default="spec", choices=sorted(eng.DEPTHS, key=eng.DEPTHS.get))
    ap.add_argument("--case", default="", help="substring filter on the case file name")
    ap.add_argument("--decomposer", default="llm", choices=["blueprint", "llm"],
                    help="how the plugin BUILDS its tree (default 'llm'): 'llm' "
                         "= a live model builds it from the goal (real, unaided "
                         "capability); 'blueprint' = a deterministic decomposer "
                         "replays the case's hand-written blueprint tree "
                         "(offline, no quota — a pre-built tree, use only for "
                         "deterministic engine tests). Either way the engine "
                         "visits + gates every node itself")
    ap.add_argument("--implementer", default="auto", choices=["auto", "llm"],
                    help="'auto' deterministic stand-in; 'llm' live model "
                         "implementer (depth execute/product only)")
    ap.add_argument("--workers", default="sim", choices=["sim", "real"],
                    help="'real' = industrial mode WITHOUT Hermes: every role "
                         "(decomposer/implementer/reviewer/researcher) is a "
                         "live worker session built from the plugin's OWN "
                         "SKILL.md + profile tool policy; overrides "
                         "--decomposer/--implementer")
    ap.add_argument("--hitl", default="auto", choices=["auto", "console"],
                    help="'console' puts a real human at every HITL "
                         "checkpoint (terminal prompt; timeout auto-approves "
                         "with an honest note); 'auto' keeps the default")
    ap.add_argument("--model",
                    default=_cfg.env("LLM_MODEL"),
                    help="LLM model for live agents — the OpenRouter free"
                         " pool is the only allowed primary (default"
                         " qwen3-coder:free via Bifrost); haiku is the"
                         " quota-exhaustion fallback inside llm_backend")
    ap.add_argument("--dashboard", action="store_true",
                    help="serve a live auto-refreshing web dashboard over this run "
                         "(interactive tree, per-node spec/versions/code/events, the "
                         "plugin's own report) — stays up after the run for review")
    ap.add_argument("--dashboard-port", type=int, default=8088,
                    help="port for --dashboard (default 8088)")
    ap.add_argument("--gateway", default="headroom",
                    choices=["headroom", "bifrost", "direct"],
                    help="LLM gateway for live agents: 'headroom' keeps the env "
                         "routing as-is (default), 'bifrost' points claude at the "
                         "Bifrost Anthropic route (env SPEC_FLOW_BIFROST_URL), "
                         "'direct' clears ANTHROPIC_BASE_URL")
    ap.add_argument("--resume", default="", metavar="RUN_DIR",
                    help="continue a stopped/crashed run: reuse RUN_DIR, "
                         "restore its journal + claim board, re-run only the "
                         "leaves that hadn't committed")
    ap.add_argument("--replan", action="store_true",
                    help="on resume, RE-RUN the decomposer (operator changed the "
                         "goal/spec → fresh plan). Default off: a resume reuses "
                         "the persisted decomposition and continues the SAME tree "
                         "from the checkpoint instead of re-decomposing from L0")
    ap.add_argument("--stop", default="", metavar="RUN_DIR",
                    help="ask a live run rooted at RUN_DIR to stop at the next "
                         "node boundary (drops a STOP sentinel + signals its pid)")
    ap.add_argument("--checkpoint", default="", metavar="RUN_DIR",
                    help="ask a live run to SNAPSHOT itself at the next node "
                         "boundary and keep running (replayable point)")
    ap.add_argument("--list-checkpoints", default="", metavar="RUN_DIR",
                    help="list the checkpoints saved under RUN_DIR/checkpoints/")
    ap.add_argument("--from", dest="from_ckpt", default="",
                    metavar="CHECKPOINT_DIR",
                    help="replay FROM a checkpoint: restore its workspace into a "
                         "fresh run dir and resume (cache-hits skip done nodes)")
    ap.add_argument("--checkpoint-every", type=int, default=0, metavar="N",
                    help="engine auto-checkpoint cadence: the plugin snapshots "
                         "itself every N node boundaries (0=off). A run/engine "
                         "parameter, like --decomposer; a case may also set "
                         "`checkpoint: {every: N}`")
    ap.add_argument("--from-run", default="", metavar="N",
                    help="clone run NUMBER N (the vNNN in a run dir) from a "
                         "checkpoint into a FRESH numbered run and resume. "
                         "Operator-friendly alias for --from <path>; pair with "
                         "--from-checkpoint M (default: the run's LAST checkpoint)")
    ap.add_argument("--from-checkpoint", default="", metavar="M",
                    help="checkpoint NUMBER M (the NNN in checkpoints/NNN__*) "
                         "within --from-run to start from")
    ap.add_argument("--doctor-enabled", dest="doctor_enabled",
                    action="store_true", default=None,
                    help="force the cause-diagnosis doctor ON for this run "
                         "(overrides the case doctor.enabled)")
    ap.add_argument("--doctor-disabled", dest="doctor_enabled",
                    action="store_false",
                    help="force the doctor OFF (baseline compare run)")
    ap.add_argument("--doctor-set", action="append", default=[], metavar="K=V",
                    help="override a doctor config key by dot-path, repeatable: "
                         "--doctor-set evaluator.votes=3 "
                         "--doctor-set causes.task_too_large.ladder='[split,record]'")
    args = ap.parse_args()
    _apply_doctor_overrides(args)
    # resolve the operator-friendly --from-run N [--from-checkpoint M] to the
    # checkpoint DIR that --from replays: clone run N's checkpoint M into a fresh
    # numbered run and resume. Numbers, not paths — the run sequence the operator
    # already sees (v028, v029, ...) and the checkpoint sequence from
    # --list-checkpoints.
    if args.from_run:
        try:
            _rn = int(args.from_run)
        except ValueError:
            ap.error(f"--from-run must be a number, got {args.from_run!r}")
        _runs = sorted(OUT_DIR.glob(f"*__v{_rn:03d}__*"))
        if not _runs:
            ap.error(f"--from-run {_rn}: no run v{_rn:03d} under {OUT_DIR}")
        _src = _runs[-1]
        _ckdir = _src / "checkpoints"
        _cks = sorted(_ckdir.glob("[0-9]*__*")) if _ckdir.is_dir() else []
        if not _cks:
            ap.error(f"--from-run {_rn}: run {_src.name} has no checkpoints "
                     f"(run it with --checkpoint-every N)")
        if args.from_checkpoint:
            try:
                _cn = int(args.from_checkpoint)
            except ValueError:
                ap.error("--from-checkpoint must be a number, got "
                         f"{args.from_checkpoint!r}")
            _match = sorted(_ckdir.glob(f"{_cn:03d}__*"))
            if not _match:
                _avail = ", ".join(p.name.split("__")[0] for p in _cks)
                ap.error(f"--from-checkpoint {_cn}: not in {_src.name}/"
                         f"checkpoints (have: {_avail})")
            args.from_ckpt = str(_match[0])
        else:
            args.from_ckpt = str(_cks[-1])   # default: the latest checkpoint
        print(f"[from-run] v{_rn:03d} checkpoint "
              f"{Path(args.from_ckpt).name} -> cloning into a fresh run")
    elif args.from_checkpoint:
        ap.error("--from-checkpoint requires --from-run")
    # one-shot control commands: act, then exit
    if args.stop:
        _stop_run(Path(args.stop))
        return
    if args.checkpoint:
        sentinel = eng.request_checkpoint(str(Path(args.checkpoint) / "workspace"))
        print(f"checkpoint requested → {sentinel}")
        print("the live run snapshots itself at its next node boundary")
        return
    if args.list_checkpoints:
        cks = eng.list_checkpoints(args.list_checkpoints)
        if not cks:
            print("no checkpoints"); return
        for m in cks:
            print(f"  {m.get('seq'):>3}  {m.get('roothash','')}  "
                  f"node={m.get('node','') or '-'}  specs={m.get('specs', 0)}  "
                  f"{m['path']}")
        return
    if args.gateway == "bifrost":
        os.environ["ANTHROPIC_BASE_URL"] = _cfg.env("BIFROST_URL")
    elif args.gateway == "direct":
        os.environ.pop("ANTHROPIC_BASE_URL", None)
    os.environ["SPEC_FLOW_LLM_MODEL"] = args.model
    # Transport (backend) is NOT hardcoded here anymore: it comes from the case
    # `workers:` block (applied by configure_workers, which wins over the
    # tests/.test.env floor). A case run via Bifrost declares `backend: openai`
    # in its workers block — see scenarios/p5_mvp_marketplace.yaml.
    if (args.decomposer == "llm" or args.workers == "real") and not args.case:
        # live LLM runs cost real quota: one call per tree node — keep the
        # default to the single smallest case; widen explicitly via --case
        args.case = "p2"
        print("[llm mode] no --case given -> restricted to the smallest case (p2)")

    tools = _load_tools()
    dash = None
    if args.dashboard:
        import live_dashboard
        dash = live_dashboard.start_in_thread(port=args.dashboard_port)
        print(f"dashboard → http://localhost:{args.dashboard_port}  "
              f"(live, refresh 2s)\n")
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    rows = []
    for path in sorted(SCENARIOS_DIR.glob("*.yaml")):
        if args.case and args.case not in path.stem:
            continue
        case = yaml.safe_load(path.read_text(encoding="utf-8"))
        name = case.get("name", path.stem)
        # --resume continues an existing run dir in place; a normal run mints
        # a fresh, numbered one. --from replays a CHECKPOINT into a fresh dir.
        # The pidfile lets --stop signal this process.
        resuming = bool(args.resume) or bool(args.from_ckpt)
        case_dir = (Path(args.resume) if args.resume
                    else OUT_DIR / f"{stamp}__v{_next_run_no(name):03d}__{name}")
        case_dir.mkdir(parents=True, exist_ok=True)
        # --from: restore the checkpoint's workspace into this run dir so the
        # engine (resume mode) re-enters from exactly that point — its journal +
        # specs drive the cache-hits, so saved nodes are skipped, not rebuilt.
        if args.from_ckpt:
            eng.restore_checkpoint(args.from_ckpt, str(case_dir / "workspace"))
            print(f"replaying from checkpoint {args.from_ckpt} → {case_dir}")
        # auto-checkpoint cadence: engine parameter, surfaced as a flag or a
        # case `checkpoint: {every: N}` block; the engine reads the env
        _ck_every = args.checkpoint_every or int(
            (case.get("checkpoint") or {}).get("every", 0) or 0)
        if _ck_every > 0:
            os.environ["SPEC_FLOW_CHECKPOINT_EVERY"] = str(_ck_every)
        else:
            os.environ.pop("SPEC_FLOW_CHECKPOINT_EVERY", None)
        (case_dir / "run.pid").write_text(str(os.getpid()) + "\n",
                                          encoding="utf-8")
        # persist the readable STARTING inputs (goal + givens) so the dashboard /
        # offline review shows what the plugin was asked to build — no hints
        inputs = {k: case[k] for k in
                  ("goal", "target", "constitution", "policy", "acceptance",
                   "imprecise", "resolved", "oracle") if k in case}
        (case_dir / "inputs.json").write_text(
            json.dumps(inputs, ensure_ascii=False, indent=2), encoding="utf-8")
        # per-role provider/model from the case YAML — configured here so the
        # meta.json map below reflects it (re-applied in _run_full; idempotent)
        from harness import llm_backend as _lb
        _lb.configure_workers(case.get("workers"))
        # service metadata (how the run was launched) for the dashboard's info pane
        (case_dir / "meta.json").write_text(json.dumps({
            "case": name, "depth": args.depth, "decomposer": args.decomposer,
            "implementer": args.implementer, "model": args.model, "stamp": stamp,
            "node_engine": case.get("node_engine", "inline"),
            "gateway": args.gateway,
            "backend": _cfg.env("LLM_BACKEND"),
            "workspace": "workspace", "run_dir": str(case_dir),
            # who answered for whom — verifies the author-vs-judge split
            "worker_models": _worker_models(),
            # the full workers block (stages + team specialists w/ provider,
            # model, params) so the dashboard team card reads the REAL config
            # instead of reconstructing a partial roster from the llm-log.
            "workers": case.get("workers"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        # fresh state per case so gate cooldowns never leak between cases
        os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix=f"specflow-{name}-")

        policy = _run_policy(path, case_dir, tools) if "imprecise" in case else None
        runnable = "blueprint" in case or args.decomposer == "llm" or args.workers == "real"
        live = args.decomposer == "llm" or args.implementer == "llm" or args.workers == "real"
        meter = None
        if live:
            from harness import cost as _cost
            meter = _cost.Meter()
        full = (_run_full(case, case_dir, args.depth, tools, args.decomposer,
                          implementer=args.implementer, meter=meter, model=args.model,
                          workers=args.workers, hitl=args.hitl, resume=resuming,
                          replan=args.replan)
                if runnable else None)

        (case_dir / "SUMMARY.md").write_text(
            _summary_md(name, case.get("goal", ""), args.depth, full, policy),
            encoding="utf-8")
        rows.append((name, case_dir, policy, full))

    print(f"depth={args.depth} · cases: {len(rows)}\n")
    for name, case_dir, policy, full in rows:
        bits = []
        if policy:
            bits.append(f"policy: {policy['imprecise']}→{policy['resolved']}")
        if full:
            audit = "audit ✅" if not full["audit_errors"] else f"audit ❌×{full['audit_errors']}"
            bits.append(f"skills {full['skills']}, profiles {full['profiles']}, "
                        f"tasks {full['tasks']}, {audit}")
            if full["oracle_ok"] is not None:
                bits.append("oracle ✅" if full["oracle_ok"] else "oracle ❌")
            if full["product"]:
                bits.append(f"product {full['product']}")
        print(f"  {name:24s} {' · '.join(bits)}")
        print(f"  {'':24s} → {case_dir.relative_to(PLUGIN_DIR)}/")

    if dash is not None:
        print(f"\ndashboard live → http://localhost:{args.dashboard_port}  "
              f"(Ctrl+C to stop)")
        try:
            import time
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            dash.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
