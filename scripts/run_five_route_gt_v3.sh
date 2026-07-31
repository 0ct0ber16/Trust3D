#!/usr/bin/env bash
set -uo pipefail

ROOT=/224010104/Jerry/trust3d
JERRY=/224010104/Jerry
PYTHON=/224010104/miniconda3/envs/trust3d-sim/bin/python
MODE=${1:-status}
LOG_ROOT=${JERRY}/logs/parallel-v3
CHECKPOINT_ROOT=${JERRY}/checkpoints/parallel_v3/gt_five_route
OUTPUT=${ROOT}/outputs/parallel_v3/gt_five_route
LOCK=${CHECKPOINT_ROOT}/runner.lock
SIMULATOR_LOCK=${JERRY}/checkpoints/parallel_v2/simulator.lock
EVALUATION_LOCK=${CHECKPOINT_ROOT}/evaluation.lock
PRIVATE_EVALUATOR_LOCK=${JERRY}/checkpoints/parallel_v2/private_evaluator.lock
LOG_PATH=${LOG_ROOT}/gt-five-route-v3.log
LOCAL_XVFB_ROOT=${JERRY}/.local/xvfb
LOCAL_XVFB_LIB=${LOCAL_XVFB_ROOT}/root/usr/lib/x86_64-linux-gnu
LOCAL_XKB_ROOT=${LOCAL_XVFB_ROOT}/root/usr/share/X11/xkb
LOCAL_FONT_ROOT=${LOCAL_XVFB_ROOT}/root/usr/share/fonts/X11

export HOME=${JERRY}
export TMPDIR=${JERRY}/.tmp
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=${ROOT}${PYTHONPATH:+:${PYTHONPATH}}
export CUDA_VISIBLE_DEVICES=''
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-8}
export LP_NUM_THREADS=${LP_NUM_THREADS:-8}

mkdir -p "${LOG_ROOT}" "${CHECKPOINT_ROOT}" "${OUTPUT}" "${TMPDIR}"
cd "${ROOT}"

run_python() {
  "${PYTHON}" -m trust3d.parallel_v3.five_route "$@"
}

stage_complete() {
  local stage=$1
  jq -e '.status == "complete"' "${CHECKPOINT_ROOT}/stages/${stage}.json" >/dev/null 2>&1
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

set_online_command() {
  local batch=$1
  local episodes selection source_root exclusions
  episodes=$(jq -r ".batches.${batch}.public_path" "${OUTPUT}/online_batches.json")
  selection=$(jq -r ".batches.${batch}.selection_path" "${OUTPUT}/online_batches.json")
  source_root=$(jq -r ".batches.${batch}.source_root" "${OUTPUT}/online_batches.json")
  exclusions=$(jq -r ".batches.${batch}.exclusion_path" "${OUTPUT}/online_batches.json")
  ONLINE_COMMAND=(
    "${XVFB_RUN[@]}" "${PYTHON}" -u
    -m trust3d.agents.run_episode
    --online
    --episodes "${episodes}"
    --method trust3d
    --planner shortest_visible_pose
    --config configs/mvp.yaml
    --selection "${selection}"
    --source-checkpoints "${source_root}"
    --online-checkpoints "data/episodes/parallel_v3/gt5/online_checkpoints/${batch}"
    --alfred-json external/alfred/data/json_2.1.0
    --exclude-groups "${exclusions}"
    --output "outputs/parallel_v3/gt_five_route/online_${batch}_traces.jsonl"
  )
}

run_contract() {
  "${PYTHON}" -m pytest -q tests/test_five_route_v3.py || return $?
  run_python contract
}

run_evaluate() {
  exec 7>"${EVALUATION_LOCK}"
  exec 6>"${PRIVATE_EVALUATOR_LOCK}"
  printf 'evaluation_wait_start=%s locks=%s,%s\n' \
    "$(date -Is)" "${EVALUATION_LOCK}" "${PRIVATE_EVALUATOR_LOCK}"
  flock 7
  flock 6
  printf 'evaluation_acquired=%s\n' "$(date -Is)"
  run_python evaluate
  local rc=$?
  flock -u 6
  flock -u 7
  return "${rc}"
}

run_prepare() {
  run_python source-plan || return $?
  prepare_display || return $?
  exec 8>"${SIMULATOR_LOCK}"
  printf 'simulator_wait_start=%s lock=%s stage=prepare\n' "$(date -Is)" "${SIMULATOR_LOCK}"
  flock 8
  printf 'simulator_acquired=%s stage=prepare\n' "$(date -Is)"
  "${XVFB_RUN[@]}" "${PYTHON}" -u -m trust3d.data.build_branches \
    --candidates data/episodes/parallel_v3/gt5/fresh_mvp_candidates.jsonl \
    --alfred-json external/alfred/data/json_2.1.0 \
    --num-source-events 40 \
    --branches fresh_stable risk_stable risk_stale \
    --questions-per-branch 1 \
    --replay-runs 1 \
    --seed 20260731 \
    --exclude-groups data/episodes/parallel_v3/gt5/fresh_empty_exclusions.json \
    --output data/episodes/parallel_v3/gt5/fresh_mvp
  local mvp_rc=$?
  if (( mvp_rc == 0 )); then
    "${XVFB_RUN[@]}" "${PYTHON}" -u -m trust3d.data.build_spatial \
      --selection data/episodes/parallel_v3/gt5/fresh_spatial_candidates.json \
      --alfred-json external/alfred/data/json_2.1.0 \
      --exclude-groups data/episodes/parallel_v3/gt5/fresh_empty_exclusions.json \
      --output data/episodes/parallel_v3/gt5/fresh_spatial \
      --target-groups 8 \
      --seed 20260732
  fi
  local spatial_rc=$?
  flock -u 8
  (( mvp_rc == 0 )) || return "${mvp_rc}"
  (( spatial_rc == 0 )) || return "${spatial_rc}"
  run_python source-audit || return $?
  run_python prepare
}

run_online() {
  run_python prepare-online || return $?
  prepare_display || return $?
  exec 8>"${SIMULATOR_LOCK}"
  printf 'simulator_wait_start=%s lock=%s\n' "$(date -Is)" "${SIMULATOR_LOCK}"
  flock 8
  printf 'simulator_acquired=%s\n' "$(date -Is)"
  if [[ ! -f ${OUTPUT}/online_interruption_probe.json ]]; then
    set_online_command sealed
    set +e
    timeout 5s "${ONLINE_COMMAND[@]}"
    local probe_rc=$?
    set -e
    run_python record-interruption --exit-code "${probe_rc}" || return $?
  fi
  local batch rc=0
  for batch in sealed replication; do
    set_online_command "${batch}"
    timeout 43200s "${ONLINE_COMMAND[@]}"
    rc=$?
    (( rc == 0 )) || break
  done
  flock -u 8
  (( rc == 0 )) || return "${rc}"
  run_python merge-online || return $?
  run_python validate-online --traces "${OUTPUT}/online_traces.jsonl"
}

run_stage() {
  local stage=$1
  if stage_complete "${stage}"; then
    printf 'resume_stage=%s action=skip_verified\n' "${stage}"
    return 0
  fi
  case "${stage}" in
    preflight) run_python preflight ;;
    contract) run_contract ;;
    prepare) run_prepare ;;
    freeze) run_python freeze ;;
    infer) run_python infer ;;
    evaluate) run_evaluate ;;
    recover) run_python recover ;;
    online) run_online ;;
    report) run_python report ;;
    *) printf '未知阶段：%s\n' "${stage}"; return 2 ;;
  esac
}

run_resume() {
  local stage
  for stage in preflight contract prepare freeze infer evaluate recover online report; do
    run_stage "${stage}" || return $?
  done
}

run_locked() {
  if [[ -z ${TMUX:-} ]]; then
    printf '%s\n' 'GT 五路 v3 runner 必须在 tmux 中执行。'
    return 125
  fi
  exec 9>"${LOCK}"
  if ! flock -n 9; then
    printf '%s\n' 'GT 五路 v3 runner 已在运行，本实例退出。'
    return 73
  fi
  printf 'start=%s host=%s tmux=%s pane=%s cwd=%s command=%q log=%s git=%s dirty=%s\n' \
    "$(date -Is)" "$(hostname)" "${TMUX}" "${TMUX_PANE:-}" "${PWD}" "$0 $*" \
    "${LOG_PATH}" "$(git rev-parse HEAD)" "$(git status --porcelain | wc -l)"
  case "${MODE}" in
    preflight|freeze|infer|recover|report) run_python "${MODE}" ;;
    evaluate) run_evaluate ;;
    prepare) run_prepare ;;
    contract) run_contract ;;
    online) run_online ;;
    resume) run_resume ;;
    status) run_python status ;;
    *) printf '不支持的 A v3 模式：%s\n' "${MODE}"; return 2 ;;
  esac
}

set +e
run_locked 2>&1 | tee -a "${LOG_PATH}"
rc=${PIPESTATUS[0]}
set -e
printf 'exit_code=%s end=%s\n' "${rc}" "$(date -Is)" | tee -a "${LOG_PATH}"
exit "${rc}"
