#!/usr/bin/env bash
set -uo pipefail

ROOT=/224010104/Jerry/trust3d
ENV_PREFIX=/224010104/Jerry/.conda/envs/cut3r
PYTHON=${ENV_PREFIX}/bin/python
CUT3R_ROOT=${ROOT}/external/cut3r
CHECKPOINT=${CUT3R_ROOT}/src/cut3r_512_dpt_4_64.pth
OUTPUT=${ROOT}/outputs/gate7/cut3r_geometry
LOG_ROOT=/224010104/Jerry/logs/gate7
LOG_PATH=${LOG_ROOT}/inference.log
EXIT_PATH=${LOG_ROOT}/inference.exit
MODE=${1:-pilot}
MIN_FREE_GPU_MIB=${GATE7_MIN_FREE_GPU_MIB:-60000}
WAIT_SECONDS=${GATE7_WAIT_FOR_GPU_SECONDS:-21600}
POLL_SECONDS=${GATE7_GPU_POLL_SECONDS:-60}

export HOME=/224010104/Jerry
export TMPDIR=/224010104/Jerry/.tmp
export PIP_CACHE_DIR=/224010104/Jerry/.cache/pip
export CUDA_HOME=${ENV_PREFIX}
export PATH=${ENV_PREFIX}/bin:${PATH}
export LD_LIBRARY_PATH=${ENV_PREFIX}/lib/python3.11/site-packages/torch/lib:${ENV_PREFIX}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}
export PYTHONPATH=${ROOT}:${CUT3R_ROOT}:${CUT3R_ROOT}/src/croco/models/curope${PYTHONPATH:+:${PYTHONPATH}}
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

mkdir -p "${LOG_ROOT}" "${OUTPUT}" "${TMPDIR}"
cd "${ROOT}"

select_gpu() {
  local started now gpu_row gpu_index free_mib used_mib utilization
  started=$(date +%s)
  while true; do
    now=$(date +%s)
    printf '[%s] 资源检查\n' "$(date -Is)"
    uptime
    free -h
    df -h /224010104/Jerry
    nvidia-smi \
      --query-gpu=index,memory.free,memory.used,memory.total,utilization.gpu,temperature.gpu \
      --format=csv,noheader,nounits
    gpu_row=$(nvidia-smi \
      --query-gpu=index,memory.free,memory.used,utilization.gpu \
      --format=csv,noheader,nounits \
      | tr -d ' ' | sort -t, -k2,2nr | head -n 1)
    IFS=, read -r gpu_index free_mib used_mib utilization <<< "${gpu_row}"
    if [[ ${free_mib} =~ ^[0-9]+$ ]] \
      && (( free_mib >= MIN_FREE_GPU_MIB )); then
      printf 'selected_gpu=%s free_mib=%s used_mib=%s utilization=%s\n' \
        "${gpu_index}" "${free_mib}" "${used_mib}" "${utilization}"
      TRUST3D_SELECTED_GPU=${gpu_index}
      return 0
    fi
    if (( now - started >= WAIT_SECONDS )); then
      printf '资源不足：等待 %s 秒后仍无至少 %s MiB 空闲显存的 GPU，暂停推理。\n' \
        "${WAIT_SECONDS}" "${MIN_FREE_GPU_MIB}"
      return 75
    fi
    printf '资源不足：不启动推理，%s 秒后仅重新检查。\n' "${POLL_SECONDS}"
    sleep "${POLL_SECONDS}"
  done
}

run() {
  printf 'start=%s tmux=%s cwd=%s\n' "$(date -Is)" "${TMUX:-unset}" "${PWD}"
  printf 'mode=%s\ncommand=scripts/run_gate7_cut3r.sh %s\nlog=%s\noutput=%s\n' \
    "${MODE}" "${MODE}" "${LOG_PATH}" "${OUTPUT}"
  if [[ -z ${TMUX:-} ]]; then
    printf '%s\n' '安全检查失败：Gate 7 推理只能在 tmux 会话中执行。'
    return 125
  fi
  if [[ ${MODE} != pilot && ${MODE} != full ]]; then
    printf '%s\n' '模式必须是 pilot 或 full。'
    return 2
  fi
  if [[ ! -f ${CHECKPOINT} ]] \
    || [[ ! -f outputs/gate7/environment.json ]] \
    || [[ ! -f outputs/gate7/weights.json ]]; then
    printf '%s\n' 'CUT3R 环境或权重报告不完整，不能启动推理。'
    return 3
  fi
  if ps -eo pid,cmd | grep '[t]rust3d.geometry.run_cut3r' >/dev/null; then
    printf '%s\n' '检测到已有 run_cut3r 进程，为避免重复占用 GPU，本次不启动。'
    return 73
  fi

  select_gpu || return $?
  export CUDA_VISIBLE_DEVICES=${TRUST3D_SELECTED_GPU}
  printf 'CUDA_VISIBLE_DEVICES=%s\n' "${CUDA_VISIBLE_DEVICES}"
  printf '%s\n' 'command=python -m trust3d.geometry.run_cut3r（RGB-only、逐 group checkpoint）'
  run_args=(
    --episodes data/episodes/spatial30/episodes_public.jsonl
    --checkpoint "${CHECKPOINT}"
    --source-checkpoints data/episodes/spatial30/checkpoints
    --dataset-root data/episodes/spatial30
    --cut3r-root "${CUT3R_ROOT}"
    --output "${OUTPUT}"
    --device cuda
  )
  if [[ ${MODE} == pilot ]]; then
    run_args+=(--max-groups 1)
  else
    run_args+=(--continue-on-error)
  fi
  timeout 43200 "${PYTHON}" -u -m trust3d.geometry.run_cut3r "${run_args[@]}" \
    || return $?

  if [[ ${MODE} == full ]]; then
    printf '%s\n' 'command=python -m trust3d.eval.evaluate_cut3r'
    "${PYTHON}" -u -m trust3d.eval.evaluate_cut3r \
      --public data/episodes/spatial30/episodes_public.jsonl \
      --private data/episodes/spatial30/oracle_private.jsonl \
      --routes outputs/gate6/routes.jsonl \
      --geometry "${OUTPUT}" \
      --source-checkpoints data/episodes/spatial30/checkpoints \
      --predictions outputs/gate7/predictions.jsonl \
      --output outputs/gate7/validation.json
    return $?
  fi
}

set +e
run 2>&1 | tee -a "${LOG_PATH}"
rc=${PIPESTATUS[0]}
set -e
printf '%s\n' "${rc}" > "${EXIT_PATH}.tmp"
mv "${EXIT_PATH}.tmp" "${EXIT_PATH}"
printf 'exit_code=%s end=%s\n' "${rc}" "$(date -Is)" | tee -a "${LOG_PATH}"
exit "${rc}"
