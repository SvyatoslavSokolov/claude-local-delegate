"""Tests for code_nav: temp dirs only, subprocess/which mocked."""

import json
import os
import subprocess
import unittest
from unittest import mock

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import code_nav  # noqa: E402


class DiscoverRepositoryTest(unittest.TestCase):
    def test_git_toplevel(self):
        with mock.patch.object(code_nav, "_run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="/repo/top\n", stderr=""
            )
            self.assertEqual(code_nav.discover_repository("/repo/top/sub"), "/repo/top")

    def test_fallback_walks_up_to_dotgit(self):
        with mock.patch.object(code_nav, "_run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="not a repo"
            )
            with tempfile_dir() as tmp:
                nested = os.path.join(tmp, "a", "b")
                os.makedirs(nested)
                os.makedirs(os.path.join(tmp, ".git"))
                self.assertEqual(code_nav.discover_repository(nested), tmp)

    def test_fallback_map_candidate(self):
        with mock.patch.object(code_nav, "_run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr=""
            )
            with tempfile_dir() as tmp:
                mapdir = os.path.join(tmp, "docs", "design")
                os.makedirs(mapdir)
                open(os.path.join(mapdir, "repository_map.yaml"), "w").close()
                nested = os.path.join(tmp, "x", "y")
                os.makedirs(nested)
                self.assertEqual(code_nav.discover_repository(nested), tmp)


def tempfile_dir():
    import tempfile
    return tempfile.TemporaryDirectory()


# tempfile_dir defined before use at runtime; keep module-level helper above tests.


class LoadMapTest(unittest.TestCase):
    def _write_map(self, tmp, text):
        d = os.path.join(tmp, "docs", "design")
        os.makedirs(d)
        path = os.path.join(d, "repository_map.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_simple_yaml_parsed_without_pyyaml(self):
        with tempfile_dir() as tmp:
            self._write_map(tmp, (
                "name: demo\n"
                "routes:\n"
                "  - name: server\n"
                "    path: server.py\n"
                "    description: api entrypoint\n"
                "    keywords: [api, http]\n"
                "  - name: nav\n"
                "    path: code_nav.py\n"
                "    child_map: docs/design/nav_map.yaml\n"
            ))
            loaded = code_nav.load_repository_map(tmp)
            self.assertTrue(loaded["parsed"])
            names = [r.get("name") for r in loaded["routes"]]
            self.assertEqual(names, ["server", "nav"])
            self.assertEqual(loaded["routes"][0]["keywords"], ["api", "http"])

    def test_missing_map_returns_hints_only(self):
        with tempfile_dir() as tmp:
            loaded = code_nav.load_repository_map(tmp)
            self.assertFalse(loaded["parsed"])
            self.assertIsNone(loaded["map_path"])
            self.assertEqual(loaded["routes"], [])

    def test_unparseable_map_falls_back_to_hints(self):
        with tempfile_dir() as tmp:
            self._write_map(tmp, "weird: [unclosed\n  - broken\ttab: x\n")
            loaded = code_nav.load_repository_map(tmp)
            self.assertFalse(loaded["parsed"])
            self.assertIsNotNone(loaded["map_path"])
            self.assertIsInstance(loaded["content_hints"], list)


class RouteRepositoryTest(unittest.TestCase):
    def test_routes_scored_from_map_only_no_fs_walk(self):
        with tempfile_dir() as tmp:
            d = os.path.join(tmp, "docs", "design")
            os.makedirs(d)
            with open(os.path.join(d, "repository_map.yaml"), "w") as fh:
                fh.write(
                    "routes:\n"
                    "  - name: server\n"
                    "    path: server.py\n"
                    "    description: api entrypoint and http handling\n"
                    "    keywords: [api, http]\n"
                    "  - name: metrics\n"
                    "    path: metrics.py\n"
                    "    description: stats counters\n"
                )
            with mock.patch.object(code_nav.os, "walk", side_effect=AssertionError(
                    "route_repository must not walk the filesystem")):
                result = code_nav.route_repository(tmp, "api http entrypoint")
            self.assertEqual(result["matched"][0]["path"], "server.py")
            self.assertGreaterEqual(result["matched"][0]["score"], 4)

    def test_child_maps_collected(self):
        with tempfile_dir() as tmp:
            d = os.path.join(tmp, "docs", "design")
            os.makedirs(d)
            with open(os.path.join(d, "repository_map.yaml"), "w") as fh:
                fh.write(
                    "routes:\n"
                    "  - name: nav\n"
                    "    path: code_nav.py\n"
                    "    child_map: docs/design/nav_map.yaml\n"
                )
            result = code_nav.route_repository(tmp, "nav")
            self.assertIn("docs/design/nav_map.yaml",
                          [item["map"] for item in result["child_maps"]])


class SearchLiteralTest(unittest.TestCase):
    def _rg_json(self, matches):
        events = [{"type": "match", "data": {
                "path": {"text": m["path"]},
                "line_number": m["line"],
                "lines": {"text": m["text"] + "\n"},
                "submatches": [{"col": m["col"]}],
            }} for m in matches]
        return "\n".join(json.dumps(event) for event in events)

    def test_single_rg_call_excludes_and_route_paths(self):
        with tempfile_dir() as tmp:
            calls = []

            def fake_run(cmd, cwd):
                calls.append((list(cmd), str(cwd)))
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout=self._rg_json([
                        {"path": "a/b.py", "line": 3, "col": 5, "text": "needle"}
                    ]), stderr=""
                )

            with mock.patch.object(code_nav, "_run", side_effect=fake_run):
                result = code_nav.search_literal(
                    tmp, "needle", route_paths=["src"], max_results=10
                )
            self.assertEqual(len(calls), 1, "exactly one rg invocation")
            cmd, cwd = calls[0]
            self.assertEqual(cmd[0], "rg")
            for flag in ("--fixed-strings", "--line-number", "--column", "--json"):
                self.assertIn(flag, cmd)
            self.assertNotIn("--shell", cmd)
            self.assertIn("src", cmd)
            self.assertIn("needle", cmd)
            self.assertIn("--glob", cmd)
            idx_git = cmd.index("!build/") if "!build/" in cmd else None
            self.assertIsNotNone(idx_git, "standard excludes present")
            self.assertEqual(cwd, tmp)
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["matches"][0]["line"], 3)
            self.assertFalse(result["truncated"])

    def test_max_results_bounds_output_not_search(self):
        with tempfile_dir() as tmp:
            many = [
                {"path": "f%d.py" % i, "line": i, "col": 1, "text": "x"}
                for i in range(10)
            ]

            def fake_run(cmd, cwd):
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout=self._rg_json(many), stderr=""
                )

            with mock.patch.object(code_nav, "_run", side_effect=fake_run):
                bounded = code_nav.search_literal(tmp, "x", max_results=4)
                exhaustive = code_nav.search_literal(tmp, "x", max_results=4,
                                                      exhaustive=True)
            self.assertEqual(bounded["count"], 4)
            self.assertTrue(bounded["truncated"])
            self.assertEqual(exhaustive["count"], 10)
            self.assertFalse(exhaustive["truncated"])

    def test_never_shell_true(self):
        with open(os.path.join(os.path.dirname(code_nav.__file__),
                               "code_nav.py")) as fh:
            src = fh.read()
        self.assertNotIn("shell=True", src)
        self.assertIn("capture_output=True", src)


class SymbolIndexTest(unittest.TestCase):
    def test_ast_fallback_for_explicit_py_files(self):
        with tempfile_dir() as tmp:
            pyfile = os.path.join(tmp, "mod.py")
            with open(pyfile, "w") as fh:
                fh.write("def alpha():\n    pass\n\nclass Beta:\n    pass\n")
            with mock.patch.object(code_nav.shutil, "which",
                                   return_value=None):
                result = code_nav.symbol_index(tmp, paths=[pyfile])
            self.assertEqual(result["backend"], "ast")
            kinds = {(s["name"], s["kind"]) for s in result["symbols"]}
            self.assertIn(("alpha", "function"), kinds)
            self.assertIn(("Beta", "class"), kinds)

    def test_ctags_used_when_available(self):
        with tempfile_dir() as tmp:
            pyfile = os.path.join(tmp, "mod.py")
            with open(pyfile, "w") as fh:
                fh.write("def gamma():\n    pass\n")
            record = json.dumps({"_tag": "gamma", "_type": "function",
                                 "_filename": pyfile, "_line": 1})
            with mock.patch.object(code_nav.shutil, "which",
                                   return_value="/usr/bin/ctags"):
                with mock.patch.object(code_nav, "_run") as run:
                    run.return_value = subprocess.CompletedProcess(
                        args=[], returncode=0, stdout=record + "\n", stderr=""
                    )
                    result = code_nav.symbol_index(tmp, paths=[pyfile])
            self.assertEqual(result["backend"], "ctags")
            self.assertEqual(result["symbols"][0]["name"], "gamma")
            cmd = run.call_args[0][0]
            for flag in ("--output-format=json", "--fields=+nK",
                         "--extras=-F", "-f", "-"):
                self.assertIn(flag, cmd)

    def test_no_paths_means_no_implicit_root_walk(self):
        with tempfile_dir() as tmp:
            result = code_nav.symbol_index(tmp)
            self.assertEqual(result["backend"], "none")
            self.assertEqual(result["symbols"], [])

    def test_dirs_walked_with_generated_exclusions(self):
        with tempfile_dir() as tmp:
            good = os.path.join(tmp, "src")
            bad = os.path.join(tmp, "build")
            os.makedirs(good)
            os.makedirs(bad)
            with open(os.path.join(good, "ok.py"), "w") as fh:
                fh.write("def ok_func():\n    pass\n")
            with open(os.path.join(bad, "gen.py"), "w") as fh:
                fh.write("def gen_func():\n    pass\n")
            with mock.patch.object(code_nav.shutil, "which",
                                   return_value=None):
                result = code_nav.symbol_index(tmp, paths=[tmp])
            names = {s["name"] for s in result["symbols"]}
            self.assertIn("ok_func", names)
            self.assertNotIn("gen_func", names)


class DoctorTest(unittest.TestCase):
    def test_tools_map_and_recommendations(self):
        which_map = {"rg": "/usr/bin/rg", "ctags": None,
                     "pyright-langserver": None, "clangd": None, "scip": None}
        with tempfile_dir() as tmp:
            with mock.patch.object(code_nav.shutil, "which",
                                   side_effect=lambda n: which_map.get(n)):
                report = code_nav.doctor(tmp)
            self.assertTrue(report["tools"]["required"]["rg"])
            self.assertFalse(report["tools"]["optional"]["ctags"])
            self.assertFalse(report["map"]["exists"])
            self.assertFalse(any("ripgrep" in r for r in report["recommendations"]))
            self.assertTrue(any("ctags" in r for r in report["recommendations"]))
            self.assertTrue(any("repository_map.yaml" in r
                                 for r in report["recommendations"]))
            self.assertIn("container", report["environment"])

    def test_map_present_removal_of_recommendation(self):
        with tempfile_dir() as tmp:
            d = os.path.join(tmp, "docs", "design")
            os.makedirs(d)
            open(os.path.join(d, "repository_map.yaml"), "w").close()
            with mock.patch.object(code_nav.shutil, "which",
                                   return_value="/usr/bin/x"):
                report = code_nav.doctor(tmp)
            self.assertTrue(report["map"]["exists"])
            self.assertFalse(any("repository_map.yaml" in r
                                  for r in report["recommendations"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
