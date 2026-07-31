#!/usr/bin/env bash
set -uo pipefail

ROOT=/224010104/Jerry/trust3d
JERRY=/224010104/Jerry
SIM_PY=/224010104/miniconda3/envs/trust3d-sim/bin/python
CUT3R_PY=/224010104/Jerry/.conda/envs/cut3r/bin/python
VGGT_PY=/224010104/Jerry/.conda/envs/vggt/bin/python
MODE=${1:-status}
LOG_ROOT=${JERRY}/logs/parallel_v2
CHECKPOINT_ROOT=${JERRY}/checkpoints/parallel_v2
OUTPUT=${ROOT}/outputs/parallel_v2/integration
DATA=${ROOT}/data/episodes/parallel_v2/integration
LOCK=${CHECKPOINT_ROOT}/integration.lock
SIMULATOR_LOCK=${CHECKPOINT_ROOT}/simulator.lock
PRIVATE_EVALUATOR_LOCK=${CHECKPOINT_ROOT}/private_evaluator.lock
LOG_PATH=${LOG_ROOT}/integration.log
ATTEMPT_LOG=${LOG_ROOT}/integration-gpu-attempt.log
LOCAL_XVFB_ROOT=${JERRY}/.local/xvfb
LOCAL_XVFB_LIB=${LOCAL_XVFB_ROOT}/root/usr/lib/x86_64-linux-gnu
LOCAL_XKB_ROOT=${LOCAL_XVFB_ROOT}/root/usr/share/X11/xkb
LOCAL_FONT_ROOT=${LOCAL_XVFB_ROOT}/root/usr/share/fonts/X11
MIN_FREE=$(jq -r '.resources.minimum_free_gpu_mib' "${ROOT}/configs/parallel_v2_protocol.json")
MAX_UTIL=$(jq -r '.resources.maximum_gpu_utilization_percent' "${ROOT}/configs/parallel_v2_protocol.json")
INTERVAL=$(jq -r '.resources.watch_interval_seconds' "${ROOT}/configs/parallel_v2_protocol.json")

export HOME=${JERRY}
export TMPDIR=${JERRY}/.tmp
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=${ROOT}${PYTHONPATH:+:${PYTHONPATH}}
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-8}
export LP_NUM_THREADS=${LP_NUM_THREADS:-8}

mkdir -p "${LOG_ROOT}" "${CHECKPOINT_ROOT}" "${OUTPUT}" "${TMPDIR}"
cd "${ROOT}"

status() {
  "${SIM_PY}" -m trust3d.parallel_v2.runtime status integration "$1" "$2" --log "${LOG_PATH}"
}

mark() {
  "${SIM_PY}" -m trust3d.parallel_v2.runtime mark "$1" complete "$1 已完成。" --output "$2" --next-checkpoint "$3"
}

stage_complete() {
  "${SIM_PY}" -m trust3d.parallel_v2.runtime stage-complete "$1" >/dev/null 2>&1
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

with_private_evaluator_lock() {
  exec 7>"${PRIVATE_EVALUATOR_LOCK}"
  flock 7
  "$@"
  local rc=$?
  flock -u 7
  return "${rc}"
}

prepare_display() {
  if command -v xvfb-run >/dev/null 2>&1; then
    XVFB_RUN=(xvfb-run -a)
    return 0
  fi
  if [[ ! -x ${LOCAL_XVFB_ROOT}/bin/Xvfb ]] \
    || [[ ! -x ${LOCAL_XVFB_ROOT}/root/usr/bin/xvfb-run ]] \
    || [[ ! -x ${ROOT}/xkbcomp ]]; then
    printf '%s\n' '本地 Xvfb 不完整，无法生成 integration holdout。'
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

run_unit() {
  status running 'C0-C1：校验冻结 router、adapter、confidence 和接口契约。'
  "${SIM_PY}" -m trust3d.parallel_v2.integration unit || return $?
  mark integration_unit outputs/parallel_v2/integration/integration_lock.json integration_prepare
}

run_prepare() {
  status running 'C2：生成与 A/B/legacy 场景和源轨迹均不重叠的 integration holdout。'
  prepare_display || return $?
  exec 8>"${SIMULATOR_LOCK}"
  flock 8
  timeout 86400s "${XVFB_RUN[@]}" "${SIM_PY}" -u \
    -m trust3d.data.build_parallel_integration build
  local rc=$?
  flock -u 8
  (( rc == 0 )) || return "${rc}"
  "${SIM_PY}" -m trust3d.agents.run_episode \
    --episodes data/episodes/parallel_v2/integration/source/episodes_public.jsonl \
    --methods always_trust always_reobserve global_ttl fact_freshness trust3d \
    --config configs/mvp.yaml \
    --output outputs/parallel_v2/integration/source_routes.jsonl || return $?
  mark integration_prepare outputs/parallel_v2/integration/prepare.json integration_gpu_watch
}

cut3r_geometry() {
  local grounding=$1
  local output=$2
  "${CUT3R_PY}" -u -m trust3d.geometry.run_cut3r \
    --episodes data/episodes/parallel_v2/integration/source/episodes_public.jsonl \
    --checkpoint external/cut3r/src/cut3r_512_dpt_4_64.pth \
    --output "${output}" \
    --source-checkpoints data/episodes/parallel_v2/integration/source/checkpoints \
    --dataset-root data/episodes/parallel_v2/integration/source \
    --sequence-manifest data/episodes/parallel_v2/integration/source/rgb_sequences.json \
    --cut3r-root external/cut3r \
    --device cuda \
    --image-size 512 \
    --center-crop-fraction 0.12 \
    --grounding "${grounding}" \
    --saliency-quantile 0.82 \
    --grounding-minimum-fraction 0.08 \
    --grounding-maximum-fraction 0.30 \
    --continue-on-error
}

vggt_geometry() {
  local grounding=$1
  local output=$2
  "${VGGT_PY}" -u -m trust3d.geometry.run_vggt \
    --episodes data/episodes/parallel_v2/integration/source/episodes_public.jsonl \
    --routes outputs/parallel_v2/integration/source_routes.jsonl \
    --checkpoint external/vggt/model.safetensors \
    --config "outputs/parallel_v2/gate7_fix/configs/vggt_${grounding}.json" \
    --output "${output}" \
    --source-checkpoints data/episodes/parallel_v2/integration/source/checkpoints \
    --dataset-root data/episodes/parallel_v2/integration/source \
    --sequence-manifest data/episodes/parallel_v2/integration/source/rgb_sequences.json \
    --vggt-root external/vggt \
    --cut3r-geometry outputs/gate7/cut3r_geometry \
    --device cuda \
    --continue-on-error
}

run_gpu_pipeline() {
  local gpu=$1
  local backend grounding geometry
  backend=$(jq -r '.best_backend' outputs/parallel_v2/gate7_fix/holdout_metrics.json)
  grounding=$(jq -r ".selected.${backend}.grounding" outputs/parallel_v2/gate7_fix/adapter_lock.json)
  geometry="outputs/parallel_v2/integration/geometry/${backend}_${grounding}"
  export CUDA_VISIBLE_DEVICES=${gpu}
  status running "GPU ${gpu} 已准入；执行冻结 ${backend}/${grounding} integration 前向。"
  if ! stage_complete integration_inference; then
    if [[ ${backend} == cut3r ]]; then
      cut3r_geometry "${grounding}" "${geometry}" || return $?
    else
      vggt_geometry "${grounding}" "${geometry}" || return $?
    fi
    "${SIM_PY}" -m trust3d.parallel_v2.integration seal \
      --geometry "${geometry}" --backend "${backend}" --grounding "${grounding}" || return $?
    mark integration_inference outputs/parallel_v2/integration/inference_complete.json integration_evaluate || return $?
  else
    printf '%s\n' 'resume_stage=integration_inference action=skip_verified'
  fi
  if ! stage_complete integration_evaluate; then
    CUDA_VISIBLE_DEVICES='' with_private_evaluator_lock "${SIM_PY}" \
      -m trust3d.parallel_v2.integration evaluate || return $?
    mark integration_evaluate outputs/parallel_v2/integration/metrics.json integration_recovery || return $?
  fi
  if ! stage_complete integration_recovery; then
    CUDA_VISIBLE_DEVICES='' "${SIM_PY}" -m trust3d.parallel_v2.integration recover \
      --geometry "${geometry}" --backend "${backend}" --grounding "${grounding}" || return $?
    mark integration_recovery outputs/parallel_v2/integration/checkpoint_recovery.json integration_report || return $?
  fi
  if ! stage_complete integration_report; then
    CUDA_VISIBLE_DEVICES='' "${SIM_PY}" -m trust3d.parallel_v2.integration report || return $?
    CUDA_VISIBLE_DEVICES='' "${SIM_PY}" -m trust3d.parallel_v2.orchestrator report || return $?
    mark integration_report outputs/parallel_v2/integration/report.json complete || return $?
  fi
  status complete 'RGB 五路联合实验已完成并生成报告。'
}

gpu_candidate() {
  nvidia-smi --query-gpu=index,memory.free,utilization.gpu --format=csv,noheader,nounits \
    | tr -d ' ' \
    | sort -t, -k2,2nr \
    | awk -F, -v min_free="${MIN_FREE}" -v max_util="${MAX_UTIL}" \
      '$2+0>=min_free && $3+0<=max_util {print; exit}'
}

atomic_gpu_recheck() {
  local gpu=$1
  nvidia-smi --id="${gpu}" --query-gpu=index,memory.free,utilization.gpu --format=csv,noheader,nounits \
    | tr -d ' ' \
    | awk -F, -v min_free="${MIN_FREE}" -v max_util="${MAX_UTIL}" \
      '$2+0>=min_free && $3+0<=max_util {print; exit}'
}

run_watch() {
  status waiting_for_resource "integration 等待满足 ${MIN_FREE} MiB/${MAX_UTIL}% 门槛的 GPU。"
  while true; do
    "${SIM_PY}" -m trust3d.parallel_v2.runtime wait-cpu integration --interval "${INTERVAL}" >/dev/null
    local selected rechecked gpu free util
    selected=$(gpu_candidate || true)
    if [[ -z ${selected} ]]; then
      sleep "${INTERVAL}"
      continue
    fi
    IFS=, read -r gpu free util <<< "${selected}"
    rechecked=$(atomic_gpu_recheck "${gpu}" || true)
    if [[ -z ${rechecked} ]]; then
      continue
    fi
    : > "${ATTEMPT_LOG}"
    set +e
    run_gpu_pipeline "${gpu}" 2>&1 | tee -a "${ATTEMPT_LOG}"
    local rc=${PIPESTATUS[0]}
    set -e
    if (( rc == 0 )); then
      return 0
    fi
    if rg -qi 'out of memory|cuda.*memory|cuda error|cublas.*alloc' "${ATTEMPT_LOG}"; then
      status waiting_for_resource "integration 遇到资源型 CUDA 故障，保留 checkpoint 后返回监测。"
      sleep "${INTERVAL}"
      continue
    fi
    return "${rc}"
  done
}

run_resume() {
  "${SIM_PY}" -m trust3d.parallel_v2.runtime wait-cpu integration --interval 1
  "${SIM_PY}" -m trust3d.parallel_v2.runtime verify-baseline
  resume_stage integration_unit run_unit || return $?
  resume_stage integration_prepare run_prepare || return $?
  if stage_complete integration_report; then
    status complete 'RGB 五路联合实验已从有效 checkpoint 确认完成。'
    return 0
  fi
  run_watch
}

run_recover_cpu() {
  local backend grounding geometry
  backend=$(jq -r '.best_backend' outputs/parallel_v2/gate7_fix/holdout_metrics.json)
  grounding=$(jq -r ".selected.${backend}.grounding" outputs/parallel_v2/gate7_fix/adapter_lock.json)
  geometry="outputs/parallel_v2/integration/geometry/${backend}_${grounding}"
  CUDA_VISIBLE_DEVICES='' "${SIM_PY}" -m trust3d.parallel_v2.integration recover \
    --geometry "${geometry}" --backend "${backend}" --grounding "${grounding}"
}

run_evaluate_cpu() {
  CUDA_VISIBLE_DEVICES='' "${SIM_PY}" -m trust3d.parallel_v2.integration evaluate
}

run_report_cpu() {
  CUDA_VISIBLE_DEVICES='' "${SIM_PY}" -m trust3d.parallel_v2.integration report && \
    CUDA_VISIBLE_DEVICES='' "${SIM_PY}" -m trust3d.parallel_v2.orchestrator report
}

run_locked() {
  if [[ -z ${TMUX:-} ]]; then
    printf '%s\n' 'integration runner 必须在 tmux 中执行。'
    return 125
  fi
  exec 9>"${LOCK}"
  if ! flock -n 9; then
    printf '%s\n' 'integration runner 已在运行，本实例退出。'
    return 73
  fi
  printf 'start=%s host=%s tmux=%s pane=%s cwd=%s command=%q log=%s git=%s dirty=%s\n' \
    "$(date -Is)" "$(hostname)" "${TMUX}" "${TMUX_PANE:-}" "${PWD}" "$0 $*" "${LOG_PATH}" "$(git rev-parse HEAD)" "$(git status --porcelain | wc -l)"
  case "${MODE}" in
    unit) run_unit ;;
    run) run_prepare && run_watch ;;
    recover) run_recover_cpu ;;
    evaluate) run_evaluate_cpu ;;
    report) run_report_cpu ;;
    resume) run_resume ;;
    status) "${SIM_PY}" -m trust3d.parallel_v2.orchestrator status ;;
    *) printf '不支持的 C 线模式：%s\n' "${MODE}"; return 2 ;;
  esac
}

set +e
run_locked 2>&1 | tee -a "${LOG_PATH}"
rc=${PIPESTATUS[0]}
set -e
printf 'exit_code=%s end=%s\n' "${rc}" "$(date -Is)" | tee -a "${LOG_PATH}"
if (( rc != 0 )); then
  status failed "integration runner 退出码 ${rc}，checkpoint 已保留。" || true
fi
exit "${rc}"
