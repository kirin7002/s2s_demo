"""Prepare an immutable code-only TLS device entry release. No credentials or CA keys."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip():
        raise SystemExit('Commit source before preparing a release')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    release = ROOT / '.run/s2s-cloud' / ('device-ip-' + revision[:12])
    release.mkdir(mode=0o700)
    manifest = {'revision': revision, 'files': {}}
    for name in ['device_ip_entry.py', 'device-ip.Caddyfile']:
        path = release / name
        shutil.copyfile(ROOT / 'deploy' / name, path)
        path.chmod(0o644)  # Mounted individually: root with cap-drop ALL can read public config.
        manifest['files'][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (release / 'ip-entry-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    archive = release.with_suffix('.tar.gz')
    with tarfile.open(archive, 'w:gz') as bundle:
        bundle.add(release, arcname=release.name)
    print(json.dumps({'revision': revision, 'release': str(release), 'archive': str(archive),
                      'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    main()
