import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import code_nav_server


class ServerTest(unittest.TestCase):
    def test_only_repository_router_is_exposed(self):
        self.assertEqual(code_nav_server.TOOL["name"], "repository_route")
        json.dumps(code_nav_server.TOOL)


if __name__ == "__main__":
    unittest.main()
