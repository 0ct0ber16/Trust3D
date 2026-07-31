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
OUTPUT=${ROOT}/outputs/parallel_v2/gate7_fix
DATA=${ROOT}/data/episodes/parallel_v2/gate7_fix
LOCK=${CHECKPOINT_ROOT}/gate7_fix.lock
SIMULATOR_LOCK=${CHECKPOINT_ROOT}/simulator.lock
PRIVATE_EVALUATOR_LOCK=${CHECKPOINT_ROOT}/private_evaluator.lock
LOG_PATH=${LOG_ROOT}/gate7-fix.log
ATTEMPT_LOG=${LOG_ROOT}/gate7-gpu-attempt.log
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
  "${SIM_PY}" -m trust3d.parallel_v2.runtime status gate7_fix "$1" "$2" --log "${LOG_PATH}"
}

mark() {
  "${SIM_PY}" -m trust3d.parallel_v2.runtime mark "$1" complete "$1 已完成。" --output "$2" --next-checkpoint "$3"
}

mark_many() {
  local stage=$1
  local next=$2
  shift 2
  local args=()
  local output
  for output in "$@"; do
    args+=(--output "${output}")
  done
  "${SIM_PY}" -m trust3d.parallel_v2.runtime mark \
    "${stage}" complete "${stage} 已完成。" "${args[@]}" --next-checkpoint "${next}"
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
    printf '%s\n' '本地 Xvfb 不完整，无法生成 Gate 7 新数据。'
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

run_lock() {
  status running 'B0：锁定 Gate 7/方案2/诊断起点。'
  "${SIM_PY}" -m trust3d.parallel_v2.gate7_fix lock || return $?
  mark gate7_lock outputs/parallel_v2/gate7_fix/protocol_lock.json gate7_unit
}

run_unit() {
  status running 'B1：执行任务头、外参方向、坐标轴和角色绑定测试。'
  "${SIM_PY}" -m trust3d.parallel_v2.gate7_fix unit || return $?
  mark gate7_unit outputs/parallel_v2/gate7_fix/unit.json gate7_prepare
}

generate_routes() {
  local split=$1
  mkdir -p "${OUTPUT}/${split}"
  "${SIM_PY}" -m trust3d.agents.run_episode \
    --episodes "data/episodes/parallel_v2/gate7_fix/${split}/episodes_public.jsonl" \
    --methods always_trust always_reobserve global_ttl fact_freshness trust3d \
    --config configs/mvp.yaml \
    --output "outputs/parallel_v2/gate7_fix/${split}/routes.jsonl"
}

run_prepare() {
  status running 'B2：生成与 legacy30、pilot、holdout 均不重叠的新数据。'
  prepare_display || return $?
  exec 8>"${SIMULATOR_LOCK}"
  flock 8
  timeout 86400s "${XVFB_RUN[@]}" "${SIM_PY}" -u \
    -m trust3d.data.build_gate7_holdout build \
    --config configs/gate7_fix_v1.json
  local rc=$?
  flock -u 8
  (( rc == 0 )) || return "${rc}"
  generate_routes pilot || return $?
  generate_routes holdout || return $?
  "${SIM_PY}" -m trust3d.parallel_v2.gate7_fix prepare || return $?
  mark gate7_prepare outputs/parallel_v2/gate7_fix/dataset_lock.json gate7_gpu_watch
}

cut3r_geometry() {
  local split=$1
  local grounding=$2
  local output=$3
  "${CUT3R_PY}" -u -m trust3d.geometry.run_cut3r \
    --episodes "data/episodes/parallel_v2/gate7_fix/${split}/episodes_public.jsonl" \
    --checkpoint external/cut3r/src/cut3r_512_dpt_4_64.pth \
    --output "${output}" \
    --source-checkpoints "data/episodes/parallel_v2/gate7_fix/${split}/checkpoints" \
    --dataset-root "data/episodes/parallel_v2/gate7_fix/${split}" \
    --sequence-manifest "data/episodes/parallel_v2/gate7_fix/${split}/rgb_sequences.json" \
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
  local split=$1
  local grounding=$2
  local output=$3
  local cut3r_reference=$4
  "${VGGT_PY}" -u -m trust3d.geometry.run_vggt \
    --episodes "data/episodes/parallel_v2/gate7_fix/${split}/episodes_public.jsonl" \
    --routes "outputs/parallel_v2/gate7_fix/${split}/routes.jsonl" \
    --checkpoint external/vggt/model.safetensors \
    --config "outputs/parallel_v2/gate7_fix/configs/vggt_${grounding}.json" \
    --output "${output}" \
    --source-checkpoints "data/episodes/parallel_v2/gate7_fix/${split}/checkpoints" \
    --dataset-root "data/episodes/parallel_v2/gate7_fix/${split}" \
    --sequence-manifest "data/episodes/parallel_v2/gate7_fix/${split}/rgb_sequences.json" \
    --vggt-root external/vggt \
    --cut3r-geometry "${cut3r_reference}" \
    --device cuda \
    --continue-on-error
}

seal_variant() {
  local split=$1
  local backend=$2
  local grounding=$3
  local geometry=$4
  "${SIM_PY}" -m trust3d.parallel_v2.gate7_fix seal \
    --split "${split}" \
    --backend "${backend}" \
    --grounding "${grounding}" \
    --geometry "${geometry}"
}

run_gpu_pipeline() {
  local gpu=$1
  export CUDA_VISIBLE_DEVICES=${gpu}
  status running "GPU ${gpu} 已准入；同一 runner 连续执行 B3-B6。"
  if ! stage_complete gate7_pilot; then
    for grounding in center_crop rgb_saliency_v1; do
      local cut3r_output="outputs/parallel_v2/gate7_fix/pilot_geometry/cut3r_${grounding}"
      local vggt_output="outputs/parallel_v2/gate7_fix/pilot_geometry/vggt_${grounding}"
      cut3r_geometry pilot "${grounding}" "${cut3r_output}" || return $?
      seal_variant pilot cut3r "${grounding}" "${cut3r_output}" || return $?
      vggt_geometry pilot "${grounding}" "${vggt_output}" "${cut3r_output}" || return $?
      seal_variant pilot vggt "${grounding}" "${vggt_output}" || return $?
    done
    with_private_evaluator_lock "${SIM_PY}" \
      -m trust3d.parallel_v2.gate7_fix pilot-evaluate || return $?
    mark_many gate7_pilot gate7_holdout \
      outputs/parallel_v2/gate7_fix/adapter_lock.json \
      outputs/parallel_v2/gate7_fix/confidence_lock.json \
      outputs/parallel_v2/gate7_fix/pilot_metrics.json || return $?
  else
    printf '%s\n' 'resume_stage=gate7_pilot action=skip_verified'
  fi

  local backend grounding geometry
  if ! stage_complete gate7_holdout; then
    for backend in cut3r vggt; do
      grounding=$(jq -r ".selected.${backend}.grounding" "${OUTPUT}/adapter_lock.json")
      geometry="outputs/parallel_v2/gate7_fix/holdout_geometry/${backend}_${grounding}"
      if [[ ${backend} == cut3r ]]; then
        cut3r_geometry holdout "${grounding}" "${geometry}" || return $?
      else
        local cut3r_grounding cut3r_reference
        cut3r_grounding=$(jq -r '.selected.cut3r.grounding' "${OUTPUT}/adapter_lock.json")
        cut3r_reference="outputs/parallel_v2/gate7_fix/holdout_geometry/cut3r_${cut3r_grounding}"
        vggt_geometry holdout "${grounding}" "${geometry}" "${cut3r_reference}" || return $?
      fi
      seal_variant holdout "${backend}" "${grounding}" "${geometry}" || return $?
    done
    local cut3r_selected vggt_selected
    cut3r_selected=$(jq -r '.selected.cut3r.grounding' "${OUTPUT}/adapter_lock.json")
    vggt_selected=$(jq -r '.selected.vggt.grounding' "${OUTPUT}/adapter_lock.json")
    mark_many gate7_holdout gate7_evaluate \
      "outputs/parallel_v2/gate7_fix/holdout/cut3r_${cut3r_selected}/inference_complete.json" \
      "outputs/parallel_v2/gate7_fix/holdout/vggt_${vggt_selected}/inference_complete.json" || return $?
  else
    printf '%s\n' 'resume_stage=gate7_holdout action=skip_verified'
  fi
  if ! stage_complete gate7_evaluate; then
    CUDA_VISIBLE_DEVICES='' with_private_evaluator_lock "${SIM_PY}" \
      -m trust3d.parallel_v2.gate7_fix evaluate || return $?
    mark gate7_evaluate outputs/parallel_v2/gate7_fix/holdout_metrics.json gate7_recovery || return $?
  fi
  if ! stage_complete gate7_recovery; then
    CUDA_VISIBLE_DEVICES='' "${SIM_PY}" -m trust3d.parallel_v2.gate7_fix recover || return $?
    mark gate7_evaluate outputs/parallel_v2/gate7_fix/holdout_metrics.json gate7_recovery || return $?
    mark gate7_recovery outputs/parallel_v2/gate7_fix/checkpoint_recovery.json gate7_report || return $?
  fi
  if ! stage_complete gate7_fix_report; then
    CUDA_VISIBLE_DEVICES='' "${SIM_PY}" -m trust3d.parallel_v2.gate7_fix report || return $?
    mark gate7_fix_report outputs/parallel_v2/gate7_fix/report.json complete || return $?
  fi
  status complete 'Gate 7 修复实验已完成并生成报告。'
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
  status waiting_for_resource "没有 GPU 满足 ${MIN_FREE} MiB/${MAX_UTIL}% 门槛，持续监测。"
  while true; do
    "${SIM_PY}" -m trust3d.parallel_v2.runtime wait-cpu gate7_fix --interval "${INTERVAL}" >/dev/null
    local selected rechecked gpu free util
    selected=$(gpu_candidate || true)
    if [[ -z ${selected} ]]; then
      sleep "${INTERVAL}"
      continue
    fi
    IFS=, read -r gpu free util <<< "${selected}"
    rechecked=$(atomic_gpu_recheck "${gpu}" || true)
    if [[ -z ${rechecked} ]]; then
      status waiting_for_resource "GPU ${gpu} 在原子复核时资源变化，返回监测。"
      continue
    fi
    status launching "GPU ${gpu} 命中并通过原子复核，立即启动 B 线。"
    : > "${ATTEMPT_LOG}"
    set +e
    run_gpu_pipeline "${gpu}" 2>&1 | tee -a "${ATTEMPT_LOG}"
    local rc=${PIPESTATUS[0]}
    set -e
    if (( rc == 0 )); then
      return 0
    fi
    if rg -qi 'out of memory|cuda.*memory|cuda error|cublas.*alloc' "${ATTEMPT_LOG}"; then
      status waiting_for_resource "GPU ${gpu} 出现资源型 CUDA 故障，保留 checkpoint 并返回监测。"
      sleep "${INTERVAL}"
      continue
    fi
    status failed "B 线发生非资源故障，退出码 ${rc}。"
    return "${rc}"
  done
}

run_recover() {
  CUDA_VISIBLE_DEVICES='' "${SIM_PY}" -m trust3d.parallel_v2.gate7_fix recover
}

run_evaluate() {
  CUDA_VISIBLE_DEVICES='' "${SIM_PY}" -m trust3d.parallel_v2.gate7_fix evaluate
}

run_report() {
  CUDA_VISIBLE_DEVICES='' "${SIM_PY}" -m trust3d.parallel_v2.gate7_fix report
}

run_resume() {
  "${SIM_PY}" -m trust3d.parallel_v2.runtime wait-cpu gate7_fix --interval 1
  "${SIM_PY}" -m trust3d.parallel_v2.runtime verify-baseline
  resume_stage gate7_lock run_lock || return $?
  resume_stage gate7_unit run_unit || return $?
  resume_stage gate7_prepare run_prepare || return $?
  if stage_complete gate7_fix_report; then
    status complete 'Gate 7 修复实验已从有效 checkpoint 确认完成。'
    return 0
  fi
  run_watch
}

run_locked() {
  if [[ -z ${TMUX:-} ]]; then
    printf '%s\n' 'Gate 7 修复 runner 必须在 tmux 中执行。'
    return 125
  fi
  exec 9>"${LOCK}"
  if ! flock -n 9; then
    printf '%s\n' 'Gate 7 修复 runner 或 watcher 已在运行，本实例退出。'
    return 73
  fi
  printf 'start=%s host=%s tmux=%s pane=%s cwd=%s command=%q log=%s git=%s dirty=%s\n' \
    "$(date -Is)" "$(hostname)" "${TMUX}" "${TMUX_PANE:-}" "${PWD}" "$0 $*" "${LOG_PATH}" "$(git rev-parse HEAD)" "$(git status --porcelain | wc -l)"
  case "${MODE}" in
    lock) run_lock ;;
    unit) run_unit ;;
    prepare) run_prepare ;;
    pilot|watch|holdout) run_watch ;;
    recover) run_recover ;;
    evaluate) run_evaluate ;;
    report) run_report ;;
    resume) run_resume ;;
    status) "${SIM_PY}" -m trust3d.parallel_v2.orchestrator status ;;
    *) printf '不支持的 B 线模式：%s\n' "${MODE}"; return 2 ;;
  esac
}

set +e
run_locked 2>&1 | tee -a "${LOG_PATH}"
rc=${PIPESTATUS[0]}
set -e
printf 'exit_code=%s end=%s\n' "${rc}" "$(date -Is)" | tee -a "${LOG_PATH}"
if (( rc != 0 )); then
  status failed "Gate 7 修复 runner 退出码 ${rc}，checkpoint 已保留。" || true
fi
exit "${rc}"
