"""
agy_delegate.py - Delegation adapter for Google Antigravity (agy) CLI.

Allows delegating tasks from Claude Code (or any supervisor agent)
to Google Antigravity (Gemini models) running in headless non-interactive mode.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_STATE_DIR = os.path.expanduser("~/.claude-local-delegate/agy_runs")
DEFAULT_MODEL = "gemini-3.8-flash-high"
AGY_BIN = os.environ.get("AGY_BIN", "agy")


class AgyDelegateManager:
    """Manages spawning, tracking, and retrieving results from agy tasks."""

    def __init__(self, state_dir: str = DEFAULT_STATE_DIR):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _meta_path(self, run_id: str) -> Path:
        return self.state_dir / f"{run_id}.json"

    def _out_path(self, run_id: str) -> Path:
        return self.state_dir / f"{run_id}.out"

    def _err_path(self, run_id: str) -> Path:
        return self.state_dir / f"{run_id}.err"

    def spawn(
        self,
        task: str,
        cwd: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        conversation_id: Optional[str] = None,
        mode: Optional[str] = None,
        dangerously_skip_permissions: bool = True,
        print_timeout: int = 0,
    ) -> Dict[str, Any]:
        """
        Spawn an agy background task.
        Returns metadata dict containing run_id, pid, and initial status.
        """
        if not task or not isinstance(task, str):
            raise ValueError("Task must be a non-empty string.")

        run_id = f"agy-{uuid.uuid4().hex[:8]}"
        work_dir = cwd or os.getcwd()

        cmd = [AGY_BIN, "-p", task, "--output-format", "json"]

        if dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if model:
            cmd.extend(["--model", model])
        if effort:
            cmd.extend(["--effort", effort])
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])
        if mode:
            cmd.extend(["--mode", mode])
        if print_timeout > 0:
            cmd.extend(["--print-timeout", f"{print_timeout}s"])

        out_file = self._out_path(run_id)
        err_file = self._err_path(run_id)

        stdout_handle = open(out_file, "w", encoding="utf-8")
        stderr_handle = open(err_file, "w", encoding="utf-8")

        start_time = time.time()
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=work_dir,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,  # detach process group
            )
        except Exception as e:
            stdout_handle.close()
            stderr_handle.close()
            raise RuntimeError(f"Failed to spawn `{AGY_BIN}`: {e}") from e

        meta = {
            "run_id": run_id,
            "pid": proc.pid,
            "task": task,
            "cwd": work_dir,
            "model": model or DEFAULT_MODEL,
            "effort": effort,
            "mode": mode,
            "conversation_id": conversation_id,
            "start_time": start_time,
            "status": "running",
            "cmd": cmd,
        }

        self._meta_path(run_id).write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return meta

    def _try_parse_output(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Try to parse the JSON output from agy if written."""
        out_path = self._out_path(run_id)
        if not out_path.exists():
            return None
        try:
            content = out_path.read_text(encoding="utf-8", errors="replace").strip()
            if not content:
                return None
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                for line in reversed(content.splitlines()):
                    line = line.strip()
                    if line.startswith("{") and line.endswith("}"):
                        try:
                            return json.loads(line)
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
        return None

    def is_pid_alive(self, pid: int) -> bool:
        """Check whether a given PID is still active and not a zombie."""
        if pid <= 0:
            return False
        try:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                return False
        except (ChildProcessError, OSError):
            pass

        try:
            os.kill(pid, 0)
        except OSError:
            return False

        # Check /proc/<pid>/status for zombie
        proc_status = Path(f"/proc/{pid}/status")
        if proc_status.exists():
            try:
                for line in proc_status.read_text().splitlines():
                    if line.startswith("State:"):
                        if "Z" in line:
                            return False
                        break
            except Exception:
                pass

        return True

    def check_status(self, run_id: str) -> Dict[str, Any]:
        """
        Check the status of an agy run.
        Updates metadata if process has completed.
        """
        meta_file = self._meta_path(run_id)
        if not meta_file.exists():
            return {"run_id": run_id, "status": "not_found", "error": f"No run found for {run_id}"}

        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        if meta.get("status") in ("completed", "failed", "stopped"):
            return meta

        parsed_result = self._try_parse_output(run_id)
        pid = meta.get("pid", 0)
        alive = self.is_pid_alive(pid)

        # If we have valid completion JSON, or process is no longer alive
        if parsed_result is not None and "status" in parsed_result:
            alive = False

        # Resolve conversation_id and attach_command early
        conv_id = meta.get("conversation_id")
        if not conv_id and parsed_result and parsed_result.get("conversation_id"):
            conv_id = parsed_result.get("conversation_id")
        if not conv_id:
            try:
                cache_file = Path.home() / ".gemini/antigravity-cli/cache/last_conversations.json"
                if cache_file.exists():
                    c_data = json.loads(cache_file.read_text(encoding="utf-8"))
                    conv_id = c_data.get(meta.get("cwd"))
            except Exception:
                pass
        if conv_id:
            meta["conversation_id"] = conv_id
            meta["attach_command"] = f"agy --conversation {conv_id}"

        if alive:
            elapsed = time.time() - meta["start_time"]
            meta["elapsed_seconds"] = round(elapsed, 2)
            meta["status"] = "running"
            meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            return meta

        # Process has finished, record result
        elapsed = time.time() - meta["start_time"]
        meta["elapsed_seconds"] = round(elapsed, 2)

        out_content = ""
        out_path = self._out_path(run_id)
        if out_path.exists():
            out_content = out_path.read_text(encoding="utf-8", errors="replace").strip()

        err_content = ""
        err_path = self._err_path(run_id)
        if err_path.exists():
            err_content = err_path.read_text(encoding="utf-8", errors="replace").strip()

        if parsed_result and parsed_result.get("status") == "SUCCESS":
            meta["status"] = "completed"
            meta["response"] = parsed_result.get("response", "")
            conv_id = parsed_result.get("conversation_id") or meta.get("conversation_id")
            meta["conversation_id"] = conv_id
            if conv_id:
                meta["attach_command"] = f"agy --conversation {conv_id}"
                t_path = Path.home() / ".gemini/antigravity-cli/brain" / conv_id / ".system_generated/logs/transcript.jsonl"
                if t_path.exists():
                    meta["transcript_path"] = str(t_path)
            meta["usage"] = parsed_result.get("usage", {})
            meta["agy_duration"] = parsed_result.get("duration_seconds")
            meta["num_turns"] = parsed_result.get("num_turns")
        else:
            meta["status"] = "failed"
            error_details = []
            if parsed_result:
                error_details.append(f"agy status: {parsed_result.get('status')}")
                if "response" in parsed_result:
                    error_details.append(parsed_result["response"])
            if err_content:
                error_details.append(f"stderr: {err_content[:500]}")
            if not error_details:
                error_details.append(out_content[:500] or "Process terminated without output.")
            meta["error"] = "\n".join(error_details)

        meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return meta

    def get_result(
        self,
        run_id: str,
        wait_seconds: int = 300,
        poll_interval: float = 1.0,
    ) -> Dict[str, Any]:
        """
        Wait server-side for an agy run to complete and return the result.
        """
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            st = self.check_status(run_id)
            if st.get("status") in ("completed", "failed", "stopped", "not_found"):
                return st
            time.sleep(poll_interval)

        # Timed out waiting
        return {
            "run_id": run_id,
            "status": "timeout",
            "error": f"Timed out waiting after {wait_seconds}s.",
            "elapsed_seconds": wait_seconds,
        }

    def stop(self, run_id: str) -> Dict[str, Any]:
        """Stop/kill a running agy task."""
        meta_file = self._meta_path(run_id)
        if not meta_file.exists():
            return {"run_id": run_id, "status": "not_found"}

        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        pid = meta.get("pid", 0)
        if self.is_pid_alive(pid):
            try:
                # Terminate process group
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                time.sleep(0.5)
                if self.is_pid_alive(pid):
                    os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass

        meta["status"] = "stopped"
        meta["stopped_at"] = time.time()
        meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return {"run_id": run_id, "status": "stopped"}

    def run_sync(
        self,
        task: str,
        cwd: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        timeout: int = 300,
    ) -> Dict[str, Any]:
        """Run a task synchronously and return final result."""
        spawn_info = self.spawn(task=task, cwd=cwd, model=model, effort=effort)
        run_id = spawn_info["run_id"]
        return self.get_result(run_id, wait_seconds=timeout)


def main():
    parser = argparse.ArgumentParser(description="Agy delegation helper CLI")
    subparsers = parser.add_subparsers(dest="subcommand")

    # spawn
    p_spawn = subparsers.add_parser("spawn", help="Spawn background agy task")
    p_spawn.add_argument("task", help="Task description / prompt")
    p_spawn.add_argument("--cwd", help="Working directory")
    p_spawn.add_argument("--model", help="Model name (e.g. gemini-3.8-flash-high)")
    p_spawn.add_argument("--effort", choices=["low", "medium", "high"], help="Reasoning effort")
    p_spawn.add_argument("--conversation", help="Resume previous conversation ID")

    # status
    p_status = subparsers.add_parser("status", help="Check status of run")
    p_status.add_argument("run_id", help="Run ID (agy-xxxxxxxx)")

    # result
    p_result = subparsers.add_parser("result", help="Wait and get result")
    p_result.add_argument("run_id", help="Run ID (agy-xxxxxxxx)")
    p_result.add_argument("--wait", type=int, default=300, help="Wait timeout in seconds")

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop running task")
    p_stop.add_argument("run_id", help="Run ID (agy-xxxxxxxx)")

    # run (sync)
    p_run = subparsers.add_parser("run", help="Run task synchronously")
    p_run.add_argument("task", help="Task description / prompt")
    p_run.add_argument("--cwd", help="Working directory")
    p_run.add_argument("--model", help="Model name")
    p_run.add_argument("--timeout", type=int, default=300)

    # test
    subparsers.add_parser("test", help="Run self-test")

    args = parser.parse_args()
    mgr = AgyDelegateManager()

    if args.subcommand == "spawn":
        res = mgr.spawn(
            task=args.task,
            cwd=args.cwd,
            model=args.model,
            effort=args.effort,
            conversation_id=args.conversation,
        )
        print(json.dumps(res, indent=2))

    elif args.subcommand == "status":
        res = mgr.check_status(args.run_id)
        print(json.dumps(res, indent=2))

    elif args.subcommand == "result":
        res = mgr.get_result(args.run_id, wait_seconds=args.wait)
        print(json.dumps(res, indent=2))

    elif args.subcommand == "stop":
        res = mgr.stop(args.run_id)
        print(json.dumps(res, indent=2))

    elif args.subcommand == "run":
        res = mgr.run_sync(
            task=args.task,
            cwd=args.cwd,
            model=args.model,
            timeout=args.timeout,
        )
        print(json.dumps(res, indent=2))

# MCP Server Implementation for Claude Code integration -----------------------

MCP_SERVER_NAME = "claude-agy-delegate"
MCP_SERVER_VERSION = "0.1.0"
MCP_PROTOCOL_VERSION = "2024-11-05"

MCP_TOOLS = [
    {
        "name": "delegate_to_agy",
        "description": (
            "Start a background task on Google Antigravity (agy CLI with Gemini models) "
            "and return its run_id immediately. Normal flow: call get_agy_result once with "
            "wait_seconds=300 to wait server-side and receive the result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Self-contained task or prompt for agy (Antigravity).",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory for agy. Defaults to current directory.",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Gemini model to use. Options: gemini-3.8-flash-high, "
                        "gemini-3.8-flash-medium, gemini-3.1-pro-high, etc. Defaults to gemini-3.8-flash-high."
                    ),
                },
                "effort": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Reasoning effort level for the model.",
                },
                "conversation_id": {
                    "type": "string",
                    "description": "Optional conversation ID to resume or follow up on a previous task.",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "check_agy_status",
        "description": "Diagnostic state snapshot for one delegated agy task. Routine waiting should use get_agy_result.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The agy run_id returned by delegate_to_agy."},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "get_agy_result",
        "description": (
            "Wait server-side for a delegated agy task to complete and return its result, "
            "token usage, and conversation ID without paying for polling turns."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The agy run_id returned by delegate_to_agy."},
                "wait_seconds": {
                    "type": "integer",
                    "description": "Maximum seconds to wait (default 300).",
                },
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "stop_agy",
        "description": "Stop/cancel a running agy task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The agy run_id returned by delegate_to_agy."},
            },
            "required": ["run_id"],
        },
    },
]


def run_mcp_server(mgr: Optional[AgyDelegateManager] = None):
    """Run JSON-RPC stdio server compliant with Model Context Protocol."""
    if mgr is None:
        mgr = AgyDelegateManager()

    def handle_tool_call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if name == "delegate_to_agy":
            task = args.get("task", "")
            cwd = args.get("cwd")
            model = args.get("model")
            effort = args.get("effort")
            conv = args.get("conversation_id")
            try:
                meta = mgr.spawn(task=task, cwd=cwd, model=model, effort=effort, conversation_id=conv)
                text = (
                    f"Spawned Google Antigravity task. run_id: {meta['run_id']}\n"
                    f"Model: {meta['model']}, PID: {meta['pid']}\n"
                    f"Call get_agy_result('{meta['run_id']}', wait_seconds=300) to wait and get the result."
                )
                return {"content": [{"type": "text", "text": text}], "isError": False}
            except Exception as e:
                return {"content": [{"type": "text", "text": f"Failed to spawn agy: {e}"}], "isError": True}

        elif name == "check_agy_status":
            run_id = args.get("run_id", "")
            res = mgr.check_status(run_id)
            return {"content": [{"type": "text", "text": json.dumps(res, indent=2)}], "isError": False}

        elif name == "get_agy_result":
            run_id = args.get("run_id", "")
            wait_sec = args.get("wait_seconds", 300)
            res = mgr.get_result(run_id, wait_seconds=wait_sec)
            status = res.get("status")
            if status == "completed":
                text = (
                    f"AGY COMPLETED ({res.get('agy_duration', 0):.1f}s)\n"
                    f"Conversation ID: {res.get('conversation_id')}\n"
                    f"Usage: {json.dumps(res.get('usage', {}))}\n\n"
                    f"--- RESULT ---\n"
                    f"{res.get('response', '')}"
                )
                return {"content": [{"type": "text", "text": text}], "isError": False}
            elif status == "timeout":
                return {"content": [{"type": "text", "text": f"AGY TIMEOUT: {res.get('error')}"}], "isError": True}
            else:
                return {"content": [{"type": "text", "text": f"AGY FAILED ({status}): {res.get('error')}"}], "isError": True}

        elif name == "stop_agy":
            run_id = args.get("run_id", "")
            res = mgr.stop(run_id)
            return {"content": [{"type": "text", "text": json.dumps(res, indent=2)}], "isError": False}

        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}

    def mcp_response(msg_id, result, error=None):
        resp = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            resp["error"] = error
        else:
            resp["result"] = result
        return resp

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method")
        msg_id = msg.get("id")

        if method == "initialize":
            resp = mcp_response(msg_id, {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION},
            })
        elif method == "tools/list":
            resp = mcp_response(msg_id, {"tools": MCP_TOOLS})
        elif method == "tools/call":
            params = msg.get("params", {})
            t_name = params.get("name")
            t_args = params.get("arguments", {})
            resp = mcp_response(msg_id, handle_tool_call(t_name, t_args))
        elif method in ("notifications/initialized", "notifications/cancelled"):
            resp = None
        elif msg_id is not None:
            resp = mcp_response(msg_id, None, error={"code": -32601, "message": f"Unknown method: {method}"})
        else:
            resp = None

        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description="Agy delegation helper CLI")
    subparsers = parser.add_subparsers(dest="subcommand")

    # spawn
    p_spawn = subparsers.add_parser("spawn", help="Spawn background agy task")
    p_spawn.add_argument("task", help="Task description / prompt")
    p_spawn.add_argument("--cwd", help="Working directory")
    p_spawn.add_argument("--model", help="Model name (e.g. gemini-3.8-flash-high)")
    p_spawn.add_argument("--effort", choices=["low", "medium", "high"], help="Reasoning effort")
    p_spawn.add_argument("--conversation", help="Resume previous conversation ID")

    # status
    p_status = subparsers.add_parser("status", help="Check status of run")
    p_status.add_argument("run_id", help="Run ID (agy-xxxxxxxx)")

    # result
    p_result = subparsers.add_parser("result", help="Wait and get result")
    p_result.add_argument("run_id", help="Run ID (agy-xxxxxxxx)")
    p_result.add_argument("--wait", type=int, default=300, help="Wait timeout in seconds")

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop running task")
    p_stop.add_argument("run_id", help="Run ID (agy-xxxxxxxx)")

    # run (sync)
    p_run = subparsers.add_parser("run", help="Run task synchronously")
    p_run.add_argument("task", help="Task description / prompt")
    p_run.add_argument("--cwd", help="Working directory")
    p_run.add_argument("--model", help="Model name")
    p_run.add_argument("--timeout", type=int, default=300)

    # test
    subparsers.add_parser("test", help="Run self-test")

    # mcp
    subparsers.add_parser("mcp", help="Run as MCP server (stdio JSON-RPC for Claude Code)")

    args = parser.parse_args()
    mgr = AgyDelegateManager()

    if args.subcommand == "mcp":
        run_mcp_server(mgr)

    elif args.subcommand == "spawn":
        res = mgr.spawn(
            task=args.task,
            cwd=args.cwd,
            model=args.model,
            effort=args.effort,
            conversation_id=args.conversation,
        )
        print(json.dumps(res, indent=2))

    elif args.subcommand == "status":
        res = mgr.check_status(args.run_id)
        print(json.dumps(res, indent=2))

    elif args.subcommand == "result":
        res = mgr.get_result(args.run_id, wait_seconds=args.wait)
        print(json.dumps(res, indent=2))

    elif args.subcommand == "stop":
        res = mgr.stop(args.run_id)
        print(json.dumps(res, indent=2))

    elif args.subcommand == "run":
        res = mgr.run_sync(
            task=args.task,
            cwd=args.cwd,
            model=args.model,
            timeout=args.timeout,
        )
        print(json.dumps(res, indent=2))

    elif args.subcommand == "test":
        print("Running agy delegation test...")
        test_task = "What is 35 * 12? Output only the numerical answer."
        print(f"Spawning task: '{test_task}'")
        spawn_res = mgr.spawn(task=test_task)
        run_id = spawn_res["run_id"]
        print(f"Spawned run_id: {run_id}, pid: {spawn_res['pid']}")
        print("Waiting for result...")
        result = mgr.get_result(run_id, wait_seconds=60)
        print("\nResult received:")
        print(json.dumps(result, indent=2))
        if result.get("status") == "completed" and "420" in result.get("response", ""):
            print("\n>>> TEST PASSED! agy delegation works perfectly! <<<")
        else:
            print("\n>>> TEST FAILED! <<<")
            sys.exit(1)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()

