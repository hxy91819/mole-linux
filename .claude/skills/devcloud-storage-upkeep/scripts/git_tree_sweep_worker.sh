#!/bin/bash
# Internal worker: BB lifecycle, registered linked trees, then full clone pass.
set -uo pipefail
git_sweep_python=$1
shift
git_sweep_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
git_sweep_clone_budget=30
if [[ $# -eq 1 && "$1" == --execute ]]; then
    git_sweep_clone_budget=30
elif [[ $# -ne 0 ]]; then
    echo 'Usage: git_tree_sweep_worker.sh PYTHON [--execute]' >&2
    exit 2
fi
BB_CLI="$(/usr/bin/python3 -I "$git_sweep_dir/git_tree_sweep_bb_cli.py")" || exit 1
export BB_CLI
git_sweep_environment_rc=0
"$git_sweep_python" -I "$git_sweep_dir/bb_environment_sweep.py" --budget-minutes 5 "$@" || git_sweep_environment_rc=$?
git_sweep_linked_rc=0
"$git_sweep_python" -I "$git_sweep_dir/worktree_sweep.py" --budget-minutes 15 "$@" || git_sweep_linked_rc=$?
git_sweep_clone_rc=0
"$git_sweep_python" -I "$git_sweep_dir/git_tree_sweep.py" --budget-minutes "$git_sweep_clone_budget" "$@" || git_sweep_clone_rc=$?
if [[ "$git_sweep_environment_rc" -ne 0 ]]; then
    exit "$git_sweep_environment_rc"
fi
if [[ "$git_sweep_linked_rc" -ne 0 ]]; then
    exit "$git_sweep_linked_rc"
fi
exit "$git_sweep_clone_rc"
