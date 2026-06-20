# Running spec-flow cases

The one entry point is `tests/lib/run_cases.py` — it runs a scenario
(`tests/scenarios/*.yaml`) as a REAL production run of the plugin and writes a
numbered run dir under `tests/runs-out/<ts>__vNNN__<case>/`.

Run from the `tests/` directory.

## Fresh run

```bash
python3 lib/run_cases.py --case p6 --depth product --workers real --decomposer llm
```

- `--case <substr>` — which scenario (substring of the file stem, e.g. `p6`).
- `--depth` — how far to take it: `spec` → `scaffold` → `verify` → `execute` →
  `product`. `execute` builds the leaves; **`product`** also assembles the app
  and runs the **boot-gate** (the web really comes up and every endpoint is
  curled).
- `--workers real` — real implementer (not the simulator).
- `--decomposer llm` — live decomposer (builds the tree from the goal).
- `--checkpoint-every N` — snapshot the workspace every N node boundaries
  (replayable points). Recommended for long runs.
- `--dashboard` (+ `--dashboard-port 8092`) — live view; or run the dashboard
  separately (see below).

Each run dir holds: `trace.jsonl` (event stream), `llm-log.jsonl` (every model
call), `workspace/` (the built product), `checkpoints/`, `SUMMARY.md`.

## Resume a stopped / crashed run

```bash
python3 lib/run_cases.py --resume runs-out/<ts>__vNNN__<case>   # continue in place
```

Committed leaves are skipped (cache-hits); only unfinished ones run.

## Clone a past run from a checkpoint into a NEW run  ← the fast iterate loop

Use this to retry/continue from an earlier run WITHOUT touching it: it copies
run N's checkpoint M into a fresh numbered run and resumes there.

```bash
# by NUMBERS (operator-friendly): clone run 28's checkpoint 3 into a new vNNN
python3 lib/run_cases.py --from-run 28 --from-checkpoint 3 \
        --case p6 --depth product --workers real --decomposer llm

# --from-checkpoint omitted -> the run's LAST checkpoint
python3 lib/run_cases.py --from-run 28 --case p6 --depth product --workers real
```

- `--from-run N` — the run NUMBER (the `vNNN` you see in run dirs / the operator
  sequence). The source run is left untouched; a fresh `vNNN+1` dir is minted.
- `--from-checkpoint M` — the checkpoint NUMBER (the `NNN` in
  `checkpoints/NNN__*`). List them with
  `python3 lib/run_cases.py --list-checkpoints runs-out/<run dir>`.
- Equivalent low-level form: `--from <checkpoints/NNN__hash dir>`.

On resume each cached leaf is re-validated against the CURRENT deterministic
gates: a leaf that passed when journaled but now fails a tightened plugin gate
is a cache MISS and is re-implemented — only it, not the whole tree. So the loop
is: **improve a plugin gate → clone the checkpoint → only the now-failing leaf
re-runs → boot-gate verifies.**

Caveat: the decomposer is re-run on resume (it is non-deterministic), so a
different tree can yield different node ids and miss leaf cache-hits; leaf reuse
is reliable when the re-decomposition reproduces the same node ids.

## Live control of a running run

```bash
python3 lib/run_cases.py --checkpoint runs-out/<run dir>   # snapshot at next node boundary, keep running
python3 lib/run_cases.py --stop       runs-out/<run dir>   # ask it to stop at the next node boundary
python3 lib/run_cases.py --list-checkpoints runs-out/<run dir>
```

## Dashboard

```bash
python3 lib/live_dashboard.py --port 8092 --run-dir runs-out/<run dir>
# then open http://localhost:8092/  (auto-refreshes)
```

Flow tab: nodes are lanes over time; checkpoints are ★ on the time axis with a
dashed line across the chart (not a lane). Tree: engine machinery (checkpoint,
gate, assembly) badges ⚙️; a late human requirement badges 📌.
