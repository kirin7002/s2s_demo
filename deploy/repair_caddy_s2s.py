"""Guarded administrator repair for the already-approved S2S Caddy site."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

try:
    from .caddy_runtime_summary import summarize
except ImportError:
    from caddy_runtime_summary import summarize

HOST = Path('/opt/caddy/Caddyfile')
MOUNT = '/etc/caddy/Caddyfile'


class RepairError(Exception):
    pass


def docker(*args, data=None):
    result = subprocess.run(['docker', *args], input=data, capture_output=True, timeout=30)
    if result.returncode:
        raise RepairError('Docker/Caddy command failed; raw output withheld.')
    return result.stdout


def execute(*args, data=None):
    return docker('exec', '-i', '-w', '/etc/caddy', 'caddy', *args, data=data)


def active_config():
    return json.loads(execute('wget', '-qO-', 'http://127.0.0.1:2019/config/'))


def adapt(data):
    return json.loads(execute('caddy', 'adapt', '--config', '-', '--adapter', 'caddyfile', data=data))


def prepare(disk, snippet, active, adapter):
    if disk.count(snippet) != 1 or len(re.findall(rb'^\s*s2s\.kirinxu\.cn\s*\{', disk, re.M)) != 1:
        raise RepairError('Expected exactly one unchanged approved S2S block; stopped.')
    if adapter(disk.replace(snippet, b'', 1)) != active:
        raise RepairError('Disk config also differs from active config outside S2S; stopped without changes.')
    candidate = adapter(disk)
    status = summarize(candidate)
    if not (status['active_s2s_route_present'] and status['active_s2s_le_issuer_present']
            and not status['active_s2s_auto_https_disabled']):
        raise RepairError('Candidate does not contain the approved HTTPS site; stopped.')
    return candidate


def main():
    if os.geteuid() != 0 or sys.argv[1:] != ['--apply-approved-site']:
        raise RepairError('Run as administrator with --apply-approved-site after reviewing the repair.')
    os.umask(0o077)
    info = json.loads(docker('inspect', 'caddy', '--format', '{{json .}}'))
    if not info['State']['Running'] or not any(
            m['Source'] == str(HOST) and m['Destination'] == MOUNT and m.get('RW')
            for m in info['Mounts']):
        raise RepairError('Unexpected Caddy state or mount; stopped.')
    label = docker('inspect', 's2s-demo', '--format', '{{index .Config.Labels "com.kirin.project"}}').strip()
    if label != b's2s_demo':
        raise RepairError('Expected S2S application was not found; stopped.')
    disk = HOST.read_bytes()
    mounted = execute('cat', MOUNT)
    before = active_config()
    print('mounted_file_matches_host=' + str(mounted == disk).lower(), flush=True)
    if summarize(before)['active_s2s_route_present']:
        raise RepairError('S2S is already active; no repair applied.')
    snippet = Path(__file__).with_name('Caddyfile.s2s').read_bytes()
    candidate = prepare(disk, snippet, before, adapt)
    print('only_s2s_change_verified=true', flush=True)
    encoded = json.dumps(candidate).encode()
    execute('caddy', 'validate', '--config', '-', data=encoded)
    backup = Path(tempfile.mkdtemp(prefix='s2s-config-backup-', dir='/opt/caddy'))
    (backup / 'host.Caddyfile').write_bytes(disk)
    (backup / 'mounted.Caddyfile').write_bytes(mounted)
    (backup / 'active.json').write_text(json.dumps(before))
    print('repair_backup_directory=' + str(backup), flush=True)
    if HOST.read_bytes() != disk or execute('cat', MOUNT) != mounted or active_config() != before:
        raise RepairError('Configuration changed during preflight; stopped without changes.')
    try:
        # Synchronize the existing mount too, including a stale single-file bind mount.
        # Never rename the host file or recreate/restart the shared Caddy container.
        execute('sh', '-c', 'cat > /etc/caddy/Caddyfile', data=disk)
        if HOST.read_bytes() != disk or execute('cat', MOUNT) != disk:
            raise RepairError('Mounted file synchronization failed.')
        # Use stdin so reload cannot silently read an older mounted file.
        execute('caddy', 'reload', '--config', '-', data=encoded)
        if active_config() != candidate:
            raise RepairError('Active configuration verification failed.')
    except (RepairError, OSError, ValueError, subprocess.TimeoutExpired):
        try:
            execute('sh', '-c', 'cat > /etc/caddy/Caddyfile', data=mounted)
            execute('caddy', 'reload', '--config', '-', data=json.dumps(before).encode())
            print('repair_rolled_back=true', flush=True)
        except (RepairError, OSError, subprocess.TimeoutExpired):
            print('repair_rollback_unconfirmed=true; administrator must use the printed backup', flush=True)
        raise RepairError('Repair did not complete; see rollback status above.') from None
    print('active_s2s_route_present=true')
    print('active_s2s_le_issuer_present=true')
    print('repair_applied=true; certificate issuance still needs verification')
    print('No model handshake, audio or generation was performed.')


if __name__ == '__main__':
    try:
        main()
    except (RepairError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        # Only our fixed errors are printable; subprocess/config exceptions may contain data.
        print(str(error) if isinstance(error, RepairError) else 'Repair stopped; internal details withheld.')
        sys.exit(1)
