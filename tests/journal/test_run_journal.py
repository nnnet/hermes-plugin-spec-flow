"""Wave journal — single writer, atomic waves, lock-free readers.

Parallelization stage 0: the journal lands BEFORE the engine uses it,
fully covered offline."""
import json
import pathlib
import subprocess
import sys
import textwrap

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from spec_flow_journal import JournalLocked, RunJournal   # noqa: E402


def test_commit_and_read_roundtrip(tmp_path):
    path = tmp_path / "journal.jsonl"
    with RunJournal(path).open() as j:
        w1 = j.commit_wave([{"kind": "node_done", "node": "a"}])
        w2 = j.commit_wave([{"kind": "requirement", "name": "web_ui"},
                            {"kind": "node_done", "node": "b"}])
    assert (w1, w2) == (1, 2)
    waves = list(RunJournal(path).waves())
    assert [w["wave"] for w in waves] == [1, 2]
    assert waves[1]["entries"][0]["name"] == "web_ui"


def test_sequence_survives_reopen(tmp_path):
    path = tmp_path / "journal.jsonl"
    with RunJournal(path).open() as j:
        j.commit_wave([{"kind": "x"}])
    with RunJournal(path).open() as j:
        assert j.commit_wave([{"kind": "y"}]) == 2


def test_second_writer_refused(tmp_path):
    path = tmp_path / "journal.jsonl"
    first = RunJournal(path).open()
    try:
        # a REAL second writer is another process — flock is per-process,
        # so the contention must be proven across a process boundary
        code = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(pathlib.Path(__file__).resolve().parents[2])!r})
            from spec_flow_journal import JournalLocked, RunJournal
            try:
                RunJournal({str(path)!r}).open()
            except JournalLocked:
                sys.exit(42)
            sys.exit(0)
        """)
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 42, proc.stderr
    finally:
        first.close()


def test_torn_tail_skipped(tmp_path):
    path = tmp_path / "journal.jsonl"
    with RunJournal(path).open() as j:
        j.commit_wave([{"kind": "ok"}])
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"wave": 2, "entries": [{"kind": "torn"')   # no newline
    waves = list(RunJournal(path).waves())
    assert [w["wave"] for w in waves] == [1]
    # the writer recovers from the COMMITTED state: the torn tail is CUT on
    # open, so the next wave is readable (not glued to the torn fragment)
    with RunJournal(path).open() as j:
        assert j.commit_wave([{"kind": "next"}]) == 2
    waves = list(RunJournal(path).waves())
    assert [w["wave"] for w in waves] == [1, 2]
    assert waves[1]["entries"][0]["kind"] == "next"


def test_since_and_kind_filters(tmp_path):
    path = tmp_path / "journal.jsonl"
    with RunJournal(path).open() as j:
        j.commit_wave([{"kind": "node_done", "node": "a"}])
        j.commit_wave([{"kind": "requirement", "name": "web_ui"}])
        j.commit_wave([{"kind": "node_done", "node": "b"}])
    late = list(RunJournal(path).entries(kind="node_done", since=1))
    assert [(e["node"], e["wave"]) for e in late] == [("b", 3)]


def test_write_without_open_refused(tmp_path):
    j = RunJournal(tmp_path / "journal.jsonl")
    with pytest.raises(RuntimeError, match="not open"):
        j.commit_wave([{"kind": "x"}])


def test_torn_tail_overwritten_consistently(tmp_path):
    # after recovery the file must stay readable end-to-end
    path = tmp_path / "journal.jsonl"
    with RunJournal(path).open() as j:
        j.commit_wave([{"kind": "a"}])
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("garbage-not-json\n")
    # readers stop at the corrupt line instead of guessing
    assert [w["wave"] for w in RunJournal(path).waves()] == [1]
