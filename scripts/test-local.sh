#!/usr/bin/env bash
set -u
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
RUN_ROOT="${AGENT_STACK_TEST_ROOT:-$ROOT_DIR/.tmp/agent-stack-tests}"
KEEP_WORK="${AGENT_STACK_TEST_KEEP_WORK:-0}"
case "$KEEP_WORK" in
  0|1) ;;
  *) echo "AGENT_STACK_TEST_KEEP_WORK must be 0 or 1." >&2; exit 2 ;;
esac
mkdir -p "$RUN_ROOT" || exit 1
RUN_ROOT="$(cd "$RUN_ROOT" && pwd -P)" || exit 1
if [ "$(uname -s)" = Linux ]; then
  FILESYSTEM="$(stat -f -c %T "$RUN_ROOT")" || exit 1
  case "$FILESYSTEM" in
    tmpfs|ramfs)
      echo "Test storage is RAM-backed. Set AGENT_STACK_TEST_ROOT to a disk directory." >&2
      exit 2
      ;;
  esac
fi
case "$RUN_ROOT/" in
  "$ROOT_DIR/"*)
    if ! git -C "$ROOT_DIR" check-ignore -q "$RUN_ROOT/storage-check"; then
      echo "A test root inside the checkout must be Git-ignored; use .tmp/agent-stack-tests." >&2
      exit 2
    fi
    ;;
esac

WORK_DIR="$(mktemp -d "$RUN_ROOT/work.XXXXXX")" || exit 1
cleanup() {
  local status=$?
  if [ "$KEEP_WORK" = 1 ]; then
    printf '\nTest workspace retained: %s\n' "$WORK_DIR"
  elif ! rm -rf -- "$WORK_DIR"; then
    echo "Could not remove the completed test workspace: $WORK_DIR" >&2
    status=1
  fi
  trap - EXIT
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

STATUS_BEFORE="$WORK_DIR/status.before"
STATUS_AFTER="$WORK_DIR/status.after"
TEST_REPO="$WORK_DIR/repo"
mkdir -p "$TEST_REPO" "$WORK_DIR/tmp" "$RUN_ROOT/provider-cache" || exit 1
export TMPDIR="$WORK_DIR/tmp"
export TF_PLUGIN_CACHE_DIR="$RUN_ROOT/provider-cache"

RESULT_ROWS=()
EXIT_CODE=0

record_result() {
  local phase="$1"
  local status="$2"
  local evidence="$3"
  RESULT_ROWS+=("| $phase | $status | $evidence |")
  if [ "$status" = "fail" ]; then
    EXIT_CODE=1
  fi
}

run_phase() {
  local phase="$1"
  shift
  echo
  echo "== $phase =="
  printf '$'
  printf ' %q' "$@"
  echo
  local output
  local status=0
  output="$("$@" 2>&1)" || status=$?
  printf '%s\n' "$output"
  if [ "$status" -eq 0 ]; then
    local evidence
    evidence="$(printf '%s\n' "$output" | tail -n 1 | sed 's/|/\\|/g')"
    record_result "$phase" "pass" "$evidence"
  else
    record_result "$phase" "fail" "exit $status"
    return "$status"
  fi
}

copy_worktree() {
  (
    cd "$ROOT_DIR" || exit 1
    {
      git ls-files -z
      git ls-files -o --exclude-standard -z
    } | while IFS= read -r -d '' path; do
      case "$path" in
        *.tfstate|*.tfstate.*|*.tfvars|*.tfvars.json|*.tfplan|*.tfplan.*|crash.log|crash.*.log|.terraform/*|.ssh/*)
          continue
          ;;
      esac
      [ -f "$path" ] || continue
      mkdir -p "$TEST_REPO/$(dirname "$path")"
      cp -p "$path" "$TEST_REPO/$path"
    done
  )
}

git -C "$ROOT_DIR" status --short --untracked-files=all > "$STATUS_BEFORE"
copy_worktree

export PYTHONDONTWRITEBYTECODE=1
export TF_DATA_DIR="$WORK_DIR/terraform-data"
mkdir -p "$TF_DATA_DIR"

run_phase "terraform init" terraform -chdir="$TEST_REPO" init -backend=false -no-color || true
run_phase "terraform fmt" terraform -chdir="$TEST_REPO" fmt -check -recursive -no-color || true
run_phase "terraform validate" terraform -chdir="$TEST_REPO" validate -no-color || true
run_phase "terraform test" terraform -chdir="$TEST_REPO" test -no-color || true
run_phase "setup unit tests" python3 -m unittest discover -s "$TEST_REPO/skills/agent-stack-setup/tests" || true
run_phase "doctor unit tests" python3 -m unittest discover -s "$TEST_REPO/skills/agent-stack-doctor/tests" || true
run_phase "test runner unit tests" python3 -m unittest discover -s "$TEST_REPO/scripts/tests" || true
run_phase "runtime shell syntax" bash -n \
  "$TEST_REPO/modules/common/templates/runtime/install-agent-stack.sh.tpl" \
  "$TEST_REPO/modules/common/templates/runtime/agent-stack-migrate-layout.sh.tpl" \
  "$TEST_REPO/modules/common/templates/runtime/mount-agent-stack-volume.sh.tpl" \
  "$TEST_REPO/modules/common/templates/runtime/agent-stack-tailscale-watchdog.sh.tpl" \
  "$TEST_REPO/modules/common/templates/runtime/tailscale-bootstrap.sh.tpl" \
  "$TEST_REPO/modules/common/templates/runtime/host-tailscale-bootstrap.sh.tpl" \
  "$TEST_REPO/modules/common/templates/runtime/agent-stack-vpn.sh.tpl" \
  "$TEST_REPO/modules/common/templates/runtime/agent-stack-vpn-openvpn.sh.tpl" \
  "$TEST_REPO/modules/common/templates/runtime/workspace-codex-update.sh.tpl" \
  "$TEST_REPO/modules/common/templates/runtime/agent-stack-workspace-codex-update.sh.tpl" \
  "$TEST_REPO/modules/common/templates/runtime/workspace-entrypoint.sh.tpl" \
  "$TEST_REPO/modules/common/templates/runtime/agent-stack-diagnostics.sh.tpl" \
  "$TEST_REPO/modules/common/templates/runtime/agent-stack-diagnostics-ssh.sh.tpl" || true
run_phase "workspace Codex control Python syntax" python3 -c \
  'import ast, pathlib, sys; ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))' \
  "$TEST_REPO/modules/common/templates/runtime/workspace-codex-control.py.tpl" || true

git -C "$ROOT_DIR" status --short --untracked-files=all > "$STATUS_AFTER"
if diff -u "$STATUS_BEFORE" "$STATUS_AFTER"; then
  record_result "repo idempotence" "pass" "git status unchanged"
else
  record_result "repo idempotence" "fail" "git status changed"
fi

echo
echo "## Test Evidence"
echo
echo "Workspace dirty before run:"
sed 's/^/  /' "$STATUS_BEFORE"
echo "Workspace dirty after run:"
sed 's/^/  /' "$STATUS_AFTER"
echo "Cloud-impacting commands run: none"
printf 'Provider cache retained: %s\n' "$TF_PLUGIN_CACHE_DIR"
echo
echo "| Phase | Status | Evidence |"
echo "| --- | --- | --- |"
printf '%s\n' "${RESULT_ROWS[@]}"

exit "$EXIT_CODE"
