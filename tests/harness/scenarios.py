"""Business-scenario harness: run a deliberately-imprecise project goal through
the plugin's gate pipeline and show that the plugin (not the human) catches and
forces resolution of the imprecision.

Pipeline modelled (per the SDD template):

    policy_gate (constitution: measurable target / spend cap / consent /
                 legality)  ->  leaf_check (structure: leaf vs branch)

A 'block' or 'clarify' from policy_gate means the spec is NOT decomposed — it is
routed back to be fixed first. Each scenario carries an 'imprecise' L0 (the
vague/risky statement) and a 'resolved' L0 (after the gate forced the fix); the
report shows both and reconciles against the template's must-catch list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

HARNESS_DIR = Path(__file__).resolve().parent
SCENARIOS_DIR = HARNESS_DIR.parent / "scenarios"


@dataclass
class Variant:
    title: str
    metrics: dict
    policy: dict
    expect_policy: str
    expect_leaf: str


@dataclass
class Scenario:
    name: str
    goal: str
    imprecision: str
    imprecise: Variant
    resolved: Variant
    template_must_catch: list[str]


def _variant(d: dict) -> Variant:
    return Variant(
        title=d["title"], metrics=d.get("metrics", {}), policy=d.get("policy", {}),
        expect_policy=d["expect_policy"], expect_leaf=d["expect_leaf"],
    )


def load_scenario(path: Path) -> Scenario:
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Scenario(
        name=d["name"], goal=d.get("goal", ""), imprecision=d.get("imprecision", ""),
        imprecise=_variant(d["imprecise"]), resolved=_variant(d["resolved"]),
        template_must_catch=d.get("template_must_catch", []),
    )


def all_scenarios() -> list[Scenario]:
    """Policy-surface scenarios only — product cases (no imprecise/resolved
    variants, e.g. p5) are exercised by the run pipeline, not the policy gate."""
    out = []
    for p in sorted(SCENARIOS_DIR.glob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        if "imprecise" in d and "resolved" in d:
            out.append(load_scenario(p))
    return out


def run_pipeline(tools: Any, variant: Variant) -> dict:
    """Run policy_gate then (only if not blocked) leaf_check, recording the
    data handed to each gate and its verdict + reasons.
    """
    policy_out = json.loads(tools._handle_policy_gate(dict(variant.policy)))
    leaf_out = json.loads(tools._handle_leaf_check(dict(variant.metrics)))

    decomposes = policy_out["verdict"] == "pass"
    return {
        "policy_in": dict(variant.policy),
        "policy_verdict": policy_out["verdict"],
        "policy_blocks": policy_out["blocks"],
        "policy_clarifications": policy_out["clarifications"],
        "leaf_in": dict(variant.metrics),
        "leaf_verdict": leaf_out["verdict"],
        "leaf_reasons": leaf_out["reasons"],
        "proceeds_to_decompose": decomposes,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_PV = {"pass": "✅ pass", "clarify": "🟡 clarify", "block": "⛔ block"}


def _reasons_block(out: dict) -> str:
    lines = []
    for b in out["policy_blocks"]:
        lines.append(f"      ⛔ {b}")
    for c in out["policy_clarifications"]:
        lines.append(f"      🟡 {c}")
    return "\n".join(lines)


def render_scenario(tools: Any, sc: Scenario) -> str:
    imp = run_pipeline(tools, sc.imprecise)
    res = run_pipeline(tools, sc.resolved)

    out: list[str] = []
    out.append(f"## {sc.name}")
    out.append(f"_{sc.goal}_\n")
    out.append(f"**Намеренная неточность:** {sc.imprecision.strip()}\n")

    # imprecise pipeline
    out.append("### 1) Как подан (размытая постановка)")
    out.append("```")
    out.append(f"L0: {sc.imprecise.title}")
    out.append(f"  ├─ policy_gate  ← {json.dumps(_short_policy(imp['policy_in']), ensure_ascii=False)}")
    out.append(f"  │   verdict: {_PV[imp['policy_verdict']]}")
    rb = _reasons_block(imp)
    if rb:
        out.append(rb)
    gate_stop = "STOP — не декомпозируется, уходит на исправление" if not imp["proceeds_to_decompose"] else "проходит дальше"
    out.append(f"  └─ leaf_check   ← modules/tasks/loc/decisions … verdict: {imp['leaf_verdict']}")
    out.append(f"  ⇒ {gate_stop}")
    out.append("```")

    # resolved pipeline
    out.append("### 2) После того как плагин заставил уточнить")
    out.append("```")
    out.append(f"L0': {sc.resolved.title}")
    out.append(f"  ├─ policy_gate  ← {json.dumps(_short_policy(res['policy_in']), ensure_ascii=False)}")
    out.append(f"  │   verdict: {_PV[res['policy_verdict']]}")
    out.append(f"  └─ leaf_check   verdict: {res['leaf_verdict']}")
    proceed = "✅ проходит в декомпозицию" if res["proceeds_to_decompose"] else "всё ещё не проходит"
    out.append(f"  ⇒ {proceed}")
    out.append("```")

    # template comparison
    out.append("### 3) Критическая сверка с шаблоном (что гейт ОБЯЗАН поймать)")
    out.append("| Требование шаблона | Поймал плагин? |")
    out.append("|---|---|")
    caught_text = " ".join(imp["policy_blocks"] + imp["policy_clarifications"]).lower()
    for req in sc.template_must_catch:
        hit = _matches(req, caught_text, imp)
        out.append(f"| {req} | {'✅ да' if hit else '❌ нет'} |")
    out.append("")
    return "\n".join(out)


def _short_policy(p: dict) -> dict:
    return {
        "measurable": p.get("measurable_target", False),
        "spend$": p.get("spend_per_action_usd", 0),
        "human": p.get("human_in_loop", False),
        "outreach": p.get("involves_outreach", False),
        "consent": p.get("consent_obtained", False),
        "legal": p.get("legal_exposure", False),
        "reviewed": p.get("legality_reviewed", False),
    }


_KEYWORDS = {
    "measurable": ("measurable", "target"),
    "spend": ("spend", "cap"),
    "outreach": ("outreach", "consent"),
    "legal": ("legal", "jurisdiction", "tos", "terms"),
}


def _matches(req: str, caught_text: str, out: dict) -> bool:
    r = req.lower()
    if "measur" in r or "target" in r or "roi" in r or "sharpe" in r:
        return any(k in caught_text for k in _KEYWORDS["measurable"])
    if "spend" in r:
        return any(k in caught_text for k in _KEYWORDS["spend"])
    if "outreach" in r or "consent" in r:
        return any(k in caught_text for k in _KEYWORDS["outreach"])
    if "legal" in r or "jurisdiction" in r or "tos" in r or "term" in r or "eligib" in r:
        return any(k in caught_text for k in _KEYWORDS["legal"])
    return False


def render_report(tools: Any) -> str:
    scenarios = all_scenarios()
    out = ["# spec-flow — отчёт по бизнес-сценариям (ловля неточностей)", ""]
    out.append("Три проекта поданы с **намеренно размытой** постановкой. Проверяем, что "
               "**плагин** (`policy_gate` + `leaf_check`), а не человек, выявляет неточность "
               "и не даёт декомпозировать спорный спек до его исправления.\n")
    out.append("Поток: `policy_gate` (конституция: измеримая цель / cap трат / consent / "
               "легальность) → `leaf_check` (структура). `block`/`clarify` = спек не "
               "декомпозируется, уходит на доработку.\n")
    for sc in scenarios:
        out.append(render_scenario(tools, sc))
    return "\n".join(out) + "\n"
