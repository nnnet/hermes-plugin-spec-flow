"""Battle tests — research/revision lane over a project timeline.

Each timeline is a sequence of board events (ticks, level-returns, accumulating
completed-task and test-error counters). We replay it through the REAL
``research_trigger_check`` and assert the lane fires exactly at the expected
points, including cooldown suppression and that cron requires configuration.
"""

from __future__ import annotations

import json

import pytest

# (reason, completed_total, errors_total, expect_trigger, note)
DATA_PIPELINE_TIMELINE = [
    ("tick",            3,  0, False, "below all thresholds"),
    ("on_level_return", 4,  0, True,  "return up a level fires the lane"),
    ("tick",            6,  0, False, "within cooldown of last fire"),
    ("tick",            10, 12, True, "test errors crossed m_test_errors"),
    ("tick",            12, 12, False, "cooldown again"),
    ("tick",            31, 12, True, "every_n_tasks crossed since last fire"),
    ("cron",            31, 12, False, "cron not configured -> no fire"),
]

URL_SHORTENER_TIMELINE = [
    ("tick",            5,  0, False, "quiet"),
    ("on_level_return", 6,  0, True,  "level return"),
    ("tick",            9,  0, False, "cooldown"),
    ("manual",          20, 0, True,  "manual revise always allowed (post-cooldown)"),
]

TIMELINES = {
    "data-pipeline": DATA_PIPELINE_TIMELINE,
    "url-shortener": URL_SHORTENER_TIMELINE,
}


@pytest.mark.parametrize("name", list(TIMELINES), ids=list(TIMELINES))
def test_research_timeline(plugin, name):
    timeline = TIMELINES[name]
    transcript = []
    for reason, completed, errors, expect, note in timeline:
        out = json.loads(plugin.tools._handle_research_trigger_check(
            {"reason": reason, "completed_tasks": completed, "test_errors": errors}
        ))
        transcript.append((reason, completed, errors, out["trigger"], expect, note))
        assert out["trigger"] == expect, (
            f"\n{name} timeline step {reason}@{completed}/{errors} expected "
            f"trigger={expect} got {out['trigger']} ({note})\n"
            f"fired_by={out['fired_by']} on_cooldown={out['on_cooldown']} "
            f"deltas={out['deltas']}"
        )


def test_manual_respects_cooldown(plugin):
    # manual fires, then a manual immediately after is suppressed by cooldown
    a = json.loads(plugin.tools._handle_research_trigger_check(
        {"reason": "manual", "completed_tasks": 30, "test_errors": 0}))
    assert a["trigger"] is True
    b = json.loads(plugin.tools._handle_research_trigger_check(
        {"reason": "manual", "completed_tasks": 31, "test_errors": 0}))
    assert b["trigger"] is False and b["on_cooldown"] is True
