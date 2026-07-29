#!/usr/bin/env bash
set -uo pipefail

ROOT=/224010104/Jerry/trust3d
LOG_ROOT=/224010104/Jerry/logs/gate6
PYTHON=/224010104/miniconda3/envs/trust3d-sim/bin/python
TIMEOUT_SECONDS=${GATE6_TIMEOUT_SECONDS:-43200}
LOG_PATH=${LOG_ROOT}/spatial.log
EXIT_PATH=${LOG_ROOT}/spatial.exit
LOCAL_XVFB_ROOT=/224010104/Jerry/.local/xvfb
LOCAL_XVFB_LIB=${LOCAL_XVFB_ROOT}/root/usr/lib/x86_64-linux-gnu
LOCAL_DRI_ROOT=${LOCAL_XVFB_LIB}/dri
LOCAL_XKB_ROOT=${LOCAL_XVFB_ROOT}/root/usr/share/X11/xkb
LOCAL_FONT_ROOT=${LOCAL_XVFB_ROOT}/root/usr/share/fonts/X11

mkdir -p "${LOG_ROOT}"
cd "${ROOT}"

prepare_display() {
  if command -v xvfb-run >/dev/null 2>&1; then
    XVFB_RUN=(xvfb-run -a)
    printf 'display_backend=system-xvfb\n'
    return 0
  fi
  if [[ ! -x ${LOCAL_XVFB_ROOT}/bin/Xvfb ]] \
    || [[ ! -x ${LOCAL_XVFB_ROOT}/root/usr/bin/xvfb-run ]] \
    || [[ ! -x ${ROOT}/xkbcomp ]]; then
    printf '%s\n' '本地 Xvfb 不完整，请先运行 scripts/bootstrap_local_xvfb.sh。'
    return 127
  fi
  export PATH="${LOCAL_XVFB_ROOT}/bin:${LOCAL_XVFB_ROOT}/root/usr/bin:${PATH}"
  export LD_LIBRARY_PATH="${LOCAL_XVFB_LIB}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
  export LIBGL_DRIVERS_PATH="${LOCAL_DRI_ROOT}"
  export LIBGL_ALWAYS_SOFTWARE=1
  export LP_NUM_THREADS=${LP_NUM_THREADS:-16}
  export TMPDIR=/224010104/Jerry/.tmp
  mkdir -p "${TMPDIR}"
  XVFB_RUN=(
    xvfb-run -a
    --server-args="-screen 0 1280x1024x24 -nolisten tcp -xkbdir ${LOCAL_XKB_ROOT} -fp ${LOCAL_FONT_ROOT}/misc,${LOCAL_FONT_ROOT}/Type1"
  )
  printf 'display_backend=workspace-xvfb\n'
  printf 'opengl_backend=llvmpipe\n'
  printf 'llvmpipe_threads=%s\n' "${LP_NUM_THREADS}"
}

run() {
  printf 'start=%s tmux=%s cwd=%s\n' "$(date -Is)" "${TMUX:-unset}" "${PWD}"
  printf 'log=%s\n' "${LOG_PATH}"
  if [[ -z ${TMUX:-} ]]; then
    printf '%s\n' '安全检查失败：Gate 6 只能在 tmux 会话中执行。'
    return 125
  fi
  uptime
  free -h
  df -h /224010104/Jerry
  nvidia-smi \
    --query-gpu=index,memory.used,memory.total,utilization.gpu,temperature.gpu \
    --format=csv,noheader
  prepare_display || return $?

  printf '%s\n' 'command=python -m pytest -q'
  "${PYTHON}" -m pytest -q || return $?

  if ! jq -e '.gate6_api_probe_pass == true' outputs/gate6/api_probe.json \
    >/dev/null 2>&1; then
    printf '%s\n' 'command=xvfb-run -a python -m trust3d.sim.probe_spatial_api'
    timeout "${TIMEOUT_SECONDS}s" "${XVFB_RUN[@]}" "${PYTHON}" -u \
      -m trust3d.sim.probe_spatial_api \
      --selection data/episodes/mvp/selection.json \
      --alfred-json external/alfred/data/json_2.1.0 \
      --output outputs/gate6/api_probe.json || return $?
  else
    printf '%s\n' 'resume=已验证 Gate 6 物体位置干预 API，跳过探针'
  fi

  spatial_args=(
    --selection data/episodes/mvp/selection.json
    --alfred-json external/alfred/data/json_2.1.0
    --exclude-groups configs/gate3_exclusions.json
    --output data/episodes/spatial30
    --target-groups 30
  )
  if [[ -n ${GATE6_MAX_NEW_CONTEXTS:-} ]]; then
    spatial_args+=(--max-new-contexts "${GATE6_MAX_NEW_CONTEXTS}")
  fi
  printf 'max_new_contexts=%s\n' "${GATE6_MAX_NEW_CONTEXTS:-all}"
  if [[ ${GATE6_MAX_NEW_CONTEXTS:-} == 0 ]] \
    || jq -e '.complete == true' data/episodes/spatial30/manifest.json \
      >/dev/null 2>&1; then
    printf '%s\n' 'command=python -m trust3d.data.build_spatial（纯 checkpoint 恢复）'
    "${PYTHON}" -u -m trust3d.data.build_spatial \
      "${spatial_args[@]}" --max-new-contexts 0 || return $?
  else
    printf '%s\n' 'command=xvfb-run -a python -m trust3d.data.build_spatial'
    timeout "${TIMEOUT_SECONDS}s" "${XVFB_RUN[@]}" "${PYTHON}" -u \
      -m trust3d.data.build_spatial "${spatial_args[@]}" || return $?
  fi
  if ! jq -e '.complete == true' data/episodes/spatial30/manifest.json \
    >/dev/null; then
    printf '%s\n' 'Gate 6 pilot 已保存 checkpoint，但尚未达到 30 个 group。'
    return 3
  fi

  printf '%s\n' 'command=python -m trust3d.agents.run_episode --methods spatial baselines'
  "${PYTHON}" -m trust3d.agents.run_episode \
    --episodes data/episodes/spatial30/episodes_public.jsonl \
    --methods always_trust always_reobserve global_ttl fact_freshness trust3d \
    --config configs/mvp.yaml \
    --output outputs/gate6/routes.jsonl || return $?

  printf '%s\n' 'command=python -m trust3d.eval.evaluate_spatial'
  "${PYTHON}" -m trust3d.eval.evaluate_spatial \
    --public data/episodes/spatial30/episodes_public.jsonl \
    --private data/episodes/spatial30/oracle_private.jsonl \
    --routes outputs/gate6/routes.jsonl \
    --predictions outputs/gate6/predictions.jsonl \
    --output outputs/gate6/validation.json
  validation_rc=$?
  printf 'validation_exit_code=%s end=%s\n' "${validation_rc}" "$(date -Is)"
  return "${validation_rc}"
}

set +e
run 2>&1 | tee -a "${LOG_PATH}"
rc=${PIPESTATUS[0]}
set -e
printf '%s\n' "${rc}" > "${EXIT_PATH}.tmp"
mv "${EXIT_PATH}.tmp" "${EXIT_PATH}"
exit "${rc}"
