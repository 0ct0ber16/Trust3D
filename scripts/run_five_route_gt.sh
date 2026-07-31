#!/usr/bin/env bash
set -uo pipefail

ROOT=/224010104/Jerry/trust3d
JERRY=/224010104/Jerry
PYTHON=/224010104/miniconda3/envs/trust3d-sim/bin/python
MODE=${1:-status}
LOG_ROOT=${JERRY}/logs/parallel_v2
CHECKPOINT_ROOT=${JERRY}/checkpoints/parallel_v2
OUTPUT=${ROOT}/outputs/parallel_v2/gt_five_route
LOCK=${CHECKPOINT_ROOT}/gt_five_route.lock
SIMULATOR_LOCK=${CHECKPOINT_ROOT}/simulator.lock
LOG_PATH=${LOG_ROOT}/gt-five-route.log
LOCAL_XVFB_ROOT=${JERRY}/.local/xvfb
LOCAL_XVFB_LIB=${LOCAL_XVFB_ROOT}/root/usr/lib/x86_64-linux-gnu
LOCAL_XKB_ROOT=${LOCAL_XVFB_ROOT}/root/usr/share/X11/xkb
LOCAL_FONT_ROOT=${LOCAL_XVFB_ROOT}/root/usr/share/fonts/X11

export HOME=${JERRY}
export TMPDIR=${JERRY}/.tmp
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=${ROOT}${PYTHONPATH:+:${PYTHONPATH}}
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-8}
export LP_NUM_THREADS=${LP_NUM_THREADS:-8}

mkdir -p "${LOG_ROOT}" "${CHECKPOINT_ROOT}" "${OUTPUT}" "${TMPDIR}"
cd "${ROOT}"

status() {
  "${PYTHON}" -m trust3d.parallel_v2.runtime status gt_five_route "$1" "$2" --log "${LOG_PATH}"
}

mark() {
  local stage=$1
  local output=$2
  local next=$3
  "${PYTHON}" -m trust3d.parallel_v2.runtime mark "${stage}" complete "${stage} 已完成。" --output "${output}" --next-checkpoint "${next}"
}

stage_complete() {
  "${PYTHON}" -m trust3d.parallel_v2.runtime stage-complete "$1" >/dev/null 2>&1
}

resume_stage() {
  local stage=$1
  local runner=$2
  if stage_complete "${stage}"; then
    printf 'resume_stage=%s action=skip_verified\n' "${stage}"
    return 0
  fi
  "${runner}"
}

prepare_display() {
  if command -v xvfb-run >/dev/null 2>&1; then
    XVFB_RUN=(xvfb-run -a)
    return 0
  fi
  if [[ ! -x ${LOCAL_XVFB_ROOT}/bin/Xvfb ]] \
    || [[ ! -x ${LOCAL_XVFB_ROOT}/root/usr/bin/xvfb-run ]] \
    || [[ ! -x ${ROOT}/xkbcomp ]]; then
    printf '%s\n' '本地 Xvfb 不完整，无法执行 AI2-THOR 在线校验。'
    return 127
  fi
  export PATH="${LOCAL_XVFB_ROOT}/bin:${LOCAL_XVFB_ROOT}/root/usr/bin:${PATH}"
  export LD_LIBRARY_PATH="${LOCAL_XVFB_LIB}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
  export LIBGL_DRIVERS_PATH=${LOCAL_XVFB_LIB}/dri
  export LIBGL_ALWAYS_SOFTWARE=1
  XVFB_RUN=(
    xvfb-run -a
    --server-args="-screen 0 1280x1024x24 -nolisten tcp -xkbdir ${LOCAL_XKB_ROOT} -fp ${LOCAL_FONT_ROOT}/misc,${LOCAL_FONT_ROOT}/Type1"
  )
}

run_prepare() {
  status running 'A0：构建 GT 五路 public/private 数据。'
  "${PYTHON}" -m trust3d.data.build_five_route --config configs/five_route_gt_v1.json || return $?
  mark gt5_prepare outputs/parallel_v2/gt_five_route/prepare.json gt5_unit
}

run_unit() {
  status running 'A1：执行五路合成契约测试。'
  "${PYTHON}" -m trust3d.parallel_v2.five_route unit || return $?
  mark gt5_unit outputs/parallel_v2/gt_five_route/unit.json gt5_pilot
}

run_pilot() {
  status running 'A2：冻结 pilot router 并执行功效审计。'
  "${PYTHON}" -m trust3d.parallel_v2.five_route pilot || return $?
  mark gt5_pilot outputs/parallel_v2/gt_five_route/router_lock.json gt5_offline
}

run_offline() {
  status running 'A3：执行 final 离线五路路由与配对 bootstrap。'
  "${PYTHON}" -m trust3d.parallel_v2.five_route offline || return $?
  mark gt5_offline outputs/parallel_v2/gt_five_route/metrics.json gt5_recovery
}

run_recover() {
  status running 'A3：执行纯 CPU checkpoint 恢复校验。'
  CUDA_VISIBLE_DEVICES='' "${PYTHON}" -m trust3d.parallel_v2.five_route recover || return $?
  mark gt5_recovery outputs/parallel_v2/gt_five_route/checkpoint_recovery.json gt5_online
}

run_online() {
  status running 'A4：执行 AI2-THOR 在线 REOBSERVE 校验。'
  "${PYTHON}" -m trust3d.parallel_v2.five_route prepare-online || return $?
  prepare_display || return $?
  exec 8>"${SIMULATOR_LOCK}"
  flock 8
  timeout 43200s "${XVFB_RUN[@]}" "${PYTHON}" -u \
    -m trust3d.agents.run_episode \
    --online \
    --episodes data/episodes/parallel_v2/gt5/online_reobserve_public.jsonl \
    --method trust3d \
    --planner shortest_visible_pose \
    --config configs/mvp.yaml \
    --selection data/episodes/mvp/selection.json \
    --source-checkpoints data/episodes/mvp \
    --online-checkpoints data/episodes/parallel_v2/gt5/online_checkpoints \
    --alfred-json external/alfred/data/json_2.1.0 \
    --exclude-groups configs/gate3_exclusions.json \
    --output outputs/parallel_v2/gt_five_route/online_traces.jsonl
  local rc=$?
  flock -u 8
  (( rc == 0 )) || return "${rc}"
  "${PYTHON}" -m trust3d.parallel_v2.five_route validate-online \
    --traces outputs/parallel_v2/gt_five_route/online_traces.jsonl || return $?
  mark gt5_online outputs/parallel_v2/gt_five_route/online_validation.json gt5_report
}

run_report() {
  status running 'A5：生成 GT 五路实验报告。'
  "${PYTHON}" -m trust3d.parallel_v2.five_route report || return $?
  mark gt_five_route_report outputs/parallel_v2/gt_five_route/report.json complete
  status complete 'GT 五路路由实验已完成。'
}

run_resume() {
  "${PYTHON}" -m trust3d.parallel_v2.runtime wait-cpu gt_five_route --interval 1
  "${PYTHON}" -m trust3d.parallel_v2.runtime verify-baseline
  resume_stage gt5_prepare run_prepare || return $?
  resume_stage gt5_unit run_unit || return $?
  resume_stage gt5_pilot run_pilot || return $?
  resume_stage gt5_offline run_offline || return $?
  resume_stage gt5_recovery run_recover || return $?
  resume_stage gt5_online run_online || return $?
  resume_stage gt_five_route_report run_report
}

run_locked() {
  if [[ -z ${TMUX:-} ]]; then
    printf '%s\n' 'GT 五路 runner 必须在 tmux 中执行。'
    return 125
  fi
  exec 9>"${LOCK}"
  if ! flock -n 9; then
    printf '%s\n' 'GT 五路 runner 已在运行，本实例退出。'
    return 73
  fi
  printf 'start=%s host=%s tmux=%s pane=%s cwd=%s command=%q log=%s git=%s dirty=%s\n' \
    "$(date -Is)" "$(hostname)" "${TMUX}" "${TMUX_PANE:-}" "${PWD}" "$0 $*" "${LOG_PATH}" "$(git rev-parse HEAD)" "$(git status --porcelain | wc -l)"
  case "${MODE}" in
    prepare) run_prepare ;;
    unit) run_unit ;;
    pilot) run_pilot ;;
    offline) run_offline ;;
    online) run_online ;;
    recover) run_recover ;;
    report) run_report ;;
    resume) run_resume ;;
    status) "${PYTHON}" -m trust3d.parallel_v2.orchestrator status ;;
    *) printf '不支持的 A 线模式：%s\n' "${MODE}"; return 2 ;;
  esac
}

set +e
run_locked 2>&1 | tee -a "${LOG_PATH}"
rc=${PIPESTATUS[0]}
set -e
printf 'exit_code=%s end=%s next_checkpoint=%s\n' "${rc}" "$(date -Is)" "$(jq -r '.next_checkpoint // "unknown"' "${CHECKPOINT_ROOT}/stages/gt5_report.json" 2>/dev/null || printf unknown)" | tee -a "${LOG_PATH}"
if (( rc != 0 )); then
  status failed "GT 五路 runner 退出码 ${rc}，保留 checkpoint。" || true
fi
exit "${rc}"
