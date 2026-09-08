#!/usr/bin/env python3
"""
Standalone concurrency benchmark driver for claude-local-delegate.

It measures how well parallel local delegation scales by driving the MCP
server (server.py) over stdio: it launches the server as a subprocess, speaks
newline-delimited JSON-RPC 2.0 to it, and runs a fan_out_to_local batch at each
concurrency level.

For each level k it issues ONE fan_out_to_local with k identical read-only
items (allowed_tools="Read,Grep,Glob"), passing shared_instruction as the FIRST
argument so all children share a token prefix (vLLM prefix-cache friendly). It
then polls check_fanout_status every --poll seconds until nothing is
working/blocked (or the per-level --timeout is hit), and records wall time,
per-agent states, and per-agent token usage.

Token usage comes from the server module's own helpers (imported directly), so
we never re-implement the transcript parsing:
    server._resolve_agent(agent_id) -> (agent_dict, err);  agent_dict["sessionId"]
    server._find_transcript(session_id) -> path
    server._token_usage(path) -> (input_plus_cache, output, turns)

Python 3 standard library only. It does NOT load the GPU by itself; running the
benchmark does (each fan_out spawns real `claude --bg` agents on the local model).
"""

import argparse
import csv
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

# claude-local-delegate dir (this file lives in its contrib/ subdir)
_HERE = Path(__file__).resolve().parents[1]
# repo root (parent of claude-local-delegate)
_REPO_ROOT = _HERE.parent

SERVER_PATH = _HERE / "server.py"


# ---- server module import (for the token-usage helpers) --------------------
sys.path.insert(0, str(_HERE))
try:
    import server  # noqa: F401  (import guarded by __main__ in server.py)
except Exception as e:  # pragma: no cover - only if server.py can't be imported
    sys.stderr.write(
        f"WARNING: could not import server module ({e}); per-agent token "
        "numbers will be reported as 0.\n"
    )
    server = None


# ---------------------------------------------------------------------------
# Minimal JSON-RPC-over-stdio client for the MCP server subprocess.
# ---------------------------------------------------------------------------
class McpClient:
    """One newline-delimited JSON object per line, both directions."""

    def __init__(self, cmd):
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        self._next_id = 0

    def _read_line(self):
        # Server may emit log noise; keep reading until we get a JSON object.
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(
                    "MCP server closed stdout before a response arrived."
                )
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue  # skip any non-JSON diagnostic line

    def _request(self, method, params=None):
        self._next_id += 1
        rid = self._next_id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        return self._read_line()

    def _notify(self, method):
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": method}
        ) + "\n")
        self.proc.stdin.flush()

    def initialize(self):
        resp = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "bench_concurrency", "version": "1.0"},
        })
        if resp.get("error"):
            raise RuntimeError(f"initialize failed: {resp['error']}")
        self._notify("notifications/initialized")
        return resp.get("result")

    def call_tool(self, name, arguments):
        resp = self._request("tools/call", {
            "name": name, "arguments": arguments,
        })
        if resp.get("error"):
            raise RuntimeError(f"{name} returned JSON-RPC error: {resp['error']}")
        result = resp.get("result")
        if result is None:
            raise RuntimeError(f"{name}: no result in response {resp!r}")
        if result.get("isError"):
            text = _first_text(result)
            raise RuntimeError(f"tool {name} reported isError=True: {text}")
        return result

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
        except Exception:
            pass


def _first_text(result):
    for item in (result.get("content") or []):
        if isinstance(item, dict) and item.get("type") == "text":
            return item.get("text", "")
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Parsing helpers for the fan_out / status result text.
# ---------------------------------------------------------------------------
_RE_BATCH_ID = re.compile(r"batch_id: ([0-9a-f]{12})")
_RE_AGENT_ID = re.compile(r"[0-9a-f]{8}")
# status line: "  - <8hex>: <state>"
_RE_STATUS_LINE = re.compile(r"^\s*-\s*([0-9a-f]{8})\s*:\s*(\S+)")
# summary line: "N working, M blocked(...), P completed, Q failed, R stopped, S unknown"
_RE_COUNTS = {
    "working": re.compile(r"(\d+)\s+working"),
    "blocked": re.compile(r"(\d+)\s+blocked"),
    "completed": re.compile(r"(\d+)\s+completed"),
    "failed": re.compile(r"(\d+)\s+failed"),
    "stopped": re.compile(r"(\d+)\s+stopped"),
    "unknown": re.compile(r"(\d+)\s+unknown"),
}


def parse_batch_id(text):
    m = _RE_BATCH_ID.search(text)
    if not m:
        raise RuntimeError(f"could not parse batch_id from: {text!r}")
    return m.group(1)


def parse_agent_ids(text):
    # The fan_out result lists them as: "agent ids: ['abcd1234', ...]"
    m = re.search(r"agent ids:\s*\[([^\]]*)\]", text)
    scope = m.group(1) if m else text
    return _RE_AGENT_ID.findall(scope)


def parse_status(text):
    """Return (agent_states: {id: state}, counts: {name: n})."""
    states = {}
    for line in text.splitlines():
        m = _RE_STATUS_LINE.match(line)
        if m:
            states[m.group(1)] = m.group(2)
    counts = {}
    for name, rx in _RE_COUNTS.items():
        mm = rx.search(text)
        counts[name] = int(mm.group(1)) if mm else 0
    return states, counts


# ---------------------------------------------------------------------------
# Per-agent token accounting via the server module's own helpers.
# ---------------------------------------------------------------------------
def agent_tokens(agent_id):
    """(state, input_plus_cache, output, turns). state='' if unresolved."""
    if server is None:
        return "", 0, 0, 0
    try:
        agent_dict, err = server._resolve_agent(agent_id)
        if agent_dict is None:
            if err:
                sys.stderr.write(f"  resolve({agent_id}): {err}\n")
            return "", 0, 0, 0
        session_id = agent_dict.get("sessionId") or ""
        state = agent_dict.get("state") or agent_dict.get("status") or ""
        if not session_id:
            return state, 0, 0, 0
        path = server._find_transcript(session_id)
        if not path:
            return state, 0, 0, 0
        inp, out, turns = server._token_usage(path)
        return state, int(inp), int(out), int(turns)
    except Exception as e:
        sys.stderr.write(f"  token accounting failed for {agent_id}: {e}\n")
        return "", 0, 0, 0


# ---------------------------------------------------------------------------
# Run a single concurrency level.
# ---------------------------------------------------------------------------
def run_level(client, level, item_text, cwd, poll, timeout):
    print(f"\n== level {level}: fanning out {level} read-only agent(s) ==")
    start = time.time()
    # shared_instruction is the FIRST argument so all children share a token prefix.
    result = client.call_tool("fan_out_to_local", {
        "shared_instruction": (
            "You are a read-only benchmark worker. "
            "Using only the Read, Grep and Glob tools, report the top-level "
            "directory layout of the current working directory in a few lines. "
            "Do not edit anything.\n"
        ),
        "items": [item_text] * level,
        "cwd": str(cwd),
        "allowed_tools": "Read,Grep,Glob",
    })
    text = _first_text(result)
    batch_id = parse_batch_id(text)
    agent_ids = parse_agent_ids(text)
    print(f"   batch_id={batch_id}  agents={agent_ids}")

    last_states = {}
    last_counts = {}
    timed_out = False
    while True:
        elapsed = time.time() - start
        if elapsed >= timeout:
            timed_out = True
            print(f"   reached per-level timeout ({timeout}s); stopping poll.")
            break
        time.sleep(poll)
        st = client.call_tool("check_fanout_status", {"batch_id": batch_id})
        st_text = _first_text(st)
        last_states, last_counts = parse_status(st_text)
        working = last_counts.get("working", 0)
        blocked = last_counts.get("blocked", 0)
        print(f"   t+{elapsed:6.1f}s  working={working} blocked={blocked} "
              f"completed={last_counts.get('completed', 0)} "
              f"failed={last_counts.get('failed', 0)} "
              f"stopped={last_counts.get('stopped', 0)} "
              f"unknown={last_counts.get('unknown', 0)}")
        if working == 0 and blocked == 0:
            break

    wall_seconds = time.time() - start

    # Best-effort fetch of the aggregate result (only meaningful when settled).
    try:
        client.call_tool("get_fanout_result", {"batch_id": batch_id})
    except Exception as e:
        print(f"   get_fanout_result not settled: {e}")

    # Per-agent token accounting.
    per_agent = []
    total_in = total_out = total_turns = 0
    completed = failed = unknown = 0
    for aid in agent_ids:
        state, inp, out, turns = agent_tokens(aid)
        # Prefer the batch's own status line for this agent id when we have it.
        if aid in last_states:
            state = last_states[aid]
        if state in ("completed", "done"):
            completed += 1
        elif state == "failed":
            failed += 1
        elif state in ("", "unknown"):
            unknown += 1
        total_in += inp
        total_out += out
        total_turns += turns
        per_agent.append({
            "level": level,
            "agent_id": aid,
            "state": state or "unknown",
            "input_tokens": inp,
            "output_tokens": out,
            "turns": turns,
        })
        print(f"   agent {aid}: state={state or 'unknown'} "
              f"in={inp} out={out} turns={turns}")

    n = len(agent_ids) or 1
    mean_output = total_out / n
    row = {
        "level": level,
        "wall_seconds": round(wall_seconds, 2),
        "agents": len(agent_ids),
        "completed": completed,
        "failed": failed,
        "stopped": last_counts.get("stopped", 0),
        "unknown": unknown,
        "spawn_errors": 0,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_turns": total_turns,
        "mean_output_per_agent": round(mean_output, 2),
        "timed_out": timed_out,
    }
    return row, per_agent, last_counts


# ---------------------------------------------------------------------------
# Warmup: one throwaway single delegate to warm the vLLM prefix cache.
# ---------------------------------------------------------------------------
def warmup(client, cwd):
    print("\n== warmup: one throwaway single delegate (prefix-cache warm) ==")
    start = time.time()
    result = client.call_tool("fan_out_to_local", {
        "shared_instruction": (
            "You are a read-only benchmark warmup worker. "
            "Read the top-level directory listing and report one line. "
            "Do not edit anything.\n"
        ),
        "items": ["Warmup item.", ],
        "cwd": str(cwd),
        "allowed_tools": "Read,Grep,Glob",
    })
    text = _first_text(result)
    try:
        batch_id = parse_batch_id(text)
    except RuntimeError:
        print(f"   warmup spawn text: {text!r}")
        return
    print(f"   warmup batch_id={batch_id}")
    # Poll until settled or a modest cap.
    deadline = time.time() + 600
    while time.time() < deadline:
        time.sleep(15)
        try:
            st = client.call_tool("check_fanout_status", {"batch_id": batch_id})
            _, counts = parse_status(_first_text(st))
        except Exception:
            break
        if counts.get("working", 0) == 0 and counts.get("blocked", 0) == 0:
            print(f"   warmup settled in {time.time() - start:.1f}s")
            return
    print(f"   warmup cap reached after {time.time() - start:.1f}s")


# ---------------------------------------------------------------------------
def parse_levels(s):
    out = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return out


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main():
    ap = argparse.ArgumentParser(
        description="Benchmark parallel local delegation (fan_out_to_local) "
                    "concurrency scaling. Drives the MCP server over stdio."
    )
    ap.add_argument("--levels", default="1,2,4,8",
                    help="comma-separated concurrency levels to test (default 1,2,4,8)")
    ap.add_argument("--cwd", default=str(_REPO_ROOT),
                    help="working dir handed to the delegates (default: repo root)")
    ap.add_argument("--out", default="bench_results.csv",
                    help="CSV output path (default bench_results.csv)")
    ap.add_argument("--warmup", dest="warmup", action="store_true", default=True,
                    help="run one throwaway single delegate first to warm the "
                         "vLLM prefix cache (default: on)")
    ap.add_argument("--no-warmup", dest="warmup", action="store_false",
                    help="disable the warmup single delegate")
    ap.add_argument("--poll", type=int, default=15,
                    help="seconds between check_fanout_status polls (default 15)")
    ap.add_argument("--timeout", type=int, default=1800,
                    help="per-level wall-clock ceiling in seconds (default 1800)")
    args = ap.parse_args()

    levels = parse_levels(args.levels)
    cwd = Path(args.cwd)
    out_path = Path(args.out)
    agents_path = Path(str(out_path)[:-4] + ".agents.csv") if out_path.suffix == ".csv" \
        else out_path.with_suffix(out_path.suffix + ".agents.csv")
    if agents_path == out_path:
        agents_path = Path(str(out_path) + ".agents.csv")

    item_text = "Identify a few representative top-level files/directories."

    level_rows = []
    agent_rows = []

    cmd = [sys.executable, str(SERVER_PATH)]
    client = McpClient(cmd)
    try:
        client.initialize()
        if args.warmup:
            warmup(client, cwd)
        for level in levels:
            row, per_agent, _ = run_level(
                client, level, item_text, cwd, args.poll, args.timeout
            )
            level_rows.append(row)
            agent_rows.extend(per_agent)
    finally:
        client.close()

    # ---- CSV output -----------------------------------------------------
    level_fields = [
        "level", "wall_seconds", "agents", "completed", "failed",
        "stopped", "unknown", "spawn_errors",
        "total_input_tokens", "total_output_tokens", "total_turns",
        "mean_output_per_agent", "timed_out",
    ]
    write_csv(out_path, level_rows, level_fields)
    agent_fields = ["level", "agent_id", "state",
                    "input_tokens", "output_tokens", "turns"]
    write_csv(agents_path, agent_rows, agent_fields)

    # ---- human summary table -------------------------------------------
    print("\n" + "=" * 78)
    print("SUMMARY (concurrency level vs wall time & tokens)")
    print("=" * 78)
    hdr = f"{'lvl':>4} {'wall_s':>9} {'agents':>6} {'done':>5} {'fail':>5} " \
          f"{'in_tok':>9} {'out_tok':>9} {'turns':>6} {'mean_out':>10}"
    print(hdr)
    print("-" * len(hdr))
    for r in level_rows:
        print(
            f"{r['level']:>4} {r['wall_seconds']:>9.2f} {r['agents']:>6} "
            f"{r['completed']:>5} {r['failed']:>5} "
            f"{r['total_input_tokens']:>9} {r['total_output_tokens']:>9} "
            f"{r['total_turns']:>6} {r['mean_output_per_agent']:>10.1f}"
        )
    print("-" * 78)
    print(f"per-level CSV : {out_path}")
    print(f"per-agent CSV : {agents_path}")
    print("(timed_out flag is stored in the CSV row when a level hit --timeout.)")


if __name__ == "__main__":
    main()