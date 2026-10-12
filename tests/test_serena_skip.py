"""Delegates whose explicit allowlist names no Serena tool start without Serena (CLAUDE_NO_SERENA=1)."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class SerenaSkipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"CLAUDE_LOCAL_DELEGATE_STATE_DIR": self.tmp.name}); env.start(); self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location("serena_skip_server", Path(__file__).resolve().parent.parent / "server.py")
        self.s = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.s)

    def _spawn_env(self, allowed_tools):
        seen = {}

        class Stop(Exception):
            pass

        def fake_run(cmd, *a, **kw):
            seen["env"] = kw.get("env") or {}
            raise Stop()

        with patch.object(self.s.subprocess, "run", side_effect=fake_run):
            try:
                self.s._spawn_native_agent("task", allowed_tools, self.tmp.name, "t")
            except Stop:
                pass
            except Exception:  # anything after the launch call is irrelevant here
                pass
        self.assertIn("env", seen, "spawner never reached the launch call")
        return seen["env"]

    def test_helper(self):
        self.assertFalse(self.s._serena_needed(["Read", "WebFetch"]))
        self.assertTrue(self.s._serena_needed(["Read", "mcp__serena__find_symbol"]))

    def test_explicit_allowlist_without_serena_skips_it(self):
        self.assertEqual(self._spawn_env("Read,Write,Bash,WebSearch,WebFetch").get("CLAUDE_NO_SERENA"), "1")

    def test_default_allowlist_keeps_serena(self):
        self.assertNotIn("CLAUDE_NO_SERENA", self._spawn_env(None))

    def test_explicit_default_allowlist_keeps_serena(self):
        # start_delegate and the deep tools pass the default set explicitly.
        self.assertNotIn("CLAUDE_NO_SERENA", self._spawn_env(self.s.DEFAULT_ALLOWED_TOOLS))

    def test_explicit_allowlist_with_serena_keeps_it(self):
        self.assertNotIn("CLAUDE_NO_SERENA", self._spawn_env("Read,mcp__serena__find_symbol"))


if __name__ == "__main__":
    unittest.main()


class DelegateHookShimTests(unittest.TestCase):
    """Delegates resolve `serena-hooks` to a shim that skips `remind` (it denied Grep/Read and
    looped unattended local models) and runs the real tool for every other subcommand."""
    setUp = SerenaSkipTests.setUp
    _spawn_env = SerenaSkipTests._spawn_env

    def test_spawn_env_marks_worker_and_puts_shim_first(self):
        env = self._spawn_env(None)
        self.assertEqual(env.get("CLAUDE_LOCAL_DELEGATE_WORKER"), "1")
        self.assertEqual(env["PATH"].split(os.pathsep)[0], self.s.DELEGATE_BIN_DIR)

    def test_shim_skips_remind_and_forwards_the_rest(self):
        import subprocess
        fake = Path(self.tmp.name) / "realbin"
        fake.mkdir()
        (fake / "serena-hooks").write_text('#!/bin/sh\necho "real $*"\n')
        (fake / "serena-hooks").chmod(0o755)
        env = {"PATH": os.pathsep.join([self.s.DELEGATE_BIN_DIR, str(fake), "/usr/bin", "/bin"])}
        remind = subprocess.run(["serena-hooks", "remind", "--client=claude-code"], env=env,
                                input="{}", capture_output=True, text=True)
        self.assertEqual((remind.returncode, remind.stdout), (0, ""))
        other = subprocess.run(["serena-hooks", "activate", "--client=claude-code"], env=env,
                               capture_output=True, text=True, timeout=10)
        self.assertEqual(other.stdout.strip(), "real activate --client=claude-code")
