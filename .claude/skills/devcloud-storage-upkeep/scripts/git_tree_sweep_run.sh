#!/bin/bash
# Start the bounded host worker; BB script runs have a hard 900-second limit.
set -euo pipefail
if [[ "${1:-}" == "--help" ]]; then
    echo 'Start host-wide Git sweep: preview 30m/33m, --execute 150m/153m; optional --reviewed-preview FILE for the first execution.'
    exit 0
fi
git_sweep_mode=dry-run
git_sweep_budget=30
git_sweep_hard_seconds=1980
if [[ $# -ne 0 ]]; then
    if [[ "${1:-}" != --execute ]] || [[ $# -ne 1 && $# -ne 3 ]] ||
        { [[ $# -eq 3 ]] && { [[ "$2" != --reviewed-preview ]] || [[ ! -f "$3" ]]; }; }; then
        echo 'Usage: git_tree_sweep_run.sh [--execute [--reviewed-preview FILE]]' >&2
        exit 2
    fi
    git_sweep_mode=execute
    git_sweep_budget=150
    git_sweep_hard_seconds=9180
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
        echo 'Git tree sweep already running; skipped.'
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
    --property=User=root --property=WorkingDirectory=/ --property="RuntimeMaxSec=$git_sweep_hard_seconds" \
    --setenv="PATH=${PATH}" \
    /root/.pyenv/versions/3.12.12/bin/python3 -I "$git_sweep_dir/git_tree_sweep.py" --budget-minutes "$git_sweep_budget" "$@"
echo "Git tree $git_sweep_mode launched; reports: /root/.local/state/devcloud-storage-upkeep/git-tree-sweep-*.json"
