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


def _next_run_no(case_name: str) -> int:
    """Sequential run number for the case: one more than anything seen in
    runs-out — both v-numbered dirs and legacy unnumbered ones count."""
    import re as _re
    total, max_v = 0, 0
    for d in OUT_DIR.glob(f"*__{case_name}"):
        total += 1
    for d in OUT_DIR.glob(f"*__v*__{case_name}"):
        total += 1
        m = _re.search(r"__v(\d+)__", d.name)
        if m:
            max_v = max(max_v, int(m.group(1)))
    return max(total, max_v) + 1


def _worker_models() -> dict:
    """Resolved role -> model map for the run's meta.json."""
    from harness import llm_backend
    return {r: llm_backend.model_for(r)
            for r in ("decomposer", "reviewer", "implementer", "verifier")}


import yaml

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
SCENARIOS_DIR = HERE / "scenarios"
OUT_DIR = HERE / "runs-out"
sys.path.insert(0, str(HERE))
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
              decomposer: str = "blueprint", implementer: str = "auto",
              meter=None, model: str = "", workers: str = "sim",
              hitl: str = "auto") -> dict:
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
        from harness import llm_backend, role_worker
        # per-role provider/model from the case YAML (env vars override);
        # MUST happen before the factories capture their models
        llm_backend.configure_workers(case.get("workers"))
        ws_dir = str(case_dir / "workspace")
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
        max_calls = int(os.environ.get("SPEC_FLOW_MAX_DECOMPOSE_CALLS", "80"))
    elif decomposer == "llm":
        from harness import llm_decomposer
        dec_fn = llm_decomposer.decompose
        if meter is not None:
            dec_fn = meter.wrap("decomposer", dec_fn)
        agents["decomposer"] = dec_fn
        # the live LLM builds its own ids → the blueprint and the id-bound
        # revisions are dropped; the oracle still checks the realized run
        exec_case = {k: v for k, v in case.items() if k not in ("blueprint", "revisions", "revision")}
        # live budget: env-overridable so a "flexible atomicity" run can widen
        # the bounds (the engine still dies loudly on non-convergence)
        max_calls = int(os.environ.get("SPEC_FLOW_MAX_DECOMPOSE_CALLS", "80"))
    else:
        from harness import blueprint_decomposer
        agents["decomposer"] = blueprint_decomposer.make(case["blueprint"])
        exec_case = {k: v for k, v in case.items() if k != "blueprint"}
    # run config from the CASE itself (as Hermes would pass it): lifecycle engine
    node_engine = case.get("node_engine", "inline")
    exec_case.pop("node_engine", None)
    exec_case.pop("workers", None)
    review_policy = case.get("review") or None
    exec_case.pop("review", None)
    # seed files: the case may ship a deterministic skeleton (app entry,
    # router/db plumbing, the end-to-end smoke suite) the workers build INTO.
    # Passed to the ENGINE — Workspace.open wipes the dir on a fresh run, so
    # seeding it beforehand is futile by design.
    seeds = case.get("seed_files") or None
    exec_case.pop("seed_files", None)
    # platform conventions live in their own block (keeps the case's domain
    # constitution readable) and merge into the constitution for execution
    plat = exec_case.pop("constitution_platform", None)
    if plat:
        exec_case["constitution"] = list(exec_case.get("constitution") or []) \
            + list(plat)
    if seeds:
        # the seeded skeleton is immutable for workers and repair rounds
        os.environ["SPEC_FLOW_PROTECTED_FILES"] = json.dumps(sorted(seeds))
    res = eng.run_project(exec_case, workspace=str(case_dir / "workspace"), depth=depth,
                          tools=tools, agents=agents or None,
                          contracts_dir=str(eng.CONTRACTS), sink=sink,
                          max_decompose_calls=max_calls, node_engine=node_engine,
                          review_policy=review_policy, seed_files=seeds,
                          standing_requirements=getattr(
                              channel, "standing_requirements", None))
    # persist the REALIZED task tree the plugin built (parent->children), so the
    # dashboard / offline review can walk the exact structure node by node
    (case_dir / "tree.json").write_text(
        json.dumps(res.project.get("tree", {}), ensure_ascii=False, indent=2),
        encoding="utf-8")
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
        "complete": summary["complete"],
    }


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


def main() -> int:
    ap = argparse.ArgumentParser(description="Run scenario cases as real plugin runs")
    ap.add_argument("--depth", default="spec", choices=sorted(eng.DEPTHS, key=eng.DEPTHS.get))
    ap.add_argument("--case", default="", help="substring filter on the case file name")
    ap.add_argument("--decomposer", default="blueprint", choices=["blueprint", "llm"],
                    help="how the plugin BUILDS its tree: 'blueprint' = a "
                         "deterministic decomposer fed by the case blueprint "
                         "(offline, no quota); 'llm' = a live model builds it "
                         "from the goal (local `claude` CLI). Either way the "
                         "engine visits + gates every node itself")
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
                    default=os.environ.get(
                        "SPEC_FLOW_LLM_MODEL",
                        "openrouter/qwen/qwen3-coder:free"),
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
    args = ap.parse_args()
    if args.gateway == "bifrost":
        os.environ["ANTHROPIC_BASE_URL"] = os.environ.get(
            "SPEC_FLOW_BIFROST_URL", "http://127.0.0.1:8080/anthropic")
    elif args.gateway == "direct":
        os.environ.pop("ANTHROPIC_BASE_URL", None)
    os.environ["SPEC_FLOW_LLM_MODEL"] = args.model
    # HARD RULE: test runs go to the OpenRouter free pool (openai backend);
    # llm_backend guards ':free' and falls back to haiku ONLY on exhaustion.
    # Must be set before harness.role_worker / llm_backend import.
    os.environ.setdefault("SPEC_FLOW_LLM_BACKEND", "openai")
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
        case_dir = OUT_DIR / f"{stamp}__v{_next_run_no(name):03d}__{name}"
        case_dir.mkdir(parents=True, exist_ok=True)
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
            "backend": os.environ.get("SPEC_FLOW_LLM_BACKEND", "claude"),
            "workspace": "workspace", "run_dir": str(case_dir),
            # who answered for whom — verifies the author-vs-judge split
            "worker_models": _worker_models(),
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
                          workers=args.workers, hitl=args.hitl)
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
