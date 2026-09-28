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

    def test_explicit_allowlist_with_serena_keeps_it(self):
        self.assertNotIn("CLAUDE_NO_SERENA", self._spawn_env("Read,mcp__serena__find_symbol"))


if __name__ == "__main__":
    unittest.main()
