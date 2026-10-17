#!/usr/bin/env bash
# Read-only handoff: no config/env/key reads, no reload, restart or issuance.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    echo 'Administrator access is required to read the existing Caddy container logs.' >&2
    exit 1
fi
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if ! docker logs --since 30m --tail 1000 caddy 2>&1 | python3 "$script_dir/filter_caddy_tls.py"; then
    echo 'Read-only Caddy log collection failed; no configuration was changed.' >&2
    exit 1
fi
