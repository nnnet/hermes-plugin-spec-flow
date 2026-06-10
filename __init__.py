"""spec-flow — Spec-Driven Development on a Hermes kanban board.

Entry point: load ``spec_flow_tools`` (which self-registers handlers via
``tools.registry``) and extend ``TOOLSETS['kanban']['tools']`` so the six
spec-flow tool names surface for the kanban orchestrator toolset. The pattern
mirrors ``chief-tools`` so future upstream merges of ``toolsets.py`` stay
conflict-free.

The deterministic gates (``leaf_check``, ``contract_check``,
``research_trigger_check``) and seed commands (``specflow_init`` /
``specflow_start`` / ``specflow_status``) live in ``spec_flow_tools``. The
reasoning lives in the bundled skills under ``skills/`` — copy them to
``$HERMES_HOME/skills`` (see ``profiles/setup-profiles.sh``).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


_TOOL_NAMES = (
    "leaf_check",
    "contract_check",
    "research_trigger_check",
    "policy_gate",
    "run_report",
    "specflow_init",
    "specflow_start",
    "specflow_status",
)


def register(ctx: Any) -> None:
    """Load the tools module (side-effect registration) and surface the tool
    names in the kanban toolset.
    """
    try:
        from . import spec_flow_tools  # noqa: F401 — module import is the side-effect
    except Exception as exc:  # noqa: BLE001
        logger.error("spec-flow: failed to load spec_flow_tools module: %s", exc)
        return

    try:
        import toolsets

        kanban_ts = toolsets.TOOLSETS.get("kanban") or {}
        kanban_tools = kanban_ts.get("tools")
        if isinstance(kanban_tools, list):
            added = 0
            for name in _TOOL_NAMES:
                if name not in kanban_tools:
                    kanban_tools.append(name)
                    added += 1
            logger.info(
                "spec-flow: registered %d tools (%d added to TOOLSETS['kanban'])",
                len(_TOOL_NAMES), added,
            )
        else:
            logger.warning(
                "spec-flow: TOOLSETS['kanban']['tools'] not a list — "
                "tools registered but won't surface in kanban toolset"
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "spec-flow: tools registered but TOOLSETS['kanban'] extension "
            "failed (%s)", exc,
        )
