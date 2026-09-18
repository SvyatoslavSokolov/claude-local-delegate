"""Unit tests for OpenResearch (orx) adapter and server research tools."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve()
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

import adapters.orx as orx
import server


class OrxAdapterTests(unittest.TestCase):
    def test_find_orx_bin_detects_system_or_cargo(self):
        binary = orx.find_orx_bin()
        # Binary should be discovered on this machine
        self.assertIsNotNone(binary)
        self.assertTrue(os.path.isfile(binary))

    def test_orx_version_returns_string(self):
        version = orx.orx_version()
        self.assertIsNotNone(version)
        self.assertTrue(version.startswith("orx"))

    def test_discover_papers_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            orx.discover_papers("test query", mode="invalid_mode")

    def test_discover_papers_parses_json(self):
        mock_output = json.dumps([
            {
                "source": "alphaxiv",
                "id": "2608.12345",
                "title": "Test Multi-Agent Paper",
                "abstract": "This is a test abstract.",
                "publicationDate": "2026-08-15T00:00:00.000Z",
            }
        ])
        with patch.object(orx.orx_adapter.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")
            papers = orx.discover_papers("multi-agent", mode="keyword", limit=1)
            self.assertEqual(len(papers), 1)
            self.assertEqual(papers[0]["id"], "2608.12345")
            self.assertEqual(papers[0]["title"], "Test Multi-Agent Paper")

            # Verify arguments passed to orx
            cmd = mock_run.call_args[0][0]
            self.assertIn("discover", cmd)
            self.assertIn("keyword", cmd)
            self.assertIn("multi-agent", cmd)
            self.assertIn("--no-telemetry", cmd)

    def test_fetch_paper_calls_orx_paper(self):
        with patch.object(orx.orx_adapter.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="Paper full content...", stderr="")
            res = orx.fetch_paper("2608.12345", full_text=True)
            self.assertEqual(res, "Paper full content...")
            cmd = mock_run.call_args[0][0]
            self.assertIn("paper", cmd)
            self.assertIn("2608.12345", cmd)
            self.assertIn("--full", cmd)


class ServerResearchHandlersTests(unittest.TestCase):
    def test_research_status_handler_success(self):
        res = server.research_status_handler({})
        self.assertFalse(res.get("isError"))
        text = res["content"][0]["text"]
        self.assertIn("OpenResearch (`orx`) is ready", text)

    def test_research_discover_handler_empty_query(self):
        res = server.research_discover_handler({"query": ""})
        self.assertTrue(res.get("isError"))

    def test_research_discover_handler_formatted_markdown(self):
        mock_papers = [
            {
                "source": "alphaxiv",
                "id": "2608.99999",
                "title": "Robotics Whole Body Control",
                "abstract": "Abstract of the WBC paper.",
                "publicationDate": "2026-08-20",
                "snippets": [{"snippet": "WBC algorithms for quadrupeds."}],
            }
        ]
        with patch("adapters.orx.discover_papers", return_value=mock_papers):
            res = server.research_discover_handler({"query": "robotics", "limit": 1})
            self.assertFalse(res.get("isError"))
            text = res["content"][0]["text"]
            self.assertIn("Robotics Whole Body Control", text)
            self.assertIn("2608.99999", text)
            self.assertIn("https://www.alphaxiv.org/abs/2608.99999", text)

    def test_research_paper_handler_missing_id(self):
        res = server.research_paper_handler({})
        self.assertTrue(res.get("isError"))

    def test_research_paper_handler_success(self):
        with patch("adapters.orx.fetch_paper", return_value="Title: Mock Paper\nContent"):
            res = server.research_paper_handler({"paper_id": "2608.99999"})
            self.assertFalse(res.get("isError"))
            self.assertEqual(res["content"][0]["text"], "Title: Mock Paper\nContent")


if __name__ == "__main__":
    unittest.main()
