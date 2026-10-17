"""Read-only Docker/Caddy status; print fixed fields, never raw configuration."""
import json
import subprocess

DOMAIN = 's2s.kirinxu.cn'
LE = 'https://acme-v02.api.letsencrypt.org/directory'


def summarize(config):
    apps = config.get('apps', {})
    servers = apps.get('http', {}).get('servers', {})
    target_servers = []

    def targets(node):
        if isinstance(node, dict):
            hosts = node.get('host', [])
            if isinstance(hosts, list) and DOMAIN in hosts:
                return True
            return any(targets(value) for value in node.values())
        return isinstance(node, list) and any(targets(value) for value in node)

    for server in servers.values():
        if targets(server.get('routes', [])):
            target_servers.append(server)
    policies = apps.get('tls', {}).get('automation', {}).get('policies', [])
    target_policies = [policy for policy in policies if DOMAIN in policy.get('subjects', [])]
    return {
        'active_s2s_route_present': bool(target_servers),
        'active_s2s_auto_https_disabled': any(
            server.get('automatic_https', {}).get('disable', False)
            or server.get('automatic_https', {}).get('disable_certificates', False)
            for server in target_servers),
        'active_s2s_explicit_policy_present': bool(target_policies),
        'active_s2s_le_issuer_present': any(
            issuer.get('module') == 'acme' and issuer.get('ca') == LE
            for policy in target_policies for issuer in policy.get('issuers', [])),
    }


def run_json(args):
    result = subprocess.run(args, capture_output=True, timeout=15)
    if result.returncode:
        return None
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError):
        return None


def main():
    state = run_json(['docker', 'inspect', 'caddy', '--format', '{{json .State}}'])
    print('caddy_running=' + str(bool(state and state.get('Running'))).lower())
    driver = run_json(['docker', 'inspect', 'caddy', '--format', '{{json .HostConfig.LogConfig.Type}}'])
    # Do not expose arbitrary driver options, paths or labels.
    print('caddy_log_driver=' + (driver if driver in {'json-file', 'local', 'journald', 'none'} else 'other'))
    config = run_json(['docker', 'exec', 'caddy', 'wget', '-qO-',
                       'http://127.0.0.1:2019/config/'])
    print('active_config_readable=' + str(isinstance(config, dict)).lower())
    if isinstance(config, dict):
        for key, value in summarize(config).items():
            print(key + '=' + str(value).lower())


if __name__ == '__main__':
    try:
        main()
    except (OSError, subprocess.TimeoutExpired, AttributeError, TypeError):
        print('runtime_summary_unavailable=true')
