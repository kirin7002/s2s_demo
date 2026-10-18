"""Build a code/wheel-only release; device and model credentials stay outside."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / '.run/s2s-cloud')
    args = parser.parse_args()
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Invalid source revision')
    release = args.directory / ('gateway-' + revision[:12])
    release.mkdir(mode=0o700)
    for folder in ('gateway', 'web'):
        sources = list((ROOT / folder).rglob('*'))
        if any(p.is_symlink() for p in sources):
            raise ValueError('Source symlinks are not permitted')
        shutil.copytree(ROOT / folder, release / folder,
            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
    shutil.copytree(args.directory / 'wheelhouse', release / 'wheelhouse')
    shutil.copyfile(ROOT / 'requirements.txt', release / 'requirements.txt')
    (release / 'deploy').mkdir()
    for name in ('update_gateway.py', 'update_ui.py'):
        shutil.copyfile(ROOT / 'deploy' / name, release / 'deploy' / name)
    manifest = {'revision': revision, 'files': {}}
    for path in sorted(release.rglob('*')):
        if path.is_file() and path.relative_to(release).parts[0] != 'deploy':
            manifest['files'][str(path.relative_to(release))] = hashlib.sha256(path.read_bytes()).hexdigest()
    (release / 'gateway-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    archive = release.with_suffix('.tar.gz')
    with tarfile.open(archive, 'w:gz') as output:
        output.add(release, arcname=release.name)
    print(json.dumps({'release_directory': str(release.resolve()), 'archive': str(archive.resolve()),
                      'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'revision': revision}))


if __name__ == '__main__':
    main()
