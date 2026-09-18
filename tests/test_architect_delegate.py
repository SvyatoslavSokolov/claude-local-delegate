"""Unit tests for Tier 1 Gemini Architect delegation and hierarchical tree monitoring."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve()
ROOT = HERE.parent.parent  # claude-local-delegate/
sys.path.insert(0, str(ROOT))

from tests.test_tool_capabilities import load_server


class ArchitectDelegationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = load_server(self.tmp.name)

        # Create dummy settings files
        self.gemini_settings = Path(self.tmp.name) / "gemini.json"
        self.gemini_settings.write_text(json.dumps({
            "env": {
                "ANTHROPIC_BASE_URL": "http://192.168.1.109:4000",
                "ANTHROPIC_MODEL": "claude-3-7-sonnet-20250219",
                "ANTHROPIC_API_KEY": "vllm-local",
            }
        }))
        self.s.DEFAULT_GEMINI_SETTINGS_PATH = str(self.gemini_settings)

    def test_tools_list_contains_architect_and_tree(self):
        tools = {t["name"] for t in self.s.PARENT_TOOLS}
        self.assertIn("delegate_to_architect", tools)
        self.assertIn("show_agent_tree", tools)

    def test_spawn_architect_role_preserves_delegation_mcp(self):
        calls = {}

        class Proc:
            returncode = 0
            stdout = b"backgrounded \xc2\xb7 aa000001"

        def fake_run(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["env"] = kwargs.get("env", {})
            return Proc()

        with patch.object(self.s.subprocess, "run", fake_run):
            run_id, err = self.s._spawn_native_agent(
                task="Plan architecture",
                allowed_tools=None,
                cwd=self.tmp.name,
                name="arch-test",
                role="architect",
                settings_override=str(self.gemini_settings),
            )

        self.assertIsNone(err)
        self.assertEqual(run_id, "aa000001")

        cmd = calls["cmd"]
        # In disallowedTools, whole mcp__claude-local-delegate must NOT be present
        disallowed_idx = cmd.index("--disallowedTools")
        disallowed = set(cmd[disallowed_idx + 1:])
        self.assertNotIn("mcp__claude-local-delegate", disallowed)
        # But recursive architect tools must be disallowed
        self.assertIn("mcp__claude-local-delegate__delegate_to_architect", disallowed)

        # Check provenance recorded backend as gemini and role as architect
        prov = self.s._provenance_for("aa000001")
        self.assertIsNotNone(prov)
        self.assertEqual(prov.get("role"), "architect")
        self.assertEqual(prov.get("backend"), "gemini")
        self.assertEqual(prov.get("parent_id"), "supervisor")

    def test_worker_spawned_by_architect_records_parent_id(self):
        calls = {}

        class Proc:
            returncode = 0
            stdout = b"backgrounded \xc2\xb7 bb000002"

        def fake_run(cmd, **kwargs):
            calls["cmd"] = cmd
            return Proc()

        # Pre-seed provenance with an architect run that has token "tok123"
        self.s._record_provenance("aa000001", {
            "role": "architect",
            "backend": "gemini",
            "token": "tok123",
            "at": 100.0,
        })

        # Simulate child worker being spawned from an environment where CLAUDE_LOCAL_DELEGATE_PARENT_TOKEN is set
        with patch.dict(os.environ, {"CLAUDE_LOCAL_DELEGATE_PARENT_TOKEN": "tok123"}), \
             patch.object(self.s, "_default_settings_path", lambda: str(self.gemini_settings)), \
             patch.object(self.s.subprocess, "run", fake_run):
            run_id, err = self.s._spawn_native_agent(
                task="Atomic edit task",
                allowed_tools=None,
                cwd=self.tmp.name,
                name="worker-test",
                role="worker",
            )

        self.assertIsNone(err)
        self.assertEqual(run_id, "bb000002")

        prov = self.s._provenance_for("bb000002")
        self.assertIsNotNone(prov)
        self.assertEqual(prov.get("role"), "worker")
        self.assertEqual(prov.get("parent_id"), "aa000001")

        # Worker must have mcp__claude-local-delegate disallowed
        cmd = calls["cmd"]
        disallowed_idx = cmd.index("--disallowedTools")
        disallowed = set(cmd[disallowed_idx + 1:])
        self.assertIn("mcp__claude-local-delegate", disallowed)

    def test_start_architect_delegate_handler(self):
        with patch.object(self.s, "_spawn_native_agent", return_value=("arch1234", None)):
            res = self.s.start_architect_delegate({"task": "High-level design"})
            self.assertFalse(res.get("isError"))
            self.assertIn("arch1234", res["content"][0]["text"])
            self.assertIn("Tier 1 Gemini Architect", res["content"][0]["text"])

    def test_start_delegate_with_profile_architect_routes_to_architect(self):
        with patch.object(self.s, "start_architect_delegate", return_value={"isError": False, "content": [{"type": "text", "text": "routed"}]}) as mock_arch:
            res = self.s.start_delegate({"task": "High-level design", "profile": "architect"})
            self.assertEqual(res["content"][0]["text"], "routed")
            mock_arch.assert_called_once()

    def test_show_agent_tree_handler(self):
        res = self.s.show_agent_tree_handler({})
        self.assertFalse(res.get("isError"))
        text = res["content"][0]["text"]
        self.assertIn("MULTI-AGENT LOCAL HARNESS MONITOR", text)
        self.assertIn("TIER 1: HYPERVISOR & ARCHITECT", text)

    def test_resolve_cwd_valid(self):
        resolved, err = self.s._resolve_cwd(self.tmp.name)
        self.assertIsNone(err)
        self.assertEqual(resolved, os.path.abspath(self.tmp.name))

    def test_resolve_cwd_invalid(self):
        resolved, err = self.s._resolve_cwd("/nonexistent/directory/xyz123")
        self.assertIsNone(resolved)
        self.assertIn("does not exist", err)

    def test_slug_from_task_cleaning(self):
        slug = self.s._slug_from_task("You are a tier 0 worker. Please refactor adapters in claude-local-delegate")
        self.assertEqual(slug, "refactor adapters in claude-local-delegate")

        slug_md = self.s._slug_from_task("# Task: Implement VHAL pipeline\nDetails below...")
        self.assertEqual(slug_md, "Implement VHAL pipeline")

        slug_ru = self.s._slug_from_task("## Задача: Проверить документацию и тесты")
        self.assertEqual(slug_ru, "Проверить документацию и тесты")

    def test_format_agent_name_prefixes(self):
        name_worker = self.s._format_agent_name(task="Fix pytest failures", role="worker")
        self.assertTrue(name_worker.startswith("⚡ [LOCAL]"))

        name_fast = self.s._format_agent_name(task="Quick lint check", profile="fast")
        self.assertTrue(name_fast.startswith("⚡ [FAST]"))

        name_arch = self.s._format_agent_name(task="Decompose big refactor", role="architect")
        self.assertTrue(name_arch.startswith("🧠 [ARCH]"))

        # User custom name preservation
        name_custom = self.s._format_agent_name(name="my-custom-task", role="worker")
        self.assertEqual(name_custom, "⚡ [LOCAL] my-custom-task")

        # Strip legacy gemini-architect-
        name_legacy = self.s._format_agent_name(name="gemini-architect-my-plan", role="architect")
        self.assertEqual(name_legacy, "🧠 [ARCH] my-plan")


if __name__ == "__main__":
    unittest.main()
