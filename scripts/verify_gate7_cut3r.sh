#!/usr/bin/env bash
set -uo pipefail

ROOT=/224010104/Jerry/trust3d
ENV_PREFIX=/224010104/Jerry/.conda/envs/cut3r
PYTHON=${ENV_PREFIX}/bin/python
CUT3R_ROOT=${ROOT}/external/cut3r
CHECKPOINT=${CUT3R_ROOT}/src/cut3r_512_dpt_4_64.pth
GEOMETRY=${ROOT}/outputs/gate7/cut3r_geometry
LOG_ROOT=/224010104/Jerry/logs/gate7
LOG_PATH=${LOG_ROOT}/recovery.log
EXIT_PATH=${LOG_ROOT}/recovery.exit
BEFORE_HASHES=${LOG_ROOT}/recovery-before.sha256
AFTER_HASHES=${LOG_ROOT}/recovery-after.sha256

export HOME=/224010104/Jerry
export TMPDIR=/224010104/Jerry/.tmp
export CUDA_VISIBLE_DEVICES=''
export LD_LIBRARY_PATH=${ENV_PREFIX}/lib/python3.11/site-packages/torch/lib:${ENV_PREFIX}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}
export PYTHONPATH=${ROOT}:${CUT3R_ROOT}:${CUT3R_ROOT}/src/croco/models/curope${PYTHONPATH:+:${PYTHONPATH}}

mkdir -p "${LOG_ROOT}" "${TMPDIR}"
cd "${ROOT}"

write_hashes() {
  find outputs/gate7/cut3r_geometry/checkpoints \
    -type f -name '*.json' -print0 \
    | sort -z \
    | xargs -0 sha256sum
  sha256sum outputs/gate7/predictions.jsonl outputs/gate7/validation.json
}

run() {
  printf 'start=%s tmux=%s cwd=%s\n' "$(date -Is)" "${TMUX:-unset}" "${PWD}"
  printf '%s\n' 'command=Gate 7 纯 checkpoint 恢复、fingerprint 校验和哈希审计'
  printf 'log=%s\n' "${LOG_PATH}"
  if [[ -z ${TMUX:-} ]]; then
    printf '%s\n' '安全检查失败：Gate 7 恢复审计只能在 tmux 会话中执行。'
    return 125
  fi

  printf '[%s] 资源检查\n' "$(date -Is)"
  uptime
  free -h
  df -h /224010104/Jerry
  nvidia-smi \
    --query-gpu=index,memory.free,memory.used,memory.total,utilization.gpu,temperature.gpu \
    --format=csv,noheader,nounits

  write_hashes > "${BEFORE_HASHES}" || return $?
  printf '%s\n' 'command=CUDA_VISIBLE_DEVICES= python -m trust3d.geometry.run_cut3r --device cpu（仅校验已有 checkpoint）'
  "${PYTHON}" -u -m trust3d.geometry.run_cut3r \
    --episodes data/episodes/spatial30/episodes_public.jsonl \
    --checkpoint "${CHECKPOINT}" \
    --source-checkpoints data/episodes/spatial30/checkpoints \
    --dataset-root data/episodes/spatial30 \
    --cut3r-root "${CUT3R_ROOT}" \
    --output "${GEOMETRY}" \
    --device cpu
  geometry_rc=$?

  printf '%s\n' 'command=python -m trust3d.eval.evaluate_cut3r（预期 Gate 7 判定退出码为 1）'
  "${PYTHON}" -u -m trust3d.eval.evaluate_cut3r \
    --public data/episodes/spatial30/episodes_public.jsonl \
    --private data/episodes/spatial30/oracle_private.jsonl \
    --routes outputs/gate6/routes.jsonl \
    --geometry "${GEOMETRY}" \
    --source-checkpoints data/episodes/spatial30/checkpoints \
    --predictions outputs/gate7/predictions.jsonl \
    --output outputs/gate7/validation.json
  evaluator_rc=$?

  write_hashes > "${AFTER_HASHES}"
  hash_rc=$?
  cmp -s "${BEFORE_HASHES}" "${AFTER_HASHES}"
  compare_rc=$?

  export TRUST3D_GATE7_GEOMETRY_RC=${geometry_rc}
  export TRUST3D_GATE7_EVALUATOR_RC=${evaluator_rc}
  export TRUST3D_GATE7_HASH_RC=${hash_rc}
  export TRUST3D_GATE7_HASHES_MATCH=$([[ ${compare_rc} -eq 0 ]] && echo 1 || echo 0)
  export TRUST3D_GATE7_BEFORE_HASHES=${BEFORE_HASHES}
  export TRUST3D_GATE7_AFTER_HASHES=${AFTER_HASHES}
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
    Path("outputs/gate7/cut3r_geometry/manifest.json").read_text(encoding="utf-8")
)
validation = json.loads(
    Path("outputs/gate7/validation.json").read_text(encoding="utf-8")
)
before = load_hashes(os.environ["TRUST3D_GATE7_BEFORE_HASHES"])
after = load_hashes(os.environ["TRUST3D_GATE7_AFTER_HASHES"])
report = {
    "schema_version": 1,
    "geometry_recovery_exit_code": int(os.environ["TRUST3D_GATE7_GEOMETRY_RC"]),
    "expected_evaluator_exit_code": 1,
    "evaluator_exit_code": int(os.environ["TRUST3D_GATE7_EVALUATOR_RC"]),
    "hash_command_exit_code": int(os.environ["TRUST3D_GATE7_HASH_RC"]),
    "checked_file_count": len(before),
    "checkpoint_count": len(manifest.get("groups", [])),
    "all_hashes_match": os.environ["TRUST3D_GATE7_HASHES_MATCH"] == "1",
    "before_sha256": before,
    "after_sha256": after,
    "manifest_complete": manifest.get("complete") is True,
    "model_load_seconds_this_run": manifest.get("model_load_seconds_this_run"),
    "gate7_pass": validation.get("gate7_pass"),
    "all_requested_groups_completed": validation.get("criteria", {}).get(
        "all_requested_groups_completed"
    ),
}
report["checkpoint_recovery_pass"] = (
    report["geometry_recovery_exit_code"] == 0
    and report["evaluator_exit_code"] == report["expected_evaluator_exit_code"]
    and report["hash_command_exit_code"] == 0
    and report["all_hashes_match"]
    and report["checked_file_count"] == 32
    and report["checkpoint_count"] == 30
    and report["manifest_complete"]
    and report["model_load_seconds_this_run"] == 0.0
    and report["gate7_pass"] is False
    and report["all_requested_groups_completed"] is True
)
path = Path("outputs/gate7/checkpoint_recovery.json")
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
  printf 'geometry_exit_code=%s evaluator_exit_code=%s report_exit_code=%s end=%s\n' \
    "${geometry_rc}" "${evaluator_rc}" "${report_rc}" "$(date -Is)"
  return "${report_rc}"
}

set +e
run 2>&1 | tee -a "${LOG_PATH}"
rc=${PIPESTATUS[0]}
set -e
printf '%s\n' "${rc}" > "${EXIT_PATH}.tmp"
mv "${EXIT_PATH}.tmp" "${EXIT_PATH}"
exit "${rc}"
