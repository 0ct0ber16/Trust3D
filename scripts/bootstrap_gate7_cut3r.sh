#!/usr/bin/env bash
set -uo pipefail

ROOT=/224010104/Jerry/trust3d
CUT3R_ROOT=${ROOT}/external/cut3r
EXPECTED_CUT3R_COMMIT=8bc15dc92a6d7fd92920b4ec81540d3dec7d3ecf
ENV_PREFIX=/224010104/Jerry/.conda/envs/cut3r
CONDA=/224010104/miniconda3/bin/conda
PYTHON=${ENV_PREFIX}/bin/python
CUDA_INCLUDE=${ENV_PREFIX}/include
CUDA_LIB=${ENV_PREFIX}/lib
TORCH_LIB=${ENV_PREFIX}/lib/python3.11/site-packages/torch/lib
LOG_ROOT=/224010104/Jerry/logs/gate7
LOG_PATH=${LOG_ROOT}/environment.log
EXIT_PATH=${LOG_ROOT}/environment.exit
STATE_ROOT=/224010104/Jerry/checkpoints/gate7/environment

export HOME=/224010104/Jerry
export CONDA_PKGS_DIRS=/224010104/Jerry/.conda/pkgs
export PIP_CACHE_DIR=/224010104/Jerry/.cache/pip
export TMPDIR=/224010104/Jerry/.tmp
export CUDA_HOME=${ENV_PREFIX}
export PATH=${ENV_PREFIX}/bin:${PATH}
export CPATH=${CUDA_INCLUDE}${CPATH:+:${CPATH}}
export CPLUS_INCLUDE_PATH=${CUDA_INCLUDE}${CPLUS_INCLUDE_PATH:+:${CPLUS_INCLUDE_PATH}}
export LIBRARY_PATH=${CUDA_LIB}${LIBRARY_PATH:+:${LIBRARY_PATH}}
export LD_LIBRARY_PATH=${TORCH_LIB}:${CUDA_LIB}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}
export TORCH_CUDA_ARCH_LIST=8.0

mkdir -p "${LOG_ROOT}" "${STATE_ROOT}" "${CONDA_PKGS_DIRS}" \
  "${PIP_CACHE_DIR}" "${TMPDIR}"
cd "${ROOT}"

mark_stage() {
  local stage=$1
  local marker=${STATE_ROOT}/${stage}.done
  printf 'stage=%s\ncompleted_at=%s\n' "${stage}" "$(date -Is)" \
    > "${marker}.tmp"
  mv "${marker}.tmp" "${marker}"
}

run_base_environment() {
  if [[ -d ${ENV_PREFIX}/conda-meta ]]; then
    timeout 3600 "${CONDA}" install -y -p "${ENV_PREFIX}" \
      python=3.11 cmake=3.14 -c conda-forge --override-channels
  else
    timeout 3600 "${CONDA}" create -y -p "${ENV_PREFIX}" \
      python=3.11 cmake=3.14 -c conda-forge --override-channels
  fi
}

validate_base_environment() {
  [[ -x ${PYTHON} ]] \
    && "${PYTHON}" -c 'import sys; assert sys.version_info[:2] == (3, 11)' \
    && "${ENV_PREFIX}/bin/cmake" --version >/dev/null
}

validate_torch() {
  "${PYTHON}" -c \
    'import torch, torchvision; assert torch.version.cuda == "12.1"; print(torch.__version__, torchvision.__version__, torch.version.cuda)'
}

validate_cuda_toolchain() {
  [[ -x ${ENV_PREFIX}/bin/nvcc ]] \
    && [[ -f ${CUDA_INCLUDE}/cuda_runtime.h ]] \
    && [[ -f ${CUDA_INCLUDE}/nv/target ]] \
    && "${ENV_PREFIX}/bin/nvcc" --version | grep -q 'release 12\.1' \
    && "${CONDA}" list -p "${ENV_PREFIX}" --json | "${PYTHON}" -c '
import json
import sys

required = {
    "cuda-cccl",
    "cuda-cudart",
    "cuda-cudart-dev",
    "cuda-nvcc",
}
versions = {package["name"]: package["version"] for package in json.load(sys.stdin)}
assert required <= versions.keys(), sorted(required - versions.keys())
cuda_versions = {name: version for name, version in versions.items() if name.startswith("cuda-")}
assert all(version.startswith("12.1") for version in cuda_versions.values()), cuda_versions
'
}

remove_incompatible_cuda_packages() {
  local -a packages=()
  mapfile -t packages < <(
    "${CONDA}" list -p "${ENV_PREFIX}" --json | "${PYTHON}" -c '
import json
import sys

for package in json.load(sys.stdin):
    if package["name"].startswith("cuda-") and not package["version"].startswith("12.1"):
        print(package["name"])
'
  )
  if (( ${#packages[@]} == 0 )); then
    return 0
  fi
  printf 'command=移除 CUT3R 独立环境中的非 12.1 CUDA 包: %s\n' "${packages[*]}"
  timeout 3600 "${CONDA}" remove -y --force-remove -p "${ENV_PREFIX}" \
    "${packages[@]}" -c nvidia --override-channels
}

validate_requirements() {
  "${PYTHON}" -c \
    'import accelerate, cv2, einops, gradio, h5py, hydra, lpips, matplotlib, numpy, roma, scipy, sklearn, transformers, trimesh, viser; from transformers import PreTrainedModel; assert transformers.__version__ == "4.55.4"' \
    && "${PYTHON}" -m pip check
}

validate_curope() {
  PYTHONPATH=${CUT3R_ROOT}/src/croco/models/curope \
    "${PYTHON}" -c 'import torch, curope; print(curope.__file__)'
}

run() {
  printf 'start=%s tmux=%s cwd=%s\n' "$(date -Is)" "${TMUX:-unset}" "${PWD}"
  printf '%s\n' 'command=创建可恢复的 Gate 7 CUT3R 独立环境并编译 CUDA RoPE'
  printf 'environment=%s\nlog=%s\nstate=%s\n' \
    "${ENV_PREFIX}" "${LOG_PATH}" "${STATE_ROOT}"
  if [[ -z ${TMUX:-} ]]; then
    printf '%s\n' '安全检查失败：Gate 7 环境安装只能在 tmux 会话中执行。'
    return 125
  fi
  if [[ ! -d ${CUT3R_ROOT}/.git ]]; then
    printf '%s\n' 'CUT3R 官方仓库不存在，不能创建环境。'
    return 2
  fi
  actual_commit=$(git -C "${CUT3R_ROOT}" rev-parse HEAD) || return $?
  if [[ ${actual_commit} != "${EXPECTED_CUT3R_COMMIT}" ]]; then
    printf 'CUT3R commit 校验失败：期望 %s，实际 %s。\n' \
      "${EXPECTED_CUT3R_COMMIT}" "${actual_commit}"
    return 4
  fi
  if ! git -C "${CUT3R_ROOT}" diff --quiet; then
    printf '%s\n' 'CUT3R 官方仓库含 tracked 修改，拒绝执行依赖安装或扩展编译。'
    return 5
  fi

  uptime
  free -h
  df -h /224010104/Jerry
  nvidia-smi \
    --query-gpu=index,memory.used,memory.total,utilization.gpu,temperature.gpu \
    --format=csv,noheader

  if validate_base_environment; then
    printf '%s\n' 'resume=基础 Conda 环境已验证，跳过安装'
  else
    printf '%s\n' 'command=conda create/install python=3.11 cmake=3.14'
    run_base_environment || return $?
    validate_base_environment || return $?
  fi
  mark_stage base

  if validate_torch; then
    printf '%s\n' 'resume=PyTorch CUDA 12.1 已验证，跳过安装'
  else
    printf '%s\n' 'command=pip install 官方 PyTorch CUDA 12.1 wheel'
    timeout 7200 "${PYTHON}" -m pip install \
      torch==2.4.1 torchvision==0.19.1 \
      --index-url https://download.pytorch.org/whl/cu121 || return $?
    validate_torch || return $?
  fi
  mark_stage torch

  if validate_cuda_toolchain; then
    printf '%s\n' 'resume=CUDA 12.1 编译器、runtime、CCCL 与开发头文件已验证，跳过安装'
  else
    printf '%s\n' 'command=conda install 完整且版本一致的 CUDA 12.1 开发工具链'
    remove_incompatible_cuda_packages || return $?
    timeout 3600 "${CONDA}" install -y -p "${ENV_PREFIX}" \
      cuda-nvcc=12.1.105 \
      cuda-cudart=12.1.105 cuda-cudart-dev=12.1.105 \
      cuda-cccl=12.1.109 \
      -c nvidia/label/cuda-12.1.1 --override-channels || return $?
    validate_cuda_toolchain || return $?
  fi
  mark_stage nvcc

  if validate_requirements; then
    printf '%s\n' 'resume=CUT3R Python 依赖已验证，跳过安装'
  else
    printf '%s\n' 'command=pip install CUT3R requirements、gdown 与固定 transformers 4.55.4'
    timeout 7200 "${PYTHON}" -m pip install \
      -r "${CUT3R_ROOT}/requirements.txt" gdown \
      transformers==4.55.4 || return $?
    validate_requirements || return $?
  fi
  mark_stage requirements

  if "${CONDA}" list -p "${ENV_PREFIX}" llvm-openmp 2>/dev/null \
    | grep -q '^llvm-openmp '; then
    printf '%s\n' 'resume=llvm-openmp 已安装，跳过安装'
  else
    printf '%s\n' 'command=conda install llvm-openmp<16'
    timeout 3600 "${CONDA}" install -y -p "${ENV_PREFIX}" \
      'llvm-openmp<16' -c conda-forge --override-channels || return $?
  fi
  mark_stage openmp

  if validate_curope; then
    printf '%s\n' 'resume=CUDA RoPE 扩展已验证，跳过编译'
  else
    printf '%s\n' 'command=python setup.py build_ext --inplace（A100 sm_80）'
    (
      cd "${CUT3R_ROOT}/src/croco/models/curope" || exit 1
      "${PYTHON}" setup.py build_ext --inplace
    ) || return $?
    validate_curope || return $?
  fi
  mark_stage curope

  export TRUST3D_CUT3R_COMMIT
  TRUST3D_CUT3R_COMMIT=$(git -C "${CUT3R_ROOT}" rev-parse HEAD) || return $?
  "${PYTHON}" - <<'PY'
import json
import os
import platform
import subprocess
from pathlib import Path

import torch
import torchvision
import transformers

report = {
    "schema_version": 1,
    "host": platform.node(),
    "cut3r_commit": os.environ["TRUST3D_CUT3R_COMMIT"],
    "environment_prefix": "/224010104/Jerry/.conda/envs/cut3r",
    "python_version": platform.python_version(),
    "torch_version": torch.__version__,
    "torchvision_version": torchvision.__version__,
    "transformers_version": transformers.__version__,
    "torch_cuda_version": torch.version.cuda,
    "cuda_available": torch.cuda.is_available(),
    "gpu_count_visible_without_mask": torch.cuda.device_count(),
    "curope_importable": True,
    "requirements_sha256": subprocess.check_output(
        ["sha256sum", "external/cut3r/requirements.txt"], text=True
    ).split()[0],
}
path = Path("outputs/gate7/environment.json")
path.parent.mkdir(parents=True, exist_ok=True)
temporary = path.with_name(path.name + ".tmp")
temporary.write_text(
    json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
temporary.replace(path)
print(json.dumps(report, ensure_ascii=False, sort_keys=True))
PY
  report_rc=$?
  printf 'environment_report_exit_code=%s end=%s\n' \
    "${report_rc}" "$(date -Is)"
  return "${report_rc}"
}

set +e
run 2>&1 | tee -a "${LOG_PATH}"
rc=${PIPESTATUS[0]}
set -e
printf '%s\n' "${rc}" > "${EXIT_PATH}.tmp"
mv "${EXIT_PATH}.tmp" "${EXIT_PATH}"
exit "${rc}"
