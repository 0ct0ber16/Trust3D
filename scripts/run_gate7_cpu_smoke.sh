#!/usr/bin/env bash
set -uo pipefail

ROOT=/224010104/Jerry/trust3d
ENV_PREFIX=/224010104/Jerry/.conda/envs/cut3r
PYTHON=${ENV_PREFIX}/bin/python
CUT3R_ROOT=${ROOT}/external/cut3r
CHECKPOINT=${CUT3R_ROOT}/src/cut3r_512_dpt_4_64.pth
OUTPUT=${ROOT}/outputs/gate7/cpu_smoke
LOG_ROOT=/224010104/Jerry/logs/gate7
LOG_PATH=${LOG_ROOT}/cpu_smoke.log
EXIT_PATH=${LOG_ROOT}/cpu_smoke.exit
MIN_AVAILABLE_MEMORY_KIB=${GATE7_CPU_SMOKE_MIN_MEMORY_KIB:-67108864}

export HOME=/224010104/Jerry
export TMPDIR=/224010104/Jerry/.tmp
export OMP_NUM_THREADS=${GATE7_CPU_SMOKE_THREADS:-8}
export MKL_NUM_THREADS=${GATE7_CPU_SMOKE_THREADS:-8}
export CUDA_VISIBLE_DEVICES=''
export LD_LIBRARY_PATH=${ENV_PREFIX}/lib/python3.11/site-packages/torch/lib:${ENV_PREFIX}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}
export PYTHONPATH=${ROOT}:${CUT3R_ROOT}:${CUT3R_ROOT}/src/croco/models/curope${PYTHONPATH:+:${PYTHONPATH}}

mkdir -p "${LOG_ROOT}" "${OUTPUT}" "${TMPDIR}"
cd "${ROOT}"

run() {
  printf 'start=%s tmux=%s cwd=%s\n' "$(date -Is)" "${TMUX:-unset}" "${PWD}"
  printf '%s\n' 'purpose=GPU 等待期间以 CPU 和 224 分辨率验证真实 CUT3R 接口，不替代最终 CUDA 结果'
  printf 'command=scripts/run_gate7_cpu_smoke.sh\nlog=%s\noutput=%s\n' \
    "${LOG_PATH}" "${OUTPUT}"
  if [[ -z ${TMUX:-} ]]; then
    printf '%s\n' '安全检查失败：CPU smoke 只能在 tmux 会话中执行。'
    return 125
  fi

  printf '[%s] 资源检查\n' "$(date -Is)"
  uptime
  free -h
  df -h /224010104/Jerry
  nvidia-smi \
    --query-gpu=index,memory.free,memory.used,memory.total,utilization.gpu,temperature.gpu \
    --format=csv,noheader,nounits
  available_memory_kib=$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)
  if [[ ! ${available_memory_kib} =~ ^[0-9]+$ ]] \
    || (( available_memory_kib < MIN_AVAILABLE_MEMORY_KIB )); then
    printf '资源不足：可用内存 %s KiB，低于 CPU smoke 阈值 %s KiB。\n' \
      "${available_memory_kib:-unknown}" "${MIN_AVAILABLE_MEMORY_KIB}"
    return 75
  fi

  timeout 21600 "${PYTHON}" -u -m trust3d.geometry.run_cut3r \
    --episodes data/episodes/spatial30/episodes_public.jsonl \
    --checkpoint "${CHECKPOINT}" \
    --source-checkpoints data/episodes/spatial30/checkpoints \
    --dataset-root data/episodes/spatial30 \
    --cut3r-root "${CUT3R_ROOT}" \
    --output "${OUTPUT}" \
    --device cpu \
    --image-size 224 \
    --max-groups 1
}

set +e
run 2>&1 | tee -a "${LOG_PATH}"
rc=${PIPESTATUS[0]}
set -e
printf '%s\n' "${rc}" > "${EXIT_PATH}.tmp"
mv "${EXIT_PATH}.tmp" "${EXIT_PATH}"
printf 'exit_code=%s end=%s\n' "${rc}" "$(date -Is)" | tee -a "${LOG_PATH}"
exit "${rc}"
