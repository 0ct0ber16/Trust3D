#!/usr/bin/env bash
set -uo pipefail

ROOT=/224010104/Jerry/trust3d
LOG_ROOT=/224010104/Jerry/logs/gate4
PYTHON=/224010104/miniconda3/envs/trust3d-sim/bin/python
LOG_PATH=${LOG_ROOT}/offline.log
EXIT_PATH=${LOG_ROOT}/offline.exit

mkdir -p "${LOG_ROOT}"
cd "${ROOT}"

run() {
  printf 'start=%s tmux=%s cwd=%s\n' "$(date -Is)" "${TMUX:-unset}" "${PWD}"
  printf '%s\n' 'command=Gate 4 离线路由、私有真值评测、成组 bootstrap 与 Pareto 绘图'
  printf 'log=%s\n' "${LOG_PATH}"
  uptime
  free -h
  df -h /224010104/Jerry
  nvidia-smi \
    --query-gpu=index,memory.used,memory.total,utilization.gpu,temperature.gpu \
    --format=csv,noheader

  printf '%s\n' 'command=python -m pytest -q'
  "${PYTHON}" -m pytest -q || return $?

  printf '%s\n' 'command=python -m trust3d.agents.run_episode'
  "${PYTHON}" -m trust3d.agents.run_episode \
    --episodes data/episodes/mvp/episodes_public.jsonl \
    --methods always_trust always_reobserve global_ttl fact_freshness trust3d \
    --config configs/mvp.yaml \
    --output outputs/gate4/routes.jsonl || return $?

  printf '%s\n' 'command=python -m trust3d.eval.evaluate_routes --include-clairvoyant'
  "${PYTHON}" -m trust3d.eval.evaluate_routes \
    --routes outputs/gate4/routes.jsonl \
    --oracle data/episodes/mvp/oracle_private.jsonl \
    --include-clairvoyant \
    --output outputs/gate4/predictions.jsonl || return $?

  printf '%s\n' 'command=python -m trust3d.eval.metrics --bootstrap 10000'
  "${PYTHON}" -m trust3d.eval.metrics \
    --predictions outputs/gate4/predictions.jsonl \
    --oracle data/episodes/mvp/oracle_private.jsonl \
    --group-key group_id \
    --bootstrap 10000 \
    --config configs/mvp.yaml \
    --output outputs/gate4/metrics.json || return $?

  printf '%s\n' 'command=python -m trust3d.eval.plots'
  "${PYTHON}" -m trust3d.eval.plots \
    --metrics outputs/gate4/metrics.json \
    --output outputs/gate4/plots || return $?
  printf 'gate4_exit_code=0 end=%s\n' "$(date -Is)"
}

set +e
run 2>&1 | tee -a "${LOG_PATH}"
rc=${PIPESTATUS[0]}
set -e
printf '%s\n' "${rc}" > "${EXIT_PATH}.tmp"
mv "${EXIT_PATH}.tmp" "${EXIT_PATH}"
exit "${rc}"
