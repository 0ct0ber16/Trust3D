#!/usr/bin/env bash
set -uo pipefail

ROOT=/224010104/Jerry/trust3d
LOG_ROOT=/224010104/Jerry/logs/gate3
PYTHON=/224010104/miniconda3/envs/trust3d-sim/bin/python
LOG_PATH=${LOG_ROOT}/verify.log
EXIT_PATH=${LOG_ROOT}/verify.exit
BEFORE_HASHES=${LOG_ROOT}/recovery-before.sha256
AFTER_HASHES=${LOG_ROOT}/recovery-after.sha256
BEFORE_PROCESSES=${LOG_ROOT}/recovery-processes-before.txt
AFTER_PROCESSES=${LOG_ROOT}/recovery-processes-after.txt

mkdir -p "${LOG_ROOT}"
cd "${ROOT}"

build_from_checkpoints() {
  "${PYTHON}" -u -m trust3d.data.build_branches \
    --candidates outputs/gate1/candidates.jsonl \
    --num-source-events 100 \
    --branches fresh_stable risk_stable risk_stale \
    --questions-per-branch 2 \
    --replay-runs 2 \
    --seed 20260728 \
    --exclude-groups configs/gate3_exclusions.json \
    --output data/episodes/mvp
}

run() {
  printf 'start=%s tmux=%s cwd=%s\n' "$(date -Is)" "${TMUX:-unset}" "${PWD}"
  printf '%s\n' 'command=Gate 3 测试、断点聚合、数据验证、视觉泄漏审计和纯 checkpoint 恢复'
  printf 'log=%s\n' "${LOG_PATH}"
  uptime
  free -h
  df -h /224010104/Jerry
  nvidia-smi \
    --query-gpu=index,memory.used,memory.total,utilization.gpu,temperature.gpu \
    --format=csv,noheader

  printf '%s\n' 'command=python -m pytest -q'
  "${PYTHON}" -m pytest -q || return $?

  printf '%s\n' 'command=python -m trust3d.data.build_branches（仅聚合有效 checkpoint）'
  build_from_checkpoints || return $?

  printf '%s\n' 'command=python -m trust3d.data.validate_dataset --gate 3 --replay-twice'
  "${PYTHON}" -m trust3d.data.validate_dataset \
    --gate 3 \
    --public data/episodes/mvp/episodes_public.jsonl \
    --private data/episodes/mvp/oracle_private.jsonl \
    --replay-twice \
    --report outputs/gate3/validation.json || return $?

  printf '%s\n' 'command=python -m trust3d.data.audit_risk_frames'
  "${PYTHON}" -m trust3d.data.audit_risk_frames \
    --public data/episodes/mvp/episodes_public.jsonl \
    --private data/episodes/mvp/oracle_private.jsonl \
    --output outputs/gate3/risk_frame_audit.json || return $?

  find data/episodes/mvp -type f ! -name '*.tmp' -print0 \
    | sort -z | xargs -0 sha256sum > "${BEFORE_HASHES}"
  pgrep -af '[t]hor-201909\|[X]vfb' | sort > "${BEFORE_PROCESSES}" || true

  printf '%s\n' 'command=第二次纯 checkpoint 聚合并比较全部产物 SHA256'
  build_from_checkpoints || return $?

  find data/episodes/mvp -type f ! -name '*.tmp' -print0 \
    | sort -z | xargs -0 sha256sum > "${AFTER_HASHES}"
  pgrep -af '[t]hor-201909\|[X]vfb' | sort > "${AFTER_PROCESSES}" || true

  cmp -s "${BEFORE_HASHES}" "${AFTER_HASHES}"
  hashes_rc=$?
  new_processes=$(comm -13 "${BEFORE_PROCESSES}" "${AFTER_PROCESSES}" | wc -l)
  file_count=$(wc -l < "${BEFORE_HASHES}")
  export TRUST3D_RECOVERY_HASHES_MATCH=$([[ ${hashes_rc} -eq 0 ]] && echo 1 || echo 0)
  export TRUST3D_RECOVERY_NEW_PROCESSES=${new_processes}
  export TRUST3D_RECOVERY_FILE_COUNT=${file_count}
  "${PYTHON}" - <<'PY'
import json
import os
from pathlib import Path

report = {
    "schema_version": 1,
    "file_count": int(os.environ["TRUST3D_RECOVERY_FILE_COUNT"]),
    "all_hashes_match": os.environ["TRUST3D_RECOVERY_HASHES_MATCH"] == "1",
    "new_simulator_process_count": int(os.environ["TRUST3D_RECOVERY_NEW_PROCESSES"]),
}
report["checkpoint_recovery_pass"] = (
    report["all_hashes_match"] and report["new_simulator_process_count"] == 0
)
path = Path("outputs/gate3/checkpoint_recovery.json")
path.parent.mkdir(parents=True, exist_ok=True)
temporary = path.with_name(path.name + ".tmp")
temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
temporary.replace(path)
print(json.dumps(report, sort_keys=True))
if not report["checkpoint_recovery_pass"]:
    raise SystemExit(1)
PY
  recovery_rc=$?
  printf 'recovery_exit_code=%s end=%s\n' "${recovery_rc}" "$(date -Is)"
  return "${recovery_rc}"
}

set +e
run 2>&1 | tee -a "${LOG_PATH}"
rc=${PIPESTATUS[0]}
set -e
printf '%s\n' "${rc}" > "${EXIT_PATH}.tmp"
mv "${EXIT_PATH}.tmp" "${EXIT_PATH}"
exit "${rc}"
