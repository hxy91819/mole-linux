#!/bin/bash
# Lightweight alias to run Mole via `mo`

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${OSTYPE:-}" != darwin* ]]; then
    case "${1:-}" in
        status) shift; exec "$SCRIPT_DIR/bin/status-go" "$@" ;;
        analyze|analyse) shift; exec "$SCRIPT_DIR/bin/analyze-go" "$@" ;;
        devcloud) shift; exec python3 "$SCRIPT_DIR/bin/devcloud.py" "$@" ;;
    esac
fi
exec "$SCRIPT_DIR/mole" "$@"
