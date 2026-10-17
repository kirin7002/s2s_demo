"""Administrator-only UI update; reuse the local app image and runtime config."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

FILES = {'index.html', 'login.html', 'app.js', 'login.js', 'style.css', 'capture.js'}


class UpdateError(Exception):
    pass


def docker(*args, environment=None):
    result = subprocess.run(['docker', *args], env=environment, capture_output=True, timeout=120)
    if result.returncode:
        # Docker errors can contain runtime values; never forward raw output.
        raise UpdateError('Docker operation failed: ' + args[0])
    return result.stdout


def inspect(name):
    return json.loads(docker('inspect', name))[0]


def launch_args(image, revision, caddy_id, values):
    args = ['run', '-d', '--name', 's2s-demo', '--label', 'com.kirin.project=s2s_demo',
            '--label', 'com.kirin.ui_revision=' + revision,
            '--network', 'container:' + caddy_id, '--restart', 'unless-stopped', '--read-only',
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges:true', '--pids-limit', '64',
            '--memory', '256m', '--cpus', '0.5']
    for name in sorted(values):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            raise UpdateError('Unexpected runtime variable name; stopped.')
        args.extend(['--env', name])  # Values stay only in the child environment.
    return args + [image]


def switch(old, image, revision, caddy_id, values, health):
    backup = 's2s-demo.pre-ui-' + revision[:12]
    renamed = False
    try:
        docker('stop', '--time', '10', old['Id'])
        docker('rename', old['Id'], backup)
        renamed = True
        environment = os.environ.copy()
        environment.update(values)
        docker(*launch_args(image, revision, caddy_id, values), environment=environment)
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
                    if labels.get('com.kirin.project') != 's2s_demo' or labels.get('com.kirin.ui_revision') != revision:
                        raise UpdateError('Unknown replacement container; manual recovery required.')
                    docker('rm', '-f', candidate['Id'])
                docker('rename', old['Id'], 's2s-demo')
            docker('start', old['Id'])
            print('ui_update_rolled_back=true')
        except (UpdateError, OSError, subprocess.TimeoutExpired):
            print('ui_update_rollback_unconfirmed=true; administrator recovery required')
        raise UpdateError('UI update failed; see rollback status.') from None
    return backup


def main():
    if os.geteuid() != 0 or sys.argv[1:] != ['--approve-ui-update']:
        raise UpdateError('Administrator execution with --approve-ui-update is required.')
    os.umask(0o077)
    release = Path(__file__).resolve().parent.parent
    manifest = json.loads((release / 'ui-manifest.json').read_text())
    revision = manifest['revision']
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or set(manifest['files']) != FILES:
        raise UpdateError('Unexpected UI manifest; stopped.')
    if set(p.name for p in (release / 'web').iterdir()) != FILES or any(
            (release / 'web' / name).is_symlink() for name in FILES):
        raise UpdateError('Unexpected files in UI bundle; stopped.')
    for name, expected in manifest['files'].items():
        if hashlib.sha256((release / 'web' / name).read_bytes()).hexdigest() != expected:
            raise UpdateError('UI bundle verification failed; stopped.')
    old, caddy = inspect('s2s-demo'), inspect('caddy')
    labels = old['Config'].get('Labels') or {}
    if (labels.get('com.kirin.project') != 's2s_demo' or not old['State']['Running']
            or not caddy['State']['Running'] or not old['HostConfig']['ReadonlyRootfs']
            or old['HostConfig']['NetworkMode'] not in {'container:caddy', 'container:' + caddy['Id']}):
        raise UpdateError('Unexpected application/proxy identity or network; stopped.')
    if labels.get('com.kirin.ui_revision') == revision:
        print('ui_revision_already_deployed=true')
        return
    base = old['Config']['Image']
    if not re.fullmatch(r's2s-demo:[a-zA-Z0-9_.-]+', base):
        raise UpdateError('Unexpected base image; stopped.')
    if json.loads(docker('image', 'inspect', base))[0]['Id'] != old['Image']:
        raise UpdateError('Base image tag changed; stopped.')
    image = 's2s-demo:ui-' + revision[:12]
    with tempfile.TemporaryDirectory(prefix='s2s-ui-build-') as directory:
        context = Path(directory)
        shutil.copytree(release / 'web', context / 'web')
        (context / 'Dockerfile').write_text('FROM ' + base + '\nCOPY web /app/web\n')
        docker('build', '--network', 'none', '--pull=false', '-t', image, str(context))
    print('ui_image_built=true', flush=True)
    values = dict(item.split('=', 1) for item in old['Config']['Env'])
    expected_css = manifest['files']['style.css']
    health_code = '''import hashlib,json,time,urllib.request
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
for attempt in range(30):
 try:
  assert opener.open('http://127.0.0.1:8080/login',timeout=2).status==200
  c=json.load(opener.open('http://127.0.0.1:8080/api/config',timeout=2))
  assert c['ready'] and c['password_required'] and c['model']=='qwen-audio-3.0-realtime-flash'
  css=opener.open('http://127.0.0.1:8080/style.css',timeout=2).read()
  assert hashlib.sha256(css).hexdigest()==CSS_HASH
  break
 except Exception:
  if attempt==29: raise SystemExit('UI health failed; no details shown.') from None
  time.sleep(.2)
'''.replace('CSS_HASH', repr(expected_css))
    backup = switch(old, image, revision, caddy['Id'], values,
                    lambda: docker('exec', 's2s-demo', 'python', '-c', health_code))
    print('DEPLOYED_UI_REVISION=' + revision)
    print('ROLLBACK_CONTAINER=' + backup)
    print('ui_login_config_and_assets_verified=true')
    print('No Caddy changes, model handshake, audio or generation performed.')


if __name__ == '__main__':
    try:
        main()
    except (UpdateError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(str(error) if isinstance(error, UpdateError) else 'UI update stopped; details withheld.')
        sys.exit(1)
