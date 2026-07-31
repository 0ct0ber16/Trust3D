#!/usr/bin/env bash
set -uo pipefail

ROOT=/224010104/Jerry/trust3d
JERRY=/224010104/Jerry
TMUX_BIN=/224010104/Jerry/bin/tmux
PYTHON=/224010104/miniconda3/envs/trust3d-sim/bin/python
MODE=${1:-status}
LOG_ROOT=${JERRY}/logs/parallel_v2
CHECKPOINT_ROOT=${JERRY}/checkpoints/parallel_v2
LOCK=${CHECKPOINT_ROOT}/orchestrator.lock
LOG_PATH=${LOG_ROOT}/orchestrator.log

export HOME=${JERRY}
export TMPDIR=${JERRY}/.tmp
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=${ROOT}${PYTHONPATH:+:${PYTHONPATH}}

mkdir -p "${LOG_ROOT}" "${CHECKPOINT_ROOT}" "${TMPDIR}"
cd "${ROOT}"

window_exists() {
  local session=$1
  local name=$2
  "${TMUX_BIN}" list-windows -t "${session}" -F '#{window_name}' | awk -v expected="${name}" '$0==expected {found=1} END {exit !found}'
}

launch_window() {
  local session=$1
  local name=$2
  local command=$3
  if window_exists "${session}" "${name}"; then
    printf 'window=%s action=reuse\n' "${name}"
    return 0
  fi
  "${TMUX_BIN}" new-window -d -t "${session}:" -n "${name}" -c "${ROOT}" \
    "bash -lc '$command'"
  printf 'window=%s action=create command=%s\n' "${name}" "${command}"
}

run_preflight() {
  "${PYTHON}" -m trust3d.parallel_v2.orchestrator preflight
}

run_protocol() {
  "${PYTHON}" -m trust3d.parallel_v2.orchestrator protocol
}

run_freeze() {
  "${PYTHON}" -m trust3d.parallel_v2.orchestrator freeze
}

run_start() {
  if [[ -z ${TMUX:-} ]]; then
    printf '%s\n' 'parallel-v2 只能从 tmux 内启动。'
    return 125
  fi
  "${PYTHON}" -m trust3d.parallel_v2.runtime verify-baseline || return $?
  "${PYTHON}" -m trust3d.parallel_v2.orchestrator verify-freeze || return $?
  local session
  session=$("${TMUX_BIN}" display-message -p -t "${TMUX_PANE}" '#{session_name}')
  launch_window "${session}" parallel-a-gt5 \
    "bash scripts/run_five_route_gt.sh resume >> '${LOG_ROOT}/gt-five-route-window.log' 2>&1"
  launch_window "${session}" parallel-b-gate7 \
    "bash scripts/run_gate7_fix.sh resume >> '${LOG_ROOT}/gate7-fix-window.log' 2>&1"
  launch_window "${session}" parallel-status \
    "sleep 2; bash scripts/run_parallel_v2.sh supervise >> '${LOG_ROOT}/supervisor-window.log' 2>&1"
  "${PYTHON}" -m trust3d.parallel_v2.runtime status orchestrator running \
    'A/B 持久 runner 已启动；C 线由 supervisor 按准入自动启动。' --log "${LOG_PATH}"
}

run_supervise() {
  "${PYTHON}" -m trust3d.parallel_v2.orchestrator supervise
  local rc=$?
  if (( rc == 10 )); then
    local session
    session=$("${TMUX_BIN}" display-message -p -t "${TMUX_PANE}" '#{session_name}')
    launch_window "${session}" parallel-c-integration \
      "bash scripts/run_parallel_integration.sh resume >> '${LOG_ROOT}/integration-window.log' 2>&1"
    while [[ ! -f ${ROOT}/outputs/parallel_v2/integration/report.json ]]; do
      "${PYTHON}" -m trust3d.parallel_v2.orchestrator status >/dev/null
      sleep 5
    done
    "${PYTHON}" -m trust3d.parallel_v2.orchestrator report
    "${PYTHON}" -m trust3d.parallel_v2.runtime status orchestrator complete \
      'A/B/C 已结束并生成总报告。' --log "${LOG_PATH}"
    return 0
  fi
  return "${rc}"
}

run_locked() {
  if [[ -z ${TMUX:-} ]]; then
    printf '%s\n' 'parallel-v2 runner 必须在 tmux 中执行。'
    return 125
  fi
  exec 9>"${LOCK}"
  if ! flock -n 9; then
    if [[ ${MODE} == status ]]; then
      "${PYTHON}" -m trust3d.parallel_v2.orchestrator status
      return $?
    fi
    printf '%s\n' 'orchestrator 已在运行，本实例不重复启动。'
    return 73
  fi
  printf 'start=%s host=%s tmux=%s pane=%s cwd=%s command=%q log=%s git=%s dirty=%s\n' \
    "$(date -Is)" "$(hostname)" "${TMUX}" "${TMUX_PANE:-}" "${PWD}" "$0 $*" "${LOG_PATH}" "$(git rev-parse HEAD)" "$(git status --porcelain | wc -l)"
  case "${MODE}" in
    preflight) run_preflight ;;
    protocol) run_protocol ;;
    freeze) run_freeze ;;
    start|resume) run_start ;;
    status) "${PYTHON}" -m trust3d.parallel_v2.orchestrator status ;;
    supervise) run_supervise ;;
    report) "${PYTHON}" -m trust3d.parallel_v2.orchestrator report ;;
    *) printf '不支持的 parallel-v2 模式：%s\n' "${MODE}"; return 2 ;;
  esac
}

set +e
run_locked 2>&1 | tee -a "${LOG_PATH}"
rc=${PIPESTATUS[0]}
set -e
printf 'exit_code=%s end=%s\n' "${rc}" "$(date -Is)" | tee -a "${LOG_PATH}"
exit "${rc}"
