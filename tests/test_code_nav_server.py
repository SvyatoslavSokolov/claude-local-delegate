import json
import unittest
from unittest import mock

import code_nav_server


class ServerTest(unittest.TestCase):
    def test_tools_are_small_and_explicit(self):
        self.assertEqual({t["name"] for t in code_nav_server.TOOLS}, {
            "repository_route", "search_literal", "symbol_index", "code_nav_doctor"
        })

    @mock.patch("code_nav_server.code_nav.discover_repository", return_value="/repo")
    @mock.patch("code_nav_server.code_nav.route_repository", return_value={"matched": []})
    def test_route_dispatch(self, route, discover):
        self.assertEqual(code_nav_server.invoke("repository_route", {
            "root": "/repo/sub", "query": "camera"
        }), {"matched": []})
        route.assert_called_once_with("/repo", "camera")

    def test_tools_serialize(self):
        json.dumps(code_nav_server.TOOLS)


if __name__ == "__main__":
    unittest.main()
