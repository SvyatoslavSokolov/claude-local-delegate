import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import code_nav


class RepositoryRouterTest(unittest.TestCase):
    def test_routes_without_walking_repository(self):
        with tempfile.TemporaryDirectory() as root:
            maps = os.path.join(root, "docs", "design", "repository_maps")
            os.makedirs(maps)
            with open(os.path.join(root, "docs", "design", "repository_map.yaml"), "w") as out:
                out.write("task_routes:\n  - id: camera\n    matches: camera ROS publisher\n"
                          "    child_maps: [sensors]\n")
            with open(os.path.join(maps, "sensors.yaml"), "w") as out:
                out.write("id: sensors\npath: source/sensors\nentry_points:\n"
                          "  - path: camera.py\n    role: publisher\nkey_paths: [config]\n")
            completed = subprocess.CompletedProcess([], 1, "", "")
            with mock.patch.object(code_nav, "_run", return_value=completed), \
                    mock.patch.object(code_nav.os, "walk",
                                      side_effect=AssertionError("must not walk")):
                result = code_nav.route_repository(root, "camera publisher")
            self.assertEqual(result["matched"][0]["id"], "camera")
            self.assertEqual(result["route_paths"],
                             ["source/sensors/camera.py", "source/sensors/config"])
            self.assertIn("Serena", result["next"])

    def test_no_map_is_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            completed = subprocess.CompletedProcess([], 1, "", "")
            with mock.patch.object(code_nav, "_run", return_value=completed):
                result = code_nav.route_repository(root, "anything")
            self.assertIsNone(result["map_path"])
            self.assertEqual(result["route_paths"], [])


if __name__ == "__main__":
    unittest.main()
