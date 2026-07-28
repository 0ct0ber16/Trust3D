#!/usr/bin/env bash
set -uo pipefail

ROOT=/224010104/Jerry/trust3d
LOG_ROOT=/224010104/Jerry/logs/gate3
PYTHON=/224010104/miniconda3/envs/trust3d-sim/bin/python
TIMEOUT_SECONDS=${GATE3_TIMEOUT_SECONDS:-43200}
LOG_PATH=${LOG_ROOT}/mvp.log
EXIT_PATH=${LOG_ROOT}/mvp.exit

mkdir -p "${LOG_ROOT}"
cd "${ROOT}"

run() {
  printf 'start=%s tmux=%s cwd=%s\n' "$(date -Is)" "${TMUX:-unset}" "${PWD}"
  uptime
  free -h
  df -h /224010104/Jerry
  nvidia-smi \
    --query-gpu=index,memory.used,memory.total,utilization.gpu,temperature.gpu \
    --format=csv,noheader
  printf 'timeout_seconds=%s\n' "${TIMEOUT_SECONDS}"
  printf '%s\n' 'command=xvfb-run -a python -m trust3d.data.build_branches --num-source-events 100 --branches fresh_stable risk_stable risk_stale --questions-per-branch 2 --replay-runs 2 --seed 20260728 --output data/episodes/mvp'

  timeout "${TIMEOUT_SECONDS}s" xvfb-run -a "${PYTHON}" -u \
    -m trust3d.data.build_branches \
    --candidates outputs/gate1/candidates.jsonl \
    --num-source-events 100 \
    --branches fresh_stable risk_stable risk_stale \
    --questions-per-branch 2 \
    --replay-runs 2 \
    --seed 20260728 \
    --output data/episodes/mvp
  build_rc=$?
  if [[ ${build_rc} -ne 0 ]]; then
    printf 'build_exit_code=%s\n' "${build_rc}"
    return "${build_rc}"
  fi

  printf '%s\n' 'command=python -m trust3d.data.validate_dataset --gate 3 --public data/episodes/mvp/episodes_public.jsonl --private data/episodes/mvp/oracle_private.jsonl --replay-twice --report outputs/gate3/validation.json'
  "${PYTHON}" -m trust3d.data.validate_dataset \
    --gate 3 \
    --public data/episodes/mvp/episodes_public.jsonl \
    --private data/episodes/mvp/oracle_private.jsonl \
    --replay-twice \
    --report outputs/gate3/validation.json
  validate_rc=$?
  printf 'validation_exit_code=%s end=%s\n' "${validate_rc}" "$(date -Is)"
  return "${validate_rc}"
}

set +e
run 2>&1 | tee -a "${LOG_PATH}"
rc=${PIPESTATUS[0]}
set -e
printf '%s\n' "${rc}" > "${EXIT_PATH}.tmp"
mv "${EXIT_PATH}.tmp" "${EXIT_PATH}"
exit "${rc}"
