#!/usr/bin/env bash
set -euo pipefail

ROOT=/224010104/Jerry/trust3d
MODE=${1:-all}
cd "${ROOT}"

if [[ ${MODE} == --staged-only ]]; then
  mapfile -t files < <(git diff --cached --name-only --diff-filter=ACMR)
else
  mapfile -t files < <(git ls-files)
fi

contains_pattern() {
  local pattern=$1
  local path=$2
  if command -v rg >/dev/null 2>&1; then
    rg -n -i -- "${pattern}" "${path}"
  else
    grep -n -I -E -i -- "${pattern}" "${path}"
  fi
}

failures=0
for path in "${files[@]}"; do
  [[ -f ${path} ]] || continue
  size=$(stat -c %s "${path}")
  if (( size > 50 * 1024 * 1024 )); then
    printf '拒绝：tracked artifact 超过 50 MiB：%s (%s bytes)\n' "${path}" "${size}"
    failures=$((failures + 1))
  fi
  if [[ ${path} != scripts/audit_parallel_artifacts.sh ]] \
    && contains_pattern \
    'OPENAI_API_KEY|CRS_OAI_KEY|BEGIN (RSA |OPENSSH )?PRIVATE KEY|github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+' \
    "${path}" >/dev/null 2>&1; then
    printf '拒绝：检测到凭据模式：%s\n' "${path}"
    failures=$((failures + 1))
  fi
  if [[ ${path} == outputs/parallel_v2/* ]] \
    && contains_pattern 'private_answer|oracle_best_route|route_losses|current_answer_gt|historical_answer_gt' "${path}" >/dev/null 2>&1; then
    case "${path}" in
      *protocol*|*metrics*|*report*|*final_decision*) ;;
      *) printf '拒绝：公开跟踪产物疑似包含 private 字段：%s\n' "${path}"; failures=$((failures + 1)) ;;
    esac
  fi
done

if (( failures > 0 )); then
  exit 1
fi
printf 'parallel-v2 Git 产物审计通过：%s 个文件。\n' "${#files[@]}"
