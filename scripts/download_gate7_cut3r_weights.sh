#!/usr/bin/env bash
set -uo pipefail

ROOT=/224010104/Jerry/trust3d
CUT3R_ROOT=${ROOT}/external/cut3r
ENV_PREFIX=/224010104/Jerry/.conda/envs/cut3r
PYTHON=${ENV_PREFIX}/bin/python
WEIGHT=${CUT3R_ROOT}/src/cut3r_512_dpt_4_64.pth
PART=${WEIGHT}.part
SOURCE_URL='https://drive.google.com/file/d/1Asz-ZB3FfpzZYwunhQvNPZEUA8XUNAYD/view?usp=drive_link'
EXPECTED_SHA256=45f7e98a0a64dbeb54901ae2b878cd8cd125f20a4497316483f0bd6f109f8103
EXPECTED_BYTES=3173761006
LOG_ROOT=/224010104/Jerry/logs/gate7
LOG_PATH=${LOG_ROOT}/weights.log
EXIT_PATH=${LOG_ROOT}/weights.exit
STATE_ROOT=/224010104/Jerry/checkpoints/gate7/weights
REPORT=${ROOT}/outputs/gate7/weights.json

export HOME=/224010104/Jerry
export PIP_CACHE_DIR=/224010104/Jerry/.cache/pip
export TMPDIR=/224010104/Jerry/.tmp

mkdir -p "${LOG_ROOT}" "${STATE_ROOT}" "${TMPDIR}" "$(dirname "${REPORT}")"
cd "${ROOT}"

inspect_checkpoint() {
  local path=$1
  TRUST3D_CUT3R_WEIGHT=${path} "${PYTHON}" - <<'PY'
import os
from pathlib import Path

import torch

path = Path(os.environ["TRUST3D_CUT3R_WEIGHT"])
checkpoint = torch.load(path, map_location="cpu", weights_only=False)
assert isinstance(checkpoint, dict), type(checkpoint)
assert "args" in checkpoint and "model" in checkpoint, checkpoint.keys()
assert isinstance(checkpoint["model"], dict) and checkpoint["model"]
model_spec = getattr(checkpoint["args"], "model", "")
assert "ARCroco3DStereo" in model_spec, model_spec
print(
    f"checkpoint={path} bytes={path.stat().st_size} "
    f"model_tensors={len(checkpoint['model'])} model={model_spec}"
)
PY
}

validate_weight_file() {
  local path=$1
  local bytes sha256
  bytes=$(stat -c %s "${path}") || return $?
  sha256=$(sha256sum "${path}" | cut -d ' ' -f 1) || return $?
  if [[ ${bytes} != "${EXPECTED_BYTES}" ]] || [[ ${sha256} != "${EXPECTED_SHA256}" ]]; then
    printf '权重校验失败：期望 bytes=%s sha256=%s，实际 bytes=%s sha256=%s。\n' \
      "${EXPECTED_BYTES}" "${EXPECTED_SHA256}" "${bytes}" "${sha256}"
    return 4
  fi
  printf '权重固定清单校验通过：bytes=%s sha256=%s\n' "${bytes}" "${sha256}"
}

write_report() {
  local sha256=$1
  export TRUST3D_CUT3R_WEIGHT=${WEIGHT}
  export TRUST3D_CUT3R_SHA256=${sha256}
  export TRUST3D_CUT3R_SOURCE=${SOURCE_URL}
  "${PYTHON}" - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import torch

path = Path(os.environ["TRUST3D_CUT3R_WEIGHT"])
checkpoint = torch.load(path, map_location="cpu", weights_only=False)
report = {
    "schema_version": 1,
    "说明": "CUT3R 官方最终 checkpoint 的本地校验清单；权重文件本身不提交 Git。",
    "checkpoint_name": path.name,
    "checkpoint_path": str(path),
    "source_url": os.environ["TRUST3D_CUT3R_SOURCE"],
    "google_drive_file_id": "1Asz-ZB3FfpzZYwunhQvNPZEUA8XUNAYD",
    "bytes": path.stat().st_size,
    "sha256": os.environ["TRUST3D_CUT3R_SHA256"],
    "model_tensor_count": len(checkpoint["model"]),
    "model_spec": getattr(checkpoint["args"], "model", ""),
    "validated_at": datetime.now(timezone.utc).isoformat(),
}
output = Path("outputs/gate7/weights.json")
temporary = output.with_name(output.name + ".tmp")
temporary.write_text(
    json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
temporary.replace(output)
print(json.dumps(report, ensure_ascii=False, sort_keys=True))
PY
}

mark_complete() {
  local sha256=$1
  local marker=${STATE_ROOT}/final.done
  printf 'sha256=%s\ncompleted_at=%s\n' "${sha256}" "$(date -Is)" \
    > "${marker}.tmp"
  mv "${marker}.tmp" "${marker}"
}

run() {
  printf 'start=%s tmux=%s cwd=%s\n' "$(date -Is)" "${TMUX:-unset}" "${PWD}"
  printf '%s\n' 'command=可续传下载并校验 CUT3R 官方最终 checkpoint'
  printf 'weight=%s\npartial=%s\nlog=%s\nstate=%s\n' \
    "${WEIGHT}" "${PART}" "${LOG_PATH}" "${STATE_ROOT}"
  if [[ -z ${TMUX:-} ]]; then
    printf '%s\n' '安全检查失败：权重下载只能在 tmux 会话中执行。'
    return 125
  fi
  if [[ ! -x ${PYTHON} ]] || [[ ! -d ${CUT3R_ROOT}/.git ]]; then
    printf '%s\n' 'CUT3R 环境或官方仓库尚未准备完成。'
    return 2
  fi

  uptime
  free -h
  df -h /224010104/Jerry

  if [[ ! -s ${WEIGHT} ]]; then
    printf 'command=gdown --continue %s --output %s\n' \
      "${SOURCE_URL}" "${PART}"
    timeout 14400 "${ENV_PREFIX}/bin/gdown" --continue \
      "${SOURCE_URL}" --output "${PART}" || return $?
    validate_weight_file "${PART}" || return $?
    inspect_checkpoint "${PART}" || return $?
    mv "${PART}" "${WEIGHT}"
  else
    printf '%s\n' 'resume=发现完整权重，重新校验后跳过下载'
    validate_weight_file "${WEIGHT}" || return $?
    inspect_checkpoint "${WEIGHT}" || return $?
  fi

  local sha256
  sha256=$(sha256sum "${WEIGHT}" | cut -d ' ' -f 1) || return $?
  write_report "${sha256}" || return $?
  mark_complete "${sha256}"
  printf 'sha256=%s bytes=%s end=%s\n' \
    "${sha256}" "$(stat -c %s "${WEIGHT}")" "$(date -Is)"
}

set +e
run 2>&1 | tee -a "${LOG_PATH}"
rc=${PIPESTATUS[0]}
set -e
printf '%s\n' "${rc}" > "${EXIT_PATH}.tmp"
mv "${EXIT_PATH}.tmp" "${EXIT_PATH}"
exit "${rc}"
