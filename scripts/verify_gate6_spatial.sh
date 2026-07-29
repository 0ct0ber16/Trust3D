#!/usr/bin/env bash
set -uo pipefail

ROOT=/224010104/Jerry/trust3d
LOG_ROOT=/224010104/Jerry/logs/gate6
PYTHON=/224010104/miniconda3/envs/trust3d-sim/bin/python
LOG_PATH=${LOG_ROOT}/verify.log
EXIT_PATH=${LOG_ROOT}/verify.exit
BEFORE_HASHES=${LOG_ROOT}/recovery-before.sha256
AFTER_HASHES=${LOG_ROOT}/recovery-after.sha256
BEFORE_PROCESSES=${LOG_ROOT}/recovery-processes-before.txt
DURING_PROCESSES=${LOG_ROOT}/recovery-processes-during.txt
AFTER_PROCESSES=${LOG_ROOT}/recovery-processes-after.txt
WATCH_STOP=${LOG_ROOT}/recovery-process-watch.stop

HASH_TARGETS=(
  data/episodes/spatial30/episodes_public.jsonl
  data/episodes/spatial30/oracle_private.jsonl
  data/episodes/spatial30/selection.json
  outputs/gate6/validation.json
)

mkdir -p "${LOG_ROOT}"
cd "${ROOT}"

simulator_pids() {
  pgrep -f '[t]hor-201909\|[X]vfb' | sort || true
}

write_hashes() {
  sha256sum "${HASH_TARGETS[@]}"
}

run() {
  printf 'start=%s tmux=%s cwd=%s\n' "$(date -Is)" "${TMUX:-unset}" "${PWD}"
  printf '%s\n' 'command=Gate 6 纯 checkpoint 恢复、哈希对比和模拟器进程审计'
  printf 'log=%s\n' "${LOG_PATH}"
  if [[ -z ${TMUX:-} ]]; then
    printf '%s\n' '安全检查失败：Gate 6 恢复审计只能在 tmux 会话中执行。'
    return 125
  fi

  write_hashes > "${BEFORE_HASHES}" || return $?
  simulator_pids > "${BEFORE_PROCESSES}"
  : > "${DURING_PROCESSES}"
  rm -f "${WATCH_STOP}"
  (
    while [[ ! -e ${WATCH_STOP} ]]; do
      simulator_pids
      sleep 0.1
    done
  ) >> "${DURING_PROCESSES}" &
  watch_pid=$!

  printf '%s\n' 'command=GATE6_MAX_NEW_CONTEXTS=0 scripts/run_gate6_spatial.sh'
  GATE6_MAX_NEW_CONTEXTS=0 scripts/run_gate6_spatial.sh
  recovery_rc=$?
  : > "${WATCH_STOP}"
  wait "${watch_pid}"
  rm -f "${WATCH_STOP}"
  sort -u -o "${DURING_PROCESSES}" "${DURING_PROCESSES}"
  simulator_pids > "${AFTER_PROCESSES}"

  write_hashes > "${AFTER_HASHES}"
  hashes_rc=$?
  cmp -s "${BEFORE_HASHES}" "${AFTER_HASHES}"
  compare_rc=$?
  new_processes=$(comm -13 "${BEFORE_PROCESSES}" "${DURING_PROCESSES}" | wc -l)

  export TRUST3D_RECOVERY_COMMAND_RC=${recovery_rc}
  export TRUST3D_RECOVERY_HASH_COMMAND_RC=${hashes_rc}
  export TRUST3D_RECOVERY_HASHES_MATCH=$([[ ${compare_rc} -eq 0 ]] && echo 1 || echo 0)
  export TRUST3D_RECOVERY_NEW_PROCESSES=${new_processes}
  export TRUST3D_RECOVERY_BEFORE_HASHES=${BEFORE_HASHES}
  export TRUST3D_RECOVERY_AFTER_HASHES=${AFTER_HASHES}
  "${PYTHON}" - <<'PY'
import json
import os
from pathlib import Path


def load_hashes(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        digest, filename = line.split(maxsplit=1)
        values[filename] = digest
    return values


manifest = json.loads(
    Path("data/episodes/spatial30/manifest.json").read_text(encoding="utf-8")
)
before = load_hashes(os.environ["TRUST3D_RECOVERY_BEFORE_HASHES"])
after = load_hashes(os.environ["TRUST3D_RECOVERY_AFTER_HASHES"])
report = {
    "schema_version": 1,
    "recovery_command_exit_code": int(os.environ["TRUST3D_RECOVERY_COMMAND_RC"]),
    "hash_command_exit_code": int(os.environ["TRUST3D_RECOVERY_HASH_COMMAND_RC"]),
    "checked_file_count": len(before),
    "all_hashes_match": os.environ["TRUST3D_RECOVERY_HASHES_MATCH"] == "1",
    "before_sha256": before,
    "after_sha256": after,
    "new_simulator_process_count": int(
        os.environ["TRUST3D_RECOVERY_NEW_PROCESSES"]
    ),
    "manifest_complete": manifest.get("complete") is True,
    "manifest_simulator_started": manifest.get("simulator_started"),
    "manifest_new_contexts_this_run": manifest.get("new_contexts_this_run"),
}
report["checkpoint_recovery_pass"] = (
    report["recovery_command_exit_code"] == 0
    and report["hash_command_exit_code"] == 0
    and report["all_hashes_match"]
    and report["new_simulator_process_count"] == 0
    and report["manifest_complete"]
    and report["manifest_simulator_started"] is False
    and report["manifest_new_contexts_this_run"] == 0
)
path = Path("outputs/gate6/checkpoint_recovery.json")
path.parent.mkdir(parents=True, exist_ok=True)
temporary = path.with_name(path.name + ".tmp")
temporary.write_text(
    json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
temporary.replace(path)
print(json.dumps(report, ensure_ascii=False, sort_keys=True))
if not report["checkpoint_recovery_pass"]:
    raise SystemExit(1)
PY
  report_rc=$?
  printf 'recovery_exit_code=%s report_exit_code=%s end=%s\n' \
    "${recovery_rc}" "${report_rc}" "$(date -Is)"
  return "${report_rc}"
}

set +e
run 2>&1 | tee -a "${LOG_PATH}"
rc=${PIPESTATUS[0]}
set -e
printf '%s\n' "${rc}" > "${EXIT_PATH}.tmp"
mv "${EXIT_PATH}.tmp" "${EXIT_PATH}"
exit "${rc}"
