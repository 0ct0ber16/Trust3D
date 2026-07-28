# Trust3D 实验状态

## Gate 0：模拟器冒烟测试

状态：通过

结论：进入 Gate 1。

环境审计：

- 主机：兼容 Ubuntu 22.04 的 x86_64 Linux
- 模拟器环境：`trust3d-sim`，Python 3.7.12
- 模拟器：`ai2thor==2.1.0`
- 显示环境：Xvfb 21.1.4
- 审计时工作区文件系统可用空间：639 TB
- GPU：Gate 0 不需要 GPU；未干预已有 GPU 任务

执行命令：

```bash
/224010104/miniconda3/envs/trust3d-sim/bin/python -m pytest -q

timeout 180s xvfb-run -a \
  /224010104/miniconda3/envs/trust3d-sim/bin/python -u \
  -m trust3d.sim.controller \
  --smoke-test \
  --scene FloorPlan1 \
  --output outputs/gate0
```

2026-07-28 验收结果：

- 单元测试：4 项通过
- 受测最终代码提交：`1d9f2220f4808998a20a15c794eb63eab6d7e843`
- AI2-THOR 构建 SHA256：`cc61de810ddb8d681e9f6a7e342dd2d2c8454db413dd681de6bc85d2b2f70459`
- RGB：300x300x3，取值范围 64-255，图像非全黑
- 深度图：300x300，90,000 个有限值，范围 671.15-1199.56 mm
- 实例分割：300x300x3，初始视角包含 3 种颜色
- Metadata：包含 Agent pose 和 75 个对象
- 合法动作：`RotateRight` 执行成功
- 确定性：两个独立命令共四次冷启动得到相同对象状态哈希
- 对象状态哈希：`57ad74a8e3b2ca5dfe909df906b5ca44fd3e373d4fb29e92cc85aa934979ef63`

Git 跟踪报告：

- `outputs/gate0/env.json`
- `outputs/gate0/report.json`

仅本地保存的产物：

- `outputs/gate0/rgb.png`
- `outputs/gate0/depth.npy`
- `outputs/gate0/instance_segmentation.png`
- `outputs/gate0/metadata.json`

问题与处理：

1. 默认 Conda channel 要求接受 Anaconda 使用条款。环境改用 `conda-forge` 和 `nodefaults`，未自动接受任何条款。
2. 未固定版本的 Flask/Werkzeug 2.2.x 会导致 AI2-THOR multipart 图像响应死锁。固定版本的 ALFRED 提交正式锁定 Flask 和 Werkzeug 2.0.3，问题随之解决。
3. 仅重置场景会使可移动对象以不同方式稳定下来。现在向 `InitialRandomSpawn` 传入 seed 17 和 `placeStationary=true`，重复运行的完整对象状态哈希保持稳定。
4. 无头 Unity 会报告音频设备和可选 `ScreenSelector.so` 插件缺失，但不影响 RGB、深度图、分割图、metadata 和动作执行。

## Gate 1：ALFRED JSON 候选事件扫描

状态：通过

结论：进入 Gate 2。

数据审计：

- ALFRED 提交：`f91f4c0c96c7a29f33d0557f86b0a21035379b3b`
- 数据集：ALFRED `data/json_2.1.0`
- Manifest 中的 JSON 文件：8,051 个，共 1,401,308,505 字节
- 包含动作计划的公开轨迹：7,080 条
- 不含动作计划的隐藏测试轨迹：971 条，其中 seen 483 条、unseen 488 条
- 数据集 manifest SHA256：`a2546fc364e3d88f418f0ead85ebe73c0f19a21b1fed9877bfe8ed18dac29874`

执行命令：

```bash
/224010104/miniconda3/envs/trust3d-sim/bin/python -m pytest -q

/224010104/miniconda3/envs/trust3d-sim/bin/python \
  -m trust3d.data.scan_alfred \
  --alfred-json external/alfred/data/json_2.1.0 \
  --events OpenObject CloseObject \
  --output outputs/gate1/candidates.jsonl \
  --stats outputs/gate1/stats.json
```

2026-07-28 验收结果：

- 单元测试：共 11 项通过，其中包含 7 项 Gate 1 扫描器测试
- 受测最终代码提交：`9c2a12f`
- 候选事件：14,306 个，其中 OpenObject 7,116 个、CloseObject 7,190 个
- 候选 split：train 13,205 个、valid_seen 516 个、valid_unseen 585 个
- 符合 MVP 动作与对象白名单：14,212 个
- 不同候选轨迹/场景：3,420 / 95
- 解析错误：7,080 条可处理轨迹中 0 条，错误率 0.00%
- 动作验证错误：0
- 可追溯性：所有候选记录都包含 source JSON、action index 和目标实例 ID
- 人工代码核验：10/10 条记录与原始动作和目标 ID 一致
- 核验覆盖：train 4 条、valid_seen 3 条、valid_unseen 3 条，同时覆盖 OpenObject 和 CloseObject
- 可复现性：两次完整扫描得到逐字节一致的输出
- 候选输出 SHA256：`1e9c4544cd30f273fce9acfc404618e8871c5b5a4ffef7a4c0a917991fab734a`
- 统计输出 SHA256：`7b79cd25bb9c53717c60927e1e936881ce0d9045ddb9fcd789b08333b9aaf12e`

Git 跟踪报告：

- `outputs/gate1/candidates.jsonl`
- `outputs/gate1/stats.json`

解释说明：

1. ALFRED 有意不向 tests_seen 和 tests_unseen 提供 `plan`。这 971 个文件仍包含在数据集 manifest 中，但记为无标注跳过项，而不是解析失败。
2. 可开闭实例数先使用 `gen/layouts/*-openable.json`，再根据轨迹 object poses 统计可移动且可开闭的对象。对于 250 个目标 ID 不在 layout 表中的 Cabinet/Drawer 候选，报告会明确标记其数量只是根据已观察 action ID 得到的下界。
3. `mvp_whitelist` 只表示动作和目标类型位于 Gate 1 白名单。可见性、精确同类对象数、状态变化和重放成功仍由 Gate 2 模拟器检查。
4. 初始开闭状态根据前序低层动作推导；如果对象之前从未出现，则根据第一次成功 OpenObject/CloseObject 动作的前置状态推导。

## Gate 2：20 事件可复现分支重放

状态：通过

结论：模拟器资源可用时进入 Gate 3。

选择与生成情况：

- Source events：20 个，来自 20 条轨迹和 14 个 FloorPlan
- Split：valid_unseen 10 个、valid_seen 5 个、train 5 个
- 动作：OpenObject 10 个、CloseObject 10 个
- 对象类型：Cabinet、Drawer、Fridge、Microwave、Safe 各 4 个
- 分支：Fresh-Stable、Risk-Stable、Risk-Stale 各 20 个
- 输出：60 个 public episodes、60 个 private oracle records、120 个独立 replay records
- Checkpoint：每个 source event 包含一个原子 context，以及每个分支各两份原子 branch-round 记录

执行命令：

```bash
/224010104/miniconda3/envs/trust3d-sim/bin/python -m pytest -q

/224010104/miniconda3/envs/trust3d-sim/bin/python \
  -m trust3d.data.build_branches \
  --candidates outputs/gate1/candidates.jsonl \
  --limit 20 \
  --branches fresh_stable risk_stable risk_stale \
  --seed 17 \
  --replay-runs 2 \
  --output data/episodes/pilot

/224010104/miniconda3/envs/trust3d-sim/bin/python \
  -m trust3d.data.validate_dataset \
  --public data/episodes/pilot/episodes_public.jsonl \
  --private data/episodes/pilot/oracle_private.jsonl \
  --replay-twice \
  --report outputs/gate2/validation.json
```

2026-07-28 验收结果：

- 单元测试：20 项通过
- 完整 source events：20/20；最低要求为 19/20
- 重放状态哈希一致性：60/60 episodes，即 100%；最低要求为 95%
- Query pose 一致性：每个 group 的三个分支完全一致
- 答案关系：所有 Fresh/Risk-Stable 答案保持不变，所有 Risk-Stale 答案均发生变化
- 符号答案与模拟器 metadata 一致：60/60
- 存在可达验证视角：60/60，即 100%；最低要求为 90%
- Query 时目标隐藏：60/60
- Public/private 泄漏：0；public 和 private episode ID 一一对应
- Manifest 产物哈希：全部匹配
- 验证结果：`gate2_pass=true`

可复现性与安全检查：

1. 最初有两个 context 失败，原因是 Agent 手持对象时 AI2-THOR 拒绝执行 `TeleportFull`。Query teleport 现已使用 `forceAction=True`；恢复运行只重建两个失败单元，其余已验证 checkpoint 全部保留。
2. 一组 Risk-Stable/Risk-Stale query frame 的原始哈希不同。逐像素审计发现 90,000 个像素中只有 6 个发生变化，占 0.0067%，且每个像素只变化一个强度级；目标保持隐藏，所有 public metadata 完全一致。该差异属于量化级渲染抖动，不构成可观察的干预线索。
3. 仅使用 checkpoint 的完整恢复未启动 Unity 或 Xvfb。恢复前后对本地全部 247 个 pilot 文件进行 SHA256 比较，结果逐字节一致。
4. 最终审计时已有 VLLM 任务占用两张 GPU。未停止或修改任何 GPU 进程，仅使用 CPU 完成 checkpoint 验证。

Git 跟踪报告：

- `outputs/gate2/validation.json`：`659a39e13dac4a477b2f55bd72a6be7bfe138786c4985ba82efb2cccf5fe0c83`

仅本地保存的产物与日志：

- 数据集和原子 checkpoint：`data/episodes/pilot/`
- 恢复哈希审计：`/224010104/Jerry/logs/gate2/checkpoint-resume.log`
- 最终测试：`/224010104/Jerry/logs/gate2/tests-final.log`
- 最终验收：`/224010104/Jerry/logs/gate2/validation-final.log`
