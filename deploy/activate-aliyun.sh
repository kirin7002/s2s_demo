#!/usr/bin/env bash
# Run by the server administrator only after reviewing the public-site change.
set -euo pipefail
umask 077

release_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
env_file=/home/kirin/apps/s2s_demo/shared/.env
caddy_file=/opt/caddy/Caddyfile
domain=s2s.kirinxu.cn
container=s2s-demo
image="s2s-demo:$(basename -- "$release_dir")"

if [[ ${1:-} != --approve-public-site || $EUID -ne 0 ]]; then
    echo 'Requires administrator execution with --approve-public-site.' >&2
    echo 'Adds only https://s2s.kirinxu.cn to existing Caddy; no firewall changes.' >&2
    exit 1
fi

# Read secrets only on the server, and never print values or put them in argv.
PYTHONPATH="$release_dir" python3 - "$env_file" <<'PY'
from pathlib import Path
import os
import stat
import sys
from gateway.config import Config
from gateway.envfile import ALLOWED_KEYS
for name in ALLOWED_KEYS:
    os.environ.pop(name, None)
p = Path(sys.argv[1])
if not p.is_file() or stat.S_IMODE(p.stat().st_mode) != 0o600:
    raise SystemExit('Remote .env must exist with mode 0600.')
try:
    c = Config.from_env(p)
except (ValueError, OSError):
    raise SystemExit('Remote .env cannot be parsed; no values shown.') from None
if not (c.api_key.strip() and c.demo_password.strip() and c.problem() is None
        and c.model == 'qwen-audio-3.0-realtime-flash'
        and c.origin == 'https://s2s.kirinxu.cn'):
    raise SystemExit('Fill the two remote secrets and retain the expected Flash/HTTPS settings.')
print('remote_configuration_valid=true')
PY

docker inspect caddy --format '{{json .}}' | python3 -c '
import json, sys
c = json.load(sys.stdin)
assert c["State"]["Running"], "Existing Caddy is not running"
assert any(m["Source"] == "/opt/caddy/Caddyfile" and m["Destination"] == "/etc/caddy/Caddyfile" for m in c["Mounts"]), "Unexpected Caddy mount"
print("existing_proxy_verified=true")'

if docker container inspect "$container" >/dev/null 2>&1; then
    echo 'Container s2s-demo already exists; no existing container was changed.' >&2
    exit 1
fi
if grep -Eq '^[[:space:]]*s2s\.kirinxu\.cn[[:space:]]*\{' "$caddy_file"; then
    echo 'Target domain already has a Caddy block; no existing site was changed.' >&2
    exit 1
fi

docker build --tag "$image" "$release_dir"
# Loopback in Caddy's namespace must be free. No host port is published.
docker run --rm --network container:caddy --entrypoint python "$image" -c \
    'import socket; s=socket.socket(); s.bind(("127.0.0.1",8080)); s.close(); print("internal_port_available=true")'

created=0
changed=0
backup=''
rollback() {
    result=$?
    if [[ $result -ne 0 ]]; then
        if [[ $changed -eq 1 ]]; then
            # Preserve the inode of the single-file Docker bind mount.
            cat "$backup" > "$caddy_file"
            docker exec caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || true
        fi
        if [[ $created -eq 1 ]]; then
            if [[ $(docker inspect "$container" --format '{{index .Config.Labels "com.kirin.project"}}' 2>/dev/null) == s2s_demo ]]; then
                docker rm -f "$container" >/dev/null 2>&1 || true
            fi
        fi
        echo 'Activation failed; the new app was removed and any Caddy edit restored.' >&2
    fi
}
trap rollback EXIT

created=1
PYTHONPATH="$release_dir" python3 - "$env_file" "$image" <<'PY'
import os
import subprocess
import sys
from gateway.envfile import ALLOWED_KEYS, read_env_file
values = read_env_file(sys.argv[1])
environment = os.environ.copy()
for name in ALLOWED_KEYS:
    environment.pop(name, None)
environment.update(values)
args = ['docker', 'run', '-d', '--name', 's2s-demo', '--label', 'com.kirin.project=s2s_demo',
        '--network', 'container:caddy', '--restart', 'unless-stopped', '--read-only',
        '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges:true', '--pids-limit', '64',
        '--memory', '256m', '--cpus', '0.5']
for name in sorted(values):
    args.extend(['--env', name])  # Docker reads the value from this process's env.
args.append(sys.argv[2])
subprocess.run(args, env=environment, stdout=subprocess.DEVNULL, check=True)
PY
# Local health only. Never open /ws or invoke a paid model automatically.
docker exec "$container" python -c '
import json, time, urllib.request
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
for attempt in range(20):
    try:
        with opener.open("http://127.0.0.1:8080/login", timeout=2) as r:
            assert r.status == 200
        with opener.open("http://127.0.0.1:8080/api/config", timeout=2) as r:
            c=json.load(r)
            assert c["ready"] and c["password_required"] and c["model"] == "qwen-audio-3.0-realtime-flash"
        print("internal_login_http=200; flash_configuration_ready=true")
        break
    except Exception:
        if attempt == 19:
            raise SystemExit("Local app health failed; no secret details shown.") from None
        time.sleep(0.25)'

backup="$(mktemp /opt/caddy/Caddyfile.pre-s2s.XXXXXX)"
cat "$caddy_file" > "$backup"
changed=1
printf '\n' >> "$caddy_file"
cat "$release_dir/deploy/Caddyfile.s2s" >> "$caddy_file"
if ! docker exec caddy caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
    echo 'Caddy validation failed; restoring its previous file.' >&2
    exit 1
fi
if ! docker exec caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
    echo 'Caddy reload failed; restoring its previous file.' >&2
    exit 1
fi
trap - EXIT
echo "Application started. Caddy certificate issuance must still be verified: https://$domain"
echo "Rollback config backup: $backup"
echo 'No model handshake, audio or generation was performed.'
