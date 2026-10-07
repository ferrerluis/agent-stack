#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
export TMPDIR="${AGENT_STACK_TEST_ROOT:-$ROOT_DIR/.tmp/agent-stack-tests}/container-tmp"
umask 077
mkdir -p "$TMPDIR"
if [ "$(uname -s)" = Linux ]; then
  case "$(stat -f -c %T "$TMPDIR")" in
    tmpfs|ramfs) echo "Container test storage is RAM-backed; choose a disk AGENT_STACK_TEST_ROOT." >&2; exit 2 ;;
  esac
fi
export PYTHONDONTWRITEBYTECODE=1
exec python3 "$ROOT_DIR/scripts/test_containers.py" "$@"
