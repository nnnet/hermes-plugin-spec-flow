"""Doctor config loader: the cause/remedy table, the LLM power tiers, the
evaluator knobs and the human-escalation policy are all DATA and live in
``spec_flow_doctor.defaults.yaml`` (not in this code). This module only loads
that file and merges it across five layers so a run can be tuned without
touching code.

Layering (lowest -> highest priority), assembled by the *_config loaders here:
  1. spec_flow_doctor.defaults.yaml     — factory table, run works with zero config
  2. env floors                         — numeric SPEC_FLOW_DOCTOR_* (read by caller)
  3. env JSON override                  — SPEC_FLOW_DOCTOR_CAUSES / _EVALUATOR / _TIERS
  4. case-YAML                          — project["doctor"|"causes"|"evaluator"], workers["tiers"]
  5. run/launch overrides               — dict passed by the runner (CLI --doctor-set)

Point the loader at a different defaults file with the env var
``SPEC_FLOW_DOCTOR_DEFAULTS`` (path relative to cwd or absolute). Nothing here
calls an LLM or touches the engine; it is pure config so it unit tests offline.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
from typing import Any, Optional

import yaml

# --- factory defaults: load the data table from YAML (layer 1) ----------------
_DEFAULTS_FILENAME = "spec_flow_doctor.defaults.yaml"


def _defaults_path() -> pathlib.Path:
    """Path to the factory defaults file: env override, else next to this module
    (never a hardcoded absolute path)."""
    override = os.environ.get("SPEC_FLOW_DOCTOR_DEFAULTS", "").strip()
    if override:
        return pathlib.Path(override).expanduser()
    return pathlib.Path(__file__).resolve().parent / _DEFAULTS_FILENAME


def _load_defaults() -> dict:
    """Read the factory defaults YAML once at import. The doctor block borrows
    causes_order and human from the top-level keys so they stay single-source."""
    with open(_defaults_path(), "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    doctor = data.setdefault("doctor", {})
    doctor.setdefault("causes_order", data.get("causes_order", []))
    doctor.setdefault("human", data.get("human", {}))
    return data


_DEFAULTS: dict = _load_defaults()

# Backward-compatible module names — same shapes as before, now sourced from the
# YAML file. Treat them as read-only factory tables (the *_config loaders copy).
DEFAULT_TIERS: dict = _DEFAULTS.get("tiers", {})
DEFAULT_EVALUATOR: dict = _DEFAULTS.get("evaluator", {})
DEFAULT_HUMAN: dict = _DEFAULTS.get("human", {})
DEFAULT_CAUSES: dict = _DEFAULTS.get("causes", {})
DEFAULT_CAUSES_ORDER: list = _DEFAULTS.get("causes_order", [])
DEFAULT_DOCTOR: dict = _DEFAULTS.get("doctor", {})
_DEFAULT_COMPLEXITY: dict = _DEFAULTS.get("complexity_to_tier", {})


# --- small accessors for data the other doctor modules used to hardcode -------
def levels_order() -> list:
    """Depth-level ladder leaf->parent->respec->human (was a literal in doctor.py)."""
    return list(_DEFAULTS.get("levels_order", ["leaf", "parent", "respec", "human"]))


def escalation_kinds() -> dict:
    """Per-level escalation kind emitted on level change (was a literal map)."""
    return dict(_DEFAULTS.get("escalation_kinds", {}))


def semantic_causes() -> list:
    """Causes the LLM classifier may pick = those whose detector is 'prompt'.
    Derived from the causes table so the two never drift (was a literal list in
    spec_flow_diagnosers.py)."""
    explicit = _DEFAULTS.get("semantic_causes")
    if explicit:
        return list(explicit)
    return [cid for cid, spec in DEFAULT_CAUSES.items()
            if (spec or {}).get("detector") == "prompt"]


# Layer 5: launch-time overrides (CLI --doctor-set / --doctor-enabled), applied
# LAST so an operator's per-run knob beats the case YAML. Set once in the runner
# entrypoint via set_overrides(); the loaders merge it on top of everything.
_OVERRIDES: dict = {}


def set_overrides(over: Optional[dict]) -> None:
    """Install launch-time doctor overrides (highest priority). Keys:
    'doctor' | 'causes' | 'evaluator' | 'tiers' | 'complexity_to_tier'."""
    global _OVERRIDES
    _OVERRIDES = dict(over or {})


def _deep_merge(base: dict, over: Optional[dict]) -> dict:
    """Recursively merge ``over`` into ``base`` (mutates and returns base)."""
    if not over:
        return base
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _env_json(name: str) -> dict:
    """Parse a SPEC_FLOW_* env var holding a JSON object, or {} if absent/bad."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except (ValueError, TypeError):
        return {}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (ValueError, TypeError):
        return default


def doctor_config(project: Optional[dict] = None,
                  overrides: Optional[dict] = None) -> dict:
    """Assemble the doctor block across all five layers."""
    cfg = copy.deepcopy(DEFAULT_DOCTOR)
    # layer 2: numeric floors from env
    cfg["ring"] = _env_int("SPEC_FLOW_DOCTOR_RING", cfg.get("ring", 6))
    cfg["node_attempts"] = _env_int("SPEC_FLOW_DOCTOR_NODE_ATTEMPTS", cfg.get("node_attempts", 6))
    cfg["nonshrink"] = _env_int("SPEC_FLOW_DOCTOR_NONSHRINK", cfg.get("nonshrink", 2))
    cfg["levels"] = _env_int("SPEC_FLOW_DOCTOR_LEVELS", cfg.get("levels", 4))
    cfg["attempts_per_level"] = _env_int(
        "SPEC_FLOW_DOCTOR_ATTEMPTS_PER_LEVEL", cfg.get("attempts_per_level", 2))
    # layer 3: env JSON
    _deep_merge(cfg, _env_json("SPEC_FLOW_DOCTOR"))
    # layer 4: case YAML
    _deep_merge(cfg, (project or {}).get("doctor"))
    # layer 5: launch overrides (explicit arg, then the global CLI overrides)
    _deep_merge(cfg, (overrides or {}).get("doctor"))
    _deep_merge(cfg, _OVERRIDES.get("doctor"))
    return cfg


def causes_config(project: Optional[dict] = None,
                  overrides: Optional[dict] = None) -> dict:
    cfg = copy.deepcopy(DEFAULT_CAUSES)
    _deep_merge(cfg, _env_json("SPEC_FLOW_DOCTOR_CAUSES"))
    _deep_merge(cfg, (project or {}).get("causes"))
    _deep_merge(cfg, (overrides or {}).get("causes"))
    _deep_merge(cfg, _OVERRIDES.get("causes"))
    return cfg


def evaluator_config(project: Optional[dict] = None,
                     overrides: Optional[dict] = None) -> dict:
    cfg = copy.deepcopy(DEFAULT_EVALUATOR)
    _deep_merge(cfg, _env_json("SPEC_FLOW_DOCTOR_EVALUATOR"))
    _deep_merge(cfg, (project or {}).get("evaluator"))
    _deep_merge(cfg, (overrides or {}).get("evaluator"))
    _deep_merge(cfg, _OVERRIDES.get("evaluator"))
    return cfg


def tiers_config(project: Optional[dict] = None,
                 overrides: Optional[dict] = None) -> dict:
    cfg = copy.deepcopy(DEFAULT_TIERS)
    _deep_merge(cfg, _env_json("SPEC_FLOW_TIERS"))
    workers = (project or {}).get("workers") or {}
    _deep_merge(cfg, workers.get("tiers"))
    _deep_merge(cfg, (overrides or {}).get("tiers"))
    return cfg


def complexity_to_tier(project: Optional[dict] = None,
                       overrides: Optional[dict] = None) -> dict:
    """Map a node-complexity label (leaf_small/leaf_big/branch) to a tier name."""
    base = copy.deepcopy(_DEFAULT_COMPLEXITY)
    workers = (project or {}).get("workers") or {}
    _deep_merge(base, workers.get("complexity_to_tier"))
    _deep_merge(base, (overrides or {}).get("complexity_to_tier"))
    return base


def solo_for_labels(project: Optional[dict] = None,
                    overrides: Optional[dict] = None) -> list:
    """Node-complexity labels that run a SINGLE coder pass (TDD: generate +
    one repair) instead of the full architect->coder->tester->fixer orchestra
    — process tiering. A simple leaf does not earn four LLM calls; the orchestra
    is reserved for branches and large/undecided leaves. Decided purely by the
    node's structural label (never the model), so it stays deterministic and
    model-independent. Default: only ``leaf_small``. Overridable via
    ``workers.solo_for_labels`` in the case YAML or ``SPEC_FLOW_SOLO_LABELS``
    (JSON list) in the environment. An empty list restores today's
    always-orchestra behaviour."""
    workers = (project or {}).get("workers") or {}
    val = workers.get("solo_for_labels")
    raw = os.environ.get("SPEC_FLOW_SOLO_LABELS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                val = parsed
        except (ValueError, TypeError):
            pass
    if (overrides or {}).get("solo_for_labels") is not None:
        val = (overrides or {})["solo_for_labels"]
    if val is None:
        val = list(_DEFAULTS.get("solo_for_labels", ["leaf_small"]))
    return [str(x) for x in (val or [])]
