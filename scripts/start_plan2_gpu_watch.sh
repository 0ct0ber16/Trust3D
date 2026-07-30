#!/usr/bin/env bash
set -euo pipefail

ROOT=/224010104/Jerry/trust3d
TMUX_BIN=/224010104/Jerry/.conda/tools/bin/tmux
SESSION=trust3d-$(hostname)
WINDOW=plan2-gpu-watch
LOG=/224010104/Jerry/logs/plan2/gpu-watch.log

mkdir -p /224010104/Jerry/logs/plan2

if ! "${TMUX_BIN}" has-session -t "${SESSION}" 2>/dev/null; then
  "${TMUX_BIN}" new-session -d -s "${SESSION}" -c "${ROOT}" -n bootstrap
fi

if "${TMUX_BIN}" list-windows -t "${SESSION}" -F '#{window_name}' | rg -x "${WINDOW}" >/dev/null; then
  printf 'GPU 监测 window 已存在：%s:%s\n' "${SESSION}" "${WINDOW}"
  exit 0
fi

if pgrep -af '[w]atch_plan2_gpu.sh' >/dev/null; then
  printf '%s\n' 'GPU 监测进程已存在，不重复启动。'
  exit 0
fi

COMMAND="cd '${ROOT}' && exec scripts/watch_plan2_gpu.sh >> '${LOG}' 2>&1"
"${TMUX_BIN}" new-window -d -t "${SESSION}" -n "${WINDOW}" "${COMMAND}"
"${TMUX_BIN}" set-option -t "${SESSION}:${WINDOW}" remain-on-exit on
printf '已启动 GPU 监测：session=%s window=%s log=%s\n' "${SESSION}" "${WINDOW}" "${LOG}"
