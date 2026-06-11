"""Battle test — Kiro spec import as run context (roadmap B2).

AWS Kiro keeps a spec as requirements.md / design.md / tasks.md. spec-flow loads
them as CONTEXT (EARS acceptance criteria, design sections, the numbered task
plan with requirement refs) without re-authoring the spec. Pure parsers,
verified WITHOUT Hermes.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

REQUIREMENTS_MD = """# Requirements

## Requirement 1: Account creation
**User Story:** As a buyer, I want to register, so that I can place orders.

#### Acceptance Criteria
1. WHEN a new email is submitted THEN the system SHALL create an account.
2. WHEN the email already exists THEN the system SHALL reject with 409.

## Requirement 2: Login
**User Story:** As a user, I want to log in, so that I can resume work.

#### Acceptance Criteria
1. WHEN valid credentials are given THEN the system SHALL issue a session token.
"""

DESIGN_MD = """# Design

## Overview
A relational store with an outbox.

## Data Model
accounts(id, email, created_at)

## Security
Tokens are short-lived.
"""

TASKS_MD = """# Implementation Plan

- [x] 1. Set up project skeleton
- [ ] 2. Account model and store
  - [ ] 2.1 Define accounts table
    _Requirements: 1.1, 1.2_
  - [ ] 2.2 Registration endpoint
    _Requirements: 1.1_
- [ ] 3. Session login
    _Requirements: 2.1_
"""


@pytest.fixture
def kiro_dir(tmp_path):
    d = tmp_path / ".kiro" / "specs" / "accounts"
    d.mkdir(parents=True)
    (d / "requirements.md").write_text(REQUIREMENTS_MD, encoding="utf-8")
    (d / "design.md").write_text(DESIGN_MD, encoding="utf-8")
    (d / "tasks.md").write_text(TASKS_MD, encoding="utf-8")
    return d


# ─── parsers ──────────────────────────────────────────────────────────


def test_requirements_with_ears_criteria(plugin):
    reqs = plugin.tools.parse_kiro_requirements(REQUIREMENTS_MD)
    assert [r["id"] for r in reqs] == ["1", "2"]
    assert reqs[0]["story"].startswith("As a buyer")
    # both SHALL clauses captured as testable criteria
    assert len(reqs[0]["criteria"]) == 2
    assert all("SHALL" in c for c in reqs[0]["criteria"])


def test_tasks_with_numbering_and_refs(plugin):
    tasks = plugin.tools.parse_kiro_tasks(TASKS_MD)
    by = {t["id"]: t for t in tasks}
    assert by["1"]["done"] is True
    assert by["2.1"]["level"] == 1 and by["1"]["level"] == 0
    assert by["2.1"]["requirements"] == ["1.1", "1.2"]
    assert by["3"]["requirements"] == ["2.1"]


def test_criteria_classified_as_ears(plugin):
    reqs = plugin.tools.parse_kiro_requirements(REQUIREMENTS_MD)
    # all fixture criteria are WHEN...SHALL -> event-driven EARS
    assert reqs[0]["ears"] == ["event", "event"]
    assert reqs[1]["ears"] == ["event"]


def test_classify_ears_patterns(plugin):
    c = plugin.tools.classify_ears
    assert c("WHEN a user clicks THEN the system SHALL save") == "event"
    assert c("WHILE charging the system SHALL show progress") == "state"
    assert c("WHERE GPS is fitted the system SHALL log position") == "optional"
    assert c("IF the battery is low THEN the system SHALL alert") == "unwanted"
    assert c("The system SHALL respond within 500ms") == "ubiquitous"
    assert c("It would be nice to have dark mode") == "non-ears"


def test_design_sections(plugin):
    design = plugin.tools.parse_kiro_design(DESIGN_MD)
    assert "Overview" in design and "Security" in design
    assert "outbox" in design["Overview"]


# ─── loader + tool ────────────────────────────────────────────────────


def test_load_kiro_spec_bundle(plugin, kiro_dir):
    bundle = plugin.tools.load_kiro_spec(str(kiro_dir))
    assert bundle["feature"] == "accounts"
    assert len(bundle["requirements"]) == 2
    assert len(bundle["tasks"]) == 5
    assert "Data Model" in bundle["design"]


def test_kiro_import_tool(plugin, kiro_dir):
    out = json.loads(plugin.tools._handle_kiro_import({"dir": str(kiro_dir)}))
    assert out["summary"]["requirements"] == 2
    assert out["summary"]["criteria"] == 3
    assert out["summary"]["tasks"] == 5
    assert out["summary"]["design_sections"] == 3


def test_kiro_import_requires_dir(plugin):
    out = json.loads(plugin.tools._handle_kiro_import({}))
    assert "error" in out


def test_kiro_import_rejects_empty_dir(plugin, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    out = json.loads(plugin.tools._handle_kiro_import({"dir": str(empty)}))
    assert "error" in out


def test_tool_registered(plugin):
    assert "kiro_import" in plugin.reg.tools
