#!/usr/bin/env bash
set -uo pipefail

ROOT=/224010104/Jerry/trust3d
LOG_ROOT=/224010104/Jerry/logs/gate3
PYTHON=/224010104/miniconda3/envs/trust3d-sim/bin/python
TIMEOUT_SECONDS=${GATE3_TIMEOUT_SECONDS:-43200}
LOG_PATH=${LOG_ROOT}/mvp.log
EXIT_PATH=${LOG_ROOT}/mvp.exit
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
  uptime
  free -h
  df -h /224010104/Jerry
  nvidia-smi \
    --query-gpu=index,memory.used,memory.total,utilization.gpu,temperature.gpu \
    --format=csv,noheader
  prepare_display || return $?
  printf 'timeout_seconds=%s\n' "${TIMEOUT_SECONDS}"
  printf '%s\n' 'command=xvfb-run -a python -m trust3d.data.build_branches --num-source-events 100 --branches fresh_stable risk_stable risk_stale --questions-per-branch 2 --replay-runs 2 --seed 20260728 --output data/episodes/mvp'

  timeout "${TIMEOUT_SECONDS}s" "${XVFB_RUN[@]}" "${PYTHON}" -u \
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
