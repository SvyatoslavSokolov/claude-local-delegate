"""local_backend.inspect recognises a direct Strata engine and still reads a LiteLLM gateway."""
import http.server
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

import local_backend


def serve(routes):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = routes.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.end_headers()
            self.wfile.write(json.dumps(body or {"error": "nf"}).encode())

        def log_message(self, *a):
            pass
    httpd = http.server.HTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class InspectTests(unittest.TestCase):
    def settings(self, port):
        d = tempfile.mkdtemp()
        p = os.path.join(d, 's.json')
        Path(p).write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{port}',
                                               'ANTHROPIC_MODEL': 'default_model',
                                               'ANTHROPIC_API_KEY': 'k'}}))
        return p

    def test_strata_engine(self):
        httpd = serve({'/v1/status': {'service': 'strata', 'engine': '0.1.41', 'model': 'qwen',
                                      'loaded': True, 'cache_max_tokens': 262144,
                                      'concurrency': {'serving': 1}}})
        self.addCleanup(httpd.shutdown)
        info = local_backend.inspect(self.settings(httpd.server_port))
        self.assertEqual((info['backend'], info['served_model'], info['concurrency']['serving']),
                         ('strata', 'qwen', 1))

    def test_litellm_gateway(self):
        httpd = serve({'/model/info': {'data': [{'model_name': 'default_model', 'litellm_params': {
            'model': 'hosted_vllm/q', 'api_base': 'http://x/v1'}}]}})
        self.addCleanup(httpd.shutdown)
        info = local_backend.inspect(self.settings(httpd.server_port))
        self.assertEqual(info['backend'], 'litellm')
        self.assertTrue(info['vllm_route_confirmed'])


if __name__ == '__main__':
    unittest.main()
