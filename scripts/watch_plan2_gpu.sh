#!/usr/bin/env bash
set -uo pipefail

ROOT=/224010104/Jerry/trust3d
JERRY=/224010104/Jerry
CONFIG=${ROOT}/configs/plan2_vggt.json
OUTPUT=${ROOT}/outputs/plan2
STATUS=${OUTPUT}/status.json
STATE=${JERRY}/checkpoints/plan2/gpu-watch.json
LOCK=${JERRY}/checkpoints/plan2/gpu-watch.lock
RUNNER=${ROOT}/scripts/run_plan2.sh
INTERVAL=$(jq -r '.resources.watch_interval_seconds' "${CONFIG}")
MIN_FREE=$(jq -r '.resources.minimum_free_gpu_mib' "${CONFIG}")
MAX_UTIL=$(jq -r '.resources.maximum_gpu_utilization_percent' "${CONFIG}")
STABLE_CHECKS=$(jq -r '.resources.stable_checks' "${CONFIG}")
STABLE_INTERVAL=$(jq -r '.resources.stable_check_interval_seconds' "${CONFIG}")

mkdir -p "${OUTPUT}" "$(dirname "${STATE}")" /224010104/Jerry/logs/plan2
cd "${ROOT}"

atomic_state() {
  local state=$1
  local message=$2
  local gpu=${3:-}
  local runner_exit=${4:-null}
  local temporary=${STATE}.tmp.$$
  jq -n \
    --arg state "${state}" \
    --arg message "${message}" \
    --arg gpu "${gpu}" \
    --arg updated_at "$(date -Is)" \
    --arg host "$(hostname)" \
    --argjson runner_exit "${runner_exit}" \
    '{schema_version:1,state:$state,message:$message,selected_gpu:($gpu|if .=="" then null else tonumber end),runner_exit_code:$runner_exit,updated_at:$updated_at,host:$host}' \
    > "${temporary}"
  mv "${temporary}" "${STATE}"
  local status_tmp=${STATUS}.tmp.$$
  jq -n \
    --arg state "${state}" \
    --arg stage gpu_watch \
    --arg message "${message}" \
    --arg current_log /224010104/Jerry/logs/plan2/gpu-watch.log \
    --arg updated_at "$(date -Is)" \
    --arg host "$(hostname)" \
    '{schema_version:1,state:$state,stage:$stage,message:$message,current_log:$current_log,updated_at:$updated_at,host:$host}' \
    > "${status_tmp}"
  mv "${status_tmp}" "${STATUS}"
}

gpu_candidate() {
  local requested=${1:-}
  nvidia-smi --query-gpu=index,memory.free,utilization.gpu --format=csv,noheader,nounits \
    | tr -d ' ' \
    | sort -t, -k2,2nr \
    | awk -F, -v requested="${requested}" -v min_free="${MIN_FREE}" -v max_util="${MAX_UTIL}" '
      ($2+0)>=min_free && ($3+0)<=max_util && (requested=="" || $1==requested) {print; exit}'
}

stable_candidate() {
  local selected=''
  local row index free_mib utilization check
  for ((check=1; check<=STABLE_CHECKS; check++)); do
    row=$(gpu_candidate "${selected}")
    if [[ -z ${row} ]]; then
      return 1
    fi
    IFS=, read -r index free_mib utilization <<< "${row}"
    selected=${index}
    printf '[%s] 稳定检查 %s/%s gpu=%s free_mib=%s utilization=%s\n' \
      "$(date -Is)" "${check}" "${STABLE_CHECKS}" "${index}" "${free_mib}" "${utilization}" >&2
    if (( check < STABLE_CHECKS )); then
      sleep "${STABLE_INTERVAL}"
    fi
  done
  printf '%s\n' "${selected}"
}

on_signal() {
  atomic_state interrupted 'GPU 监测器收到终止信号，checkpoint 保留。'
  exit 130
}

trap on_signal INT TERM HUP

if [[ -z ${TMUX:-} ]]; then
  printf '%s\n' 'GPU 监测器必须在 tmux 中运行。'
  exit 125
fi
if [[ ! -x ${RUNNER} ]]; then
  printf 'Plan 2 runner 不可执行：%s\n' "${RUNNER}"
  exit 2
fi

exec 9>"${LOCK}"
if ! flock -n 9; then
  printf '%s\n' '已有 GPU 监测器持有锁，本实例退出。'
  exit 73
fi

printf 'start=%s tmux=%s host=%s cwd=%s command=%s interval=%ss min_free=%sMiB max_util=%s%%\n' \
  "$(date -Is)" "${TMUX}" "$(hostname)" "${PWD}" "$0" "${INTERVAL}" "${MIN_FREE}" "${MAX_UTIL}"
atomic_state waiting_for_gpu 'GPU 当前不满足门槛，持续监测。'

while true; do
  if [[ -f ${JERRY}/checkpoints/plan2/stages/decide.json ]] \
    && jq -e '.status=="complete"' "${JERRY}/checkpoints/plan2/stages/decide.json" >/dev/null; then
    atomic_state complete 'Plan 2 已全部完成，GPU 监测器正常退出。' '' 0
    printf '[%s] Plan 2 已完成。\n' "$(date -Is)"
    exit 0
  fi

  printf '[%s] GPU 快照\n' "$(date -Is)"
  nvidia-smi --query-gpu=index,memory.used,memory.free,utilization.gpu,temperature.gpu --format=csv,noheader,nounits
  selected=$(stable_candidate || true)
  if [[ -z ${selected} ]]; then
    atomic_state waiting_for_gpu "没有 GPU 连续 ${STABLE_CHECKS} 次满足门槛。"
    if [[ ${PLAN2_WATCH_ONCE:-0} == 1 ]]; then
      exit 75
    fi
    sleep "${INTERVAL}"
    continue
  fi

  atomic_state launching "GPU ${selected} 满足门槛，立即启动 Plan 2。" "${selected}"
  printf '[%s] selected_gpu=%s，启动 scripts/run_plan2.sh resume\n' "$(date -Is)" "${selected}"
  if [[ ${PLAN2_WATCH_DRY_RUN:-0} == 1 ]]; then
    atomic_state dry_run "GPU ${selected} 满足门槛；dry-run 未启动方案。" "${selected}" 0
    exit 0
  fi
  set +e
  PLAN2_SELECTED_GPU=${selected} "${RUNNER}" resume
  rc=$?
  set -e
  if [[ ${rc} -eq 0 ]]; then
    atomic_state complete 'Plan 2 runner 正常完成。' "${selected}" 0
    exit 0
  fi
  if [[ ${rc} -eq 75 || ${rc} -eq 73 ]]; then
    atomic_state waiting_for_gpu "资源条件变化或已有任务，runner 退出码 ${rc}，继续监测。" "${selected}" "${rc}"
    sleep "${INTERVAL}"
    continue
  fi
  atomic_state failed "Plan 2 出现非资源故障，停止自动重试，退出码 ${rc}。" "${selected}" "${rc}"
  exit "${rc}"
done
