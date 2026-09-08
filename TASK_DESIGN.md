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
| Run the capture mechanism before you brief it | `git stash` on clean files is a no-op that exits 0 |

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

### Verify from INSIDE the container, always

Not merely "pin the interpreter" — run the checks *in the container*. Delegates work on
the host, and the host is a different machine for this purpose:

- `/opt/isaaclab-venv` does not exist on the host, so every project package is missing.
- The host `python3` is 3.8; the project is 3.12. PEP 585 (`dict[str, str]`) and `match`
  statements raise on the host, and an agent reports that as a defect in the code.
- **The Isaac Lab and Isaac Sim SOURCES exist only in the container** — `/workspace/isaaclab`
  and `/isaac-sim`. An agent reasoning about upstream behaviour from the host is guessing;
  from the container it can read the actual file. Say this explicitly in the task, because
  an agent that cannot find upstream will quietly substitute recall for reading.
- The host `/tmp` is not bind-mounted in (see rule 10) — only the repo is, at
  `/workspace/framework`. Scratch files must go under the repo to be visible on both sides.

So the shape of every verification line in a task is:

```
docker exec virtual-hal-jazzy bash -lc 'cd /workspace/framework && /opt/isaaclab-venv/bin/python …'
```

and for anything touching upstream, add: *"Isaac Lab / Isaac Sim sources are at
`/workspace/isaaclab` and `/isaac-sim` inside that container — read them there, do not
reason from memory."* A review that ran on the host has confirmed the *shape* of a problem
at best; it has not measured anything.

**Scratch belongs in the container's own `/tmp`, never in the user's repo.** Two different
facts get conflated here, and conflating them cost a session: the *host's* `/tmp` is not
visible inside the container, but the *container's own* `/tmp` is perfectly writable — it is
overlay storage, private to the container, and gone when it is recreated. That is the right
home for probes, throwaway clones and ad-hoc venvs. An agent running everything through
`docker exec` never needs to write into the bind-mounted repo at all.

Say it explicitly, because the default is worse than nothing: told only that the host `/tmp`
is invisible, agents put their scratch in the project root. Three artifacts piled up in one
session before the user noticed — a 30 MB venv built just to get `black` and `codespell`, a
leftover probe script, and a submodule clone, all untracked and two of them root-owned, in a
working tree the user reads with `git status` all day.

So write into every task: *"scratch goes in the container's own `/tmp` (e.g.
`/tmp/vhal_scratch/`), never under `/workspace/framework`; delete what you created before you
report."* The narrow exception is a file that genuinely must be visible from both host and
container — name that path explicitly when it comes up, and clean it up by hand.

And the meta-rule this exposed: **do not edit the user's project files to make your own
tooling more convenient.** Adding a `.gitignore` entry to accommodate agent scratch is fixing
the symptom in someone else's repo. Cleanup is part of the task, not an afterthought — an
agent that reports success while leaving root-owned junk behind has not finished.

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

## 10. If the task hinges on a mechanism, prove the mechanism

Rules 1-9 are about verifying the *result*. This one is about the *method* you hand
over. When a task's before/after capture, isolation or shadowing depends on some
machinery behaving a particular way, prove that machinery works before briefing it —
otherwise the delegate either takes it on faith (and may be wrong) or burns turns
re-deriving it, and the worst case is a green gate that measured nothing.

Two real instances, both on the same task:

- **The no-op capture.** A draft captured "before" with `git stash push` on files that
  were already committed and clean. `git stash` prints "No local changes to save",
  creates nothing, and exits 0. The "before" run would have executed the *after* code,
  the comparison would have shown ~0 diff, and a GPU-session-sized gate would have gone
  green having never run the code under test. A no-op that exits 0 is the most dangerous
  shape of wrong.
- **The unproven shadow.** Its replacement — a git worktree at the parent revision plus
  `PYTHONPATH` — was asserted, not demonstrated. The delegate rightly stopped on it:
  the package is a PEP 660 editable install served by a `MetaPathFinder`, and
  `sys.meta_path` is consulted before `sys.path`. It happens to work, because every
  `_EditableFinder` is registered *after* `PathFinder`, but that is a fact about this
  environment, not a guarantee.

The check that settles it is cheap and belongs in the briefing as an established fact:

```bash
mkdir -p /tmp/shadowprobe/<pkg> && echo 'MARKER="shadow"' > /tmp/shadowprobe/<pkg>/__init__.py
PYTHONPATH=/tmp/shadowprobe <interpreter> -c "import <pkg> as m; print(m.__file__, getattr(m,'MARKER',None))"
```

A marker file proves shadowing directly. Reasoning about `sys.meta_path` ordering does not.

A third instance, same task, same shape: the replacement mechanism said "put the worktree in
`/tmp`". But the simulator runs **inside a container**, and the host `/tmp` is not bind-mounted
into it — only the repo is. The worktree would have been invisible to the process under test,
which would have silently imported the installed (migrated) package: the identical false pass,
moved one step further along. When the thing under test runs somewhere else — a container, a
remote host, another user — an isolation path is only real if it is visible *there*. Check the
mount table (`docker inspect <c> --format '{{range .Mounts}}...'`) or write a probe file and
look for it from the other side.

So: **before writing "capture the before state by X", run X and confirm it changed what
you think it changed.** State the proof in the task under Established facts, so the
delegate does not have to rediscover it — and so that if it *is* wrong, it is wrong in
your terminal instead of in a green report.

## 11. A verified premise decays — re-check it at run time, not at write time

Rule 10 says prove the mechanism. This one says the proof has a shelf life. When several
agents edit one tree, a fact you *measured* becomes a fact you *remember* within the hour,
and a procedure built on it silently changes meaning.

The instance: a capture procedure said "the migrated files are clean in the working tree,
so a path-scoped `git checkout <parent-rev> -- <files>` is safe." That was true when it was
written and measured. Forty minutes later another agent — commissioned by the same person
who wrote the sentence — removed a bootstrap from two of those exact files. The procedure
would now `checkout` over uncommitted work, the restore would bring the files back only to
their *committed* state, and the work would be gone. The sting is that the procedure's own
success check, "expect `git status` to show only `kernels.py`", would then read **green** —
because the destroyed files had been restored to a clean committed state. A passing check
over a silently altered tree: the same failure class as the no-op stash, wearing a new
costume.

Two habits follow:

- **Enumerate hazardous state at run time.** Do not hardcode which files are dirty, which
  processes are running, or which packages are installed. Make step 0 of the procedure
  `git status --porcelain > before.txt` and have later steps read *that*, not a list typed
  into the document. The invariant to write down is the general one — "never `checkout` or
  `reset` a file that is currently dirty without backing it up" — not the instance you
  happened to notice.
- **Compare against a recording, never an expectation.** `diff before.txt after.txt` fails
  when something unexpected changed. "Expect exactly ` M kernels.py`" passes in precisely
  the case you most need to catch.
- **Compare the bytes, not the metadata.** The `diff before.txt after.txt` above was itself
  insufficient, and only executing the procedure revealed why: `git status --porcelain`
  reports *which* paths are dirty, not *what is in them*. It goes red when a file reverts to
  clean — the failure the author had in mind — and stays green when a dirty file returns as a
  *different* dirty version (a stale backup, another agent rewriting it mid-run, a
  wrong-revision restore). `diff -r backup/ worktree/` catches both. A check one level too
  abstract is the most durable kind of false pass, because it is right about something.

**Corollary — a procedure nobody has run is a draft, not a procedure.** Three successive
versions of that capture were wrong *by reading*: a no-op stash, a destructive checkout, and a
content-blind check. Each review found the previous one's bug and introduced its own. The
defect that survived all three reviews died in the first ten minutes of *executing* the thing
against a throwaway clone with the hazard reproduced. When a procedure gates something
expensive — a GPU session, a release, a migration — budget a dry run on a scratch copy before
anyone follows it for real. Reviewing is cheap and finds the errors you thought to look for;
running finds the rest.

And when writing a task: state facts with their timestamp and tell the delegate to re-verify
any that gate a destructive step. "Verified at 14:05; re-check before acting on it" is
honest. An unqualified "the tree is clean" is a claim about the future.

## 12. Name the file by its full path — a basename is not an identifier

A task said "`base.py` must be split, not moved whole." The repo has two: `controller/base.py`
(32 lines, the `ControllerBase` protocol) and `controller/robot/base.py` (297 lines, the robot
body and slot map). The plan being corrected referred to *different ones* in two of its
sections, which is how the confusion started — and my task statement inherited the ambiguity
without noticing. The delegate spent four extra steps discovering there were two files and
deciding which I meant. It guessed right; that was luck.

The same applies to symbols. After moving control math into `robots`, the tree holds both
`robots/actuators/kinematics.py` and `controller/robot/kinematics.py` — unrelated files, one
of which moves and one of which stays.

So: **every path in a task is repo-relative and complete**, and when two files share a
basename, say so explicitly and say which is which. Cheap insurance: before sending a task,
`find . -name "<basename>"` for each file you named. If it returns more than one, your task is
ambiguous no matter how clear it felt to write.

## 13. Placement is a decision — make the agent justify it against upstream

When a task moves code, "where exactly" is the part most worth specifying, and the part an
agent will quietly get wrong for a defensible-sounding reason. Asked to move three modules
into `external/robots`, an agent put all three under `actuators/` and wrote in its own report:
*"kinematics fits less cleanly but I kept it with the other two to avoid scattering the one
coherent control-math unit."* That is a real argument — and it is the wrong call. Two of the
three (a joint↔motor transmission, a PD torque with limits) genuinely are actuator concerns.
The third rotates a vector from world frame into body frame; it is frame math, and upstream
Isaac Lab keeps exactly that in `utils/math.py` next to `quat_apply_inverse`.

The convention already exists — the mistake is not consulting it. This project's rule is that
where our layout and Isaac Lab disagree, Isaac Lab wins, and its sources are readable in the
container at `/workspace/isaaclab`. So put this in the task:

> State where you are placing each file and why, and check the placement against upstream's
> own layout (`/workspace/isaaclab/source/isaaclab/isaaclab/`) before deciding. Quote what you
> found. If upstream contradicts the placement named here, STOP and report instead of
> proceeding.

Two habits follow. Decide placement **per file**, never per batch: "keep them together" is how
one misfit rides in on the coattails of two correct ones. And when you name a destination in a
task, name it for each file separately — a single directory for a group invites exactly this.

### A suite result must name what it collected

An agent reported "83 passed, 10 skipped" for a submodule and called it clean. Running `pytest`
from the same directory myself gave **2 failed, 122 passed, 10 skipped** — it had collected only
`controller/tests/` and never seen the second test directory, `tests/`, at the submodule root,
where two pre-existing failures live. Its number was true about what it ran and silent about
what it did not, which reads as coverage it never had.

So require the collection path in the report, not just the counts: *"say which paths pytest
collected, and paste the command"*. And when you verify a delegate's suite claim, run it yourself
from the submodule root rather than reproducing their invocation — a narrower selection is
invisible in the numbers alone. (`external/controller` has two test directories; assume any
submodule might.)

### Give the tool's location, not just its absence

Telling an agent "`pre-commit` is not installed in the container" is true and useless: it cannot
run the check, so it reports that honestly and then *assumes* the outcome. One wrote "a later
isort run should be a no-op, but that is unverified" — and black, isort and end-of-file-fixer all
failed when actually run. An unrunnable check turns into an optimistic guess every time.

So when a tool is missing where the agent works, say where it DOES live:
`/home/svyatoslav/.local/bin/pre-commit` on the host. And note which hooks auto-fix: a first run
that reports Failed for black/isort/end-of-file-fixer is normal, and the second run is the real
verdict — an agent that does not know this reports a failure that is not one, or stops.

## 14. A long read-only investigation is fragile — make it produce something early

Two delegates in one session died on "The response stopped arriving" after 39 and 16 steps,
having verified almost everything and written **nothing**. One had spent 534k output tokens.
Every finding survived only because the narration is readable after the fact and I mined it by
hand; had I not, the whole run was waste.

The pattern is specific: read-only analysis tasks that end in "write it all to one report file"
put the entire payoff in the last step, so any stream failure loses all of it. Worse, the
investigation phase is exactly what makes those runs long, which raises the chance of the
failure.

Three ways to reduce it, in order of effect:

- **Hand over the facts.** Most of that budget went to re-deriving things I already knew or a
  sibling had measured. An "Established facts — do not re-derive" block, with an explicit
  "CONFIRM only these three" list, cuts a 39-step run to a handful. This is also rule 11's
  companion: state the facts *with* their provenance so the agent knows what is safe to trust.
- **Write incrementally.** Tell the agent to create the report file early, with the section
  headings, and fill it in as it goes. A half-written report is a recoverable artifact; a
  perfect report that was never written is not. **This was then measured:** three agents died
  on the same stream error within one minute of each other; the two that had been told to write
  incrementally left 49-line and 214-line reports on disk, and the earlier one that had not
  left nothing after 534k tokens. Cheap instruction, whole-run payoff.

**Concurrency is itself a failure cause.** Those three deaths came while six delegates ran
against one local model; an earlier attempt at eight produced the same simultaneous-drop
signature. The drops are `The response stopped arriving`, not the output-token cap — raising
`CLAUDE_CODE_MAX_OUTPUT_TOKENS` does nothing for them. Treat the pool size as a tuning knob
with evidence behind it: when several agents drop within the same minute, that is load, not
coincidence, and the fix is fewer of them, not a bigger cap. `project_sync` now reports the
real headroom (`pool.local_running` / `pool.headroom`), counting only delegates recorded as
running on the local backend — a paid background session no longer occupies a slot it never
used. `contrib/bench_concurrency.py` measures where the ceiling actually sits (levels 1/2/4/8
into CSV); run it on an idle GPU before trusting a number.
- **Split the question.** Two 15-step tasks beat one 40-step task, and their failures are
  independent.

And when one does die mid-run: `watch_delegate` still shows the full narration. Mine it and
fold the findings into the respawn as established facts — never restart the same investigation
from zero.

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
