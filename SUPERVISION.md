# Supervising a Delegated Background Agent

## Before you delegate: writing the task

This file is about a run that already exists. The task text is what decides whether it
succeeds — see **[TASK_DESIGN.md](TASK_DESIGN.md)** for the briefing rules distilled from
real failures (consumer discovery as a precondition, why `py_compile` never proves an
import move, oracle + wrong-direction control for numeric refactors, one file per task
with the anchors pre-found, pinning the exact environment in every command).

Note the division of labour: `~/.claude/agents/local-worker.md` is the **persona** — it
governs how the agent behaves in any task (run a real check, show the output, list
assumptions, say what it could not verify). `TASK_DESIGN.md` is for **you**, the caller —
the per-task facts the persona cannot know: which line the anchor is on, which callers
must be checked first, which interpreter in the container actually sees the package.


A short operator guide for the parent Claude Code session that owns a
delegated `claude --bg` run. You see only the child's plain-text messages —
use these MCP tools to watch, intervene, and redirect.

## TL;DR — the loop that works

**watch_delegate → stop_delegate → delegate_to_local (re-delegate).**

A running local `claude --bg` agent does **not** read a mid-run
`SendMessage` — it finishes its current run first, and a fast local model
usually finishes before the message is ever looked at; once it is `done`
it is unreachable by `SendMessage` entirely. So do not sit in a
watch→SendMessage→watch loop hoping a correction lands. When an agent
drifts: `stop_delegate` (it settles to `done` in ~10–15 s, before its next
step), then `delegate_to_local` again with a sharper task. Nothing is lost
— the stopped run's transcript stays readable via `get_delegate_result`,
so fold anything useful it already produced into the new task text.

`SendMessage` is reliable in exactly one case: the agent is **`blocked`**
on its own question — answer that. (If the parent session's
permission-mode class differs from the delegate's — e.g. parent `auto`,
delegate default `bypassPermissions` — that one message is held for a
one-time user approval; approve it. The MCP tools below are never gated.)
A **read-only** delegation (allowlist of `Read,Grep,Glob`) is spawned in
`dontAsk` instead, where the allowlist is enforced rather than advisory.

## Watch: `watch_delegate(run_id)`

The token-cheap view. Returns the agent's plan plus its per-step
plain-text narration (no code, no tool output) and the tokens it has
burned so far. Use this in a light loop to stay on top of a run without
paying for a transcript.

## Poll: `check_delegate_status(run_id)`

Cheap one-shot snapshot: current state, the last sentence of the run, and
tokens used. Use it when `watch_delegate` isn't needed and you just want
to know "is it still alive, and where is it?"

**A still token counter is not a stall.** An agent in `working` whose output
tokens have not moved between two polls is very often loading context — the
prefill of a large prompt produces no output tokens and can take a while,
especially on a busy backend. Do not `stop_delegate` on that evidence: you
throw away a run that was about to continue, and the replacement pays the same
context cost again from zero.

The signals that actually justify intervening:

- state is **`blocked`** — a real state. Read its last words, because the
  three causes need different responses:
  - *"API Error: The response stopped arriving"* — the stream dropped. Check
    whether it left a file half-edited, then re-delegate; nothing else to do.
  - *"exceeded the 16384 output token maximum"* — it tried to emit a report
    bigger than the cap. This one is **preventable, not recoverable**: the next
    brief must say "keep the final report short, no large excerpts". An agent
    asked to paste a whole file will hit this every time.
  - it is genuinely waiting on its own question — the one case where
    `SendMessage` is the right tool.
- `watch_delegate` narration shows it *doing* the wrong thing — reading files
  outside the task, re-deriving what the brief already gave it, looping over
  the same check.
- it edited a file and then went quiet: verify the file is not half-edited
  (a partial import move can pass `py_compile` and still be broken), and if it
  is, revert that file rather than waiting.

"Quiet" on its own means keep waiting.

## Stop: `stop_delegate(run_id, mode)`

Halt a drifting run:

- `mode: "interrupt"` — SIGINT; the agent gets a chance to wind down
  cleanly. Prefer this first.
- `mode: "terminate"` — SIGTERM; hard stop when the run is hopeless.

## Course-correct after stopping

The stopped agent is now `done` and **not** reachable by `SendMessage`.
Read what it managed to do with `get_delegate_result(run_id)`, then call
`delegate_to_local` again with a smaller, better-scoped task — carry any
useful partial result forward in the new task text.

`get_delegate_result` returns the **tail** of the answer (plus a sha256 of the
full text) rather than all of it, because a long answer is charged to this
session. When you actually need the whole thing, pass `full: true`; the
transcript on disk was never truncated.

## Why the narration is readable

Delegated runs start with an `announce_plan` preamble (on by default): the
agent must post a short numbered plan before its first tool call and a
terse plain sentence before/after each step. That discipline is what makes
`watch_delegate` output a readable progress feed instead of a wall of
tool spam. If a child isn't narrating, its task prompt probably dropped
the preamble.