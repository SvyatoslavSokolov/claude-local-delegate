"""Unit tests for scripts/check_updates.py."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve()
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from scripts import check_updates


class CheckUpdatesTests(unittest.TestCase):
    def test_check_claude_detects_update(self):
        def fake_run(cmd, **kwargs):
            if "claude" in cmd:
                return MagicMock(returncode=0, stdout="2.1.270 (Claude Code)")
            elif "npm" in cmd:
                return MagicMock(returncode=0, stdout="2.1.276\n")
            return MagicMock(returncode=1)

        with patch.object(check_updates.subprocess, "run", fake_run):
            info = check_updates.check_claude()
            self.assertEqual(info["name"], "Claude Code CLI")
            self.assertEqual(info["installed"], "2.1.270")
            self.assertEqual(info["latest"], "2.1.276")
            self.assertTrue(info["update_available"])
            self.assertIn("npm install", info["update_cmd"])

    def test_check_orx_detects_up_to_date(self):
        def fake_run(cmd, **kwargs):
            return MagicMock(returncode=0, stdout="orx 0.2.4\norx is up to date.\n")

        with patch.object(check_updates, "shutil") as mock_shutil, \
             patch.object(check_updates.subprocess, "run", fake_run):
            mock_shutil.which.return_value = "/fake/bin/orx"
            info = check_updates.check_orx()
            self.assertEqual(info["installed"], "0.2.4")
            self.assertFalse(info["update_available"])

    def test_check_pytest_detects_latest(self):
        mock_pypi_resp = json.dumps({"info": {"version": "9.9.9"}}).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value.read.return_value = mock_pypi_resp
        mock_resp.__enter__.return_value = MagicMock(read=lambda: mock_pypi_resp)

        with patch.object(check_updates.urllib.request, "urlopen", return_value=mock_resp):
            with patch.dict("sys.modules", {"pytest": MagicMock(__version__="8.0.0")}):
                info = check_updates.check_pytest()
                self.assertEqual(info["installed"], "8.0.0")
                self.assertEqual(info["latest"], "9.9.9")
                self.assertTrue(info["update_available"])

    def test_format_table_renders_badges(self):
        sample = [
            {
                "name": "Component A",
                "installed": "1.0.0",
                "latest": "2.0.0",
                "update_available": True,
                "update_cmd": "update-cmd",
            },
            {
                "name": "Component B",
                "installed": "1.0.0",
                "latest": "1.0.0",
                "update_available": False,
                "update_cmd": None,
            },
        ]
        table = check_updates.format_table(sample)
        self.assertIn("Component A", table)
        self.assertIn("⚡ UPDATE -> 2.0.0", table)
        self.assertIn("✅ UP TO DATE", table)


if __name__ == "__main__":
    unittest.main()
