# Supervising a Delegated Background Agent

A short operator guide for the parent Claude Code session that owns a
delegated `claude --bg` run. You see only the child's plain-text messages —
use these MCP tools to watch, intervene, and redirect.

## Watch: `watch_delegate(run_id)`

The token-cheap view. Returns the agent's plan plus its per-step
plain-text narration (no code, no tool output) and the tokens it has
burned so far. Use this in a light loop to stay on top of a run without
paying for a transcript.

## Poll: `check_delegate_status(run_id)`

Cheap one-shot snapshot: current state, the last sentence of the run, and
tokens used. Use it when `watch_delegate` isn't needed and you just want
to know "is it still alive, and where is it?"

## Stop: `stop_delegate(run_id, mode)`

Halt a drifting run:

- `mode: "interrupt"` — SIGINT; the agent gets a chance to wind down
  cleanly. Prefer this first.
- `mode: "terminate"` — SIGTERM; hard stop when the run is hopeless.

## Redirect after stopping

The stopped agent keeps its transcript. Either:

- `SendMessage` to the run to redirect it — it resumes from where it left
  off with the new instruction, or
- re-delegate a smaller, better-scoped task to a fresh run.

## Why the narration is readable

Delegated runs start with an `announce_plan` preamble (on by default): the
agent must post a short numbered plan before its first tool call and a
terse plain sentence before/after each step. That discipline is what makes
`watch_delegate` output a readable progress feed instead of a wall of
tool spam. If a child isn't narrating, its task prompt probably dropped
the preamble.