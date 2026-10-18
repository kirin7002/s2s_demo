"""Root-only, existing-site gateway update. No proxy/firewall changes or model calls."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

if __package__:
    from .update_ui import UpdateError, docker, inspect, launch_args
    from .gateway_checks import OFFLINE_CHECK, health_check_code
else:
    from update_ui import UpdateError, docker, inspect, launch_args
    from gateway_checks import OFFLINE_CHECK, health_check_code


def verified_manifest(release):
    manifest = json.loads((release / 'gateway-manifest.json').read_text())
    revision = manifest['revision']
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise UpdateError('Invalid gateway revision')
    for name, digest in manifest['files'].items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or path.parts[0] not in {
                'gateway', 'web', 'wheelhouse', 'deploy', 'requirements.txt', 'public-isrg-root-x1.pem'}:
            raise UpdateError('Unexpected release file')
        target = release / path
        if target.is_symlink() or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise UpdateError('Release hash mismatch')
    for folder in ('gateway', 'web', 'wheelhouse', 'deploy'):
        actual = {str(p.relative_to(release)) for p in (release / folder).rglob('*') if p.is_file()}
        expected = {n for n in manifest['files'] if Path(n).parts[0] == folder}
        if actual != expected:
            raise UpdateError('Unexpected release contents')
    return revision


def switch_gateway(old, image, revision, caddy_id, values, health):
    backup = 's2s-demo.pre-gateway-' + revision[:12]
    renamed = False
    try:
        docker('stop', '--time', '10', old['Id'])
        docker('rename', old['Id'], backup)
        renamed = True
        args = launch_args(image, revision, caddy_id, values)
        args[-1:-1] = ['--label', 'com.kirin.gateway_revision=' + revision]
        environment = os.environ.copy()
        environment.update(values)
        docker(*args, environment=environment)
        health()
    except (UpdateError, OSError, subprocess.TimeoutExpired):
        try:
            if renamed:
                try:
                    candidate = inspect('s2s-demo')
                except UpdateError:
                    candidate = None
                if candidate:
                    labels = candidate['Config'].get('Labels') or {}
                    if labels.get('com.kirin.project') != 's2s_demo' or labels.get('com.kirin.gateway_revision') != revision:
                        raise UpdateError('Unknown replacement container; manual recovery required')
                    docker('rm', '-f', candidate['Id'])
                docker('rename', old['Id'], 's2s-demo')
            docker('start', old['Id'])
            print('gateway_update_rolled_back=true')
        except (UpdateError, OSError, subprocess.TimeoutExpired):
            print('gateway_update_rollback_unconfirmed=true')
        raise UpdateError('Gateway update failed; inspect rollback status') from None
    return backup


def rollback_gateway(revision):
    backup_name = 's2s-demo.pre-gateway-' + revision[:12]
    failed_name = 's2s-demo.rolled-back-' + revision[:12]
    current, backup = inspect('s2s-demo'), inspect(backup_name)
    labels = current['Config'].get('Labels') or {}
    if (labels.get('com.kirin.project') != 's2s_demo'
            or labels.get('com.kirin.gateway_revision') != revision
            or (backup['Config'].get('Labels') or {}).get('com.kirin.project') != 's2s_demo'):
        raise UpdateError('Rollback identity mismatch; nothing changed')
    renamed = False
    try:
        docker('stop', '--time', '10', current['Id'])
        docker('rename', current['Id'], failed_name)
        renamed = True
        docker('rename', backup['Id'], 's2s-demo')
        docker('start', backup['Id'])
    except (UpdateError, OSError, subprocess.TimeoutExpired):
        try:
            if renamed:
                docker('rename', backup['Id'], backup_name)
                docker('rename', current['Id'], 's2s-demo')
            docker('start', current['Id'])
        except (UpdateError, OSError, subprocess.TimeoutExpired):
            print('manual_rollback_recovery_unconfirmed=true')
        raise UpdateError('Manual rollback failed; inspect recovery state') from None
    print('gateway_manual_rollback_completed=true')
    print('retained_new_container=' + failed_name)


def main():
    if os.geteuid() != 0 or sys.argv[1:] not in (
            ['--approve-gateway-update'], ['--rollback-gateway-update']):
        raise UpdateError('Requires administrator execution with --approve-gateway-update')
    os.umask(0o077)
    release = Path(__file__).resolve().parent.parent
    revision = verified_manifest(release)
    if sys.argv[1:] == ['--rollback-gateway-update']:
        rollback_gateway(revision)
        return
    old, caddy = inspect('s2s-demo'), inspect('caddy')
    labels = old['Config'].get('Labels') or {}
    if (labels.get('com.kirin.project') != 's2s_demo' or not old['State']['Running']
            or not caddy['State']['Running'] or not old['HostConfig']['ReadonlyRootfs']
            or old['HostConfig']['NetworkMode'] not in {'container:caddy', 'container:' + caddy['Id']}):
        raise UpdateError('Unexpected S2S/proxy identity or network')
    if labels.get('com.kirin.gateway_revision') == revision:
        print('gateway_revision_already_deployed=true')
        return
    base = old['Config']['Image']
    if not re.fullmatch(r's2s-demo:[A-Za-z0-9_.-]+', base):
        raise UpdateError('Unexpected base image')
    if json.loads(docker('image', 'inspect', base))[0]['Id'] != old['Image']:
        raise UpdateError('Base image tag changed')
    values = dict(v.split('=', 1) for v in old['Config']['Env'])
    if (not values.get('DASHSCOPE_API_KEY') or not values.get('S2S_DEMO_PASSWORD')
            or values.get('S2S_ORIGIN') != 'https://s2s.kirinxu.cn'
            or values.get('QWEN_MODEL') != 'qwen-audio-3.0-realtime-flash'):
        raise UpdateError('Existing server configuration differs; no secrets changed')
    device_file = Path('/home/kirin/apps/s2s_demo/shared/stackchan-device-config.json')
    if not device_file.exists():
        raise UpdateError('Provision the already approved private device config on server first; no token was generated or changed')
    if device_file.stat().st_mode & 0o077:
        raise UpdateError('Device config must be private')
    device = json.loads(device_file.read_text())
    if (device.get('host') != 's2s.kirinxu.cn' or device.get('port') != 443
            or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', device.get('device_token', ''))
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', device.get('device_id', ''))
            or device.get('root_ca_pem', '').strip() != (release / 'public-isrg-root-x1.pem').read_text().strip()):
        raise UpdateError('Unexpected device connection config')
    values.pop('S2S_STACKCHAN_TEST_MAX_DEG', None)
    values.update(S2S_STACKCHAN_ENABLED='1', S2S_STACKCHAN_DEVICE_ID=device['device_id'],
                  S2S_STACKCHAN_DEVICE_TOKEN=device['device_token'], S2S_STACKCHAN_AUDIT='1')
    image = 's2s-demo:gateway-' + revision[:12]
    with tempfile.TemporaryDirectory(prefix='s2s-gateway-build-') as directory:
        context = Path(directory)
        import shutil
        for folder in ('gateway', 'web', 'wheelhouse'):
            shutil.copytree(release / folder, context / folder)
        shutil.copyfile(release / 'requirements.txt', context / 'requirements.txt')
        (context / 'Dockerfile').write_text('FROM ' + base + '''
USER root
COPY wheelhouse /tmp/s2s-wheels
COPY requirements.txt /tmp/s2s-requirements.txt
RUN pip install --no-index --find-links=/tmp/s2s-wheels -r /tmp/s2s-requirements.txt && rm -rf /tmp/s2s-wheels /tmp/s2s-requirements.txt
COPY gateway /app/gateway
COPY web /app/web
USER app
''')
        docker('build', '--network', 'none', '--pull=false', '-t', image, str(context))
    # Verify imports before stopping anything. No external connection is made.
    docker('run', '--rm', '--network', 'none', '--entrypoint', 'python', image, '-c',
           OFFLINE_CHECK)
    health_code = health_check_code(json.loads((release / 'gateway-manifest.json').read_text()))
    backup = switch_gateway(old, image, revision, caddy['Id'], values,
        lambda: docker('exec', 's2s-demo', 'python', '-c', health_code))
    print('DEPLOYED_GATEWAY_REVISION=' + revision)
    print('ROLLBACK_CONTAINER=' + backup)
    print('login_model_and_device_auth_verified=true')
    print('DEVICE_CONFIG_PATH=' + str(device_file))
    print('No proxy/firewall changes or model/audio/device execution performed.')


if __name__ == '__main__':
    try:
        main()
    except (UpdateError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(str(error) if isinstance(error, UpdateError) else 'Gateway update stopped; private output withheld.', file=sys.stderr)
        raise SystemExit(1) from None
