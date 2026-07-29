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

## Gate 3：100 个候选事件的 TrustEQA-Dynamic-MVP

状态：通过

结论：进入 Gate 4 离线二路路由验证。

生成与筛选情况：

- 初始 source events：100 个，全部完成 context 和分支 checkpoint
- 数据质量排除：2 个 source events；原始 checkpoint 保留且未删除
- 最终有效 source events：98 个
- 分支：Fresh-Stable、Risk-Stable、Risk-Stale 各 196 个 episode
- 输出：588 个 public episodes、588 个 private oracle records、1176 个独立 replay records
- 问题模板：每个分支 2 个中性模板
- 模态：历史、query 和验证观测均包含 RGB、depth、instance segmentation

执行命令：

```bash
scripts/run_gate3_mvp.sh

scripts/verify_gate3_mvp.sh
```

2026-07-28 验收结果：

- 单元测试：28 项通过
- 有效 source events：98/100；最低要求为 90
- 重放成功率：98%；98 个有效 group 均包含完整三分支和两个问题模板
- 重放状态哈希一致性：588/588 episodes，即 100%
- 分支比例：Fresh-Stable : Risk-Stable : Risk-Stale = 1:1:1
- 答案平衡：open 294 个、closed 294 个，各占 50%
- Query 时目标隐藏：588/588
- 可达验证视角：588/588；最短验证成本范围 2-42，均值 19.24
- Public/private 泄漏：0；episode ID 一一对应
- Public 路由成本：全部存在，并在同一 group 的所有分支间保持一致
- RGB、depth、instance 和验证观测产物：全部存在且 manifest SHA256 匹配
- 最终验证结果：`gate3_pass=true`

视觉泄漏审计：

1. 初次审计发现 2/100 个 group 的风险分支 query 观测存在真实差异，而非普通量化抖动。最大 RGB 变化比例为 0.0104%，但最大强度差为 94，同时 depth 最大差为 20.01，instance mask 也有像素变化。
2. 这两个 group 已通过 `configs/gate3_exclusions.json` 明确记录并从最终聚合数据中排除；未删除其原始 checkpoint，也未用静默丢弃掩盖问题。
3. 最终 98 个 group 的 196 对 Risk-Stable/Risk-Stale query 观测逐像素一致：RGB、depth 和 instance 的最大差异均为 0。
4. 所有公开风险对的其余字段保持一致，隐藏干预、当前答案、陈旧标签和分支名称均不进入 public 文件。

断点恢复与服务器安全检查：

1. Gate 3 使用用户态 Xvfb 和 CPU llvmpipe，线程上限为 16；未使用、停止或修改两张 GPU 上已有的 VLLM 任务。
2. 最终恢复测试直接读取 checkpoint 聚合，不启动 Xvfb 或 Unity。恢复前后比较本地 2906 个文件，SHA256 全部一致。
3. 恢复测试前后新增模拟器进程数为 0，证明服务器重启后可从原子 checkpoint 恢复而不重复运行已完成单元。
4. 原始长任务日志、失败原因、状态审计和恢复哈希均保存在 `/224010104/Jerry/logs/gate3/`。

Git 跟踪报告：

- `outputs/gate3/validation.json`
- `outputs/gate3/risk_frame_audit.json`
- `outputs/gate3/checkpoint_recovery.json`

仅本地保存的产物与日志：

- 数据集、模态缓存和原子 checkpoint：`data/episodes/mvp/`
- 生成日志：`/224010104/Jerry/logs/gate3/mvp.log`
- 正式验收：`/224010104/Jerry/logs/gate3/verify.log`
- 全量恢复哈希：`/224010104/Jerry/logs/gate3/recovery-before.sha256` 和 `/224010104/Jerry/logs/gate3/recovery-after.sha256`

## Gate 4：离线二路证据路由

状态：通过

结论：核心的“准确率与重新观察成本折中”假设成立，可以进入 Gate 5 在线执行；但当前结果不能证明复杂控制器优于简单 TTL/事实时效规则。

实验设置：

- 输入：Gate 3 的 588 个 public episodes；路由阶段无法读取 private oracle
- 方法：Always-Trust、Always-Reobserve、Global-TTL、Fact-Freshness、5 个预先固定成本权重的 Trust3D-Controller
- 诊断上界：Clairvoyant-Oracle，仅在隔离评测阶段读取隐藏真值
- 主策略：`trust3d_lambda_0.01`
- 统计：按 98 个 `group_id` 配对 bootstrap 10,000 次，seed 为 20260728
- 配置 `configs/mvp.yaml` 已在运行真实 Gate 4 前随 Gate 3 阶段提交固定并推送

执行命令：

```bash
scripts/run_gate4_offline.sh
```

2026-07-28 核心结果：

| 方法 | 准确率 | Stale-memory error | 不必要重观察率 | 重观察比例 | 平均验证成本 |
|---|---:|---:|---:|---:|---:|
| Always-Trust | 66.67% | 100.00% | 0.00% | 0.00% | 0.00 |
| Always-Reobserve | 100.00% | 0.00% | 100.00% | 100.00% | 19.24 |
| Global-TTL | 100.00% | 0.00% | 50.00% | 66.67% | 12.84 |
| Fact-Freshness | 100.00% | 0.00% | 50.00% | 66.67% | 12.84 |
| Trust3D 主策略 | 100.00% | 0.00% | 50.00% | 66.67% | 12.84 |
| Clairvoyant-Oracle | 100.00% | 0.00% | 0.00% | 33.33% | 6.43 |

相对于两个极端基线：

- 相对 Always-Trust，主策略 stale-memory error 下降 100 个百分点；95% group-bootstrap CI 为 `[100, 100]` 个百分点
- 相对 Always-Reobserve，主策略准确率差为 0 个百分点；95% CI 为 `[0, 0]`
- 相对 Always-Reobserve，新观察比例下降 33.33%；95% CI 为 `[33.33, 33.33]` 个百分点
- 5 个预先指定成本权重 `0.001/0.005/0.01/0.02/0.05` 的 expected utility 均高于两个极端策略中的较优者，95% CI 下界均大于 0
- 成本敏感 Pareto 路径有效：`lambda=0.02` 时准确率 90.48%、重观察 47.62%；`lambda=0.05` 时准确率 72.79%、重观察 12.24%
- Gate 4 所有验收项通过：`gate4_pass=true`

结论边界：

1. 当前 public cue 只区分 Fresh 与 Risk，Risk-Stable/Risk-Stale 对 Agent 完全不可区分。主策略因此在 `lambda=0.01` 下选择“Fresh 信任、Risk 全部重观察”。
2. Global-TTL、Fact-Freshness 和 Trust3D 主策略在当前数据上得到完全相同的路由，Gate 4 只支持相对于两个极端策略的核心折中，不支持“复杂成本控制器已经优于简单时效规则”的更强结论。
3. 成本权重增大时 Trust3D 会放弃高成本重观察并形成有效 Pareto 曲线，说明成本项确实参与决策，但进一步区分简单规则需要 Gate 5 在线证据或后续更多风险条件。
4. 路由文件只包含公开字段；答案、分支、陈旧标签和最短真实成本仅由隔离的 `evaluate_routes` 阶段读取。相关泄漏测试已通过。

Git 跟踪报告：

- `outputs/gate4/metrics.json`
- `outputs/gate4/plots/accuracy_cost_pareto.png`
- `outputs/gate4/plots/risk_tradeoff.png`
- `outputs/gate4/plots/manifest.json`

仅本地保存的产物与日志：

- 5292 条路由：`outputs/gate4/routes.jsonl`
- 5880 条隔离评测记录：`outputs/gate4/predictions.jsonl`
- 完整运行日志：`/224010104/Jerry/logs/gate4/offline.log`
- 结果审计：`/224010104/Jerry/logs/gate4/final-summary.log`

## Gate 5：在线二路 Agent

状态：通过

结论：在线执行与 Gate 4 离线模拟一致，可以进入 Gate 6 空间记忆实验。

实验设置：

- 输入：Gate 3 的 98 个有效 group、294 个真实环境单元和 588 个公开问题
- Agent：仅接收 public episode 和固定配置，不接收 private oracle
- 路由：Gate 4 主策略 `trust3d_lambda_0.01`
- 重观察：Fresh-Stable 信任历史；Risk-Stable 和 Risk-Stale 执行真实模拟器重观察
- Checkpoint：每个 `candidate_id/branch` 独立原子写入，恢复时同时校验配置、源 checkpoint 和产物哈希
- 显示：用户态 Xvfb 与 CPU llvmpipe；未使用 GPU

执行命令：

```bash
scripts/run_gate5_online.sh
```

2026-07-28 至 2026-07-29 验收结果：

- 完成环境单元：294/294，生成 588 条 trace，覆盖 98 个 group
- 在线答案准确率：100%；与 Gate 4 离线答案差异率为 0%
- 路由一致性：588/588 与离线主策略一致
- 动作尝试：6414 次，失败 0 次，动作失败率 0%
- 重新观察：392 次；相对 Always-Reobserve 减少 33.33%
- 规划移动成本：7548；所有 trace 均与 Gate 3 已验证最短成本一致
- 陈旧事实：196/196 条冲突 trace 均使旧事实失效，stale-memory error 为 0%
- 证据：所有答案均指向存在的历史或新观察文件
- 泄漏：trace 中 private 字段为 0，public/private ID 一一对应
- 最终验证结果：`gate5_pass=true`

在线执行边界与故障处理：

1. 旧版 AI2-THOR 在 `FloorPlan20` 的 300x300 软件渲染中出现长时间 RPC 无返回。逐阶段日志将瓶颈定位到重复渲染，而非
   checkpoint、路由或答案逻辑；受控中断只停止本工作区进程，已完成 checkpoint 全部保留。
2. 后段运行先以 `ChangeResolution` 将证据渲染降为 150x150，因此 Gate 5 新观察包含 300x300 和 150x150 两种分辨率；
   模拟器状态、对象 ID、位姿和真值不受分辨率影响。
3. 44 个后段重观察单元使用 `verified_endpoint`：在线执行一次真实 `TeleportFull` 到 Gate 3 已验证的可达终点，并重新
   读取目标和缓存证据；`movement_steps` 仍记录 Gate 3 最短路径成本，`movement_action_count` 如实记录为 1。其余单元执行
   完整的逐网格最短路径。该边界意味着 Gate 5 验证的是在线路由、终点观察和规划成本，不应把 44 个单元表述为逐动作导航
   执行。
4. 本地保留 4 条修复前失败记录用于审计；对应单元均已在修复后生成有效成功 checkpoint，不计入最终失败率。

断点恢复与服务器安全检查：

1. 最终纯 checkpoint 恢复得到 294/294 单元和 588 条 trace，未启动 Unity 或 Xvfb。
2. 恢复前后 trace SHA256 均为 `cdae41959d0628a53051ca58d415d6c2da9b2c33ffc054821afc78ab3143f3a5`。
3. 最终恢复与验收退出码均为 0；完整过程、受控中断原因和进度保存在 `/224010104/Jerry/logs/gate5/`。
4. 全量运行期间未停止、修改或占用其他用户的进程和文件。

Git 跟踪报告：

- `outputs/gate5/traces.jsonl`
- `outputs/gate5/validation.json`

仅本地保存的产物与日志：

- 在线观察、原子 checkpoint 和失败审计：`data/episodes/online/`
- 完整运行日志：`/224010104/Jerry/logs/gate5/online.log`
- 进度日志：`/224010104/Jerry/logs/gate5/progress.log`
- Watchdog 审计：`/224010104/Jerry/logs/gate5/watchdog.log`
- 纯恢复验证：`/224010104/Jerry/logs/gate5/resume-verification.log`

## Gate 6：第一视角空间记忆与 freshness 路由

状态：通过

结论：GT-3D 与确定性 RGB-D 对象几何均证明了第一视角空间记忆和 freshness 路由的有效性，可以进入 Gate 7 的 CUT3R 小规模接入；当前结果不代表已经超过训练后的 RGB 检索模型。

实验设置：

- 输入：30 个锁定的 ALFRED source events，每个 event 包含 Fresh-Stable、Risk-Stable、Risk-Stale 三个分支和四类空间问题
- 输出：30 个 group、360 个 public episodes 和 360 个隔离的 private oracle records
- 问题：左右、前后、二者谁更近、目标是否更近
- 几何：模拟器 GT 对象位置，以及从真实可见历史观测的 depth、instance mask 和相机位姿确定性反投影得到的 RGB-D 对象位置
- 位置干预：旧版 AI2-THOR 的 `TeleportObject`，使用顶层 `x/y/z` 参数交换两个同类型对象
- Checkpoint：每个 source event 独立原子写入，版本为 `spatial_build_version=6`
- 显示：工作区内 Xvfb 与 CPU llvmpipe，线程上限为 16；不使用 GPU

执行命令：

```bash
scripts/run_gate6_spatial.sh

scripts/verify_gate6_spatial.sh
```

2026-07-29 核心结果：

| 方法 | 准确率 | Stale 子集准确率 | 新观察数 | 移动成本 |
|---|---:|---:|---:|---:|
| Persistent 3D（GT） | 75.00% | 25.00% | 0 | 0 |
| Persistent 3D（RGB-D） | 75.00% | 25.00% | 0 | 0 |
| Always-Reobserve（GT/RGB-D） | 100.00% | 100.00% | 540 | 3724 |
| Trust3D（GT） | 100.00% | 100.00% | 360 | 2422 |
| Trust3D（RGB-D） | 100.00% | 100.00% | 360 | 2422 |

- 相对 Always-Reobserve，Trust3D 减少 33.33% 新观察，并将移动成本从 3724 降至 2422
- 相对无 freshness 的 Persistent 3D，Trust3D 将 stale 子集准确率从 25% 提升至 100%
- 第一视角确定性工具与模拟器真值的答案误差率为 0%，低于 2% 门槛
- 所有 6 项预设验收条件通过，最终结果为 `gate6_pass=true`
- 路由输出没有 private 字段泄漏，`route_private_leak_count=0`

数据与视觉泄漏审计：

1. Risk-Stable 与 Risk-Stale 的公开 query RGB 使用同一个稳定分支文件，30/30 checkpoint 均逐路径一致；实际 stale query 只保存在私有审计观测中。
2. 30 个最终 checkpoint 的实际 stale query 最大归一化 RGB 平均差异为 `0.0007164`，低于构造器的 1% 拒绝阈值；目标和参照物在 query 时均不可见。
3. 构造器会逐一尝试同类型对象对，30 个成功 checkpoint 共保留 30 次不可达或不合格物体对拒绝记录，不会因第一对失败而丢弃整个 source event。
4. Public/private episode ID 一一对应，公开路由阶段不读取当前对象位置、隐藏分支或 oracle 答案。

实现边界与失败处理：

1. 固定的 2019 AI2-THOR 在第二次调用 `SetObjectPoses` 时会报告成功但不移动对象；最小 API 探针确认顶层 `x/y/z` 的 `TeleportObject` 双向位置误差均为 0，因此正式构造使用后者。
2. 原始 ALFRED 历史帧通常不能同时看见两个合格物体。Gate 6 改为在同一恢复场景中分别寻找真实可达位姿并采集两个对象的 RGB-D 历史证据，不伪造同帧可见性。
3. 开发和早期 schema 尝试留下 28 条失败记录：11 条旧对象 ID 不再存在、10 条目标不可达、2 条无合格同类物体对、4 条 RGB 泄漏和 1 条 instance 泄漏。记录全部保留供审计；成功单元在版本 6 修复后重新验证。
4. `Current-RGB` 与 `All-history RGB` 当前只返回 `unknown`，因此其 0% 准确率是诊断占位结果。Gate 6 证明的是确定性 GT/RGB-D 几何和 freshness 路由有效，不能据此声称优于训练后的 RGB 检索或视觉模型。

断点恢复与服务器安全检查：

1. 纯 checkpoint 恢复得到 30/30 group，`new_contexts_this_run=0` 且 `simulator_started=false`。
2. 恢复期间每 0.1 秒监测 Unity/Xvfb，新增模拟器进程数为 0；没有停止、修改或占用其他用户的进程。
3. 恢复前后 public、private、锁定 selection 和 validation 四个关键文件 SHA256 全部一致，`checkpoint_recovery_pass=true`。
4. 最终测试为 44 项全部通过，数据和输出目录没有残留 `.tmp` 文件。

Git 跟踪报告：

- `outputs/gate6/api_probe.json`
- `outputs/gate6/validation.json`
- `outputs/gate6/checkpoint_recovery.json`

仅本地保存的产物与日志：

- RGB-D 数据、私有 oracle、模态缓存、失败审计和原子 checkpoint：`data/episodes/spatial30/`
- 完整生成与恢复日志：`/224010104/Jerry/logs/gate6/spatial.log`
- 最终恢复审计：`/224010104/Jerry/logs/gate6/verify.log`
- 本轮过程总记录：`/224010104/Jerry/logs/gate6/continue-audit.log`
