# Trust3D

Trust3D 研究具身智能体应在何时信任持久化证据，以及何时应重新观察环境。项目按照 `Trust3D_服务器最小可执行实验方案.md` 中的验收门逐步实现；当前阶段未通过验收前，不进入下一阶段。

## Gate 0：模拟器冒烟测试

系统前置依赖：`xvfb-run`（Ubuntu 软件包 `xvfb` 和 `xauth`）。

创建隔离的模拟器环境：

```bash
conda env create -f environment/trust3d-sim.yml
```

运行单元测试和模拟器冒烟测试：

```bash
conda run -n trust3d-sim pytest -q
xvfb-run -a conda run -n trust3d-sim python -m trust3d.sim.controller \
  --smoke-test \
  --scene FloorPlan1 \
  --output outputs/gate0
```

冒烟测试会启动两个全新的模拟器进程；如果两次初始对象状态哈希不一致，测试即失败。生成的 RGB、深度图、分割图和完整 metadata 仅保存在本地，Git 只跟踪脱敏后的环境报告和验收报告。

## Gate 1：ALFRED 候选事件扫描

将固定版本的 ALFRED 仓库放在 `external/alfred`，然后在不启动 AI2-THOR 的情况下扫描轨迹 JSON：

```bash
python -m trust3d.data.scan_alfred \
  --alfred-json external/alfred/data/json_2.1.0 \
  --events OpenObject CloseObject \
  --output outputs/gate1/candidates.jsonl \
  --stats outputs/gate1/stats.json

pytest -q tests/test_scan_alfred.py
```

扫描器根据前序低层动作和第一次开关动作的前置状态推导动作前的开闭状态。场景级对象数量来自固定版本 ALFRED 的 `gen/layouts/*-openable.json`，可移动且可开闭的对象则根据轨迹中的 object poses 计数。如果两个来源都没有列出被操作实例，扫描器会根据 action ID 给出明确标注的数量下界，并由 Gate 2 使用模拟器 metadata 进一步核验。官方测试 split 仍纳入 manifest，但由于 ALFRED 有意隐藏其动作计划，扫描时会跳过这些轨迹。

## Gate 2：20 事件可复现重放

在 Xvfb 下生成包含 20 个事件的 pilot。任务按 source event、branch 和 replay round 保存 checkpoint；重复运行相同命令时会先验证已有产物，只恢复未完成单元：

```bash
xvfb-run -a python -u -m trust3d.data.build_branches \
  --candidates outputs/gate1/candidates.jsonl \
  --limit 20 \
  --branches fresh_stable risk_stable risk_stale \
  --seed 17 \
  --replay-runs 2 \
  --output data/episodes/pilot

python -m trust3d.data.validate_dataset \
  --public data/episodes/pilot/episodes_public.jsonl \
  --private data/episodes/pilot/oracle_private.jsonl \
  --replay-twice \
  --report outputs/gate2/validation.json
```

episode 数据、图像、private 真值和 checkpoint 均保存在本地 `data/` 目录中；Git 只跟踪完成泄漏检查后的聚合验收报告。

## Gate 3：100 事件 MVP 数据集

完整构建必须在 tmux 中通过可恢复脚本运行：

```bash
scripts/run_gate3_mvp.sh
```

脚本会在启动时记录服务器资源和完整命令，校验已有 checkpoint，并继续生成 100 个 source events、三种 branch、每个 branch 两个中性问题模板的 MVP 数据。构建完成后会自动运行 Gate 3 验收；日志和退出码分别保存在 `/224010104/Jerry/logs/gate3/mvp.log` 与 `/224010104/Jerry/logs/gate3/mvp.exit`。

## Gate 7：CUT3R RGB-only 几何

Gate 7 使用独立 Conda 环境和固定官方 checkpoint。所有命令必须在 tmux 中执行；GPU 不满足安全空闲阈值时，运行脚本只等待，不会抢占或终止其他任务：

```bash
scripts/bootstrap_gate7_cut3r.sh
scripts/download_gate7_cut3r_weights.sh
scripts/run_gate7_cut3r.sh pilot
scripts/run_gate7_cut3r.sh full
scripts/verify_gate7_cut3r.sh
```

adapter 对每个 group 分别运行稳定和变化后的五帧 RGB 序列，并原子保存包含输入 fingerprint 的 checkpoint。推理阶段不读取 depth、instance mask 或 private oracle 答案；private branch 只由隔离 evaluator 用于选择实际发生的观测。

30 组最终实验全部成功运行，但 Trust3D-CUT3R 准确率为 56.11%，相对 GT-3D 的 QA drop 为 43.89 个百分点，因此按预设标准只保留为失败分析。完整结论、几何一致性、成本和恢复审计见 `STATUS.md` 与 `outputs/gate7/validation.json`。
