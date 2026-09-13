"""Unit tests for the capability-rich default tool set (v0.9).

Covers:
1. DEFAULT_ALLOWED_TOOLS is the full writer set and never contains Agent/Task.
2. DEFAULT_READ_ONLY_TOOLS classifies read-only while the full default does not.
3. _spawn_native_agent prunes Agent/Task from --tools even when explicitly named.
4. The local-worker persona's navigation order: map direct Read -> LSP -> exact
   Grep, with Web only for EXTERNAL facts.
"""
import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


HERE = Path(__file__).resolve()
ROOT = HERE.parent.parent  # claude-local-delegate/


def test_code_nav_mcp_names_are_separate_from_delegation():
    with tempfile.TemporaryDirectory() as tmp:
        server = load_server(tmp)
        assert len(server.CODE_NAV_MCP_TOOLS) == 4
        assert all(name.startswith("mcp__code-nav__")
                   for name in server.CODE_NAV_MCP_TOOLS)


def load_server(tmp):
    spec = importlib.util.spec_from_file_location('cap_server', ROOT / 'server.py')
    s = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(s)
    s.STATE_DIR = tmp
    s.PROVENANCE_PATH = os.path.join(tmp, 'runs.json')
    s.VERIFIED_DIR = os.path.join(tmp, 'verified')
    s.BATCHES_DIR = os.path.join(tmp, 'batches')
    return s


class DefaultAllowedToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = load_server(self.tmp.name)

    def test_default_contains_full_writer_set(self):
        tools = {t.strip() for t in self.s.DEFAULT_ALLOWED_TOOLS.split(',') if t.strip()}
        expected = {'Read', 'Grep', 'Glob', 'Edit', 'Write', 'Bash',
                    'WebSearch', 'WebFetch', 'LSP', 'NotebookRead', 'NotebookEdit'}
        self.assertEqual(tools, expected,
                         "DEFAULT_ALLOWED_TOOLS must be exactly the capability-rich writer set")

    def test_default_excludes_spawners(self):
        tools = {t.strip() for t in self.s.DEFAULT_ALLOWED_TOOLS.split(',') if t.strip()}
        self.assertNotIn('Agent', tools)
        self.assertNotIn('Task', tools)
        self.assertTrue(self.s.DELEGATION_BLOCKED_TOOLS <= {'Agent', 'Task'})


class ReadOnlyClassificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = load_server(self.tmp.name)

    def test_read_only_subset_is_classified_read_only(self):
        self.assertTrue(self.s._is_read_only(self.s.DEFAULT_READ_ONLY_TOOLS))

    def test_full_default_is_a_writer_not_read_only(self):
        self.assertFalse(self.s._is_read_only(self.s.DEFAULT_ALLOWED_TOOLS))

    def test_read_only_subset_has_no_write_or_shell_tools(self):
        tools = {t.strip() for t in self.s.DEFAULT_READ_ONLY_TOOLS.split(',') if t.strip()}
        for forbidden in ('Edit', 'Write', 'Bash'):
            self.assertNotIn(forbidden, tools)


class SpawnPruningTest(unittest.TestCase):
    """_spawn_native_agent strips DELEGATION_BLOCKED_TOOLS from --tools."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = load_server(self.tmp.name)
        self.settings = Path(self.tmp.name) / 'vllm.json'
        self.settings.write_text(json.dumps({'env': {
            'ANTHROPIC_BASE_URL': 'http://192.168.1.109:4000',
            'ANTHROPIC_MODEL': 'local-qwen', 'ANTHROPIC_API_KEY': 'x'}}))

    def _spawn_cmd(self, allowed_tools):
        calls = {}

        class Proc:
            returncode = 0
            stdout = b'backgrounded  aa46976f'

        def fake_run(cmd, **kwargs):
            calls['cmd'] = cmd
            return Proc()

        with patch.object(self.s, '_default_settings_path', lambda: str(self.settings)), \
             patch.object(self.s.subprocess, 'run', fake_run):
            run_id, err = self.s._spawn_native_agent(
                'task', allowed_tools, self.tmp.name, 'n')
        self.assertIsNone(err)
        return calls['cmd']

    @staticmethod
    def _tools_value(cmd):
        i = cmd.index('--tools')
        return {t for t in cmd[i + 1].split(',') if t}

    def test_explicit_agent_and_task_are_pruned_from_tools_schema(self):
        cmd = self._spawn_cmd('Read,Grep,Agent,Task,Bash')
        tools = self._tools_value(cmd)
        self.assertNotIn('Agent', tools)
        self.assertNotIn('Task', tools)
        self.assertIn('Read', tools)
        self.assertIn('Bash', tools)

    def test_default_spawn_has_no_spawner_in_tools_schema(self):
        cmd = self._spawn_cmd(None)  # None -> DEFAULT_ALLOWED_TOOLS
        tools = self._tools_value(cmd)
        self.assertNotIn('Agent', tools)
        self.assertNotIn('Task', tools)
        self.assertIn('LSP', tools)
        self.assertIn('NotebookEdit', tools)

    def test_mcp_tools_stay_out_of_builtin_tools_flag(self):
        cmd = self._spawn_cmd('Read,mcp__ParallelSearch__web_search,Agent')
        tools = self._tools_value(cmd)
        self.assertNotIn('mcp__ParallelSearch__web_search', tools)
        self.assertNotIn('Agent', tools)
        # MCP entries still reach --allowedTools untouched.
        allowed = cmd[cmd.index('--allowedTools') + 1:]
        self.assertIn('mcp__ParallelSearch__web_search', allowed)


PERSONA_PATH = os.path.expanduser('~/.claude/agents/local-worker.md')


def load_navigation_order_text():
    with open(PERSONA_PATH, encoding='utf-8') as fh:
        text = fh.read()
    m = re.search(r"CODE NAVIGATION ORDER.*?(?=\nSEARCH DISCIPLINE)", text, re.S)
    if not m:
        raise AssertionError("local-worker.md has no CODE NAVIGATION ORDER section")
    return m.group(0)


class PersonaNavigationOrderTest(unittest.TestCase):
    def test_map_direct_read_first(self):
        t = load_navigation_order_text().lower()
        self.assertRegex(t, r"first:.*mapped exact file path or symbol.*direct read",
                         "FIRST step must be a mapped anchor as a direct Read target")
        self.assertRegex(t, r"do not search for it",
                         "a mapped anchor must be opened directly, not searched")

    def test_lsp_second_grep_third(self):
        t = load_navigation_order_text().lower()
        lsp_pos = t.find('second:')
        grep_pos = t.find('third:')
        self.assertGreater(lsp_pos, -1)
        self.assertGreater(grep_pos, lsp_pos, "LSP (SECOND) must precede Grep (THIRD)")
        self.assertIn('lsp', t[lsp_pos:grep_pos])
        self.assertIn('grep', t[grep_pos:])

    def test_web_only_for_external_facts(self):
        t = load_navigation_order_text().lower()
        web_line = next(l for l in t.splitlines() if 'websearch' in l)
        self.assertIn('only for external facts', web_line)
        self.assertIn('not for navigating this repository', web_line)


if __name__ == '__main__':
    unittest.main()
