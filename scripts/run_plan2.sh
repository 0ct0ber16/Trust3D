#!/usr/bin/env bash
set -uo pipefail

ROOT=/224010104/Jerry/trust3d
JERRY=/224010104/Jerry
LOG_ROOT=${JERRY}/logs/plan2
STATE_ROOT=${JERRY}/checkpoints/plan2/stages
OUTPUT=${ROOT}/outputs/plan2
STATUS=${OUTPUT}/status.json
LOCK=${JERRY}/checkpoints/plan2/runner.lock
CONFIG=${ROOT}/configs/plan2_vggt.json
VGGT_ROOT=${ROOT}/external/vggt
VGGT_ENV=${JERRY}/.conda/envs/vggt
VGGT_PY=${VGGT_ENV}/bin/python
MODEL=${VGGT_ROOT}/model.safetensors
MODEL_PART=${MODEL}.part
BASE_PY=/224010104/Jerry/.conda/envs/cut3r/bin/python
SIM_PY=/224010104/miniconda3/envs/trust3d-sim/bin/python
MODE=${1:-status}
NVIDIA_SMI=${PLAN2_NVIDIA_SMI:-nvidia-smi}

export HOME=${JERRY}
export TMPDIR=${JERRY}/.tmp
export PIP_CACHE_DIR=${JERRY}/.cache/pip
export HF_HOME=${JERRY}/.cache/huggingface
export CONDA_PKGS_DIRS=${JERRY}/.conda/pkgs
export CONDA_ENVS_PATH=${JERRY}/.conda/envs
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=${ROOT}:${VGGT_ROOT}${PYTHONPATH:+:${PYTHONPATH}}

mkdir -p "${LOG_ROOT}" "${STATE_ROOT}" "${OUTPUT}" "${TMPDIR}" "$(dirname "${LOCK}")"
cd "${ROOT}"

utc_stamp() {
  date -u +%Y%m%dT%H%M%SZ
}

atomic_jq() {
  local destination=$1
  shift
  local temporary=${destination}.tmp.$$
  jq "$@" > "${temporary}"
  mv "${temporary}" "${destination}"
}

write_status() {
  local state=$1
  local stage=$2
  local message=$3
  local log_path=${4:-}
  atomic_jq "${STATUS}" -n \
    --arg state "${state}" \
    --arg stage "${stage}" \
    --arg message "${message}" \
    --arg current_log "${log_path}" \
    --arg updated_at "$(date -Is)" \
    --arg host "$(hostname)" \
    '{schema_version:1,state:$state,stage:$stage,message:$message,current_log:$current_log,updated_at:$updated_at,host:$host}'
}

stage_path() {
  printf '%s/%s.json\n' "${STATE_ROOT}" "$1"
}

stage_done() {
  local path
  path=$(stage_path "$1")
  [[ -f ${path} ]] && jq -e '.status == "complete"' "${path}" >/dev/null
}

mark_stage() {
  local stage=$1
  local output_path=$2
  local path
  path=$(stage_path "${stage}")
  atomic_jq "${path}" -n \
    --arg stage "${stage}" \
    --arg output "${output_path}" \
    --arg completed_at "$(date -Is)" \
    --arg commit "$(git rev-parse HEAD)" \
    '{schema_version:1,stage:$stage,status:"complete",output:$output,completed_at:$completed_at,git_commit:$commit}'
}

host_preflight() {
  [[ -n ${TMUX:-} ]] || {
    printf '%s\n' '安全检查失败：Plan 2 只能在 tmux 中执行。'
    return 125
  }
  printf '[%s] 主机资源检查\n' "$(date -Is)"
  uptime
  free -h
  df -h "${JERRY}"
  "${NVIDIA_SMI}" --query-gpu=index,memory.used,memory.free,utilization.gpu,temperature.gpu --format=csv,noheader,nounits
  local available_kib free_kib
  available_kib=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
  free_kib=$(df -Pk "${JERRY}" | awk 'NR==2{print $4}')
  (( available_kib >= 100 * 1024 * 1024 )) || {
    printf '%s\n' '可用内存不足 100 GiB，暂停任务。'
    return 75
  }
  (( free_kib >= 20 * 1024 * 1024 )) || {
    printf '%s\n' '工作区可用磁盘不足 20 GiB，暂停任务。'
    return 75
  }
}

baseline_hashes() {
  printf '%s  %s\n' \
    2c640c1279c7a4724ca60287e6f3ec9f943416ce2964018d2d8397e280fe94d5 outputs/gate7/validation.json \
    8af0a03e9f4ac956dbcd5f859eb093fc5197d1c581b779eb8367c185a5df40fc outputs/gate7/predictions.jsonl \
    83a10737770129b97cf36c3b72b616501c9d5910ef49a74b01c446cfe690694c outputs/gate7/checkpoint_recovery.json \
    426fde42f5ed78d1bca75575ff727e972b9441d335cb5107474128fc5094be76 outputs/gate7/cut3r_geometry/manifest.json \
    9d5cd67479fb1e1c8291effb0d77ed876b0cc27d6b7f9cd02eb43045f3022263 data/episodes/spatial30/episodes_public.jsonl \
    e81df1510a292be065dd8db93d155ee8afa798f435e6064f80ea08b2d484cf21 data/episodes/spatial30/oracle_private.jsonl \
    057c60055eaa0ee59cf8d4d76d1db22a900c3697e2667c495e05ec07336cf888 outputs/gate6/routes.jsonl \
    | sha256sum -c -
}

run_baseline() {
  if stage_done baseline; then
    printf '%s\n' 'baseline 已完成，跳过。'
    return 0
  fi
  host_preflight || return $?
  baseline_hashes || return $?
  local regression=${OUTPUT}/cut3r_regression
  mkdir -p "${regression}"
  set +e
  "${SIM_PY}" -m trust3d.eval.evaluate_cut3r \
    --public data/episodes/spatial30/episodes_public.jsonl \
    --private data/episodes/spatial30/oracle_private.jsonl \
    --routes outputs/gate6/routes.jsonl \
    --geometry outputs/gate7/cut3r_geometry \
    --source-checkpoints data/episodes/spatial30/checkpoints \
    --predictions "${regression}/predictions.jsonl" \
    --output "${regression}/validation.json"
  local evaluator_rc=$?
  set -e
  [[ ${evaluator_rc} -eq 1 ]] || {
    printf 'CUT3R 回归 evaluator 退出码异常：%s\n' "${evaluator_rc}"
    return 1
  }
  [[ $(sha256sum "${regression}/predictions.jsonl" | awk '{print $1}') == 8af0a03e9f4ac956dbcd5f859eb093fc5197d1c581b779eb8367c185a5df40fc ]] || return 1
  [[ $(sha256sum "${regression}/validation.json" | awk '{print $1}') == 2c640c1279c7a4724ca60287e6f3ec9f943416ce2964018d2d8397e280fe94d5 ]] || return 1
  "${SIM_PY}" -m trust3d.eval.diagnose_cut3r \
    --geometry outputs/gate7/cut3r_geometry \
    --predictions outputs/gate7/predictions.jsonl \
    --private data/episodes/spatial30/oracle_private.jsonl \
    --routes outputs/gate6/routes.jsonl \
    --source-checkpoints data/episodes/spatial30/checkpoints \
    --dataset-root data/episodes/spatial30 \
    --output "${OUTPUT}/cut3r_diagnostics.json" || return $?
  atomic_jq "${OUTPUT}/baseline.json" -n \
    --arg checked_at "$(date -Is)" \
    --arg commit "$(git rev-parse HEAD)" \
    '{schema_version:1,checked_at:$checked_at,git_commit:$commit,group_count:30,episode_count:360,cut3r_accuracy:0.5611111111111111,gt_accuracy:1.0,qa_drop:0.4388888888888889,all_hashes_match:true,evaluator_regression_match:true,original_gate7_modified:false}'
  baseline_hashes || return $?
  mark_stage baseline "${OUTPUT}/baseline.json"
}

bootstrap_environment() {
  local rebuild=0
  if [[ ! -x ${VGGT_PY} ]]; then
    rebuild=1
  elif ! "${VGGT_PY}" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)'; then
    rebuild=1
  fi
  if (( rebuild == 1 )); then
    if [[ -d ${VGGT_ENV} ]]; then
      mv "${VGGT_ENV}" "${VGGT_ENV}.failed.$(utc_stamp)"
    fi
    "${BASE_PY}" -m venv "${VGGT_ENV}" || return $?
  fi
  "${VGGT_PY}" -m pip install --upgrade pip || return $?
  "${VGGT_PY}" -m pip install -r "${VGGT_ROOT}/requirements.txt" pytest || return $?
}

download_weights() {
  local expected_size expected_sha revision url
  expected_size=$(jq -r '.model_file_bytes' "${CONFIG}")
  expected_sha=$(jq -r '.model_sha256' "${CONFIG}")
  revision=$(jq -r '.model_revision' "${CONFIG}")
  url="https://huggingface.co/facebook/VGGT-1B/resolve/${revision}/model.safetensors"
  if [[ -f ${MODEL} ]]; then
    if [[ $(stat -c %s "${MODEL}") -eq ${expected_size} ]] \
      && [[ $(sha256sum "${MODEL}" | awk '{print $1}') == "${expected_sha}" ]]; then
      printf '%s\n' 'VGGT 权重大小和 SHA256 已验证，跳过下载。'
      return 0
    fi
    mv "${MODEL}" "${MODEL}.invalid.$(utc_stamp)"
  fi
  if [[ -f ${MODEL_PART} ]] && (( $(stat -c %s "${MODEL_PART}") > expected_size )); then
    mv "${MODEL_PART}" "${MODEL_PART}.invalid.$(utc_stamp)"
  fi
  curl -fL --retry 20 --retry-all-errors --retry-delay 10 \
    --connect-timeout 30 --speed-limit 1048576 --speed-time 120 --continue-at - \
    --output "${MODEL_PART}" "${url}" || return $?
  [[ $(stat -c %s "${MODEL_PART}") -eq ${expected_size} ]] || return 1
  [[ $(sha256sum "${MODEL_PART}" | awk '{print $1}') == "${expected_sha}" ]] || return 1
  mv "${MODEL_PART}" "${MODEL}"
  curl -fsSL \
    "https://huggingface.co/facebook/VGGT-1B/resolve/${revision}/config.json" \
    --output "${VGGT_ROOT}/config.json.tmp" || return $?
  mv "${VGGT_ROOT}/config.json.tmp" "${VGGT_ROOT}/config.json"
}

run_bootstrap() {
  if stage_done bootstrap; then
    printf '%s\n' 'bootstrap 已完成，跳过。'
    return 0
  fi
  stage_done baseline || {
    printf '%s\n' '必须先完成 baseline。'
    return 2
  }
  host_preflight || return $?
  local commit
  commit=$(jq -r '.repository_commit' "${CONFIG}")
  if [[ ! -d ${VGGT_ROOT}/.git ]]; then
    git clone --filter=blob:none "$(jq -r '.repository' "${CONFIG}")" "${VGGT_ROOT}" || return $?
  fi
  git -C "${VGGT_ROOT}" checkout --detach "${commit}" || return $?
  [[ $(git -C "${VGGT_ROOT}" rev-parse HEAD) == "${commit}" ]] || return 1
  bootstrap_environment || return $?
  download_weights || return $?
  "${VGGT_PY}" -c 'from safetensors import safe_open; import sys; f=safe_open(sys.argv[1],framework="pt",device="cpu"); keys=list(f.keys()); assert "aggregator.camera_token" in keys and len(keys)>100; print(f"safetensors_key_count={len(keys)}")' "${MODEL}" || return $?
  "${VGGT_PY}" -m pytest -q tests/test_gate7_cut3r.py tests/test_plan2_vggt.py || return $?
  local torch_version python_version license_sha readme_sha
  torch_version=$("${VGGT_PY}" -c 'import torch; print(torch.__version__)')
  python_version=$("${VGGT_PY}" -c 'import platform; print(platform.python_version())')
  license_sha=$(sha256sum "${VGGT_ROOT}/LICENSE.txt" | awk '{print $1}')
  readme_sha=$(sha256sum "${VGGT_ROOT}/README.md" | awk '{print $1}')
  atomic_jq "${OUTPUT}/vggt_environment.json" -n \
    --arg repository_commit "${commit}" \
    --arg python "${python_version}" \
    --arg torch "${torch_version}" \
    --arg license_sha256 "${license_sha}" \
    --arg readme_sha256 "${readme_sha}" \
    '{schema_version:1,repository_commit:$repository_commit,python:$python,torch:$torch,license_sha256:$license_sha256,readme_sha256:$readme_sha256,environment_complete:true}'
  atomic_jq "${OUTPUT}/vggt_weights.json" -n \
    --arg path "${MODEL}" \
    --arg sha256 "$(sha256sum "${MODEL}" | awk '{print $1}')" \
    --argjson bytes "$(stat -c %s "${MODEL}")" \
    '{schema_version:1,path:$path,sha256:$sha256,bytes:$bytes,verified:true}'
  mark_stage bootstrap "${OUTPUT}/vggt_environment.json"
}

gpu_row() {
  local requested=${1:-}
  "${NVIDIA_SMI}" --query-gpu=index,memory.free,utilization.gpu --format=csv,noheader,nounits \
    | tr -d ' ' \
    | awk -F, -v requested="${requested}" -v min_free=60000 -v max_util=10 '
      ($2+0)>=min_free && ($3+0)<=max_util && (requested=="" || $1==requested) {print; exit}'
}

gpu_preflight() {
  if pgrep -af '[t]rust3d.geometry.run_vggt' >/dev/null; then
    printf '%s\n' '检测到已有 Plan 2 VGGT 进程，不重复启动。'
    return 73
  fi
  local requested=${PLAN2_SELECTED_GPU:-}
  local selected=''
  local check row index free_mib utilization
  for check in 1 2 3; do
    row=$(gpu_row "${requested}")
    if [[ -z ${row} ]]; then
      printf 'GPU 检查 %s/3 未找到合格设备。\n' "${check}"
      return 75
    fi
    IFS=, read -r index free_mib utilization <<< "${row}"
    if [[ -n ${selected} && ${selected} != "${index}" ]]; then
      printf '%s\n' '连续检查未保持同一张 GPU，暂停。'
      return 75
    fi
    selected=${index}
    requested=${index}
    printf 'GPU 检查 %s/3: index=%s free_mib=%s utilization=%s\n' \
      "${check}" "${index}" "${free_mib}" "${utilization}"
    if (( check < 3 )); then
      sleep 10
    fi
  done
  SELECTED_GPU=${selected}
  export SELECTED_GPU
}

ensure_protocol_lock() {
  local lock=${OUTPUT}/protocol_lock.json
  local config_sha adapter_sha evaluator_sha
  config_sha=$(sha256sum "${CONFIG}" | awk '{print $1}')
  adapter_sha=$(sha256sum trust3d/geometry/run_vggt.py | awk '{print $1}')
  evaluator_sha=$(sha256sum trust3d/eval/evaluate_cut3r.py | awk '{print $1}')
  if [[ -f ${lock} ]]; then
    jq -e \
      --arg commit "$(git rev-parse HEAD)" \
      --arg config "${config_sha}" \
      --arg adapter "${adapter_sha}" \
      --arg evaluator "${evaluator_sha}" \
      '.git_commit==$commit and .config_sha256==$config and .adapter_sha256==$adapter and .evaluator_sha256==$evaluator and .qa_revealed==false' \
      "${lock}" >/dev/null
    return $?
  fi
  [[ -z $(git status --porcelain --untracked-files=no) ]] || {
    printf '%s\n' '协议冻结前 Git tracked 工作树必须干净。'
    return 1
  }
  atomic_jq "${lock}" -n \
    --arg created_at "$(date -Is)" \
    --arg git_commit "$(git rev-parse HEAD)" \
    --arg config_sha256 "${config_sha}" \
    --arg adapter_sha256 "${adapter_sha}" \
    --arg evaluator_sha256 "${evaluator_sha}" \
    --arg model_sha256 "$(jq -r '.model_sha256' "${CONFIG}")" \
    --arg public_sha256 9d5cd67479fb1e1c8291effb0d77ed876b0cc27d6b7f9cd02eb43045f3022263 \
    --arg routes_sha256 057c60055eaa0ee59cf8d4d76d1db22a900c3697e2667c495e05ec07336cf888 \
    '{schema_version:1,created_at:$created_at,git_commit:$git_commit,config_sha256:$config_sha256,adapter_sha256:$adapter_sha256,evaluator_sha256:$evaluator_sha256,model_sha256:$model_sha256,public_sha256:$public_sha256,routes_sha256:$routes_sha256,qa_revealed:false}'
}

vggt_args() {
  printf '%s\n' \
    --episodes data/episodes/spatial30/episodes_public.jsonl \
    --routes outputs/gate6/routes.jsonl \
    --checkpoint "${MODEL}" \
    --config "${CONFIG}" \
    --output "${OUTPUT}/vggt_geometry" \
    --source-checkpoints data/episodes/spatial30/checkpoints \
    --dataset-root data/episodes/spatial30 \
    --vggt-root "${VGGT_ROOT}" \
    --cut3r-geometry outputs/gate7/cut3r_geometry
}

run_smoke() {
  if stage_done smoke; then
    printf '%s\n' 'smoke 已完成，跳过。'
    return 0
  fi
  stage_done baseline && stage_done bootstrap || return 2
  host_preflight || return $?
  ensure_protocol_lock || return $?
  gpu_preflight || return $?
  mapfile -t args < <(vggt_args)
  local group_id
  group_id=$(jq -r '.smoke_group_id' "${CONFIG}")
  set +e
  CUDA_VISIBLE_DEVICES=${SELECTED_GPU} OMP_NUM_THREADS=8 \
    "${VGGT_PY}" -u -m trust3d.geometry.run_vggt \
      "${args[@]}" --device cuda --group-id "${group_id}" --max-groups 1
  local rc=$?
  set -e
  if [[ ${rc} -ne 0 ]]; then
    if jq -e '.error_type | test("OutOfMemory|CUDA"; "i")' \
      "${OUTPUT}/vggt_geometry/checkpoints/${group_id}.json" >/dev/null 2>&1; then
      return 75
    fi
    return "${rc}"
  fi
  jq -e '.complete and .success_group_count==1' "${OUTPUT}/vggt_geometry/manifest.json" >/dev/null || return 1
  CUDA_VISIBLE_DEVICES='' "${VGGT_PY}" -u -m trust3d.geometry.run_vggt \
    "${args[@]}" --device cpu --group-id "${group_id}" --max-groups 1 || return $?
  mark_stage smoke "${OUTPUT}/vggt_geometry/checkpoints/${group_id}.json"
}

run_full() {
  if stage_done full; then
    printf '%s\n' 'full 已完成，跳过。'
    return 0
  fi
  stage_done smoke || return 2
  host_preflight || return $?
  ensure_protocol_lock || return $?
  local success_count=0
  if [[ -f ${OUTPUT}/vggt_geometry/manifest.json ]]; then
    success_count=$(jq -r '.success_group_count // 0' "${OUTPUT}/vggt_geometry/manifest.json")
  fi
  if (( success_count < 30 )); then
    gpu_preflight || return $?
  else
    SELECTED_GPU=''
  fi
  mapfile -t args < <(vggt_args)
  set +e
  CUDA_VISIBLE_DEVICES=${SELECTED_GPU} OMP_NUM_THREADS=8 \
    "${VGGT_PY}" -u -m trust3d.geometry.run_vggt \
      "${args[@]}" --device "$([[ -n ${SELECTED_GPU} ]] && echo cuda || echo cpu)" --continue-on-error
  local geometry_rc=$?
  set -e
  if [[ ${geometry_rc} -ne 0 ]]; then
    if rg -l 'OutOfMemory|CUDA out of memory' "${OUTPUT}/vggt_geometry/checkpoints"/*.json >/dev/null 2>&1; then
      return 75
    fi
    return "${geometry_rc}"
  fi
  jq -e '.complete and .success_group_count==30 and .failure_group_count==0' \
    "${OUTPUT}/vggt_geometry/manifest.json" >/dev/null || return 1
  set +e
  "${VGGT_PY}" -u -m trust3d.eval.evaluate_cut3r \
    --backend-id vggt \
    --public data/episodes/spatial30/episodes_public.jsonl \
    --private data/episodes/spatial30/oracle_private.jsonl \
    --routes outputs/gate6/routes.jsonl \
    --geometry "${OUTPUT}/vggt_geometry" \
    --source-checkpoints data/episodes/spatial30/checkpoints \
    --predictions "${OUTPUT}/vggt_predictions.jsonl" \
    --output "${OUTPUT}/vggt_validation.json" \
    --reference-predictions outputs/gate7/predictions.jsonl
  local evaluator_rc=$?
  set -e
  [[ ${evaluator_rc} -eq 0 || ${evaluator_rc} -eq 1 ]] || return "${evaluator_rc}"
  local before=${LOG_ROOT}/recovery-before.sha256
  local after=${LOG_ROOT}/recovery-after.sha256
  find "${OUTPUT}/vggt_geometry/checkpoints" -maxdepth 1 -type f -name '*.json' -print0 \
    | sort -z | xargs -0 sha256sum > "${before}"
  sha256sum "${OUTPUT}/vggt_predictions.jsonl" "${OUTPUT}/vggt_validation.json" >> "${before}"
  CUDA_VISIBLE_DEVICES='' "${VGGT_PY}" -u -m trust3d.geometry.run_vggt \
    "${args[@]}" --device cpu --continue-on-error || return $?
  find "${OUTPUT}/vggt_geometry/checkpoints" -maxdepth 1 -type f -name '*.json' -print0 \
    | sort -z | xargs -0 sha256sum > "${after}"
  sha256sum "${OUTPUT}/vggt_predictions.jsonl" "${OUTPUT}/vggt_validation.json" >> "${after}"
  cmp -s "${before}" "${after}" || return 1
  atomic_jq "${OUTPUT}/checkpoint_recovery.json" -n \
    --arg before "${before}" \
    --arg after "${after}" \
    '{schema_version:1,checkpoint_recovery_pass:true,all_hashes_match:true,checked_file_count:32,checkpoint_count:30,model_load_seconds_this_run:0.0,before_sha256_file:$before,after_sha256_file:$after}'
  mark_stage full "${OUTPUT}/vggt_validation.json"
}

run_diagnose() {
  if stage_done diagnose; then
    printf '%s\n' 'diagnose 已完成，跳过。'
    return 0
  fi
  stage_done full || return 2
  "${VGGT_PY}" -u -m trust3d.eval.plan2_decision diagnose \
    --validation "${OUTPUT}/vggt_validation.json" \
    --predictions "${OUTPUT}/vggt_predictions.jsonl" \
    --cut3r-diagnostics "${OUTPUT}/cut3r_diagnostics.json" \
    --geometry "${OUTPUT}/vggt_geometry" \
    --public data/episodes/spatial30/episodes_public.jsonl \
    --private data/episodes/spatial30/oracle_private.jsonl \
    --routes outputs/gate6/routes.jsonl \
    --source-checkpoints data/episodes/spatial30/checkpoints \
    --reference-predictions outputs/gate7/predictions.jsonl \
    --output "${OUTPUT}/conditional_diagnostics.json" || return $?
  mark_stage diagnose "${OUTPUT}/conditional_diagnostics.json"
}

run_decide() {
  if stage_done decide; then
    printf '%s\n' 'decide 已完成，跳过。'
    return 0
  fi
  stage_done diagnose || return 2
  "${VGGT_PY}" -u -m trust3d.eval.plan2_decision decide \
    --validation "${OUTPUT}/vggt_validation.json" \
    --recovery "${OUTPUT}/checkpoint_recovery.json" \
    --conditional-diagnostics "${OUTPUT}/conditional_diagnostics.json" \
    --cut3r-validation outputs/gate7/validation.json \
    --output "${OUTPUT}/final_decision.json" || return $?
  mark_stage decide "${OUTPUT}/final_decision.json"
}

run_resume() {
  run_baseline || return $?
  run_bootstrap || return $?
  run_smoke || return $?
  run_full || return $?
  run_diagnose || return $?
  run_decide || return $?
}

show_status() {
  if [[ -f ${STATUS} ]]; then
    jq . "${STATUS}"
  else
    printf '%s\n' '{"state":"not_started"}'
  fi
  printf '%s\n' '阶段状态：'
  for stage in baseline bootstrap smoke full diagnose decide; do
    if stage_done "${stage}"; then
      printf '  %-10s complete\n' "${stage}"
    else
      printf '  %-10s pending\n' "${stage}"
    fi
  done
  "${NVIDIA_SMI}" --query-gpu=index,memory.used,memory.free,utilization.gpu --format=csv,noheader,nounits
}

dispatch() {
  case "${MODE}" in
    baseline) run_baseline ;;
    bootstrap) run_bootstrap ;;
    smoke) run_smoke ;;
    full) run_full ;;
    diagnose) run_diagnose ;;
    decide) run_decide ;;
    resume) run_resume ;;
    status) show_status ;;
    *) printf '用法: %s <baseline|bootstrap|smoke|full|diagnose|decide|resume|status>\n' "$0"; return 2 ;;
  esac
}

if [[ ${MODE} == status ]]; then
  dispatch
  exit $?
fi

exec 9>"${LOCK}"
if ! flock -n 9; then
  printf '%s\n' '已有 Plan 2 runner 持有工作区锁。'
  exit 73
fi

LOG_PATH=${LOG_ROOT}/$(utc_stamp)-${MODE}.log
write_status running "${MODE}" '阶段正在执行' "${LOG_PATH}"
set +e
{
  printf 'start=%s tmux=%s host=%s cwd=%s command=%s log=%s\n' \
    "$(date -Is)" "${TMUX:-unset}" "$(hostname)" "${PWD}" "$0 ${MODE}" "${LOG_PATH}"
  dispatch
  rc=$?
  printf 'exit_code=%s end=%s\n' "${rc}" "$(date -Is)"
  exit "${rc}"
} 2>&1 | tee -a "${LOG_PATH}"
rc=${PIPESTATUS[0]}
set -e
atomic_jq "${LOG_PATH%.log}.exit.json" -n \
  --arg mode "${MODE}" \
  --arg ended_at "$(date -Is)" \
  --argjson exit_code "${rc}" \
  '{schema_version:1,mode:$mode,exit_code:$exit_code,ended_at:$ended_at}'
if [[ ${rc} -eq 0 ]]; then
  write_status complete "${MODE}" '阶段执行完成' "${LOG_PATH}"
elif [[ ${rc} -eq 75 ]]; then
  write_status waiting_for_gpu "${MODE}" '资源条件变化，返回监测' "${LOG_PATH}"
else
  write_status failed "${MODE}" "执行失败，退出码 ${rc}" "${LOG_PATH}"
fi
exit "${rc}"
