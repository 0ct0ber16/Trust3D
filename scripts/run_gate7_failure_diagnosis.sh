#!/usr/bin/env bash
set -uo pipefail

ROOT=/224010104/Jerry/trust3d
JERRY=/224010104/Jerry
CONFIG=${ROOT}/configs/gate7_failure_diagnosis.json
OUTPUT=${ROOT}/outputs/gate7_diagnosis
LOG_ROOT=${JERRY}/logs/gate7-diagnosis
STATE_ROOT=${JERRY}/checkpoints/gate7_diagnosis/stages
LOCK=${JERRY}/checkpoints/gate7_diagnosis/runner.lock
STATUS=${OUTPUT}/status.json
REPORT=${ROOT}/Trust3D_Gate7_CUT3R_VGGT失败原因诊断报告.md
CUT3R_PY=${JERRY}/.conda/envs/cut3r/bin/python
VGGT_PY=${JERRY}/.conda/envs/vggt/bin/python
MODE=${1:-status}
NVIDIA_SMI=${GATE7_DIAG_NVIDIA_SMI:-nvidia-smi}

export HOME=${JERRY}
export TMPDIR=${JERRY}/.tmp
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=${ROOT}:${ROOT}/external/vggt${PYTHONPATH:+:${PYTHONPATH}}

mkdir -p "${OUTPUT}" "${LOG_ROOT}" "${STATE_ROOT}" "${TMPDIR}" "$(dirname "${LOCK}")"
cd "${ROOT}"

RUNNER_LOG=${LOG_ROOT}/runner.log
exec > >(tee -a "${RUNNER_LOG}") 2>&1

STARTED_AT=$(date -Is)
printf '\n[%s] runner 开始 pid=%s host=%s mode=%s cwd=%s command=%q\n' \
  "${STARTED_AT}" "$$" "$(hostname)" "${MODE}" "${PWD}" "$0 $*"

runner_exit() {
  local rc=$? stage=${MODE} current_log=${RUNNER_LOG} backend='' gpu_uuid=''
  if (( rc != 0 )) && declare -F write_status >/dev/null; then
    if [[ -f ${STATUS} ]]; then
      stage=$(jq -r '.stage // empty' "${STATUS}")
      current_log=$(jq -r '.current_log // empty' "${STATUS}")
      backend=$(jq -r '.backend // empty' "${STATUS}")
      gpu_uuid=$(jq -r '.gpu_uuid // empty' "${STATUS}")
    fi
    write_status failed "${stage:-${MODE}}" \
      "runner 非零退出（exit=${rc}），请查看阶段日志后 resume" \
      "${current_log:-${RUNNER_LOG}}" "${backend}" "${gpu_uuid}" || true
  fi
  printf '[%s] runner 结束 pid=%s mode=%s exit=%s\n' \
    "$(date -Is)" "$$" "${MODE}" "${rc}"
}
trap runner_exit EXIT

atomic_jq() {
  local destination=$1
  shift
  local temporary=${destination}.tmp.$$
  jq "$@" > "${temporary}" || return $?
  sync -f "${temporary}"
  mv "${temporary}" "${destination}"
  sync -d "$(dirname "${destination}")" 2>/dev/null || true
}

write_status() {
  local state=$1 stage=$2 message=$3 log_path=${4:-}
  local backend=${5:-} gpu_uuid=${6:-}
  local completed_cut3r=0 completed_vggt=0
  if [[ -d ${OUTPUT}/cut3r/checkpoints ]]; then
    while IFS= read -r -d '' path; do
      if jq -e '.status == "success"' "${path}" >/dev/null 2>&1; then
        ((completed_cut3r += 1))
      fi
    done < <(find "${OUTPUT}/cut3r/checkpoints" -maxdepth 1 -type f -name '*.json' -print0)
  fi
  if [[ -d ${OUTPUT}/vggt/checkpoints ]]; then
    while IFS= read -r -d '' path; do
      if jq -e '.status == "success"' "${path}" >/dev/null 2>&1; then
        ((completed_vggt += 1))
      fi
    done < <(find "${OUTPUT}/vggt/checkpoints" -maxdepth 1 -type f -name '*.json' -print0)
  fi
  atomic_jq "${STATUS}" -n \
    --arg state "${state}" --arg stage "${stage}" --arg message "${message}" \
    --arg current_log "${log_path}" --arg backend "${backend}" \
    --arg gpu_uuid "${gpu_uuid}" --arg host "$(hostname)" \
    --arg updated_at "$(date -Is)" --argjson runner_pid "$$" \
    --argjson completed_cut3r "${completed_cut3r}" \
    --argjson completed_vggt "${completed_vggt}" \
    --arg protocol_revision "$(jq -r '.protocol_revision' "${CONFIG}")" \
    '{schema_version:1,state:$state,stage:$stage,message:$message,current_log:$current_log,backend:$backend,gpu_uuid:$gpu_uuid,hostname:$host,runner_pid:$runner_pid,protocol_revision:$protocol_revision,completed_groups:{cut3r:$completed_cut3r,vggt:$completed_vggt},pending_groups:{cut3r:(30-$completed_cut3r),vggt:(30-$completed_vggt)},updated_at:$updated_at}'
}

stage_path() {
  printf '%s/%s.json\n' "${STATE_ROOT}" "$1"
}

stage_done() {
  local stage=$1 path output expected actual expected_config actual_config
  path=$(stage_path "${stage}")
  [[ -f ${path} ]] || return 1
  expected_config=$(jq -r '.config_sha256 // empty' "${path}")
  actual_config=$(sha256sum "${CONFIG}" | awk '{print $1}')
  [[ ${expected_config} == "${actual_config}" ]] || return 1
  [[ $(jq -r '.protocol_revision // empty' "${path}") == \
    "$(jq -r '.protocol_revision' "${CONFIG}")" ]] || return 1
  output=$(jq -r '.output' "${path}")
  expected=$(jq -r '.output_sha256' "${path}")
  [[ -f ${output} ]] || return 1
  actual=$(sha256sum "${output}" | awk '{print $1}')
  [[ ${actual} == "${expected}" ]]
}

mark_stage() {
  local stage=$1 output=$2
  local sha config_sha protocol_revision
  sha=$(sha256sum "${output}" | awk '{print $1}') || return $?
  config_sha=$(sha256sum "${CONFIG}" | awk '{print $1}') || return $?
  protocol_revision=$(jq -r '.protocol_revision' "${CONFIG}") || return $?
  atomic_jq "$(stage_path "${stage}")" -n \
    --arg stage "${stage}" --arg output "${output}" --arg output_sha256 "${sha}" \
    --arg completed_at "$(date -Is)" --arg git_commit "$(git rev-parse HEAD)" \
    --arg config_sha256 "${config_sha}" --arg protocol_revision "${protocol_revision}" \
    '{schema_version:1,stage:$stage,status:"complete",output:$output,output_sha256:$output_sha256,completed_at:$completed_at,git_commit:$git_commit,config_sha256:$config_sha256,protocol_revision:$protocol_revision}'
}

run_logged() {
  local log_path=$1
  shift
  printf '[%s] 工作目录=%s\n[%s] 日志=%s\n[%s] 完整命令=' \
    "$(date -Is)" "${PWD}" "$(date -Is)" "${log_path}" "$(date -Is)"
  printf '%q ' "$@"
  printf '\n'
  "$@" 2>&1 | tee -a "${log_path}"
  local rc=${PIPESTATUS[0]}
  printf '[%s] 退出码=%s\n' "$(date -Is)" "${rc}" | tee -a "${log_path}"
  return "${rc}"
}

host_preflight() {
  [[ -n ${TMUX:-} ]] || {
    printf '%s\n' '安全检查失败：所有阶段只能在 tmux 中执行。'
    return 125
  }
  printf '[%s] 主机资源检查\n' "$(date -Is)"
  uptime
  free -h
  df -h "${JERRY}"
  "${NVIDIA_SMI}" --query-gpu=index,uuid,memory.used,memory.free,utilization.gpu,temperature.gpu --format=csv,noheader,nounits
  local load cores available_kib free_kib
  load=$(awk '{print $1}' /proc/loadavg)
  cores=$(nproc)
  available_kib=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
  free_kib=$(df -Pk "${JERRY}" | awk 'NR==2{print $4}')
  awk -v load="${load}" -v cores="${cores}" 'BEGIN{exit !(load < cores*0.75)}' || {
    printf 'CPU 1分钟负载 %s 超过 75%% 门槛，暂停。\n' "${load}"
    return 75
  }
  (( available_kib >= 64 * 1024 * 1024 )) || {
    printf '%s\n' '可用内存不足 64 GiB，暂停。'
    return 75
  }
  (( free_kib >= 100 * 1024 * 1024 )) || {
    printf '%s\n' '工作区可用磁盘不足 100 GiB，暂停。'
    return 75
  }
}

run_prepare() {
  if stage_done prepare; then
    printf '%s\n' 'P0 prepare 已通过 checkpoint 校验，跳过。'
    return 0
  fi
  host_preflight || return $?
  write_status running prepare '执行 P0 静态边界检查' "${LOG_ROOT}/prepare.log"
  run_logged "${LOG_ROOT}/prepare.log" "${VGGT_PY}" -u -m \
    trust3d.eval.diagnose_gate7_layers prepare --config "${CONFIG}" || return $?
  mark_stage prepare "${OUTPUT}/prepare.json"
}

run_lock() {
  if stage_done lock; then
    printf '%s\n' 'D0 lock 已通过 checkpoint 校验，跳过。'
    return 0
  fi
  host_preflight || return $?
  write_status running lock '冻结 r000 协议和原结果哈希' "${LOG_ROOT}/lock.log"
  run_logged "${LOG_ROOT}/lock.log" "${VGGT_PY}" -u -m \
    trust3d.eval.diagnose_gate7_layers lock --config "${CONFIG}" || return $?
  mark_stage lock "${OUTPUT}/protocol_lock.json"
}

run_unit() {
  if stage_done unit; then
    printf '%s\n' 'D1 unit 已通过 checkpoint 校验，跳过。'
    return 0
  fi
  host_preflight || return $?
  write_status running unit '执行合成坐标、角色和任务头契约' "${LOG_ROOT}/unit.log"
  run_logged "${LOG_ROOT}/unit.log" "${VGGT_PY}" -m pytest -q \
    tests/test_gate7_cut3r.py tests/test_plan2_vggt.py \
    tests/test_gate7_geometry_contract.py || return $?
  run_logged "${LOG_ROOT}/unit.log" "${VGGT_PY}" -u -m \
    trust3d.eval.diagnose_gate7_layers unit-summary --config "${CONFIG}" || return $?
  mark_stage unit "${OUTPUT}/synthetic_contract.json"
}

run_offline() {
  if stage_done offline; then
    printf '%s\n' 'D2 offline 已通过 checkpoint 校验，跳过。'
    return 0
  fi
  host_preflight || return $?
  write_status running offline '执行公开候选和私有 oracle 离线分解' "${LOG_ROOT}/offline.log"
  run_logged "${LOG_ROOT}/offline.log" "${VGGT_PY}" -u -m \
    trust3d.eval.diagnose_gate7_layers offline --config "${CONFIG}" || return $?
  jq -e '.complete and .baseline_reproduction.cut3r.exact_reproduction and .baseline_reproduction.vggt.exact_reproduction and .task_head.T0_accuracy==1 and .task_head.R0_accuracy==1 and .role_binding_pass' \
    "${OUTPUT}/offline_audit.json" >/dev/null || return 1
  mark_stage offline "${OUTPUT}/offline_audit.json"
}

gpu_sample() {
  "${NVIDIA_SMI}" --query-gpu=index,uuid,memory.free,utilization.gpu \
    --format=csv,noheader,nounits
}

claim_gpu_once() {
  local minimum_free=${1:-20480}
  local first index uuid free utilization second
  while IFS=',' read -r index uuid free utilization; do
    index=${index// /}; uuid=${uuid// /}; free=${free// /}; utilization=${utilization// /}
    if (( free >= minimum_free && utilization <= 10 )); then
      first="${index},${uuid}"
      break
    fi
  done < <(gpu_sample)
  [[ -n ${first:-} ]] || return 1
  IFS=',' read -r index uuid <<< "${first}"
  second=$(gpu_sample | awk -F',' -v wanted="${index}" '$1+0==wanted {gsub(/ /,"",$0); print $0}')
  IFS=',' read -r check_index check_uuid free utilization <<< "${second}"
  if [[ ${check_uuid} == "${uuid}" ]] && (( free >= minimum_free && utilization <= 10 )); then
    printf '%s,%s\n' "${index}" "${uuid}"
    return 0
  fi
  return 1
}

wait_for_gpu() {
  local claim
  write_status waiting_for_gpu gpu-all '等待单次采样满足 20 GiB/10% 门槛' "${LOG_ROOT}/gpu-watch.log"
  while true; do
    if ! host_preflight >> "${LOG_ROOT}/gpu-watch.log" 2>&1; then
      write_status failed_engineering gpu-all 'CPU、内存或磁盘安全检查失败' "${LOG_ROOT}/gpu-watch.log"
      return 75
    fi
    if claim=$(claim_gpu_once 20480); then
      printf '[%s] GPU 准入命中并通过原子复核：%s\n' "$(date -Is)" "${claim}" | tee -a "${LOG_ROOT}/gpu-watch.log"
      printf '%s\n' "${claim}"
      return 0
    fi
    printf '[%s] GPU 忙，继续等待：%s\n' "$(date -Is)" "$(gpu_sample | tr '\n' ';')" >> "${LOG_ROOT}/gpu-watch.log"
    sleep 1
  done
}

verify_selected_gpu_for_vggt() {
  local index=$1 uuid=$2 line free utilization
  while true; do
    line=$(gpu_sample | awk -F',' -v wanted="${index}" '$1+0==wanted {gsub(/ /,"",$0); print $0}')
    IFS=',' read -r _ check_uuid free utilization <<< "${line}"
    if [[ ${check_uuid} == "${uuid}" ]] && (( free >= 20480 && utilization <= 10 )); then
      return 0
    fi
    printf '[%s] backend 切换复核未通过，保持 runner 并等待：%s\n' "$(date -Is)" "${line}" >> "${LOG_ROOT}/gpu-watch.log"
    sleep 1
  done
}

run_gpu_all() {
  if stage_done gpu; then
    printf '%s\n' 'D3/D4 GPU 已通过 checkpoint 校验，跳过。'
    return 0
  fi
  local claim index uuid
  claim=$(wait_for_gpu) || return $?
  claim=$(printf '%s\n' "${claim}" | tail -n 1)
  IFS=',' read -r index uuid <<< "${claim}"
  printf '[%s] 诊断实验开始，GPU index=%s uuid=%s\n' "$(date -Is)" "${index}" "${uuid}" | tee -a "${JERRY}/logs/gate7-diagnosis/notifications.log"
  write_status running gpu-all 'CUT3R 模型已领取 GPU' "${LOG_ROOT}/gpu-all.log" cut3r "${uuid}"
  run_logged "${LOG_ROOT}/cut3r-full.log" env CUDA_VISIBLE_DEVICES="${index}" \
    "${CUT3R_PY}" -u -m trust3d.geometry.run_cut3r \
    --episodes data/episodes/spatial30/episodes_public.jsonl \
    --checkpoint external/cut3r/src/cut3r_512_dpt_4_64.pth \
    --output "${OUTPUT}/cut3r" \
    --source-checkpoints data/episodes/spatial30/checkpoints \
    --dataset-root data/episodes/spatial30 --cut3r-root external/cut3r \
    --device cuda --image-size 512 --center-crop-fraction 0.12 \
    --diagnostic-config "${CONFIG}" || return $?
  jq -e '.complete and .success_group_count==30 and .diagnostic_only' \
    "${OUTPUT}/cut3r/manifest.json" >/dev/null || return 1

  write_status recovering gpu-all 'CUT3R 完成，切换 VGGT 前执行一次无等待复核' "${LOG_ROOT}/gpu-all.log" vggt "${uuid}"
  verify_selected_gpu_for_vggt "${index}" "${uuid}" || return $?
  write_status running gpu-all 'VGGT 模型开始执行' "${LOG_ROOT}/gpu-all.log" vggt "${uuid}"
  run_logged "${LOG_ROOT}/vggt-full.log" env CUDA_VISIBLE_DEVICES="${index}" \
    "${VGGT_PY}" -u -m trust3d.geometry.run_vggt \
    --episodes data/episodes/spatial30/episodes_public.jsonl \
    --routes outputs/gate6/routes.jsonl \
    --checkpoint external/vggt/model.safetensors \
    --config configs/plan2_vggt.json --output "${OUTPUT}/vggt" \
    --source-checkpoints data/episodes/spatial30/checkpoints \
    --dataset-root data/episodes/spatial30 --vggt-root external/vggt \
    --cut3r-geometry outputs/gate7/cut3r_geometry --device cuda \
    --diagnostic-config "${CONFIG}" || return $?
  jq -e '.complete and .success_group_count==30 and .diagnostic_only' \
    "${OUTPUT}/vggt/manifest.json" >/dev/null || return 1
  mark_stage gpu "${OUTPUT}/vggt/manifest.json"
  printf '[%s] 双后端 GPU 前向完成。\n' "$(date -Is)" | tee -a "${JERRY}/logs/gate7-diagnosis/notifications.log"
}

checkpoint_hashes() {
  find "${OUTPUT}/cut3r/checkpoints" "${OUTPUT}/vggt/checkpoints" \
    -maxdepth 1 -type f -name '*.json' -print0 | sort -z | xargs -0 sha256sum
}

run_recovery() {
  if stage_done recovery; then
    printf '%s\n' 'checkpoint recovery 已通过 checkpoint 校验，跳过。'
    return 0
  fi
  host_preflight || return $?
  local before=${OUTPUT}/recovery_before.sha256 after=${OUTPUT}/recovery_after.sha256
  checkpoint_hashes > "${before}" || return $?
  write_status recovering recovery '在 CPU 模式校验全部 checkpoint 并确认不加载模型' "${LOG_ROOT}/recovery.log"
  run_logged "${LOG_ROOT}/recovery.log" env CUDA_VISIBLE_DEVICES='' \
    "${CUT3R_PY}" -u -m trust3d.geometry.run_cut3r \
    --episodes data/episodes/spatial30/episodes_public.jsonl \
    --checkpoint external/cut3r/src/cut3r_512_dpt_4_64.pth \
    --output "${OUTPUT}/cut3r" --source-checkpoints data/episodes/spatial30/checkpoints \
    --dataset-root data/episodes/spatial30 --cut3r-root external/cut3r \
    --device cpu --image-size 512 --center-crop-fraction 0.12 \
    --diagnostic-config "${CONFIG}" || return $?
  run_logged "${LOG_ROOT}/recovery.log" env CUDA_VISIBLE_DEVICES='' \
    "${VGGT_PY}" -u -m trust3d.geometry.run_vggt \
    --episodes data/episodes/spatial30/episodes_public.jsonl --routes outputs/gate6/routes.jsonl \
    --checkpoint external/vggt/model.safetensors --config configs/plan2_vggt.json \
    --output "${OUTPUT}/vggt" --source-checkpoints data/episodes/spatial30/checkpoints \
    --dataset-root data/episodes/spatial30 --vggt-root external/vggt \
    --cut3r-geometry outputs/gate7/cut3r_geometry --device cpu \
    --diagnostic-config "${CONFIG}" || return $?
  checkpoint_hashes > "${after}" || return $?
  cmp -s "${before}" "${after}" || return 1
  atomic_jq "${OUTPUT}/checkpoint_recovery.json" -n \
    --arg checked_at "$(date -Is)" --arg before_sha256 "$(sha256sum "${before}" | awk '{print $1}')" \
    --arg after_sha256 "$(sha256sum "${after}" | awk '{print $1}')" \
    '{schema_version:1,diagnostic_only:true,checkpoint_recovery_pass:true,all_hashes_match:true,checked_checkpoint_count:60,before_sha256:$before_sha256,after_sha256:$after_sha256,checked_at:$checked_at}'
  mark_stage recovery "${OUTPUT}/checkpoint_recovery.json"
}

run_attribute() {
  if stage_done attribute; then
    printf '%s\n' 'D5 attribute 已通过 checkpoint 校验，跳过。'
    return 0
  fi
  host_preflight || return $?
  write_status running attribute '执行配对 group bootstrap 与最终归因' "${LOG_ROOT}/attribute.log"
  run_logged "${LOG_ROOT}/attribute.log" "${VGGT_PY}" -u -m \
    trust3d.eval.diagnose_gate7_layers attribute --config "${CONFIG}" || return $?
  jq -e '.complete_failure_analysis and (.backends|length)==2' \
    "${OUTPUT}/final_diagnosis.json" >/dev/null || return 1
  mark_stage attribute "${OUTPUT}/final_diagnosis.json"
}

run_report() {
  if stage_done report; then
    printf '%s\n' '详细报告已通过 checkpoint 校验，跳过。'
    return 0
  fi
  host_preflight || return $?
  write_status running report '生成详细中文实验报告' "${LOG_ROOT}/report.log"
  run_logged "${LOG_ROOT}/report.log" "${VGGT_PY}" -u -m \
    trust3d.eval.diagnose_gate7_layers report --config "${CONFIG}" --report "${REPORT}" || return $?
  [[ -s ${REPORT} ]] || return 1
  mark_stage report "${REPORT}"
  write_status complete complete_failure_analysis '计划与详细报告全部完成' "${LOG_ROOT}/report.log"
  printf '[%s] 诊断计划执行结束，报告=%s\n' "$(date -Is)" "${REPORT}" | tee -a "${JERRY}/logs/gate7-diagnosis/notifications.log"
}

run_resume() {
  run_prepare || return $?
  run_lock || return $?
  run_unit || return $?
  run_offline || return $?
  run_gpu_all || return $?
  run_recovery || return $?
  run_attribute || return $?
  run_report || return $?
}

show_status() {
  if [[ -f ${STATUS} ]]; then
    jq . "${STATUS}"
  else
    printf '%s\n' '{"state":"not_started"}'
  fi
  printf '\n[阶段]\n'
  for path in "${STATE_ROOT}"/*.json; do
    [[ -e ${path} ]] || continue
    jq -r '[.stage,.status,.completed_at,.output] | @tsv' "${path}"
  done
  printf '\n[GPU]\n'
  gpu_sample
}

if [[ ${MODE} == status ]]; then
  show_status
  exit 0
fi

exec 9>"${LOCK}"
if ! flock -n 9; then
  printf '%s\n' '已有 Jerry Gate 7 diagnosis runner 持有锁，本次拒绝重复启动。'
  exit 75
fi

case "${MODE}" in
  prepare) run_prepare ;;
  lock) run_lock ;;
  unit) run_unit ;;
  offline) run_offline ;;
  gpu-all|watch) run_gpu_all ;;
  recovery) run_recovery ;;
  attribute) run_attribute ;;
  report) run_report ;;
  resume) run_resume ;;
  *) printf '未知 mode: %s\n' "${MODE}"; exit 2 ;;
esac
