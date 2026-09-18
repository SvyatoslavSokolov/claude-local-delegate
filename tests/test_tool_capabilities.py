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
sys.path.insert(0, str(ROOT))


def test_navigation_mcp_names_are_read_only_and_separate_from_delegation():
    with tempfile.TemporaryDirectory() as tmp:
        server = load_server(tmp)
        # code-nav is only the repository-map router now; Serena owns semantics.
        assert server.CODE_NAV_MCP_TOOLS == ("mcp__code-nav__repository_route",)
        assert set(server.SERENA_READ_ONLY_MCP_TOOLS) == {
            "mcp__serena__activate_project",
            "mcp__serena__initial_instructions",
            "mcp__serena__get_current_config",
            "mcp__serena__get_symbols_overview",
            "mcp__serena__find_symbol",
            "mcp__serena__find_referencing_symbols",
            "mcp__serena__find_declaration",
            "mcp__serena__find_implementations",
            "mcp__serena__get_diagnostics_for_file",
            "mcp__serena__list_memories",
            "mcp__serena__read_memory",
        }
        # Every granted navigation MCP tool must be read-only (in READ_ONLY_TOOLS),
        # so a read-only allowlist stays safe under dontAsk.
        assert set(server.NAVIGATION_MCP_TOOLS) <= server.READ_ONLY_TOOLS
        assert not any(t.endswith("__write_memory") or t.endswith("__edit_memory")
                       or t.endswith("__delete_memory") or t.endswith("__rename_memory")
                       for t in server.NAVIGATION_MCP_TOOLS)
        assert "mcp__serena__onboarding" not in server.NAVIGATION_MCP_TOOLS


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
                    'WebSearch', 'WebFetch', 'LSP', 'NotebookRead', 'NotebookEdit',
                    'Skill', 'SendMessage', 'ListAgents', 'TodoWrite', 'ReportFindings',
                    'ScheduleWakeup', 'WaitForMcpServers', 'EnterWorktree', 'ExitWorktree',
                    'CronCreate', 'CronDelete', 'CronList'}
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

    def test_mcp_only_allowlist_explicitly_disables_builtin_schemas(self):
        cmd = self._spawn_cmd('mcp__serena__find_symbol')
        i = cmd.index('--tools')
        self.assertEqual(cmd[i + 1], '')

    def test_read_only_mcp_run_denies_ungranted_builtins_and_serena_edits(self):
        cmd = self._spawn_cmd('mcp__serena__find_symbol')
        denied = set(cmd[cmd.index('--disallowedTools') + 1:])
        self.assertIn('Grep', denied)
        self.assertIn('Bash', denied)
        self.assertIn('mcp__serena__replace_in_files', denied)
        self.assertIn('mcp__serena__onboarding', denied)

    def test_read_only_granted_builtin_is_not_denied(self):
        cmd = self._spawn_cmd('Read,mcp__serena__find_symbol')
        denied = set(cmd[cmd.index('--disallowedTools') + 1:])
        self.assertNotIn('Read', denied)


PERSONA_PATH = os.path.expanduser('~/.claude/agents/local-worker.md')


def load_navigation_order_text():
    with open(PERSONA_PATH, encoding='utf-8') as fh:
        text = fh.read()
    m = re.search(r"CODE NAVIGATION ORDER.*?(?=\nSEARCH DISCIPLINE)", text, re.S)
    if not m:
        raise AssertionError("local-worker.md has no CODE NAVIGATION ORDER section")
    return m.group(0)


class PersonaNavigationOrderTest(unittest.TestCase):
    def test_map_router_then_direct_anchor(self):
        t = load_navigation_order_text().lower()
        self.assertRegex(t, r"first for every repository task:.*repository_route",
                         "FIRST step must route through the repository map")
        self.assertIn("mapped exact file path or symbol", t)
        self.assertRegex(t, r"do not search for it",
                         "a mapped anchor must be opened directly, not searched")

    def test_serena_precedes_text_search(self):
        t = load_navigation_order_text().lower()
        serena_pos = t.find('activate_project')
        grep_pos = t.find('built-in grep')
        self.assertGreater(serena_pos, -1)
        self.assertGreater(grep_pos, serena_pos,
                           "Serena semantic navigation must precede text search")

    def test_literals_are_not_guessed_as_symbols(self):
        # Exact strings (CLI flags, config keys, error messages) go to a TEXT
        # search (Grep / one scoped rg), NOT to LSP symbol search. Serena has no
        # text-search tool (there is no search_for_pattern in the installed serena
        # and none is granted), so the persona must say so and point to Grep/rg.
        t = load_navigation_order_text().lower()
        self.assertIn("exact string", t)
        self.assertIn("serena has no text search", t)
        self.assertIn("lsp symbol search is not a text search", t)
        self.assertIn("stop when every edge", t)
        # The phantom tool must not be advertised in the persona.
        self.assertNotIn("search_for_pattern", t)

    def test_web_only_for_external_facts(self):
        t = load_navigation_order_text().lower()
        web_line = next(l for l in t.splitlines() if 'websearch' in l)
        self.assertIn('only for external facts', web_line)
        self.assertIn('not for navigating this repository', web_line)


if __name__ == '__main__':
    unittest.main()
