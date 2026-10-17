#!/usr/bin/env bash
# Read-only handoff: fixed status fields and filtered logs; no env/key reads.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    echo 'Administrator access is required to read the existing Caddy container logs.' >&2
    exit 1
fi
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
python3 "$script_dir/caddy_runtime_summary.py"
if ! docker logs --since 24h --tail 10000 caddy 2>&1 | python3 "$script_dir/filter_caddy_tls.py"; then
    echo 'Read-only Caddy log collection failed; no configuration was changed.' >&2
    exit 1
fi
