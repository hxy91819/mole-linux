#!/bin/bash
# Start the bounded host worker; BB script runs have a hard 900-second limit.
set -euo pipefail
if [[ "${1:-}" == "--help" ]]; then
    echo 'Start the host-wide Git dry-run worker (30m budget, 33m hard limit).'
    exit 0
fi
if [[ $# -ne 0 ]]; then
    echo 'Unexpected arguments; this deployed launcher runs the approved dry-run only.' >&2
    exit 2
fi
git_sweep_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
git_sweep_unit=devcloud-git-tree-sweep.service
git_sweep_lookup_rc=0
git_sweep_state="$(systemctl show "$git_sweep_unit" --property=LoadState --property=ActiveState)" || git_sweep_lookup_rc=$?
if [[ "$git_sweep_lookup_rc" != 0 && "$git_sweep_state" != *LoadState=not-found* ]]; then
    echo "Cannot read worker state; inspect: systemctl status $git_sweep_unit" >&2
    exit 1
fi
case "$git_sweep_state" in
    *ActiveState=active* | *ActiveState=activating* | *ActiveState=deactivating*)
        echo 'Git tree preview already running; skipped.'
        exit 0
        ;;
esac
if [[ "$git_sweep_state" != *LoadState=not-found* && "$git_sweep_state" != *LoadState=loaded* ]]; then
    echo "Unknown worker load state; inspect: systemctl status $git_sweep_unit" >&2
    exit 1
fi
if [[ "$git_sweep_state" != *ActiveState=inactive* && "$git_sweep_state" != *ActiveState=failed* ]]; then
    echo "Unknown worker activity state; inspect: systemctl status $git_sweep_unit" >&2
    exit 1
fi
systemd-run --unit="$git_sweep_unit" --collect --service-type=exec \
    --property=User=root --property=WorkingDirectory=/var/tmp --property=RuntimeMaxSec=1980 \
    --setenv="PATH=${PATH}" \
    /root/.pyenv/versions/3.12.12/bin/python3 -I "$git_sweep_dir/git_tree_sweep.py" --budget-minutes 30
echo 'Git tree dry-run launched; reports: /root/.local/state/devcloud-storage-upkeep/git-tree-sweep-*.json'
