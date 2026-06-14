"""П2: the wave journal is wired into the engine — a real run writes one
``node_commit`` wave per committed node to ``<workspace>/.spec-flow/waves.jsonl``.
The trail is OPTIONAL (never gates work) but, when a workspace root exists,
it must record the leaves a resume could later skip."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402

# repo root holds spec_flow_journal.py
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import spec_flow_journal as journal  # noqa: E402

_BRANCH = {"modules": 3, "tasks": 12, "interfaces": 3, "estimated_loc": 900,
           "open_decisions": 0, "single_concern": False,
           "testable_criteria": True}
_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
         "open_decisions": 0, "single_concern": True,
         "testable_criteria": True}

_POLICY = {"measurable_target": True, "spend_per_action_usd": 1,
           "human_in_loop": False, "involves_outreach": False,
           "consent_obtained": True, "legal_exposure": False,
           "legality_reviewed": True}


def _two_leaf_decomposer(ctx):
    if ctx["depth"] == 0:
        return {"metrics": dict(_BRANCH), "children": [
            {"id": "alpha", "title": "Alpha module"},
            {"id": "beta", "title": "Beta module"}]}
    return {"metrics": dict(_LEAF)}


def _run(tmp_path):
    return eng.run_project(
        {"name": "journal-case", "goal": "g", "target": "x", "policy": _POLICY},
        workspace=str(tmp_path / "wk"), depth="scaffold",
        agents={"decomposer": _two_leaf_decomposer})


def test_run_writes_a_waves_journal(tmp_path):
    _run(tmp_path)
    wpath = tmp_path / "wk" / ".spec-flow" / "waves.jsonl"
    assert wpath.is_file(), "the engine must open and write the wave journal"
    waves = list(journal.RunJournal(wpath).waves())
    assert waves, "at least one wave must be committed"
    # wave sequence numbers are monotonic and start at 1
    seqs = [w["wave"] for w in waves]
    assert seqs == list(range(1, len(seqs) + 1))


def test_every_leaf_has_a_node_commit_wave(tmp_path):
    _run(tmp_path)
    wpath = tmp_path / "wk" / ".spec-flow" / "waves.jsonl"
    commits = list(journal.RunJournal(wpath).entries(kind="node_commit"))
    nodes = {e["node"] for e in commits}
    # both leaves committed; their commit carries the verdict + resume cursor
    assert {"alpha", "beta"} <= nodes
    leaf_commits = [e for e in commits if e["node"] in ("alpha", "beta")]
    assert all(e["verdict"] == "leaf" for e in leaf_commits)
    assert all("completed" in e and "version" in e for e in leaf_commits)


def test_journal_off_without_workspace_root(tmp_path):
    # an in-memory workspace (no root) must not crash — the trail just stays off
    e = eng.Engine(workspace=str(tmp_path / "wk"))
    e._journal_open()
    assert e._wave_journal is not None  # has a root → on
    e._journal_close()
    assert e._wave_journal is None
