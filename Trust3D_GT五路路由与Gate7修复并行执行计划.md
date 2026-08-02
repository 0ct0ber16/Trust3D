# Trust3D：GT 五路路由与 Gate 7 修复并行执行计划

> 版本：B/C 线保持 `parallel-v2`；A 线修订为 `gt-five-route-v3`
>
> 制定日期：2026-07-31
>
> 工作区：`/224010104/Jerry/trust3d`
>
> 基线提交：`117393a8b4358f736d7947850cecb459c0a3f605`
>
> A 线 v3 修订起点：`e66b229`（保留 v2 功效不足报告）
>
> 文档性质：可直接据此实现 runner、执行实验和生成报告的冻结协议

> v3 修订：`parallel-v2` A 线已因最坏情形功效假设备案为 `inconclusive_underpowered`。本修订不覆盖该结果，不降低任何验收门槛；它将 A 线主张拆成可独立证明的路由契约、风险、配对非劣和成本优势，并使用未评测的 sealed final 加全新 replication set 做一次性确认。B/C 线协议和既有 Gate 结果不变。

## 1. 一句话决策

两条路线可以并行，但必须先冻结共同的证据接口、数据边界和评测协议：

- **A 线：GT 五路路由**验证“证据可靠时，控制器能否正确选择低成本证据路线”；
- **B 线：Gate 7 修复**验证“RGB-only 输入能否产生足够可靠的对象、相机和三维证据”；
- **C 线：联合 Gate**只在 A、B 分别通过后执行，验证感知误差进入五路控制器后是否仍保留 accuracy-cost 优势。

开发与运行可以并行，最终科学结论不能跳过 C 线直接相加。

## 2. 当前证据与结论边界

### 2.1 已经成立的结论

1. Gate 0--6 已证明：在 simulator GT pose/depth、确定性答案器和 oracle 最短可见路径条件下，时效感知的 `TRUST_MEMORY/REOBSERVE` 二路决策可以减少 stale-memory error 和不必要重观察。
2. 在线 AI2-THOR 重观察已经证明该收益不是纯离线 evaluator 现象。
3. 原 Gate 7 CUT3R、方案 2 VGGT 以及后续失败归因均已执行完成，原始结果和门槛保持不变。
4. Gate 7 的失败不能只归因于 backbone：当前证据包括已证明的任务头语义错误、明显的 pose/全局对齐误差、较弱的 grounding 贡献，以及在 29-group complete-case 分析中占主导的 backbone geometry gap。

### 2.2 尚未成立的结论

1. 二路路由成功不等于完整五路控制器已经成功。
2. GT 五路成功只能作为可靠证据条件下的控制器上界，不能作为 RGB-only 主结果。
3. Gate 7 修复成功不等于五路路由会自动成功；感知置信度和路由阈值之间仍有耦合。
4. 当前 30-group 已被用于多次诊断，后续可以继续作为不可覆盖的回归集，但不能再作为调参后“新主结果”的唯一测试集。

### 2.3 A 线 v2 已获得的新证据

1. v2 pilot 的 20 个 group 五路完全平衡，公开输入选择与 private oracle route 为 `20/20` 一致。
2. `trust3d_five_route` 正确 `16/20`，`always_reobserve` 正确 `4/20`；配对差为 12 个 `+1`、8 个 `0`、0 个 `-1`，平均差 `+0.60`。
3. v2 功效审计以“真实配对差等于 0、仅靠 0.02 非劣界区分”为最坏情形备择，得到 `N=4958`。该计算可作为零优势情形的保守敏感性分析，但不适合作为已经观察到强正向配对差后验证五路机制可行性的唯一硬停止条件。
4. v2 的 60-group final 已生成并有 SHA256，但没有 `router_lock.json`、`metrics.json` 或 offline evaluator 结果。它仍可作为 v3 的只读 sealed confirmatory set；必须先完成未揭示审计，且 v3 规则冻结后只能评价一次。

## 3. 要验证的三个独立主张

| ID | 主张 | 对应实验 | 允许输入 | 通过后能声称什么 |
|---|---|---|---|---|
| C-A | 可靠证据下五路控制器有 accuracy-cost 优势 | A 线 | GT/oracle evidence provider；router 仅见公开字段 | 路由与记忆 idea 的可行性和上界 |
| C-B | RGB-only 感知可替代 GT 证据接口 | B 线 | RGB、公开问题、冻结模型；GT 仅作离线评价 | 视觉几何接口达到现实设置或主结果门槛 |
| C-C | RGB 误差进入路由后系统仍有效 | C 线 | B 线冻结输出与 A 线冻结 router | 完整五路 Trust3D 的端到端结论 |

三项必须分开报告。任何一项失败都不得用另两项的结果掩盖。

### 3.1 主估计量与次估计量

| 实验 | 主估计量 | 次估计量 | 不允许替代主估计量的指标 |
|---|---|---|---|
| A 线 v3 | 公开证据到五路动作的独立契约正确性，以及在风险约束下相对 `always_reobserve` 的配对准确率非劣和成本下降 | route regret、route macro recall、stale error、coverage、在线执行一致性 | 只报 route 分类准确率，或只报 answer accuracy |
| B 线 | sealed holdout 上相对 GT/RGB-D 的 QA drop | grounding、pose、geometry 分层误差与 calibration | legacy30 上调参后的最好结果 |
| C 线 | RGB 五路相对冻结 A 上界和 `always_reobserve` 的准确率、风险与成本差 | provider failure 后的 route shift 和 regret | A/B 两条线结果的简单相加 |

所有验收先看预注册主估计量。route 分类、oracle 消融和单类别提升只能解释原因，不能替代端到端 accuracy-cost-risk 结果。

## 4. 不可覆盖基线

以下文件和目录全部只读使用，不得由本计划覆盖、移动或重新解释：

```text
Trust3D_服务器最小可执行实验方案.md
Trust3D_服务器最小可执行实验方案2.md
Trust3D_最小可执行实验详细报告.md
Trust3D_服务器最小可执行实验方案2详细报告.md
Trust3D_Gate7_CUT3R_VGGT失败原因诊断报告.md
outputs/gate0/
outputs/gate2/
outputs/gate3/
outputs/gate4/
outputs/gate5/
outputs/gate6/
outputs/gate7/
outputs/plan2/
outputs/gate7_diagnosis/
Trust3D_GT五路路由实验报告.md
outputs/parallel_v2/gt_five_route/
data/episodes/parallel_v2/gt5/
```

其中 `parallel_v2/gt5/final_*` 只允许由 v3 sealing 工具读取字节、权限和哈希；在 `evaluation.lock` 获取前不得解析 private 内容。v3 产物必须写入 `data/episodes/parallel_v3/`、`outputs/parallel_v3/`、`checkpoints/parallel_v3/` 和 `logs/parallel_v3/`，不得覆盖 v2 同名文件。

执行前将这些路径的 SHA256 manifest 写入：

```text
outputs/parallel_v2/protocol/baseline_manifest.sha256
```

每次 `resume` 和最终报告前都重新校验。任一哈希变化立即停止，不允许自动修复或覆盖旧产物。

## 5. 总体依赖图

```text
                         P0 共同协议冻结
                    /                           \
       A0-A7：GT 五路路由 v3              B0-B6：Gate 7 修复
       CPU / AI2-THOR                     GPU / RGB-only
                    \                           /
                      C0 协议兼容与置信度冻结
                                  |
                      C1-C3：RGB 五路联合 Gate
                                  |
                         三份分报告 + 总报告
```

允许的并行关系：

- P0 通过后，A0--A7 与 B0--B6 可以同时推进，但两线 simulator/private evaluator 阶段必须使用共享锁；
- A 线不得等待或读取 B 线实验答案来改变 GT router；
- B 线不得读取 A 线 private oracle route 来调视觉适配器；
- C 线必须等待 A7 和 B6 都产生不可变完成标记。

## 6. P0：共同协议冻结

### 6.1 五路动作的唯一语义

| Route | 允许证据 | 触发条件 | 主要成本 | 禁止行为 |
|---|---|---|---|---|
| `USE_CURRENT_VIEW` | 当前时刻可见且达到像素阈值的传感器证据 | 当前视图足以满足 required facts 和误差约束 | 当前感知/VLM 成本 | 不得读取不可见对象 GT 状态 |
| `RETRIEVE_HISTORY` | 历史事实表中的属性或非几何证据 | 事实仍新鲜且问题不需要重新计算三维关系 | 检索成本 | 不得把 stale 事实伪装成当前观测 |
| `QUERY_3D_MEMORY` | 带时间、来源和坐标系的历史三维证据 | 需要空间计算且相关几何仍有效 | 几何查询成本 | 不得使用未观测生成内容作为事实 |
| `REOBSERVE` | 新传感器观测和随后更新的事实 | 旧证据风险超阈值且重观察预期损失更低 | 移动、观察和感知成本 | 不得使用 private 最短答案路径以外的信息 |
| `ABSTAIN` | 无答案证据 | 所有可用路线都无法满足最大错误风险，或合法重观察不可达 | abstain penalty | 不得用普遍拒答获得虚高准确率 |

旧二路动作的映射固定为：

```text
TRUST_MEMORY -> RETRIEVE_HISTORY 或 QUERY_3D_MEMORY
REOBSERVE    -> REOBSERVE
```

`USE_CURRENT_VIEW` 与 `ABSTAIN` 是新增动作，不能通过重命名旧记录伪造五路覆盖。

### 6.2 共同 EvidencePacket

A、B、C 三条线必须读写同一 schema。建议新增：

```text
trust3d/agents/evidence.py
configs/schemas/evidence_packet_v1.json
```

最小字段固定为：

```json
{
  "schema_version": 1,
  "episode_id": "...",
  "query_id": "...",
  "object_id": "...",
  "predicate": "left_right|front_behind|distance|attribute",
  "value": null,
  "source": "current_view|history|gt_3d|rgb_3d|reobserve",
  "observed_at": 0,
  "valid_until": 0,
  "reference_frame": "world|current_egocentric",
  "pose_convention": "camera_to_world",
  "confidence": 0.0,
  "is_observed": true,
  "provenance": [],
  "cost": {
    "move_steps": 0,
    "new_observations": 0,
    "vlm_calls": 0,
    "geometry_calls": 0,
    "wall_seconds": 0.0
  }
}
```

规则：

1. `confidence` 必须来自预注册规则，不允许从 test answer 反推。
2. GT provider 与 RGB provider 只能在 packet 生成端不同；router 和 evaluator 不得按 provider 写两套逻辑。
3. `provenance` 记录输入帧、checkpoint、适配器版本和哈希，不记录 private answer。
4. `value=null` 是合法失败输出；不得用默认方向或零点补齐。
5. packet 写入临时文件后原子重命名。

### 6.3 Public/private 边界

Router 可见：

```text
question program
required_facts
current visibility/public detections
历史 packet 的 source/timestamp/confidence/validity
intervention_window 等公开风险线索
公开的候选路线成本估计
```

Router 永远不可见：

```text
changed/unchanged branch 真值
当前对象 GT 状态
private answer
oracle_best_route
GT mask、GT bbox、GT object center
用于评价的 GT camera/point alignment
```

GT mask、GT bbox、GT pose alignment 只允许出现在 `diagnostic_only=true` 的 B 线 oracle 文件中，且文件名、schema 和输出目录必须与主结果分离。

### 6.4 五路可识别性契约

五路实验必须先证明“公开输入足以区分应采取的证据路线”，否则 route accuracy 没有意义。每个 final group 同时通过两项审计：

1. **信息可识别**：不存在 public 输入逐字段完全相同、private `oracle_best_route` 却不同的 sibling；若存在，标记 `publicly_ambiguous=true`，只能进入 utility 统计，不能进入 route-classification 统计。
2. **优化可识别**：满足风险约束的最优路线唯一，且与第二优路线的预期 loss 间隔不小于 `minimum_route_loss_margin`；近似平局进入 `near_tie=true` 分层，不计入 per-route recall 门槛。

private oracle 必须保存五条路线的完整反事实表，而不只保存最佳标签：

```json
{
  "route_losses": {
    "USE_CURRENT_VIEW": null,
    "RETRIEVE_HISTORY": 0.0,
    "QUERY_3D_MEMORY": 0.0,
    "REOBSERVE": 0.0,
    "ABSTAIN": 0.0
  },
  "oracle_best_route": "REOBSERVE",
  "second_best_route": "ABSTAIN",
  "route_loss_margin": 0.0,
  "publicly_ambiguous": false,
  "near_tie": false
}
```

主结论评价实际 answer/cost/risk，不把 route oracle 当训练标签。可识别性只用于确认五个动作确实各有独立实验支持。

### 6.5 共同成本与风险

继续使用：

\[
C(r)=\lambda_m N_{move}+\lambda_o N_{new\_obs}+\lambda_v N_{VLM}+\lambda_g N_{geometry}+\lambda_t T.
\]

共同风险约束：

\[
P(error\mid public\ evidence,r)\leq\epsilon.
\]

默认冻结值写入 `configs/parallel_v2_protocol.json`：

```json
{
  "schema_version": 1,
  "protocol_revision": "parallel-v2",
  "max_error_probability": 0.05,
  "minimum_coverage": 0.75,
  "accuracy_noninferiority_margin_pp": 2.0,
  "minimum_cost_reduction": 0.20,
  "minimum_route_loss_margin": 0.01,
  "bootstrap_groups": 10000,
  "seed": 20260731,
  "route_tie_break": [
    "USE_CURRENT_VIEW",
    "RETRIEVE_HISTORY",
    "QUERY_3D_MEMORY",
    "REOBSERVE",
    "ABSTAIN"
  ]
}
```

风险阈值、成本权重、非劣界和 `minimum_route_loss_margin` 必须在任何 pilot 结果揭示前冻结。只有实现相关的 confidence/calibration 参数允许在 pilot 后、final manifest 生成前形成新的 protocol revision；旧 revision 不得覆盖。任何参数都不得根据 final 的 route 分布或答案回调。

`minimum_route_loss_margin` 使用与 route loss 相同的归一化单位。

### 6.6 A 线 v3 统计修订

旧 `configs/parallel_v2_protocol.json` 保持只读。A 线新增 `configs/gt_five_route_v3_protocol.json`，继续固定 `epsilon=0.05`、coverage `>=0.75`、accuracy 非劣界 `-0.02`、成本下降 `>=0.20` 和相同五路成本权重；只修订估计量、样本组织和有限样本判定方法：

```json
{
  "protocol_revision": "gt-five-route-v3",
  "development_groups": 20,
  "sealed_confirmatory_groups": 60,
  "fresh_replication_groups": 40,
  "confirmatory_groups": 100,
  "groups_per_route": 20,
  "accuracy_interval": "paired-tango-score-one-sided-95",
  "error_interval": "clopper-pearson-one-sided-95",
  "cost_interval": "scene-cluster-bootstrap-one-sided-95",
  "bootstrap_resamples": 100000,
  "seed": 20260731
}
```

v3 不再用 `((z_alpha+z_power)*pilot_std/0.02)^2` 作为硬停止条件。该式检验的是“真实差值恰好为 0”这一最坏情形，而 v2 development pilot 已观察到 `+0.60` 且无负向 discordant pair。v3 在打开 confirmatory private GT 前冻结：

1. 配对准确率差使用 matched-pair Tango score 单侧 95% 下界，硬门槛仍为 `-0.02`；
2. selective error、unsafe answer 和 false abstain 使用 Clopper--Pearson 单侧 95% 上界，硬门槛仍为 `0.05`；
3. 成本下降使用按 scene 聚类的 100,000 次固定 seed bootstrap，要求点估计 `>=0.20` 且单侧 95% 下界 `>0`；
4. 功效只作为设计敏感性分析，必须枚举保守 discordance 网格并写入 `power_design.json`，不得参与 final 后的样本追加或门槛改变。

在至少 75 个 answered groups 上零错误时，Clopper--Pearson 单侧 95% 上界约为 3.91%，因此固定总样本 100、五路各 20 且 coverage 至少 0.75 能直接检验 5% 风险门槛。任何实际错误都按预注册精确区间重新判定，不能以该零错误设计值替代真实结果。

### 6.7 P0 代码与测试产物

新增文件的最小集合：

```text
configs/parallel_v2_protocol.json
configs/schemas/evidence_packet_v1.json
trust3d/agents/evidence.py
trust3d/agents/five_route.py
tests/test_parallel_protocol.py
tests/test_evidence_packet.py
scripts/run_parallel_v2.sh
scripts/audit_parallel_artifacts.sh
```

P0 测试至少覆盖：

1. 五个 route enum 严格且互斥；
2. packet schema 往返无字段丢失；
3. private 字段注入 router 时立即失败；
4. GT/RGB provider 产生相同外层 schema；
5. `value=null` 会触发 `REOBSERVE` 或 `ABSTAIN`，不会生成默认答案；
6. 同输入、同 seed 的 route 和成本完全一致；
7. 旧二路结果的只读回归哈希不变。
8. public-identical/private-different sibling 会被识别为不可识别，不会被强制打 route 标签；
9. near-tie 与唯一最优 case 的统计口径分离；
10. inference 阶段尝试打开 private 文件时立即失败并记录访问审计。

执行命令：

```bash
bash scripts/run_parallel_v2.sh protocol
```

通过条件：全部测试成功，并生成：

```text
outputs/parallel_v2/protocol/validation.json
/224010104/Jerry/checkpoints/parallel_v2/stages/protocol.json
```

## 7. 目录、日志与 checkpoint

### 7.1 新产物边界

B/C 线和共同协议继续使用既有 `parallel_v2` 命名空间；A 线 v3 只写入新命名空间：

```text
outputs/parallel_v2/protocol/
outputs/parallel_v2/gate7_fix/
outputs/parallel_v2/integration/
data/episodes/parallel_v2/<B-or-C-owned-subdir>/
/224010104/Jerry/checkpoints/parallel_v2/
/224010104/Jerry/logs/parallel-v2/

outputs/parallel_v3/gt_five_route/
data/episodes/parallel_v3/gt5/
/224010104/Jerry/checkpoints/parallel_v3/gt_five_route/
/224010104/Jerry/logs/parallel-v3/
```

`outputs/parallel_v2/gt_five_route/` 和 `data/episodes/parallel_v2/gt5/` 是 v2 只读证据源，不属于 v3 runner。严禁复用旧目录作为 v3 `--output`，也不得把 v3 通过状态回写到 v2 `status.json`。

### 7.2 状态机

每条线使用独立状态文件：

```text
outputs/parallel_v3/gt_five_route/status.json
outputs/parallel_v2/gate7_fix/status.json
outputs/parallel_v2/integration/status.json
```

状态只能为：

```text
pending
running
waiting_for_resource
recovering
complete
failed_engineering
failed_scientific
```

状态更新必须先写 `.tmp.<pid>`、执行 `fsync`，再在同一文件系统内原子重命名。状态文件必须同时记录 `protocol_revision`，v3 runner 发现 v2 revision 时拒绝恢复。

### 7.3 单元 checkpoint

- A v3：每个 `dataset_layer + group_id + route_policy` 一个 checkpoint；contract、prepare、infer、evaluate、online 分阶段保存；
- B 线：每个 `group_id + backend + adapter_revision` 一个 checkpoint；
- C 线：每个 `group_id + frozen_router + frozen_provider` 一个 checkpoint；
- checkpoint 必须保存输入 manifest 哈希、配置哈希、代码提交、seed、退出码、输出哈希和 private access count；
- `resume` 只跳过 `status=success` 且 fingerprint 与当前协议完全一致的单元；
- checkpoint 先写临时文件并 `fsync` 后原子重命名；失败尝试移入 `failed_attempts/<timestamp>/`，不得覆盖；
- evaluator 的一次性打开标记、predictions 哈希和 private manifest 哈希必须单独原子保存，服务器重启后也不能重新评价同一 revision。

### 7.4 Git 跟踪与大文件边界

仓库采用 default-deny `.gitignore`。P0 必须显式加入经过审阅的最小白名单，不能使用 `git add -f .`：

```text
跟踪：configs、source、tests、runner、protocol validation、manifest、metrics、final_decision、中文报告
不跟踪：模型权重、RGB/depth 原始帧、临时缓存、conda 环境、日志正文、锁文件、private answer、完整密钥
按审计决定：逐 group checkpoint JSON 和 predictions JSONL；先统计大小并确认不含 private 字段
```

每个里程碑提交前执行：

```bash
git status --short --untracked-files=all
git diff --check
bash scripts/audit_parallel_artifacts.sh --staged-only
```

审计脚本从权限为 `600` 的 `/224010104/Jerry/.codex/auth.json` 在内存中读取当前 key，只比较完整值或哈希，不输出 key；同时检测常见私钥/令牌模式、auth 文件、private answer 字段和超大文件。任一真实凭据、private answer 或未经批准的大文件命中时立即停止提交。提交分为 `protocol`、`A-v3-result`、`B-result`、`C-result/report` 独立里程碑，推送前再次确认 remote 和 staged diff。

## 8. tmux 与资源调度

### 8.1 固定窗口与锁

保留正在运行的 B 线窗口，在现有 `trust3d-<hostname>` session 中新增或复用：

```text
parallel-v2-audit
parallel-a-gt5-v3
parallel-b-gate7
parallel-status
parallel-report
```

只允许一个 A v3 runner，且不得为 v3 另起第二个 B runner。锁文件：

```text
/224010104/Jerry/checkpoints/parallel_v3/gt_five_route/runner.lock
/224010104/Jerry/checkpoints/parallel_v3/gt_five_route/evaluation.lock
/224010104/Jerry/checkpoints/parallel_v2/orchestrator.lock
/224010104/Jerry/checkpoints/parallel_v2/gate7_fix.lock
/224010104/Jerry/checkpoints/parallel_v2/integration.lock
/224010104/Jerry/checkpoints/parallel_v2/simulator.lock
/224010104/Jerry/checkpoints/parallel_v2/private_evaluator.lock
```

A v3 的 simulator 和 private evaluation 除获取自己的阶段锁外，还必须获取上述 `parallel_v2` 共享锁；这保证它不会与当前 B2 数据构建或 B 线评价并发。

### 8.2 A/B 并行资源边界

| 工作流 | 设备 | 上限 | 并行规则 |
|---|---|---|---|
| A v3 审计、契约、manifest、离线推理 | CPU | `OMP_NUM_THREADS=8` | 可与 B GPU 或 B simulator 同时运行 |
| A v3 新 source 生成与在线验证 | CPU + Xvfb/llvmpipe | `LP_NUM_THREADS=8`，一次一个 simulator worker | 必须等待 B2 释放共享 `simulator.lock` |
| B 静态测试/评价 | CPU | `OMP_NUM_THREADS=8` | 可与 A v3 离线阶段并行 |
| B pilot/holdout 数据生成 | CPU + Xvfb/llvmpipe | 一次一个 simulator worker | 与 A v3 simulator 阶段互斥 |
| B 模型前向 | 单张 GPU | 空闲显存至少 20 GiB，利用率不高于 10% | 单次采样命中后原子复核并立即启动 |
| C 联合实验 | CPU 或单张 GPU | 继承冻结 provider 需要 | A v3、B 都 complete 后才允许 |

CPU 阶段的原子准入阈值固定为：`load1 / nproc <= 0.8`、available memory `>=64 GiB`、工作区所在文件系统可用空间 `>=100 GiB`。任一条件不满足时写入 `waiting_for_resource` 并等待；不得降低其他用户进程优先级、终止进程或绕过检查强行启动。

A 线强制：

```bash
CUDA_VISIBLE_DEVICES='' OMP_NUM_THREADS=8 LP_NUM_THREADS=8
```

B 线取得 GPU 后由同一 runner 连续完成选定后端的所有 holdout group，不用无效张量占显存，也不干预他人进程。兼容矩阵固定为：

```text
A contract/prepare + B data build -> 允许，但 A 不启动 simulator
A offline          + B GPU        -> 允许
A simulator/online + B data build -> 禁止，共享 simulator.lock
A online           + B GPU        -> 仅 CPU load/内存安全时允许
A report           + B evaluate   -> 允许，但 private evaluator 互斥
任何阶段            + C            -> 禁止，C 必须等待 A/B 完成
```

### 8.3 代码冻结点

共同 P0 保持 v2 冻结。A v3 只能新增 v3 配置、独立 oracle、统计器和 runner；如确需修改共同 schema/evaluator，必须等待当前 B 阶段安全退出，提升 protocol revision，并重跑受影响的 P0，不能在 B 运行时热改。A v3 的 contract、manifest、统计方法和代码必须形成 `code_lock.json` 后才能执行 confirmatory inference。

### 8.4 统一入口

```bash
bash scripts/run_parallel_v2.sh status
bash scripts/run_five_route_gt_v3.sh preflight
bash scripts/run_five_route_gt_v3.sh contract
bash scripts/run_five_route_gt_v3.sh prepare
bash scripts/run_five_route_gt_v3.sh freeze
bash scripts/run_five_route_gt_v3.sh infer
bash scripts/run_five_route_gt_v3.sh evaluate
bash scripts/run_five_route_gt_v3.sh online
bash scripts/run_five_route_gt_v3.sh report
bash scripts/run_five_route_gt_v3.sh status
bash scripts/run_five_route_gt_v3.sh resume
```

`resume` 必须先校验 checkpoint、manifest、code lock、Git commit 和一次性评价标记，不得从头覆盖。A v3 的启动和恢复不得停止、重建或改变现有 `parallel-b-gate7`。

## 9. 数据划分与防止后验调参

### 9.1 A 线数据

v3 固定使用三层数据，不覆盖 v2 数据，也不把开发数据重新包装成新测试集：

| 数据层 | 数量 | 每路数量 | 用途 | 是否进入主结论 |
|---|---:|---:|---|---|
| `development20` | 20 | 4 | v2 pilot；实现、契约调试和统计设计 | 否 |
| `sealed60` | 60 | 12 | v2 已生成但尚未评测的 final | 是，但必须先通过未揭示审计 |
| `replication40` | 40 | 8 | 从未被 v2 使用的 source 构造的独立复现集 | 是 |

主确认集固定为 `confirmatory100 = sealed60 + replication40`，共 100 groups、每路 20 groups。三层数据的 `source_event_id` 必须互斥；相同 source 的 counterfactual sibling 只能存在于同一层。scene 无法完全互斥时保留 scene ID，并在成本区间中按 scene 聚类，禁止把同 scene groups 当成完全独立样本。

`sealed60` 的既有哈希固定为：

```text
public  8a301d2a27b431ca9fc1df2326bdc1e47f6b1b33c3d61d7eb1987fcf1b4c6884
private 138748543e9847ae703b9f81ce62a841d4a5a3ef7742c5bf8fbebeadc4312d12
```

A0 必须审计 shell/runner 日志、已有 metrics/predictions/router lock、文件哈希和 Git 历史。只有同时满足“没有 final predictions、没有 final metrics、没有基于 final 的 router 修改、哈希一致”时，`sealed60` 才保留确认资格。只要任一项无法证明，立即把它整体降级为 development 数据，并从全新 source 生成替代 `sealed60_new`；不得挑选其中看起来安全的子集。

`replication40` 使用固定 seed 从剩余 source pool 选择 32 个 MVP groups 和 8 个 spatial groups，并在查看 private route/outcome 前冻结候选顺序、去重规则和失败替补顺序。若池不足，只能继续生成全新 source events；不得复用 development20、sealed60、旧 Gate 7 或 C 线 source。

每个 source group 优先生成共享问题但证据可用性不同的配对 counterfactual，使 route 差异来自 visibility、freshness、geometry validity、reachability 和 cost，而不是对象类别或问题类型混杂。每一层生成后执行：

```text
source/scene/group 去重
五路线 loss 全枚举
public-identical/private-different 冲突检测
route loss margin 分布
按 predicate/object/scene 的 route 支持交叉表
```

confirmatory100 的 route-balanced 采样是动作覆盖压力测试，不代表自然环境中的路线流行率。报告必须同时给出：

1. 预注册 balanced macro 指标；
2. 按候选 source population route 分布重加权的指标；
3. 未加权原始计数与每个权重的来源。

不得把平衡测试集上的 route 频率表述为真实部署频率。固定 `N=100` 的功效和最小可检出效应只作为揭示前敏感性分析；confirmatory100 打开后不得追加样本、删难例、重算门槛或改用更有利的区间方法。

### 9.2 B 线数据

划分为三个用途：

1. `legacy30_regression`：当前 30 groups，只做旧结果复现和后验诊断；
2. `adapter_pilot`：与 legacy30 不同 source/scene 的至少 10 groups，用于选择任务头、坐标和 RGB-only grounding 方案；
3. `gate7_holdout`：与前两者 source/scene 均不重叠的至少 30 groups，直到 adapter fingerprint 冻结后才运行，作为修复后的主评价。

`gate7_holdout` 使用两阶段封存：

1. **Inference phase**：runner 只能访问 RGB、公开问题和公开 manifest；private 路径通过访问审计显式禁止。
2. **Evaluation phase**：全部 predictions 和其 SHA256 manifest 原子封存后，独立 evaluator 在 `private_evaluator.lock` 下读取 private GT，一次性生成 metrics；模型 runner 不得重新启动或按答案覆盖 predictions。

private 目录权限固定为 `700`、private 文件固定为 `600`。权限只是第二道防线，不能代替进程级访问审计；inference manifest 中必须记录 private open count 为 0。

adapter pilot 负责冻结 confidence calibration。允许的方法仅为预注册单调映射或温度/分箱校准；输入只能是 pilot prediction 与 pilot GT。校准参数写入 `confidence_lock.json`，B/C holdout 禁止重拟合。

若无法构造未使用过的 `gate7_holdout`，B 线只能输出“后验工程改进”，不得声称 Gate 7 已被科学地重新通过。

### 9.3 C 线数据

C 线使用与 A v3 confirmatory 同 schema 的独立 integration holdout；可以共享 source generator，但不得共享同一 `source_event_id` 或 `group_id`。同样采用 inference/evaluation 两阶段封存，禁止在 C holdout 上重新标定 confidence、route threshold 或 abstain penalty。

## 10. A 线 v3：GT 五路路由可行性验证

### A0：v2 证据审计与重新封存

**目标**：先证明 `sealed60` 未被用于评价或调参；不能证明时自动切换到全新确认集，而不是带着泄漏风险继续。

执行：

```bash
bash scripts/run_five_route_gt_v3.sh preflight
```

必须完成：

1. 复核 v2 报告中的 20-group pilot 原始计数、`N=4958` 公式和 `inconclusive_underpowered` 状态；
2. 校验 v2 public/private 哈希、旧 Gate 0--7 哈希和 Git 起点；
3. 搜索 final predictions、metrics、router lock、评价 checkpoint、日志访问记录和基于 final 的代码修改；
4. 生成 `sealed_audit.json`，结论只能是 `eligible_unrevealed` 或 `downgraded_to_development`；
5. 将 v2 原报告和状态保持只读，禁止将 v3 结果解释为 v2 当时已经通过。

`sealed60` 审计不通过时，runner 必须在 private 内容仍关闭的情况下冻结全新 source 候选顺序，并生成替代 60 groups。A0 只决定数据资格，不允许查看 route 表现。

### A1：独立 oracle、全因子契约与 mutation test

**目标**：排除“router 与 evaluator 复制同一段错误逻辑后相互证明正确”的循环验证。

新增最小组件：

```text
configs/gt_five_route_v3_protocol.json
trust3d/eval/five_route_oracle_v3.py
trust3d/eval/five_route_statistics_v3.py
tests/fixtures/five_route_contract_v3.jsonl
tests/test_five_route_contract_v3.py
tests/test_five_route_mutations_v3.py
scripts/run_five_route_gt_v3.sh
```

独立 oracle 必须以“五路可行性、答案损失、风险和真实成本的全枚举”选择最低 loss route，不得调用 `trust3d/agents/five_route.py`，也不得复用 router 的条件分支。人工冻结 fixture 至少覆盖以下合法笛卡尔积及边界：

```text
current visible / invisible
required fact present / missing
history fresh / stale
predicate attribute / geometry
3D memory valid / invalid
reobserve reachable / unreachable
risk below / equal / above threshold
cost unique optimum / near tie / exact tie
```

测试必须验证 route、答案、coverage、cost ledger、memory side effect 和 abstain 语义。mutation test 至少注入 fresh/stale 反转、reachable 反转、坐标需求反转、成本符号反转、五路 label 置换、阈值边界错误和 donor/target 交换；所有登记 mutation 必须被测试杀死。再用固定 seed property test 检查：提高某路线成本不会使其更优、删除证据不会凭空提高可答性、private 字段注入会立即失败、相同 public 输入始终给出相同 route。

执行：

```bash
bash scripts/run_five_route_gt_v3.sh contract
```

通过条件：全部人工 fixture、全因子、边界、property 和 mutation 测试通过，且 oracle 与 router 无禁止依赖。

### A2：构造 replication40 并锁定 confirmatory100

**目标**：在不查看 private outcome 的情况下获得五路各 20 个、source-disjoint 的确认样本。

执行：

```bash
bash scripts/run_five_route_gt_v3.sh prepare
```

固定步骤：

1. 把 development20 写入只读开发 manifest，不进入任何确认指标；
2. 按 A0 结论引用 eligible `sealed60`，或生成完整替代 60；
3. 从剩余 pool 按固定 seed 选 32 个 MVP 和 8 个 spatial source，构造 `replication40`；
4. 在 private evaluator 内做五路平衡，失败替补严格使用揭示前冻结顺序；
5. 验证 development、sealed、replication、Gate 7 和 C 线之间 `source_event_id` 无交集；
6. 生成 public/private manifest、source lineage、权限、SHA256 和 `confirmatory100_lock.json`；
7. private 目录设为 `700`、文件设为 `600`，inference runner 的访问审计计数必须为 0。

最终必须得到 `sealed60` 每路 12、`replication40` 每路 8、合并后每路 20。缺少任一路支持时只能生成新 source，不能移动 development 样本、复制 group 或删除难例凑数。

### A3：冻结统计实现与有限样本判定

**目标**：在打开 confirmatory private GT 前，把所有统计方法、分母、seed 和失败判定变成可测试代码。

执行：

```bash
bash scripts/run_five_route_gt_v3.sh freeze
```

必须冻结：

1. coverage 分母为 confirmatory100；selective error 分母为实际 answered groups；
2. false-abstain 分母为 private oracle 非 `ABSTAIN` groups；unsafe-answer 分母为全部 confirmatory100；
3. 三个错误率均使用 Clopper--Pearson 单侧 95% 上界，硬门槛 `<=0.05`；
4. 与 `always_reobserve` 的配对准确率差使用 matched-pair Tango score 单侧 95% 下界，硬门槛 `>=-0.02`；
5. 成本下降定义为 `1 - mean(cost_trust3d) / mean(cost_always_reobserve)`，按 scene 聚类做固定 seed 的 100,000 次 bootstrap，要求点估计 `>=0.20` 且单侧 95% 下界 `>0`；
6. paired counts、各区间端点、bootstrap scene 抽样和四舍五入规则；
7. development20 的保守 discordance 网格、最小可检出效应和功效曲线只写入 `power_design.json`，不产生追加样本规则；
8. 用手算 fixture 和至少一个独立统计实现的固定参考值验证区间代码。

区间判定使用未四舍五入值。固定 `N=100`、coverage 至少 0.75 时，零 selective error 的单侧 95% 上界约 3.91%；这只是设计校验，真实评价必须按实际分母重算。

### A4：router、代码和 evaluator 锁定

只允许使用 development20 修复实现错误和选择协议允许的 calibration。风险阈值、成本权重、route enum、非劣界、数据量、统计方法和验收门槛不得改变。冻结产物：

```text
outputs/parallel_v3/gt_five_route/router_lock.json
outputs/parallel_v3/gt_five_route/code_lock.json
outputs/parallel_v3/gt_five_route/statistics_lock.json
outputs/parallel_v3/gt_five_route/confirmatory100_lock.json
```

四个锁必须保存 Git commit、代码/配置/manifest 哈希、seed、依赖版本和旧基线哈希。锁定后工作树存在影响 A 线的改动时，`infer` 必须拒绝启动；修复只能提升 revision 并重新执行 A0--A4，旧 revision 全部保留。

### A5：sealed inference 与一次性 private evaluation

先执行只能读取 public manifest 的推理：

```bash
bash scripts/run_five_route_gt_v3.sh infer
```

最小 baseline 固定为：

```text
always_current
always_history
always_3d_memory
always_reobserve
always_abstain
legacy_two_route
trust3d_five_route
```

100 groups 的全部 predictions、route、公开证据 fingerprint 和 cost request 生成后，原子封存 `predictions.sha256`。随后 runner 退出，独立 evaluator 同时获取 v3 `evaluation.lock` 与共享 `private_evaluator.lock`，只打开一次 private GT：

```bash
bash scripts/run_five_route_gt_v3.sh evaluate
```

评价必须输出 pooled confirmatory100 主结果，并分别输出 sealed60、replication40、五路、predicate、scene 和 source 类型结果；不得用某个更有利子集替代 pooled 主结果。必须报告 route confusion、avoidable regret、answer/coverage/risk、stale-memory error、所有成本项、paired discordant counts、精确区间、scene-cluster bootstrap、自然 source population 重加权敏感性和 public/private access audit。

### A6：AI2-THOR 在线执行与恢复验证

**目标**：确认离线 route 不只是标签正确，而是能产生五种预期执行副作用和真实成本。

执行：

```bash
bash scripts/run_five_route_gt_v3.sh online
```

该阶段必须等待当前 B2 释放 `/224010104/Jerry/checkpoints/parallel_v2/simulator.lock`，获取锁后按 group checkpoint 执行 confirmatory100。检查：

1. 在线 route 与 sealed prediction 一致；
2. `USE_CURRENT_VIEW` 不移动、不写入伪新观测；
3. `RETRIEVE_HISTORY` 只读新鲜非几何事实；
4. `QUERY_3D_MEMORY` 使用冻结坐标系和有效几何；
5. `REOBSERVE` 实际执行 planner 路径，达到可见阈值后才更新 memory；
6. `ABSTAIN` 不移动、不生成答案；
7. move steps、new observations、geometry/VLM calls 和 wall time 与账本一致；
8. 人为中断一个未完成单元后，`resume` 从最近有效 checkpoint 恢复，且不重复已完成 group。

在线不可恢复的 source 必须原样计为执行失败，不能静默改用离线 GT。任何新 source 生成或在线 simulator 操作都不能与 B2 并发。

### A7：严格验收与中文报告

以下条件全部满足，A v3 才标记 `complete`：

1. sealed 未揭示审计通过，或已按规则整体替换；development、sealed、replication source 完全隔离；
2. 独立 oracle 的全因子、边界、property 和全部登记 mutation 测试通过；
3. confirmatory100 五路各 20 个明确支持，router 与 oracle 为 `100/100`，avoidable route regret 为 0；
4. `coverage >=0.75`，selective error 的 Clopper--Pearson 单侧 95% 上界 `<=0.05`；
5. false-abstain 和 unsafe-answer 的预注册单侧 95% 上界均 `<=0.05`；
6. 相对 `always_reobserve` 的配对准确率差 Tango 单侧 95% 下界 `>=-0.02`；
7. 相对 `always_reobserve` 的成本下降点估计 `>=0.20`，scene-cluster bootstrap 单侧 95% 下界 `>0`；
8. stale-memory error 低于 `always_history` 和 legacy 无条件信任子集，并完整报告配对结果；
9. confirmatory100 在线 route 与离线封存 route 一致，五路副作用、成本账本和 checkpoint 恢复测试全部通过；
10. private access count、缺失输出、非法默认答案和旧基线哈希变化均为 0。

任一条件失败都保留完整结果并标记 `failed_scientific` 或 `failed_engineering`；不得在 confirmatory100 上调阈值、删样本、追加样本或改统计方法。通过后允许的结论仅为“在可靠 GT 证据和预注册平衡压力测试下，完整五路路由控制器具有可行性、风险受控且相对 always-reobserve 节省成本”；不得扩展成 RGB-only 端到端系统已经成立。

最终生成：

```text
Trust3D_GT五路路由可行性验证报告_v3.md
```

报告必须同时保留 v2 `inconclusive_underpowered` 的原因、v3 修订理由、逐项验收证据、失败项和结论边界。

## 11. B 线：Gate 7 修复

### B0：锁定诊断起点

读取但不覆盖：

```text
outputs/gate7/
outputs/plan2/
outputs/gate7_diagnosis/
```

将以下已知事实写入新 protocol lock：

- contract-valid task head：`gate7-planar-yaw-v1`；
- legacy task head：`gate7-camera-z-v1`；
- CUT3R/VGGT baseline、29-group complete-case 指标和缺失 group；
- 当前模型、配置、代码和数据哈希；
- `legacy30_regression` 只允许诊断，不作为调参后主结果。

后续所有表必须同时保留两列，禁止用修正 evaluator 覆盖历史数值：

```text
legacy_metric：严格复现原 Gate 7/方案 2，仅作历史回归
contract_valid_metric：使用数据契约一致的任务头，作为新 holdout 主评价
```

任务头变化带来的提升与 geometry/grounding 提升分别记账，不能把 evaluator 语义修复计作 backbone 改善。

命令：

```bash
bash scripts/run_gate7_fix.sh lock
```

### B1：任务头与坐标契约

先修复可证明错误，不运行大模型。新增或扩展测试：

1. planar yaw 与数据标签一致；
2. camera-to-world/world-to-camera 往返；
3. OpenCV/OpenGL 轴符号；
4. 90/180/270 度相机旋转；
5. donor/target 交换；
6. target/reference 角色绑定；
7. Sim(3) 只能作为 diagnostic oracle，不能进入 main adapter；
8. 前后、左右和距离的类别对称合成 fixture。

命令：

```bash
bash scripts/run_gate7_fix.sh unit
```

通过条件：合成契约 100% 通过、round-trip tolerance `<=1e-6`、旧输出哈希不变。

### B2：分层适配器

主路径必须拆成四个可单独替换和记录的阶段：

```text
RGB frames
  -> RGB-only object grounding
  -> camera/intrinsic/extrinsic normalization
  -> depth/point unprojection and object geometry
  -> contract-valid spatial task head
```

每阶段写独立中间 artifact 和置信度，不允许一个函数同时读取 GT mask、修改 pose 并生成答案。

置信度也必须分层记录：

```text
grounding_confidence
camera_confidence
geometry_confidence
answer_confidence
calibrated_error_probability
```

聚合公式在 pilot 前预注册。缺少任一必需层或出现 NaN 时，输出失败 packet，并由 C 线 router 决定 `REOBSERVE/ABSTAIN`，不得用固定高置信度代替。

建议新增：

```text
trust3d/geometry/rgb_grounding.py
trust3d/geometry/camera_contract.py
trust3d/geometry/gate7_adapter.py
trust3d/data/build_gate7_holdout.py
trust3d/eval/evaluate_gate7_fix.py
tests/test_gate7_fix_adapter.py
configs/gate7_fix_v1.json
```

先生成并冻结与 `legacy30_regression` 不重叠的数据：

```bash
bash scripts/run_gate7_fix.sh prepare
```

`prepare` 必须输出 pilot/holdout 的 source、scene、group 去重审计和 public/private manifest；发现交集立即失败。

### B3：pilot 因素阶梯

只在 `adapter_pilot` 上按固定顺序运行：

| 变体 | 唯一变化 | 目的 |
|---|---|---|
| F0 | 冻结旧 baseline | 确认 evaluator 和数据未漂移 |
| F1 | F0 + contract-valid task head | 去除已证明语义错误 |
| F2 | F1 + 明确的外参方向/轴约定 | 隔离 camera/coordinate adapter |
| F3 | F2 + 冻结 RGB-only grounding | 隔离中心 crop 污染 |
| F4-C | F3 + CUT3R geometry | 评价修复后的 CUT3R 路径 |
| F4-V | F3 + VGGT depth-unprojection | 评价修复后的 VGGT 路径 |

GT bbox/mask、GT pose-aligned 只进入带 `oracle_` 前缀的诊断表，不参与主适配器选择。

候选选择顺序固定为：

1. private leak 和 geometry failure 必须为 0；
2. task contract 必须通过；
3. 先最小化 QA drop；
4. QA drop 相同则选参数更少、运行更稳定的适配器；
5. 不按单一问题类别选择对其有利的候选。

CUT3R 与 VGGT 可以各自冻结一个 adapter fingerprint，但每个 backend 只能有一个主候选。pilot 上未胜出的变体全部保留为消融，不能在 holdout 结果揭示后更换主候选。

完成后冻结：

```text
outputs/parallel_v2/gate7_fix/adapter_lock.json
```

执行命令：

```bash
bash scripts/run_gate7_fix.sh pilot
```

### B4：GPU 准入与 holdout 前向

命令：

```bash
bash scripts/run_gate7_fix.sh watch
```

`watch` 命中并通过原子复核后必须由同一进程 `exec` 到 `holdout`，不得另开第二个 runner。

规则：

1. 每次先检查 CPU、内存、磁盘和所有 GPU；
2. 单张 GPU 一次采样满足空闲显存 `>=20 GiB` 且利用率 `<=10%` 时立即执行一次原子复核；
3. 复核失败回到高频监测，不强行启动；
4. 命中后同一 runner 连续完成 CUT3R/VGGT 已冻结候选；
5. 不通过无效张量填满显存，不干预其他用户；
6. 每个 group 完成后原子保存 checkpoint；
7. OOM/CUDA 错误保留失败记录并回到等待，不降低图像尺寸或改变配置后偷偷继续同 revision。

holdout 前向只能读取 public manifest。每个 backend 完成后先写：

```text
predictions.jsonl
predictions.sha256
inference_access_audit.json
inference_complete.json
```

只有上述四项均验证成功，独立 evaluator 才能获取 `private_evaluator.lock` 并打开 private GT。任何 inference 阶段 private open count 非 0 都使该 revision 作废。

实际前向：

```bash
bash scripts/run_gate7_fix.sh holdout
```

`holdout` 是 watcher/orchestrator 的内部阶段；存在有效 watcher 时禁止人工并行调用。

### B5：CPU 恢复与严格评价

```bash
CUDA_VISIBLE_DEVICES='' bash scripts/run_gate7_fix.sh recover
CUDA_VISIBLE_DEVICES='' bash scripts/run_gate7_fix.sh evaluate
```

`evaluate` 只接受已封存 predictions SHA256；发现文件修改时间、大小或哈希变化立即停止。评价后不得返回模型前向阶段补写预测。

恢复测试必须证明：

- 不加载 CUT3R/VGGT 模型；
- GPU 显存不增加；
- 所有 checkpoint 哈希前后一致；
- 只跳过 fingerprint 完全匹配的成功 group；
- 失败 group 不被当作错误答案静默计入。

### B6：B 线验收

硬门槛：

1. private file open count 为 0；
2. GT mask/bbox/pose alignment 不进入 main result；
3. geometry group failure rate 为 0；
4. contract-valid task head 和坐标测试全部通过；
5. 30-group legacy regression 与旧版本精确复现；
6. holdout checkpoint recovery 通过；
7. 报告 CUT3R、VGGT 及 GT/RGB-D 上界的 paired group bootstrap；
8. 同时报告 legacy_metric 与 contract_valid_metric，并将任务头、grounding、pose 和 backbone 的增益分开；
9. confidence calibration 只来自 pilot，holdout 仅评价 Brier/ECE、risk-coverage 和 failure detection；
10. 原 Gate 7、方案 2 和 diagnosis 哈希不变。

结果等级：

| holdout 相对 GT/RGB-D 的 QA drop | 结论 |
|---|---|
| `<=10` 个百分点 | B 线主结果通过，可进入 C 线主实验 |
| `>10` 且 `<=20` 个百分点 | 现实设置通过，可进入 C 线，但只能作为受限结果 |
| `>20` 个百分点 | B 线科学失败，不运行 C 线；继续作为失败分析 |

QA drop 等级沿用原方案的点估计门槛，同时必须报告 paired 95% CI。若点估计过门但 CI 跨越更差等级，报告使用较保守的证据标签，例如 `point_pass_interval_uncertain`，不得省略区间不确定性。

## 12. C 线：冻结后的 RGB 五路联合 Gate

### C0：兼容与冻结

仅当以下文件均为 `complete` 才启动：

```text
/224010104/Jerry/checkpoints/parallel_v2/stages/gt_five_route_report.json
/224010104/Jerry/checkpoints/parallel_v2/stages/gate7_fix_report.json
```

将 A 线 `router_lock.json` 与 B 线 `adapter_lock.json` 合并为只读 `integration_lock.json`。不得在 integration holdout 上调整：

- route threshold；
- confidence mapping；
- crop/bbox/track 参数；
- 坐标符号；
- task head；
- abstain penalty。

同时锁定 B pilot 产生的 `confidence_lock.json`。C 线只允许把 B packet 的 `calibrated_error_probability` 代入 A router；不得读取 integration GT 重新校准。缺失、过期、坐标契约失败或置信度非法的 packet 使用预注册 fallback：优先合法 `REOBSERVE`，不可达或风险仍超阈值时 `ABSTAIN`。

### C1：接口回归

用同一批 public packet 分别运行 GT provider 和 RGB provider，验证 schema、单位、route 可达性和 failure semantics 一致。GT 与 RGB 数值不要求相等，但字段缺失和坐标约定必须一致。

```bash
bash scripts/run_parallel_integration.sh unit
```

### C2：integration holdout

```bash
bash scripts/run_parallel_integration.sh run
bash scripts/run_parallel_integration.sh recover
bash scripts/run_parallel_integration.sh evaluate
```

`run` 与 `evaluate` 之间必须封存 predictions manifest，执行与 B 线相同的 private 访问隔离。integration evaluator 不得调用模型或修改 packet。

必须同时报告：

- GT 五路上界；
- RGB 五路实际结果；
- always-current/history/3D/reobserve/abstain；
- 旧二路 policy；
- 按证据失败类型发生的 route shift；
- RGB confidence 与真实错误的 calibration；
- route regret、answer regret 和 cost regret；
- 感知失败后是否安全转向 `REOBSERVE/ABSTAIN`。

### C3：联合验收

1. B 线仍满足其 QA drop 等级；
2. RGB 五路相对 `always_reobserve` 的配对准确率差点估计不低于 -2 个百分点，且 95% CI 下界不低于非劣界；
3. RGB 五路平均移动或新观察成本下降点估计至少 20%，且 95% CI 下界大于 0；
4. selective error `<=0.05`，coverage `>=0.75`；
5. stale-memory、unsafe-answer、false-abstain 均不劣于旧二路 policy；
6. provider failure 时默认行为是重新观察或拒答，不是猜测；
7. 所有 route 至少有一次合法执行，且按 route 分层结果完整；
8. checkpoint recovery、public/private 审计和原基线哈希全部通过。

另外必须把 C 相对 A 的 route regret 分解成三类：provider value error、confidence/calibration error、router decision error。若无法归类，记为 `unattributed_integration_error`，不得全部归给 backbone。

C3 失败不推翻 A 线的控制器上界或 B 线的感知结果，但意味着完整 idea 尚未通过端到端验证。

## 13. runner 的最小实现要求

### 13.1 顶层 runner

须新增 A v3 独立入口，同时保留 B/C 既有入口：

```text
scripts/run_five_route_gt_v3.sh
scripts/run_parallel_v2.sh
scripts/run_gate7_fix.sh
scripts/run_parallel_integration.sh
```

`scripts/run_five_route_gt_v3.sh` 至少支持：

```text
preflight
contract
prepare
freeze
infer
evaluate
online
report
status
resume
```

runner 的强制逻辑：

1. 获取 A v3 `runner.lock`，发现同 revision 活进程时只报告状态；
2. 每个阶段先记录资源快照、工作目录、完整命令、Git commit、日志路径和输入 fingerprint；
3. 校验旧基线、v2 sealed、confirmatory manifest、代码和统计锁；
4. `infer` 进程禁止访问 private 路径，访问即失败；
5. predictions 原子封存后 `evaluate` 才能获取双重 evaluator 锁，且只允许一次；
6. 需要 simulator 时等待共享锁，不停止或修改正在运行的 B 线；
7. `resume` 校验逐阶段和逐 group checkpoint，只恢复未验证成功的单元；
8. 任一科学门槛失败只停止 A v3，B 继续；共同协议损坏时才阻止后续 C；
9. 退出时原子写入退出码、结束时间、最后有效 checkpoint 和下一恢复命令。

### 13.2 日志格式

每次阶段执行必须持续写入 `/224010104/Jerry/logs/parallel-v3/`，至少记录：

```text
开始时间与结束时间
主机和 tmux session/window
工作目录与完整命令
Git commit 和 worktree dirty/clean 状态
配置、代码和输入 manifest SHA256
public/private access audit
CPU、内存、磁盘和 GPU 快照
阶段与单元退出码
checkpoint 写入和校验结果
下一恢复点
```

### 13.3 自动报告

最终生成：

```text
Trust3D_GT五路路由可行性验证报告_v3.md
Trust3D_Gate7修复实验报告.md
Trust3D_GT五路与RGB几何联合实验报告.md
```

A v3 报告必须逐项列出验收门槛、点估计、区间、分母、原始计数、pass/fail、产物哈希和恢复审计，并明确区分：

```text
已证明
条件式证据
未证明
工程失败
科学失败
后验诊断
确认性主结果
```

## 14. 并行期间的变更所有权

| 路径 | 所有者 |
|---|---|
| `trust3d/agents/evidence.py`、schema、共同 evaluator | P0；冻结后只读 |
| `trust3d/agents/five_route.py`、v3 oracle/statistics、`run_five_route_gt_v3.sh` | A v3 |
| `trust3d/geometry/*gate7*`、`rgb_grounding.py`、`camera_contract.py` | B 线 |
| `outputs/parallel_v3/gt_five_route/`、`data/episodes/parallel_v3/gt5/` | A v3 runner |
| `outputs/parallel_v2/gate7_fix/` | B runner |
| `outputs/parallel_v2/integration/` | C runner |
| `outputs/parallel_v2/gt_five_route/`、`data/episodes/parallel_v2/gt5/` | 无所有者，只读 |
| 旧 `outputs/gate*`、`outputs/plan2`、`outputs/gate7_diagnosis` | 无所有者，只读 |

如 A、B 都要求修改 P0 文件，必须等正在运行阶段到达可恢复边界，暂停 Jerry 自己的相关 runner，提升 protocol revision 并重跑 P0；不得并发写同一文件，也不得停止其他用户进程。

## 15. 失败处理与继续规则

| 情况 | 处理 |
|---|---|
| tmux 客户端断开 | runner 继续，日志和 checkpoint 不受影响 |
| 服务器重启 | 重新进入 `enter_trust3d.sh`，执行 `run_five_route_gt_v3.sh resume`；tmux 消失不等于数据丢失 |
| `sealed60` 未揭示审计失败或证据不足 | 整体降级为 development；按冻结规则生成全新替代 60，不选取性保留 |
| replication source pool 不足 | 继续生成全新 source；等待 simulator 锁，不复用旧 source |
| B2 持有 simulator 锁 | A contract/离线阶段继续；A source 生成和 online 标记 `waiting_for_resource` |
| A 单 group simulator 失败 | 保存失败 checkpoint；可继续独立 group；最终按预注册缺失/失败规则判定，不静默丢弃 |
| checkpoint 哈希不匹配 | 标记损坏并停止受影响阶段，不覆盖或盲目重跑 |
| public/private 泄漏 | A v3 立即 `failed_engineering`，该 revision 确认结果作废 |
| predictions 封存后要求改代码、数据或阈值 | 终止当前 revision 并保留产物；新 revision 从 A0 开始，不能重新使用已揭示集作确认 |
| confirmatory 门槛未过 | `failed_scientific`，保留结果；不得调门槛、删难例、追加样本或更换区间方法 |
| 固定 N 的功效敏感性较低 | 如实报告；不能恢复 v2 的 `N=4958` 硬停止，也不能在揭示后扩样 |
| A v3 通过、B 失败 | 只报告可靠 GT 证据下控制器上界，不进入 C |
| B 通过、A v3 失败 | 只报告感知改进，不声称完整路由成立 |
| C 失败 | 分别保留 A/B 结论，明确集成耦合尚未解决 |

“自己解决问题”指在上述冻结边界内自动恢复、补充全新 source、修复工程错误并提升 revision，不是保证制造阳性结果。科学门槛失败时必须保留真实结论。

## 16. 最小执行顺序

当前 B 线继续运行；A v3 按以下顺序独立推进：

```bash
# 0. 查看现有 B 状态，不停止、不重复启动
bash scripts/run_parallel_v2.sh status

# 1. A v3 资源、旧哈希和 sealed 资格审计
bash scripts/run_five_route_gt_v3.sh preflight

# 2. 独立 oracle、全因子、边界、property 和 mutation test
bash scripts/run_five_route_gt_v3.sh contract

# 3. 构造 replication40，锁定 confirmatory100
bash scripts/run_five_route_gt_v3.sh prepare

# 4. 冻结 router、统计方法、代码、manifest 和 Git revision
bash scripts/run_five_route_gt_v3.sh freeze

# 5. public-only 推理和 predictions 封存
bash scripts/run_five_route_gt_v3.sh infer

# 6. 获取 private evaluator 锁，一次性评价
bash scripts/run_five_route_gt_v3.sh evaluate

# 7. 等 B2 释放 simulator 锁后执行完整在线验证
bash scripts/run_five_route_gt_v3.sh online

# 8. 输出中文详细报告
bash scripts/run_five_route_gt_v3.sh report

# 任意中断或服务器重启后的唯一恢复命令
bash scripts/run_five_route_gt_v3.sh resume
```

若 A2 需要 simulator 而 B2 尚未结束，runner 只等待该子阶段；A1、A3 的实现和静态验证可以继续。C 仍只在 A v3 和 B 都产生有效 completion checkpoint 后执行。

## 17. 最终决策表

| A v3 | B 线 | C 线 | 最终允许结论 |
|---|---|---|---|
| 通过 | 未通过 | 不运行 | 五路 idea 在可靠 GT 证据下可行；RGB 感知仍是瓶颈 |
| 未通过 | 通过 | 不运行 | 感知改善，但五路控制器设计未被确认 |
| 通过 | 10--20 pp 受限通过 | 通过 | 完整系统在现实设置下有条件成立，并明确 backbone gap |
| 通过 | `<=10 pp` 通过 | 通过 | 可以作为完整 RGB 五路 Trust3D 主结果 |
| 通过 | 通过 | 未通过 | 两模块各自有效，但置信度/路由耦合仍未解决 |
| 未通过 | 未通过 | 不运行 | 当前实现和完整 idea 均未获得新增支持 |

## 18. 完成清单

### 共同协议

- [ ] 原基线 manifest 和 SHA256 已复核且未变化
- [ ] EvidencePacket schema、五路动作和成本语义保持冻结
- [ ] public/private 泄漏测试通过
- [ ] code lock、Git commit 和最小 `.gitignore` 白名单已冻结
- [ ] 当前 B 线未被 A v3 中断或覆盖

### A v3

- [ ] v2 `inconclusive_underpowered` 报告和数据保持只读
- [ ] sealed60 未揭示资格已证明，或已整体替换
- [ ] development20、sealed60、replication40 source 无交集
- [ ] confirmatory100 共 100 groups、五路各 20
- [ ] 独立 oracle 不依赖 router 实现
- [ ] 全因子、边界、property 和 mutation tests 全部通过
- [ ] Clopper--Pearson、Tango 和 scene-cluster bootstrap 已用参考 fixture 验证
- [ ] router/code/statistics/confirmatory locks 已冻结
- [ ] public inference 的 private access count 为 0
- [ ] predictions 已封存，private evaluator 只打开一次
- [ ] route match `100/100`、avoidable regret 为 0
- [ ] coverage、selective error、false abstain 和 unsafe answer 全部过门槛
- [ ] 配对准确率非劣和成本优势全部过门槛
- [ ] confirmatory100 在线副作用、成本账本和恢复测试通过
- [ ] `Trust3D_GT五路路由可行性验证报告_v3.md` 已生成

### B 线

B 线于 2026-08-02 完成全部执行阶段；最佳 holdout QA drop 为 48.89 个百分点，科学等级为 `failed_scientific`，因此按预注册门槛不启动 C 线主实验。

- [x] legacy30 回归锁定
- [x] 任务头/坐标契约测试通过
- [x] RGB-only grounding 与几何阶段已分离
- [x] adapter pilot 与 holdout 不重叠
- [x] adapter 和 confidence lock 已冻结且只使用 pilot
- [x] CUT3R/VGGT holdout 前向完成
- [x] predictions 已在 private evaluator 打开前封存
- [x] private leak 和 geometry failure 均为 0
- [x] CPU checkpoint recovery 完成
- [x] `Trust3D_Gate7修复实验报告.md` 已生成

### C 线

- [ ] A v3/B completion checkpoint 均有效
- [ ] integration lock 已冻结
- [ ] provider schema 回归通过
- [ ] RGB 五路 integration holdout 完成
- [ ] integration predictions 已封存且未重新校准
- [ ] accuracy-cost-risk 验收完成
- [ ] `Trust3D_GT五路与RGB几何联合实验报告.md` 已生成
- [ ] 所有旧实验哈希保持不变

## 19. 本计划的核心防错原则

1. **先解耦再并行**：共同接口冻结后，A 验证 controller，B 验证 perception。
2. **独立实现交叉验证**：oracle、router 和统计器不得共享会形成循环证明的决策分支。
3. **GT 只定义上界**：GT 五路成功不能替代 RGB 联合实验。
4. **开发集与确认集隔离**：development20 只能修工程；sealed/replication 只能一次性确认。
5. **先封存再评价**：confirmatory inference 看不到 private GT，预测哈希冻结后 evaluator 才能打开真值。
6. **固定门槛解决功效误判**：保留 v2 `N=4958` 为敏感性分析，使用预注册有限样本区间验证实际风险、非劣和成本优势。
7. **并行不争写**：A v3、B、simulator 和 private evaluator 都有唯一锁与所有者。
8. **失败也是结果**：不降低门槛、不删除难例、不把缺失输出填成默认答案。
9. **结论不可回写**：v3 通过不能改写成 v2 已通过，也不能改写原 Gate 7 失败。
10. **最终必须汇合**：A、B 的独立成功只有经过 C 线才能支持完整 RGB 五路 Trust3D 系统。
