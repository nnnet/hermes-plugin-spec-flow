"""Battle test — spec-kit tasks.md → kanban import (roadmap B1).

spec-flow consumes an upstream spec-kit artifact directly: a phased markdown
checklist of atomic tasks. The parser is pure plugin code and is verified
WITHOUT Hermes; the import handler degrades gracefully when the `hermes kanban`
binary is absent (it still parses every card and reports the seed attempts).
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# a representative spec-kit tasks.md (abridged from the github/spec-kit format)
TASKS_MD = """# Tasks: Photo Album Service

## Phase 3.1: Setup
- [x] T001 Create project structure per implementation plan
- [ ] T002 [P] Initialize Python project with FastAPI deps in backend/pyproject.toml

## Phase 3.2: Tests First (TDD)
- [ ] T004 [P] Contract test POST /api/albums in tests/contract/test_albums_post.py
- [ ] T005 [P] Contract test GET /api/albums in tests/contract/test_albums_get.py

## Phase 3.3: Core Implementation
- [ ] T008 [P] Album model in src/models/album.py
- [ ] T009 Albums service in src/services/album_service.py

## Dependencies
- (T004-T005) before (T008-T009)
- T008 blocks T009
"""


def _parser(plugin):
    return plugin.tools.parse_speckit_tasks


# ─── pure parser ──────────────────────────────────────────────────────


def test_parses_all_tasks(plugin):
    cards = _parser(plugin)(TASKS_MD)
    ids = [c["id"] for c in cards]
    assert ids == ["T001", "T002", "T004", "T005", "T008", "T009"]


def test_parallel_marker_and_done_flag(plugin):
    cards = {c["id"]: c for c in _parser(plugin)(TASKS_MD)}
    assert cards["T001"]["done"] is True
    assert cards["T002"]["done"] is False
    assert cards["T002"]["parallel"] is True
    assert cards["T009"]["parallel"] is False
    # the [P] marker is stripped from the title
    assert not cards["T002"]["title"].startswith("[P]")


def test_phase_grouping(plugin):
    cards = {c["id"]: c for c in _parser(plugin)(TASKS_MD)}
    assert cards["T001"]["phase"].startswith("Phase 3.1")
    assert cards["T004"]["phase"].startswith("Phase 3.2")
    assert cards["T008"]["phase"].startswith("Phase 3.3")


def test_file_paths_extracted(plugin):
    cards = {c["id"]: c for c in _parser(plugin)(TASKS_MD)}
    assert "src/models/album.py" in cards["T008"]["files"]
    assert "tests/contract/test_albums_post.py" in cards["T004"]["files"]


def test_dependencies_resolved(plugin):
    cards = {c["id"]: c for c in _parser(plugin)(TASKS_MD)}
    # 'T008 blocks T009' -> T009 depends on T008
    assert "T008" in cards["T009"]["deps"]
    # '(T004-T005) before (T008-T009)' -> T008,T009 depend on T004,T005
    assert set(cards["T008"]["deps"]) >= {"T004", "T005"}
    assert set(cards["T009"]["deps"]) >= {"T004", "T005", "T008"}
    # setup tasks have no prereqs
    assert cards["T001"]["deps"] == []


# ─── import handler (graceful without Hermes) ─────────────────────────


def test_import_parse_only(plugin):
    out = json.loads(plugin.tools._handle_speckit_import(
        {"project": "photo", "tasks": TASKS_MD, "seed": False}))
    assert out["parsed"] == 6
    assert out["parallel"] == 4          # T002,T004,T005,T008
    assert out["with_deps"] == 2         # T008,T009
    assert out["seeded"] == []
    assert any(p.startswith("Phase 3.2") for p in out["phases"])


def test_import_seeds_every_card_without_hermes(plugin):
    out = json.loads(plugin.tools._handle_speckit_import(
        {"project": "photo", "tasks": TASKS_MD}))
    # one seed attempt per parsed card, even though hermes is absent
    assert len(out["seeded"]) == out["parsed"] == 6
    assert {s["id"] for s in out["seeded"]} == {"T001", "T002", "T004", "T005", "T008", "T009"}


def test_import_reads_file(plugin, tmp_path):
    p = tmp_path / "tasks.md"
    p.write_text(TASKS_MD, encoding="utf-8")
    out = json.loads(plugin.tools._handle_speckit_import(
        {"project": "photo", "tasks_md": str(p), "seed": False}))
    assert out["parsed"] == 6


def test_import_requires_project(plugin):
    out = json.loads(plugin.tools._handle_speckit_import({"tasks": TASKS_MD}))
    assert "error" in out


def test_import_rejects_non_speckit(plugin):
    out = json.loads(plugin.tools._handle_speckit_import(
        {"project": "x", "tasks": "# just a heading\n\nno tasks here", "seed": False}))
    assert "error" in out


def test_tool_registered(plugin):
    assert "speckit_import" in plugin.reg.tools
