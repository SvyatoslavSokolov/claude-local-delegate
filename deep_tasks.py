"""Deep delegation: task texts, report paths and the per-project findings store.

A deep run investigates one thing all the way down (deep_dive), prepares a
working briefing before the supervisor starts a task (context_pack), or
reviews a diff adversarially (review_diff). Each writes a Markdown dossier to a
path chosen here, under FINDINGS_DIR/<project>/, so the supervisor reads the
whole report instead of a compacted chat tail, and later runs on the same
project can build on earlier findings.
"""
import os
import re
import time
import hashlib

KINDS = ("dive", "pack", "review", "expert")


def project_key(cwd):
    """Stable per-project directory name: basename + short hash of the path."""
    path = os.path.realpath(cwd)
    digest = hashlib.sha1(path.encode()).hexdigest()[:8]
    return f"{os.path.basename(path.rstrip(os.sep)) or 'root'}-{digest}"


def findings_dir(root, cwd):
    return os.path.join(root, project_key(cwd))


def _slug(text, max_len=48):
    words = re.findall(r"[A-Za-z0-9]+", text.lower())
    return ("-".join(words)[:max_len].strip("-")) or "task"


def new_report_path(root, cwd, kind, title):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(findings_dir(root, cwd), f"{stamp}-{kind}-{_slug(title)}.md")


def list_findings(root, cwd, query=None, limit=30):
    """Newest first: [{file, kind, title, mtime, bytes}]. query filters by
    case-insensitive substring over the file name and first heading."""
    folder = findings_dir(root, cwd)
    try:
        names = [n for n in os.listdir(folder) if n.endswith(".md")]
    except OSError:
        return []
    rows = []
    for name in names:
        path = os.path.join(folder, name)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                head = f.read(2000)
            st = os.stat(path)
        except OSError:
            continue
        title = next((ln.lstrip("# ").strip() for ln in head.splitlines() if ln.startswith("#")), "")
        parts = name.split("-")
        kind = parts[2] if len(parts) > 2 and parts[2] in KINDS else "?"
        if query and query.lower() not in (name + " " + title).lower():
            continue
        rows.append({"file": name, "kind": kind, "title": title,
                     "mtime": int(st.st_mtime), "bytes": st.st_size})
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:limit]


def _prior_block(folder):
    return (
        f"PRIOR FINDINGS for this project are in {folder} (Markdown dossiers from earlier "
        "deep runs, newest file name sorts last). List it first; read the ones relevant to "
        "this task and build on them. Say explicitly where you confirm, extend or contradict "
        "an earlier finding (cite its file name). They may be stale: re-check anything you rely on.\n"
    )


def _footer(report_path, edits_allowed):
    edits = ("You MAY modify project files if the task requires it; list every change in the dossier."
             if edits_allowed else
             "Do NOT modify project files. Scratch work goes in /tmp.")
    return (
        f"\n{edits}\nDOSSIER FILE: {report_path}\n"
        "Create it within your first few tool calls with the section headings and the "
        "question, then append each fact to it as soon as you have verified it. A run can be "
        "terminated at any time by a watchdog; whatever is not in this file by then is lost. "
        "Final chat message: the report path, the one-line answer with confidence, and the "
        "most important facts, ending with the `result:` status line."
    )


def dive_task(question, context, report_path, folder, edits_allowed=False):
    return (
        "DEEP DIVE. Investigate the question below exhaustively for the supervising model, "
        "which will act on your dossier without redoing the work.\n\n"
        f"QUESTION:\n{question.strip()}\n\n"
        + (f"WHAT THE SUPERVISOR ALREADY KNOWS / SUSPECTS:\n{context.strip()}\n\n" if context else "")
        + _prior_block(folder)
        + _footer(report_path, edits_allowed)
    )


def pack_task(task, focus, report_path, folder):
    return (
        "CONTEXT PACK. The supervising model is about to start the task below. Prepare the "
        "briefing it would otherwise spend its first hour building, so it can start editing "
        "with the right mental model. Do not implement the task.\n\n"
        f"UPCOMING TASK:\n{task.strip()}\n\n"
        + (f"START FROM: {focus.strip()}\n\n" if focus else "")
        + _prior_block(folder)
        + "\nUse these dossier sections instead of the default ones:\n"
        "# <task in one line>\n"
        "## Where it lives: every file/symbol the task will touch or must respect, as "
        "path:line with one line on its role.\n"
        "## How it flows: the call/data path relevant to the task (caller -> callee, config -> "
        "value), each edge cited.\n"
        "## Tests and checks: which tests cover this code, the exact command to run them, and "
        "their current result (RUN them).\n"
        "## History: recent commits touching these paths (git log --oneline -- <paths>) and "
        "what they tell you (reverted attempts, known bugs).\n"
        "## Conventions to follow: patterns the surrounding code uses that a change must match.\n"
        "## Traps: non-obvious couplings, duplicated logic, generated files, places a naive "
        "change breaks.\n"
        "## Open questions: what you could not determine.\n"
        "## Suggested plan: an ordered list of concrete steps.\n"
        + _footer(report_path, False)
    )


def review_task(base, focus, test_cmd, report_path, folder):
    return (
        "ADVERSARIAL DIFF REVIEW. Another model wrote the change below; find what is wrong with "
        "it before it ships. You are a reviewer, not an author: do not fix anything.\n\n"
        f"THE CHANGE: `git diff {base}` in the project root, plus untracked files from "
        "`git status --porcelain` (read them in full). Read the surrounding code of every hunk, "
        "not just the hunk.\n"
        + (f"FOCUS: {focus.strip()}\n" if focus else "")
        + (f"TEST COMMAND: `{test_cmd}` -- run it and report the real result.\n" if test_cmd else
           "Find and run the tests that cover the changed code; report the real result.\n")
        + "\nFor every candidate problem, try to PROVE it: construct the failing input or state "
        "and, where possible, run it (a small script in /tmp, a targeted test). Keep only "
        "findings you proved or can argue concretely; list the rest under 'Ruled out'.\n"
        "Look for: logic errors, wrong edge cases, broken callers of changed signatures, "
        "error paths that skip cleanup, concurrency and resource leaks, tests that do not test "
        "what they claim, behaviour changes the change did not intend.\n\n"
        + _prior_block(folder)
        + "\nUse these dossier sections:\n"
        "# Review: <PASS | CHANGES NEEDED>\n"
        "## Findings: most severe first; each: path:line, the defect in one sentence, the "
        "concrete failure scenario (input/state -> wrong result), evidence (what you ran), "
        "severity (blocker / major / minor).\n"
        "## Tests run: command -> result.\n"
        "## Ruled out: suspicions you checked and dismissed, with why.\n"
        "## Not reviewed: anything you could not cover.\n"
        + _footer(report_path, False)
    )


def expert_task(scope, focus, report_path, folder):
    """A resident module expert: maps `scope` once, then answers questions that arrive as
    cross-session messages, each turn resumed with its conversation still cached on the engine.
    Measured 2026-10-12 (vhal_planner): mapping 5 min, then a traced answer in 1-3.5 min."""
    return (
        f"RESIDENT EXPERT for `{scope}`. You stay available for the supervising model for as long as "
        "it works on this project: it will send you questions while it works, and your answers save it "
        "the time of reading the code itself.\n\n"
        f"PHASE 1 (now): study `{scope}` until you can answer detailed questions about its flow, failure "
        "handling, contracts with the rest of the repository and its tests. "
        + (f"Start from: {focus.strip()}. " if focus else "")
        + "Do not modify any file. Write a compact map (entry points, key symbols with path:line, data "
        f"flow, invariants, traps) to {report_path}.\n"
        + _prior_block(folder)
        + "End this turn with `result: mapped <scope>, waiting for questions`.\n\n"
        "PHASE 2 (each later turn): a question arrives as a <cross-session-message from=\"...\"> block. "
        "Verify the answer against the code (cite path:line; run a quick check when it settles a doubt; "
        "read sibling repositories if the answer lives there). Then SendMessage the answer to the "
        "`from=` address of that message, starting with the question's number or first words. Lead with "
        "the answer, then the evidence, then your confidence; flag anything the question assumed wrongly. "
        "Never modify files. End every turn with a `result:` line. If the code changed since you mapped "
        "it (`git status`, `git log -1`), re-read what changed before answering."
    )
