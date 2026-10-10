"""Load the explicit local profile without leaking credentials into diagnostics."""
import json
import os
from pathlib import Path
import urllib.request
import urllib.error


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
    headers = {'Authorization': 'Bearer ' + env['ANTHROPIC_API_KEY']}
    # A Strata engine answers /v1/status itself; a LiteLLM gateway answers /model/info.
    try:
        with urllib.request.urlopen(urllib.request.Request(base + '/v1/status', headers=headers),
                                    timeout=15) as response:
            status = json.load(response)
    except (urllib.error.URLError, ValueError):
        status = None
    if isinstance(status, dict) and status.get('service') == 'strata':
        return {'settings_path': str(Path(path).expanduser()), 'base_url': base,
                'requested_model': env['ANTHROPIC_MODEL'], 'backend': 'strata',
                'engine': status.get('engine'), 'served_model': status.get('model'),
                'loaded': status.get('loaded'), 'context_max': status.get('cache_max_tokens'),
                'concurrency': status.get('concurrency'),
                'note': 'Direct Strata engine (no gateway); Strata serves any requested model name.'}
    req = urllib.request.Request(base + '/model/info', headers=headers)
    with urllib.request.urlopen(req, timeout=15) as response:
        data = json.load(response)
    routes = []
    for item in data.get('data', []):
        if item.get('model_name') == env['ANTHROPIC_MODEL']:
            params = item.get('litellm_params', {})
            routes.append({'model': params.get('model'), 'api_base': params.get('api_base')})
    return {'settings_path': str(Path(path).expanduser()), 'base_url': base,
            'requested_model': env['ANTHROPIC_MODEL'], 'backend': 'litellm', 'routes': routes,
            'vllm_route_confirmed': bool(routes) and all(str(r['model']).startswith('hosted_vllm/') for r in routes),
            'note': 'Gateway configuration evidence; not per-request tracing or proof of the loaded weight files.'}


def doctor_check(settings_path=None, timeout=5.0):
    if settings_path is None:
        settings_path = os.path.expanduser('~/.claude/vllm.delegate.settings.json')
    base_url = None
    model = None
    try:
        env = profile(settings_path)
        base_url = env['ANTHROPIC_BASE_URL']
        model = env['ANTHROPIC_MODEL']
    except Exception as exc:
        return {'ok': False, 'base_url': base_url, 'model': model,
                'status': 'config-error', 'error': str(exc)}
    try:
        urllib.request.urlopen(base_url, timeout=timeout)
    except Exception as exc:
        return {'ok': False, 'base_url': base_url, 'model': model,
                'status': 'unreachable', 'error': str(exc)}
    return {'ok': True, 'base_url': base_url, 'model': model, 'status': 'ok', 'error': None}
