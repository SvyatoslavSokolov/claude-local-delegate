# Writing a task for a delegated agent

`SUPERVISION.md` covers what to do once an agent is running. This covers the
part that decides whether it succeeds at all: **the task text**.

Every failure below is real, observed over a two-day refactoring run against a
large Isaac Lab / ROS 2 codebase. In each case the delegate did exactly what it
was told — the defect was in the briefing.

## TL;DR

| Rule | Because |
|---|---|
| Consumer discovery is a **precondition**, never a verify step | it applied, then found it broke a caller |
| `py_compile` never proves an import move | a file with deleted imports still compiles |
| Give a float **oracle + a wrong-direction control**, not a tolerance | `1e-6` is ordinary GPU rounding |
| One file per task, with the anchors you found yourself | 900-line files kill runs at 200–500k tokens |
| Hand over a finished sibling as a template | removes the design uncertainty entirely |
| Ask explicitly "what could you NOT verify" | otherwise "all green" reads as "it works" |
| Name the exact environment for every command | a wrong-env test run is a fake baseline |

---

## 1. Consumer discovery goes first, not last

A task to package a flat script directory listed "grep for callers of the old
path" as verification item 7. The agent did items 1–6 (the edit), reached item 7,
found three live callers of `scripts/mimic/run_pickplace_xr.py`, and reported —
correctly and honestly — that it had broken the VR data-collection path. The
change had to be reverted.

**Write it as a gate, at the top:**

> Before editing anything: `grep -rn "<old path or symbol>" <perimeter>`. Classify
> every hit as (a) a definition, (b) a re-export, (c) a **live caller**, (d) docs.
> **If any hit is (c), STOP and report it — do not edit.**

The same shape applies to deleting anything. "Is it dead?" is a question to answer
before the deletion, with the answer written out, not after.

## 2. `py_compile` does not verify an import move

An agent was asked to move `import rclpy` into a conditional branch. It deleted the
module-level imports, died on an API error before adding the branch, and left a file
that **compiled cleanly** while every use site was an undefined name. `py_compile`
checks syntax; it does not resolve names.

**For any task that moves, defers or removes an import, demand name resolution:**

> For each name you moved (`X`, `Y`, `Z`): grep every use site and show it is inside
> a scope where that name is bound. Paste the grep output and reason per name.
> `py_compile` is necessary but NOT sufficient — a file with deleted imports still compiles.

A related trap: a symbol moved into a function is a latent `NameError` if it is used
in a *different* method. Ask the agent to read the whole class, not just the import.
(A good pattern the model found on its own: bind the imported names to instance
attributes in `__init__` and reference `self._x` elsewhere.)

## 3. For numeric refactors, give an oracle and a positive control

A migration task said: "stop if the difference exceeds `1e-6`". On CUDA float32 the
correct edit differed by ~`1e-6` — because the old code materialised `M.T.contiguous()`
and the new one passed a transposed view, so a different cuBLAS kernel and FMA order.
The rule would have halted a bit-correct change.

The agent repaired the method itself and it is worth stealing:

> Compare in **float64** — old vs new must be exactly `0.000e+00`.
> Also print a **deliberately wrong-direction row** (e.g. transpose the matrix the
> other way) so the numbers are interpretable: a real error is O(1), rounding is ~1e-6.

Ask for the control row explicitly. A difference with nothing to compare it against
is not evidence.

## 4. One file per task, and find the anchors yourself first

A task spanning two run files (994 + 542 lines) failed **four times**: agents burned
200–500k tokens reading and re-summarising, two died on the 16 384-token output cap,
one left the tree broken. Splitting it by file and pasting the exact line numbers made
the smaller half succeed on the first try.

**In the task text:**

- give the anchors as `:LINE` with the code, found by you before delegating;
- say "re-check them before editing, they shift as you edit";
- say **"read only around the anchors — do not read or summarise the whole file"**;
- say **"keep the final report short; no large excerpts"** (the output cap is real);
- add: "if you cannot finish, revert your partial changes (`git checkout -- <file>`)
  rather than leaving the file half-edited".

## 5. Hand over a finished sibling as a template

The second of two symmetrical files only became tractable after the first landed and
its exact shape could be pasted into the brief — branch layout, the `x = None`
sentinel for the else-branch, the guarded teardown. Design uncertainty is what the
agent spends its budget on; a worked example deletes it.

When two tasks are symmetrical, run one, verify it, then brief the second **with the
first as the template**. Do not run them in parallel.

## 6. The honest negative — already in the persona, reinforce it when it matters

`~/.claude/agents/local-worker.md` (the default persona every delegation runs with)
already requires evidence, assumptions and "say what you could not verify". That is
why reports come back volunteering that a numeric harness proves algebra and not
runtime, or that a count came from an environment the agent could not reproduce.

So do not re-state it in every task. Do re-state it — pointedly — when the result
will drive a decision and the gap is specific:

> The static checks and unit tests do NOT prove control behaviour. Say so plainly;
> do not imply the swap is behaviourally verified when only algebra was checked.

Naming *which* gap you expect is what makes the answer useful; a generic reminder is
already covered by the persona.

## 7. Pin the environment in every command

Test counts that shift between runs are usually the environment, not the code. In this
repo a suite gave `152 / 121 / 187 passed` on three runs until the real cause was found:
one ROS overlay was sometimes sourced and sometimes not, and without it a module-level
import aborted collection entirely.

**Put the exact command in the task, with its setup**, and give the expected numbers:

> Run exactly this — any other invocation aborts at collection and gives meaningless
> counts: `docker exec <c> bash -lc 'source /opt/ros/jazzy/setup.bash && source
> /workspace/isaac_sim_ws/install/local_setup.bash && cd <dir> && python -m pytest tests -q'`
> Baseline is **187 passed, 5 skipped, 0 failed**. Any NEW failure is a blocker — name it and STOP.

Likewise name the right interpreter. `docker exec … python3` and
`docker exec … /opt/isaaclab-venv/bin/python` are different worlds; an agent that
probes with the wrong one reports a confident false "clean".

## 8. Scope by exclusion, not just inclusion

"Edit only X" is not enough when other agents run concurrently or the user is editing.
List the forbidden paths explicitly, and say why:

> Never touch `source/vhal_cli` (the owner is rewriting it), `ros2/ros_bridge.py`
> (another agent is mid-edit), or any reference directory under `external/`.

Also tell the agent what dirt it will find that is **not** its own — otherwise it
wastes turns investigating a pre-existing modified file, or worse, "cleans" it.

## 9. Make a green result falsifiable

For a test-fixing task, require proof the test can still fail:

> In a scratch copy under `/tmp` (never in the repo), break the behaviour the test
> asserts and show it goes red; then delete the scratch.

A suite made green by weakening assertions is worse than a red one. Ban the shortcuts
by name: no `assert True`, no deleted checks, no widened tolerances, no `skip` added
to dodge a failure.

## A task template that works

```
TASK: <one sentence>. <Why it matters, one sentence.>

SCOPE: edit ONLY <paths>. Never touch <forbidden paths + reason>.

### Established facts — rely on them, do not re-derive
<what you already verified, with file:line — this is the budget you save>

### Preconditions — check FIRST, stop if violated
<consumer discovery / dead-code proof / anything whose failure means "do not edit">

### The change
<anchors as :LINE with code; the template from a sibling task if one exists>

### VERIFY — exactly these N checks
<name-resolution check, not just py_compile; the exact env-pinned commands;
 the expected numbers; a falsifiability proof if it is a test>

Keep the final report short — no large excerpts (16k output cap).
State plainly what you could NOT verify. Do NOT commit.
```
