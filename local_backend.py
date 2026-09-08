"""Load the explicit local profile without leaking credentials into diagnostics."""
import json
import os
from pathlib import Path
import urllib.request


def profile(path):
    data = json.loads(Path(path).expanduser().read_text())
    env = data.get('env', {})
    for key in ('ANTHROPIC_BASE_URL', 'ANTHROPIC_MODEL', 'ANTHROPIC_API_KEY'):
        if not isinstance(env.get(key), str) or not env[key].strip():
            raise ValueError('Local profile must explicitly set ' + key)
    if not env['ANTHROPIC_BASE_URL'].startswith(('http://', 'https://')):
        raise ValueError('Local backend URL must use HTTP(S)')
    return env


def environment(path):
    env = os.environ.copy()
    for key in ('CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_AUTH_TOKEN',
                'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
        env.pop(key, None)
    env.update({k: str(v) for k, v in profile(path).items() if not k.startswith('//')})
    return env


def inspect(path):
    env = profile(path)
    base = env['ANTHROPIC_BASE_URL'].rstrip('/')
    req = urllib.request.Request(base + '/model/info',
                                headers={'Authorization': 'Bearer ' + env['ANTHROPIC_API_KEY']})
    with urllib.request.urlopen(req, timeout=15) as response:
        data = json.load(response)
    routes = []
    for item in data.get('data', []):
        if item.get('model_name') == env['ANTHROPIC_MODEL']:
            params = item.get('litellm_params', {})
            routes.append({'model': params.get('model'), 'api_base': params.get('api_base')})
    return {'settings_path': str(Path(path).expanduser()), 'base_url': base,
            'requested_model': env['ANTHROPIC_MODEL'], 'routes': routes,
            'vllm_route_confirmed': bool(routes) and all(str(r['model']).startswith('hosted_vllm/') for r in routes),
            'note': 'Gateway configuration evidence; not per-request tracing or proof of the loaded weight files.'}
