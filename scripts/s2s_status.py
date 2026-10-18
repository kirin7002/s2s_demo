"""Private read-only device diagnostics; no model calls or control acquisition."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.stackchan_hardware import request_local


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--op', choices=['status', 'get_state'], default='status')
    parser.add_argument('--directory', type=Path, default=ROOT / '.run/s2s-live')
    args = parser.parse_args()
    response = asyncio.run(request_local({'control_socket': str(args.directory / 'control.sock')}, {'op': args.op}))
    print(json.dumps(response, ensure_ascii=False))
    return int(not response.get('ok'))


if __name__ == '__main__':
    raise SystemExit(main())
