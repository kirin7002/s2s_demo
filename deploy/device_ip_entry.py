"""Isolated TLS8443 device proxy. Default is read-only preflight; --apply is explicit."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import sys
import time

sys.dont_write_bytecode = True
BASE = Path('/home/kirin/apps/s2s_demo')
SHARED = BASE / 'shared/device-ip-entry'
NAME = 's2s-device-ip'
PROJECT = 's2s_demo_device_ip'
IP = '47.116.102.49'
PORT = 8443


class EntryError(Exception):
    pass


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, timeout=60)
    if result.returncode:
        raise EntryError('Docker ' + args[0] + ' failed; raw private output withheld')
    return result.stdout


def inspect(name, optional=False):
    result = subprocess.run(['docker', 'inspect', name], capture_output=True, timeout=15)
    if result.returncode:
        if optional and b'no such' in result.stderr.lower():
            return None
        raise EntryError('Cannot inspect expected container; raw output withheld')
    return json.loads(result.stdout)[0]


def verify_bundle(release):
    manifest = json.loads((release / 'ip-entry-manifest.json').read_text())
    if not re.fullmatch(r'[0-9a-f]{40}', manifest.get('revision', '')):
        raise EntryError('Invalid source revision')
    expected = {'device_ip_entry.py', 'device-ip.Caddyfile'}
    if set(manifest['files']) != expected:
        raise EntryError('Unexpected release manifest')
    for name, digest in manifest['files'].items():
        path = release / name
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise EntryError('Release checksum mismatch')
    if not (release / 'device-ip.Caddyfile').stat().st_mode & 0o004:
        raise EntryError('Public sidecar configuration must be readable by its runtime UID')
    return manifest['revision']


def container_owned(value):
    return bool(value and (value['Config'].get('Labels') or {}).get('com.kirin.project') == PROJECT)


def preflight(release):
    revision = verify_bundle(release)
    caddy, gateway = inspect('caddy'), inspect('s2s-demo')
    if (not caddy['State']['Running'] or not gateway['State']['Running']
            or (gateway['Config'].get('Labels') or {}).get('com.kirin.project') != 's2s_demo'
            or gateway['HostConfig']['NetworkMode'] not in {'container:caddy', 'container:' + caddy['Id']}):
        raise EntryError('Existing gateway/proxy identity or network differs')
    image = caddy['Image']
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise EntryError('Expected immutable existing Caddy image ID')
    version = docker('run', '--rm', '--pull=never', '--network', 'none', image, 'version').decode()
    match = re.search(r'v\d+\.\d+\.\d+', version)
    if not match:
        raise EntryError('Caddy version unavailable')
    print('installed_caddy_version=' + match.group(), flush=True)
    networks = []
    for name in sorted(caddy['NetworkSettings']['Networks']):
        if name in {'bridge', 'host', 'none'}:
            continue
        data = json.loads(docker('network', 'inspect', name))[0]
        if data.get('Driver') == 'bridge':
            networks.append(name)
    if len(networks) != 1:
        raise EntryError('Expected exactly one existing user-defined Caddy bridge network')
    current = inspect(NAME, optional=True)
    if current and not container_owned(current):
        raise EntryError('Target container name belongs to another service')
    if (current and (current['Config'].get('Labels') or {}).get('com.kirin.ip_entry_revision') != revision
            and inspect(NAME + '.pre-' + revision[:12], optional=True)):
        raise EntryError('Backup container name already exists; no change made')
    if not current:
        with socket.socket() as check:
            check.bind(('0.0.0.0', PORT))
    ca = SHARED / 'ca'
    for path in (ca, ca / 'root.key', ca / 'root.crt'):
        if path.is_symlink() or not path.exists():
            raise EntryError('Dedicated server CA is missing or unsafe')
    if (ca / 'root.key').stat().st_mode & 0o077:
        raise EntryError('CA private key permissions are not private')
    root = (ca / 'root.crt').read_text()
    device = json.loads((BASE / 'shared/stackchan-device-ip-config.json').read_text())
    original = json.loads((BASE / 'shared/stackchan-device-config.json').read_text())
    env = dict(item.split('=', 1) for item in gateway['Config']['Env'])
    if (device.get('host') != IP or device.get('port') != PORT or device.get('root_ca_pem') != root
            or device.get('device_token') != original.get('device_token')
            or device.get('device_token') != env.get('S2S_STACKCHAN_DEVICE_TOKEN')
            or device.get('device_id') != env.get('S2S_STACKCHAN_DEVICE_ID')
            or env.get('S2S_ORIGIN') != 'https://s2s.kirinxu.cn'):
        raise EntryError('Device identity, CA or original gateway origin differs')
    # Validate against the exact installed image without touching main Caddy.
    uid, gid = (ca / 'root.key').stat().st_uid, (ca / 'root.key').stat().st_gid
    print('checking_isolated_caddy_configuration=true', flush=True)
    docker('run', '--rm', '--pull=never', '--network', 'none', '--user', str(uid) + ':' + str(gid),
           '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--read-only',
           '--tmpfs', '/data:rw,noexec,nosuid,size=16m,uid=' + str(uid) + ',gid=' + str(gid) + ',mode=0700',
           '--tmpfs', '/config:rw,noexec,nosuid,size=4m,uid=' + str(uid) + ',gid=' + str(gid) + ',mode=0700',
           '--tmpfs', '/tmp:rw,noexec,nosuid,size=4m',
           '--mount', 'type=bind,src=' + str(ca) + ',dst=/ca,readonly',
           '--mount', 'type=bind,src=' + str(release / 'device-ip.Caddyfile') + ',dst=/etc/caddy/Caddyfile,readonly',
           image, 'validate', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile')
    print('exact_existing_caddy_image_and_config_verified=true', flush=True)
    return {'revision': revision, 'image': image, 'network': networks[0], 'current': current,
            'root': root, 'token': device['device_token']}


def launch_args(release, plan):
    args = ['run', '-d', '--pull=never', '--name', NAME, '--label', 'com.kirin.project=' + PROJECT,
            '--label', 'com.kirin.ip_entry_revision=' + plan['revision'],
            '--network', plan['network'], '--publish', '0.0.0.0:8443:8443/tcp',
            '--restart', 'unless-stopped', '--user', '0:0', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges:true', '--pids-limit', '64', '--memory', '128m',
            '--cpus', '0.5', '--tmpfs', '/tmp:rw,noexec,nosuid,size=8m']
    for source, target, readonly in [(release / 'device-ip.Caddyfile', '/etc/caddy/Caddyfile', True),
                                     (SHARED / 'ca', '/ca', True), (SHARED / 'data', '/data', False),
                                     (SHARED / 'config', '/config', False)]:
        args += ['--mount', 'type=bind,src=' + str(source) + ',dst=' + target + (',readonly' if readonly else '')]
    return args + [plan['image'], 'run', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile']


def tls_request(context, *, identity=IP, path='/device/v1/ws', token=None, upgrade=False, port=PORT):
    with socket.create_connection(('127.0.0.1', port), timeout=4) as tcp:
        # Python sends no SNI for IP identities and verifies the IP SAN.
        with context.wrap_socket(tcp, server_hostname=identity) as tls:
            headers = ['GET ' + path + ' HTTP/1.1', 'Host: ' + IP + ':' + str(port)]
            if token:
                headers.append('Authorization: Bearer ' + token)
            if upgrade:
                headers += ['Connection: Upgrade', 'Upgrade: websocket', 'Sec-WebSocket-Version: 13',
                            'Sec-WebSocket-Key: ' + base64.b64encode(secrets.token_bytes(16)).decode()]
            else:
                headers.append('Connection: close')
            tls.sendall(('\r\n'.join(headers) + '\r\n\r\n').encode())
            data = b''
            while b'\r\n' not in data and len(data) < 2048:
                chunk = tls.recv(512)
                if not chunk:
                    break
                data += chunk
            line = data.split(b'\r\n', 1)[0]
            match = re.fullmatch(rb'HTTP/1\.[01] ([1-5][0-9]{2})(?: [^\r\n]*)?', line)
            if not match:
                raise EntryError('Empty or malformed HTTP status; raw response withheld')
            return int(match.group(1))
            # A successful upgrade is closed here without a device.hello frame.


def health(plan):
    trust = ssl.create_default_context(cadata=plan['root'])
    last = None
    for _ in range(30):
        try:
            if tls_request(trust) == 401:
                break
        except (OSError, ValueError, EntryError) as error:
            last = type(error).__name__
        time.sleep(.3)
    else:
        raise EntryError('Trusted IP TLS/unauthorized route check failed: ' + str(last))
    legacy = ssl.create_default_context(cadata=plan['root'])
    legacy.minimum_version = legacy.maximum_version = ssl.TLSVersion.TLSv1_2
    if tls_request(legacy) != 401 or tls_request(trust, token='invalid-diagnostic-token') != 401:
        raise EntryError('TLS1.2 or device authentication boundary failed')
    for path in ['/', '/login', '/api/config', '/api/stackchan', '/ws']:
        if tls_request(trust, path=path) != 404:
            raise EntryError('Non-device path is exposed')
    for ctx, identity in [(ssl.create_default_context(), IP), (trust, '127.0.0.2')]:
        try:
            tls_request(ctx, identity=identity)
        except ssl.SSLCertVerificationError:
            continue
        raise EntryError('Wrong CA or wrong IP was not rejected')
    if tls_request(trust, token=plan['token'], upgrade=True) != 101:
        raise EntryError('Authenticated upgrade failed; no device hello was sent')
    return {'ip_san_no_sni_verified': True, 'tls12_verified': True, 'wrong_ca_rejected': True,
            'wrong_ip_rejected': True, 'missing_and_wrong_token_401': True, 'other_paths_404': True,
            'valid_token_upgrade_101_without_hello': True, 'real_model_calls': 0, 'device_actions': 0}


def switch(release, plan, check=health):
    old = plan['current']
    backup = NAME + '.pre-' + plan['revision'][:12]
    renamed = False
    started = None
    try:
        if old:
            docker('stop', '--time', '10', old['Id'])
            docker('rename', old['Id'], backup)
            renamed = True
        started = docker(*launch_args(release, plan)).decode().strip()
        result = check(plan)
        state = {'revision': plan['revision'], 'container_id': started,
                 'previous_id': old['Id'] if old else None, 'previous_name': backup if old else None}
        temporary = SHARED / 'deployment-state.tmp'
        temporary.write_text(json.dumps(state, indent=2) + '\n')
        os.chmod(temporary, 0o600)
        temporary.replace(SHARED / 'deployment-state.json')
    except (EntryError, OSError, ValueError, subprocess.TimeoutExpired):
        candidate = inspect(NAME, optional=True)
        if candidate and old and candidate['Id'] == old['Id'] and not renamed:
            docker('start', old['Id'])
            raise EntryError('Entry switch stopped before rename; previous entry restarted') from None
        if (candidate and container_owned(candidate)
                and (candidate['Config'].get('Labels') or {}).get('com.kirin.ip_entry_revision') == plan['revision']
                and (not started or candidate['Id'] == started)):
            docker('rm', '-f', candidate['Id'])
        elif candidate:
            raise EntryError('Unknown replacement identity; recovery requires administrator inspection') from None
        if renamed:
            docker('rename', old['Id'], NAME)
            docker('start', old['Id'])
        raise EntryError('IP entry failed; candidate removed and previous entry restored if present') from None
    return result


def rollback(revision):
    state = json.loads((SHARED / 'deployment-state.json').read_text())
    current = inspect(NAME)
    if (state['revision'] != revision or current['Id'] != state['container_id']
            or not container_owned(current)):
        raise EntryError('Rollback identity mismatch; no change made')
    disabled = NAME + '.disabled-' + revision[:12]
    if inspect(disabled, optional=True):
        raise EntryError('Retained container name already exists; no change made')
    previous = inspect(state['previous_name']) if state['previous_id'] else None
    if previous and (previous['Id'] != state['previous_id'] or not container_owned(previous)):
        raise EntryError('Previous entry identity mismatch')
    renamed = False
    try:
        docker('stop', '--time', '10', current['Id'])
        docker('rename', current['Id'], disabled)
        renamed = True
        if previous:
            docker('rename', previous['Id'], NAME)
            docker('start', previous['Id'])
    except (EntryError, OSError, subprocess.TimeoutExpired):
        occupied = inspect(NAME, optional=True)
        if occupied and previous and occupied['Id'] == previous['Id']:
            docker('rename', previous['Id'], state['previous_name'])
        elif occupied and occupied['Id'] != current['Id']:
            raise EntryError('Unknown entry during rollback; administrator recovery needed') from None
        if renamed:
            docker('rename', current['Id'], NAME)
        docker('start', current['Id'])
        raise EntryError('Rollback failed; current entry restored') from None
    print('ip_entry_rolled_back=true; main_caddy_and_gateway_untouched=true')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--apply', action='store_true')
    group.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise EntryError('Administrator execution required; no password is accepted by this script')
    os.umask(0o077)
    release = Path(__file__).resolve().parent
    revision = verify_bundle(release)
    if args.rollback:
        rollback(revision)
        return
    plan = preflight(release)
    if not args.apply:
        print('preflight_passed=true; deployment_not_started=true')
        return
    for folder in [SHARED, SHARED / 'ca', SHARED / 'data', SHARED / 'config']:
        folder.mkdir(mode=0o700, exist_ok=True)
        if folder.is_symlink():
            raise EntryError('Unsafe persistent directory')
        os.chown(folder, 0, 0)
        os.chmod(folder, 0o700)
    for path in (SHARED / 'ca').iterdir():
        if path.is_symlink() or not path.is_file():
            raise EntryError('Unexpected CA contents')
        os.chown(path, 0, 0)
        os.chmod(path, 0o600)
    current = plan['current']
    if current and (current['Config'].get('Labels') or {}).get('com.kirin.ip_entry_revision') == revision:
        checks = health(plan)
        print('ip_entry_revision_already_deployed=true')
    else:
        checks = switch(release, plan)
    print('DEPLOYED_IP_ENTRY_REVISION=' + revision)
    print('DEVICE_ENDPOINT=wss://47.116.102.49:8443/device/v1/ws')
    print('DEVICE_CONFIG_PATH=' + str(BASE / 'shared/stackchan-device-ip-config.json'))
    print(json.dumps(checks))
    print('main_caddy_and_s2s_unchanged=true; ca_private_key_exported=false')


if __name__ == '__main__':
    try:
        main()
    except (EntryError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(str(error) if isinstance(error, EntryError) else 'IP entry stopped; private details withheld', file=sys.stderr)
        raise SystemExit(1) from None
