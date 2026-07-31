# Trust3D：GT 五路路由与 Gate 7 修复并行执行计划

> 版本：`parallel-v2`
>
> 制定日期：2026-07-31
>
> 工作区：`/224010104/Jerry/trust3d`
>
> 基线提交：`117393a8b4358f736d7947850cecb459c0a3f605`
>
> 文档性质：可直接据此实现 runner、执行实验和生成报告的冻结协议

> v2 修订：补充五路可识别性、pilot 功效审计、sealed holdout、置信度冻结、simulator 互斥、CI 判定和 Git 安全同步规则；不改变任何既有 Gate 结果。

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
| A 线 | 在错误风险约束下，相对 `always_reobserve` 的配对准确率差和成本下降 | route regret、route macro recall、stale error、coverage | 单独的 oracle-route 分类准确率 |
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
```

执行前将这些路径的 SHA256 manifest 写入：

```text
outputs/parallel_v2/protocol/baseline_manifest.sha256
```

每次 `resume` 和最终报告前都重新校验。任一哈希变化立即停止，不允许自动修复或覆盖旧产物。

## 5. 总体依赖图

```text
                         P0 共同协议冻结
                    /                           \
       A0-A5：GT 五路路由                 B0-B6：Gate 7 修复
       CPU / AI2-THOR                     GPU / RGB-only
                    \                           /
                      C0 协议兼容与置信度冻结
                                  |
                      C1-C3：RGB 五路联合 Gate
                                  |
                         三份分报告 + 总报告
```

允许的并行关系：

- P0 通过后，A0--A5 与 B0--B6 可以同时推进；
- A 线不得等待或读取 B 线实验答案来改变 GT router；
- B 线不得读取 A 线 private oracle route 来调视觉适配器；
- C 线必须等待 A5 和 B6 都产生不可变完成标记。

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

### 6.6 P0 代码与测试产物

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

### 7.1 新产物只能写入

```text
outputs/parallel_v2/protocol/
outputs/parallel_v2/gt_five_route/
outputs/parallel_v2/gate7_fix/
outputs/parallel_v2/integration/
data/episodes/parallel_v2/
/224010104/Jerry/checkpoints/parallel_v2/
/224010104/Jerry/logs/parallel-v2/
```

严禁复用旧目录作为 `--output`。

### 7.2 状态机

每条线使用独立状态文件：

```text
outputs/parallel_v2/gt_five_route/status.json
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

状态更新必须先写 `.tmp.<pid>`，`fsync` 后原子重命名。

### 7.3 单元 checkpoint

- A 线：每个 `group_id + route_policy` 一个 checkpoint；
- B 线：每个 `group_id + backend + adapter_revision` 一个 checkpoint；
- C 线：每个 `group_id + frozen_router + frozen_provider` 一个 checkpoint；
- checkpoint 必须保存输入 manifest 哈希、配置哈希、代码提交、seed、退出码和输出哈希；
- `resume` 只跳过 `status=success` 且 fingerprint 与当前协议完全一致的单元；
- 失败尝试移入 `failed_attempts/<timestamp>/`，不得覆盖。

### 7.4 Git 跟踪与大文件边界

仓库采用 default-deny `.gitignore`。P0 必须显式加入经过审阅的最小白名单，不能使用 `git add -f .`：

```text
跟踪：configs、source、tests、runner、protocol/validation、manifest、metrics、final_decision、三份 Markdown 报告
不跟踪：模型权重、RGB/depth 原始帧、临时缓存、conda 环境、日志正文、锁文件、private answer、完整密钥
按审计决定：逐 group checkpoint JSON 和 predictions JSONL；先统计大小并确认不含 private 字段
```

每个里程碑提交前执行：

```bash
git status --short --untracked-files=all
git diff --check
bash scripts/audit_parallel_artifacts.sh --staged-only
```

审计脚本从权限为 `600` 的 `/224010104/Jerry/.codex/auth.json` 在内存中读取当前 key，只比较完整值或哈希，不输出 key；同时检测常见私钥/令牌模式、auth 文件、private answer 字段和超大文件。它只能输出命中文件路径与规则名。任一真实凭据、private answer 或未经批准的大文件命中时立即停止提交。提交分为 `protocol`、`A-result`、`B-result`、`C-result/report` 四个独立里程碑，推送前再次确认 remote 和 diff。

## 8. tmux 与资源调度

### 8.1 固定窗口

在现有 `trust3d-<hostname>` session 中创建：

```text
parallel-orchestrator
parallel-a-gt5
parallel-b-gate7
parallel-status
parallel-report
```

只允许一个 orchestrator。锁文件：

```text
/224010104/Jerry/checkpoints/parallel_v2/orchestrator.lock
/224010104/Jerry/checkpoints/parallel_v2/gt_five_route.lock
/224010104/Jerry/checkpoints/parallel_v2/gate7_fix.lock
/224010104/Jerry/checkpoints/parallel_v2/integration.lock
/224010104/Jerry/checkpoints/parallel_v2/simulator.lock
/224010104/Jerry/checkpoints/parallel_v2/private_evaluator.lock
```

### 8.2 A/B 并行资源边界

| 工作流 | 设备 | 上限 | 并行规则 |
|---|---|---|---|
| A 离线路由 | CPU | `OMP_NUM_THREADS=8` | 可与 B GPU 同时运行 |
| A AI2-THOR | CPU + Xvfb/llvmpipe | `LP_NUM_THREADS=8`，一次一个 simulator worker | CPU load 或内存异常时暂停 |
| B 静态测试/评价 | CPU | `OMP_NUM_THREADS=8` | 可与 A 离线阶段并行 |
| B pilot/holdout 数据生成 | CPU + Xvfb/llvmpipe | 一次一个 simulator worker | 与 A online 共享 `simulator.lock`，不得并行 |
| B 模型前向 | 单张 GPU | 空闲显存至少 20 GiB，利用率不高于 10% | 单次采样命中后原子复核并立即启动 |
| C 联合实验 | CPU 或单张 GPU | 继承冻结 provider 需要 | A、B 都 complete 后才允许 |

CPU 阶段的原子准入阈值固定为：`load1 / nproc <= 0.8`、available memory `>=64 GiB`、工作区所在文件系统可用空间 `>=100 GiB`。任一条件不满足时写入 `waiting_for_resource` 并等待；不得通过降低其他用户进程优先级、终止进程或绕过检查强行启动。

A 线强制：

```bash
CUDA_VISIBLE_DEVICES='' OMP_NUM_THREADS=8 LP_NUM_THREADS=8
```

B 线取得 GPU 后由同一 runner 连续完成选定后端的所有 holdout group，不用无效张量占显存，也不干预他人进程。

并行不等于所有阶段同时抢资源。orchestrator 使用以下兼容矩阵：

```text
A offline   + B GPU       -> 允许
A online    + B GPU       -> 仅 CPU load/内存安全时允许
A online    + B data build -> 禁止，共享 simulator.lock
A report    + B evaluate  -> 允许
任何阶段     + C           -> 禁止，C 必须独占本项目 runner
```

### 8.3 代码冻结点

P0、A runner 和 B runner 必须先实现、测试并形成 `code_lock.json`，再启动长任务。A/B 运行期间禁止修改共同 schema、evaluator、数据 manifest 或正在执行的模块；确需修复时：

1. 停止本项目相关 runner，不停止其他用户进程；
2. 保存失败 checkpoint 和 revision；
3. 提升协议或实现 revision；
4. 重跑受影响的 P0/pilot；
5. 原 revision 结果保留，不混合汇总。

### 8.4 统一入口

```bash
bash scripts/run_parallel_v2.sh preflight
bash scripts/run_parallel_v2.sh protocol
bash scripts/run_parallel_v2.sh freeze
bash scripts/run_parallel_v2.sh start
bash scripts/run_parallel_v2.sh status
bash scripts/run_parallel_v2.sh resume
bash scripts/run_parallel_v2.sh report
```

`start` 只创建/恢复 A、B 两个持久 runner；`resume` 必须先校验 checkpoint，不得从头覆盖。

## 9. 数据划分与防止后验调参

### 9.1 A 线数据

从现有公开 source events 中按固定 seed 做可行性筛选，优先不重新下载数据：

- `pilot`：每个 oracle-best route 至少 2 groups，用于实现和发现工程错误；
- `final`：每个 oracle-best route 至少 10 groups，总计至少 50 groups；
- 同一个 source event 不得跨 pilot/final；
- final 生成后立即冻结 public/private manifest 和哈希。

每个 source group 优先生成共享问题但证据可用性不同的配对 counterfactual，使 route 差异来自 visibility、freshness、geometry validity、reachability 和 cost，而不是对象类别或问题类型混杂。生成后执行：

```text
source/scene/group 去重
五路线 loss 全枚举
public-identical/private-different 冲突检测
route loss margin 分布
按 predicate/object/scene 的 route 支持交叉表
```

final 的 route-balanced 采样是动作覆盖压力测试，不代表自然环境中的路线流行率。报告必须同时给出：

1. 预注册 balanced macro 指标；
2. 按候选 source population route 分布重加权的指标；
3. 未加权原始计数与每个权重的来源。

不得把平衡测试集上的 route 频率表述为真实部署频率。

pilot 完成后、final 生成前做一次功效审计：根据 paired pilot 的准确率差和成本差方差，计算在 `alpha=0.05`、power `>=0.8` 下检验 2 个百分点非劣界和 20% 成本下降所需的 group 数。`N_final=max(50,N_power)` 并写入不可覆盖 manifest。若所需规模超出服务器可承受范围，停止并将 A 线标为 `failed_scientific`、原因记为 `inconclusive_underpowered`，不得先看 final 再追加样本。

若现有事件不能为某个动作提供至少 10 个可识别 group，停止在 `failed_scientific`，先生成新的不重叠 source events，不能删除难例来凑平衡。

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

C 线使用与 A final 同 schema 的独立 integration holdout；可以共享 source generator，但不得共享同一 `group_id`。同样采用 inference/evaluation 两阶段封存，禁止在 C holdout 上重新标定 confidence、route threshold 或 abstain penalty。

## 10. A 线：GT 五路路由

### A0：GT provider 与 route oracle

**目标**：只替换证据提供端，不改变问题、成本和 evaluator 定义。

新增：

```text
trust3d/agents/gt_evidence_provider.py
trust3d/data/build_five_route.py
trust3d/eval/build_route_oracle.py
configs/five_route_gt_v1.json
tests/test_gt_evidence_provider.py
```

private `oracle_best_route` 按“满足误差约束的最低真实成本”计算，并同时保存全部五路反事实 loss。相同成本只为确定性序列化使用固定顺序；若 loss margin 小于协议阈值，必须标记 `near_tie`，不能把 tie-break 后的标签当作强监督。所有 private route 字段只进入 evaluator。

命令：

```bash
bash scripts/run_five_route_gt.sh prepare
```

产物：

```text
data/episodes/parallel_v2/gt5/pilot_public.jsonl
data/episodes/parallel_v2/gt5/pilot_private.jsonl
data/episodes/parallel_v2/gt5/final_public.jsonl
data/episodes/parallel_v2/gt5/final_private.jsonl
outputs/parallel_v2/gt_five_route/prepare.json
```

通过条件：public/private ID 一一对应、public 无禁止字段、五类 final support 均不少于 10 groups。

### A1：五路合成契约测试

为每条路线建立至少 4 个合成 case，并包括：

- 当前可见与不可见；
- history fresh/stale；
- 需要属性事实与需要三维关系；
- reobserve 可达/不可达；
- 高风险但成本过高；
- 缺失证据和错误置信度；
- route 成本平局。

命令：

```bash
bash scripts/run_five_route_gt.sh unit
```

通过条件：所有合成 case 的 route、答案、成本和 abstain 行为完全匹配预注册期望。

### A2：pilot 路由冻结

只允许在 pilot 上完成：

1. schema/单位修正；
2. 数值稳定性修正；
3. 协议允许的 confidence/calibration 实现参数选择；风险、成本、非劣界和验收门槛不得改变；
4. 对五路信息可识别性、优化可识别性和近似平局比例的检查；
5. confidence/risk mapping 的冻结，但不得使用 final 或 integration 数据。

禁止在 pilot 上选择能直接利用 `oracle_best_route` 的规则。完成后生成：

```text
outputs/parallel_v2/gt_five_route/router_lock.json
```

其中保存 router 代码哈希、配置哈希、pilot manifest、阈值和 route enum。生成后不得覆盖，只能以新 revision 重开完整实验。

命令：

```bash
bash scripts/run_five_route_gt.sh pilot
```

### A3：final 离线路由

最小 baseline：

```text
always_current
always_history
always_3d_memory
always_reobserve
always_abstain
legacy_two_route
trust3d_five_route
```

命令：

```bash
bash scripts/run_five_route_gt.sh offline
```

主要指标：

- 全部问题准确率；
- answered accuracy、coverage、selective error；
- stale-memory error；
- unnecessary-reobserve rate；
- false-abstain 和 unsafe-answer rate；
- move/new observation/VLM/geometry/time 成本；
- 按 route、branch、predicate、group 分层结果；
- publicly ambiguous、near-tie 与 identifiable 子集分层；
- 相对 private 反事实表的 route regret，而非只报 route label accuracy；
- 10,000 次 paired group bootstrap；
- accuracy-cost Pareto 与 risk-coverage 曲线。

### A4：AI2-THOR 在线执行

只对 A3 冻结的 `trust3d_five_route` 和必要极端 baseline 在线运行。每个 group 独立 checkpoint，使用现有 Xvfb/llvmpipe 与 shortest-visible-pose planner。

命令：

```bash
bash scripts/run_five_route_gt.sh online
```

在线验证必须检查：

1. 离线选定 route 与在线执行 route 一致；
2. `REOBSERVE` 实际移动成本等于 planner 记录；
3. 只有达到可见阈值的新观测才能更新事实；
4. `USE_CURRENT_VIEW` 不发生额外移动；
5. `ABSTAIN` 不生成答案；
6. memory 更新和失效链可回放；
7. 中断恢复不重复执行已完成 group。

### A5：A 线验收

全部满足才标记 `complete`：

1. 五个动作在 identifiable final 子集中各有至少 10 个 oracle support；ambiguous/near-tie 比例完整报告；
2. 主方法对五个动作均至少有一次 true-positive 选择，且不存在某动作在有明确 oracle support 时产生 100% avoidable regret；否则只能声称“受限动作集有效”，不能声称完整五路通过；
3. `coverage >= 0.75`，answered selective error 不高于 `0.05`；
4. 相对 `always_reobserve` 的配对准确率差点估计不低于 -2 个百分点，且 95% CI 下界不低于预注册非劣界；
5. 相对 `always_reobserve` 的平均移动或新观察成本下降点估计至少 20%，且 95% CI 下界大于 0；
6. stale-memory error 低于 `always_history` 和旧 `legacy_two_route` 的无条件信任子集，并报告配对 CI；
7. false-abstain rate 不高于 5%，unsafe-answer rate 不高于协议风险阈值；
8. route regret、per-route recall、paired bootstrap、功效审计、恢复校验和 public/private 审计全部通过；
9. 在线结果方向与离线结果一致，真实执行成本与离线成本账本一致；
10. 原 Gate 0--7 哈希不变。

CI 下界未达到门槛但点估计达到时，状态标为 `failed_scientific`，原因写为 `inconclusive_interval`；不得用“趋势正确”替代通过。

任一统计门槛失败标记 `failed_scientific`，保留完整结果，不在 final 上调阈值重跑。

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

须新增并由 P0 测试约束的 runner：

```text
scripts/run_parallel_v2.sh
scripts/run_five_route_gt.sh
scripts/run_gate7_fix.sh
scripts/run_parallel_integration.sh
```

`scripts/run_parallel_v2.sh` 至少支持：

```text
preflight
protocol
freeze
start
status
resume
report
```

`start` 的逻辑：

1. 获取 orchestrator `flock`；
2. 校验 P0、`code_lock.json`、基线哈希与 Git commit；工作树存在未审阅代码改动时拒绝启动；
3. 检查是否已有 A/B/simulator/private evaluator runner，禁止重复；
4. 创建或复用 `parallel-a-gt5`；
5. 创建或复用 `parallel-b-gate7`；
6. A 线以 CPU-only 启动；
7. B 线如无 GPU 则进入 watcher；
8. 创建状态监视窗口；
9. 任一线失败只停止该线，另一线继续，但共同协议失败必须停止两线；
10. A/B 都完成后由 orchestrator 判断是否满足 C 线准入，不自动降低门槛。

### 13.2 日志格式

每次阶段执行必须记录：

```text
开始时间
结束时间
主机
tmux session/window
工作目录
完整命令
日志路径
Git commit
Git worktree dirty/clean 状态
配置与输入 manifest SHA256
public/private access audit
资源快照
退出码
下一恢复点
```

### 13.3 自动报告

最终生成：

```text
Trust3D_GT五路路由实验报告.md
Trust3D_Gate7修复实验报告.md
Trust3D_GT五路与RGB几何联合实验报告.md
```

报告必须区分：

```text
已证明
条件式证据
未证明
工程失败
科学失败
后验 oracle 诊断
主结果
```

## 14. 并行期间的变更所有权

为了避免两个 runner 相互覆盖：

| 路径 | 所有者 |
|---|---|
| `trust3d/agents/evidence.py`、schema、共同 evaluator | P0；冻结后只读 |
| `trust3d/agents/five_route.py`、`build_five_route.py` | A 线 |
| `trust3d/geometry/*gate7*`、`rgb_grounding.py`、`camera_contract.py` | B 线 |
| `outputs/parallel_v2/gt_five_route/` | A runner |
| `outputs/parallel_v2/gate7_fix/` | B runner |
| `outputs/parallel_v2/integration/` | C runner |
| 旧 `outputs/gate*`、`outputs/plan2`、`outputs/gate7_diagnosis` | 无所有者，只读 |

如 A、B 都要求修改 P0 文件，必须暂停两个 runner，提升 protocol revision，重新跑 P0；不得并发写同一文件。

## 15. 失败处理与继续规则

| 情况 | 处理 |
|---|---|
| tmux 客户端断开 | runner 继续，日志和 checkpoint 不受影响 |
| 服务器重启 | 重新进入 `enter_trust3d.sh`，执行 `run_parallel_v2.sh resume` |
| A 单 group simulator 失败 | 保存失败 checkpoint；其他 group 可继续；报告失败率 |
| B GPU 忙 | `waiting_for_resource`，持续安全轮询 |
| B OOM/CUDA 错误 | 保存失败配置和 traceback；释放本任务资源；同 revision 不改参数 |
| checkpoint 哈希不匹配 | 标记损坏并停止该阶段，不覆盖 |
| public/private 泄漏 | 整条线立即 `failed_engineering`，相关结果作废 |
| pilot 功效不足且所需 final 超资源预算 | `failed_scientific`，原因 `inconclusive_underpowered` |
| final/integration 中途要求改代码或阈值 | 终止当前 revision，保留产物；新 revision 必须从 P0/pilot 重开 |
| final 门槛未过 | `failed_scientific`，保留结果，不在 final 上调参 |
| A 通过、B 失败 | 只发表/报告 GT 控制器上界，不进入 C |
| B 通过、A 失败 | 只报告感知改进，不声称完整路由成立 |
| C 失败 | 分别保留 A/B 结论，明确集成耦合尚未解决 |

## 16. 最小执行顺序

```bash
# 0. 按第 6、10、11、13 节实现 runner/source/tests，完成 code review
bash scripts/run_parallel_v2.sh preflight
bash scripts/run_parallel_v2.sh protocol

# 0.1 冻结代码、schema、pilot/final 划分和Git提交
bash scripts/run_parallel_v2.sh freeze

# 1. 同时启动 A/B
bash scripts/run_parallel_v2.sh start

# 2. 随时查看，不重复启动
bash scripts/run_parallel_v2.sh status

# 3. 中断或重启后恢复
bash scripts/run_parallel_v2.sh resume

# 4. A/B 分别完成后生成分报告
bash scripts/run_five_route_gt.sh report
bash scripts/run_gate7_fix.sh report

# 5. 仅在准入通过后执行 C
bash scripts/run_parallel_integration.sh resume

# 6. 总报告
bash scripts/run_parallel_v2.sh report
```

## 17. 最终决策表

| A 线 | B 线 | C 线 | 最终允许结论 |
|---|---|---|---|
| 通过 | 未通过 | 不运行 | idea 在可靠证据下可行；RGB 几何仍是瓶颈 |
| 未通过 | 通过 | 不运行 | 感知改善，但五路控制器设计未被验证 |
| 通过 | 10--20 pp 受限通过 | 通过 | 完整系统在现实设置下有条件成立，明确 backbone gap |
| 通过 | `<=10 pp` 通过 | 通过 | 可以作为完整 RGB 五路 Trust3D 主结果 |
| 通过 | 通过 | 未通过 | 两模块各自有效，但置信度/路由耦合导致集成失败 |
| 未通过 | 未通过 | 不运行 | 当前实现和完整 idea 均未获得新增支持 |

## 18. 完成清单

### 共同协议

- [ ] 原基线 manifest 和 SHA256 已冻结
- [ ] EvidencePacket schema 已实现
- [ ] 五路动作和成本语义已冻结
- [ ] 信息可识别、优化可识别和 near-tie 审计通过
- [ ] public/private 泄漏测试通过
- [ ] code lock、Git commit 和最小 `.gitignore` 白名单已冻结
- [ ] P0 checkpoint 完成

### A 线

- [ ] 五类 route 均有足量、可识别 final groups
- [ ] pilot 功效审计与 final N 已在揭示 final 前冻结
- [ ] GT provider 与 private route oracle 分离
- [ ] 五路合成测试通过
- [ ] pilot router lock 已冻结
- [ ] final 离线结果完成
- [ ] AI2-THOR 在线结果完成
- [ ] checkpoint recovery 和 bootstrap 完成
- [ ] `Trust3D_GT五路路由实验报告.md` 已生成

### B 线

- [ ] legacy30 回归锁定
- [ ] 任务头/坐标契约测试通过
- [ ] RGB-only grounding 与几何阶段已分离
- [ ] adapter pilot 与 holdout 不重叠
- [ ] adapter lock 已冻结
- [ ] confidence lock 已冻结且只使用 pilot
- [ ] CUT3R/VGGT holdout 前向完成
- [ ] predictions 已在 private evaluator 打开前封存
- [ ] private leak 和 geometry failure 均为 0
- [ ] CPU checkpoint recovery 完成
- [ ] `Trust3D_Gate7修复实验报告.md` 已生成

### C 线

- [ ] A/B completion checkpoint 均有效
- [ ] integration lock 已冻结
- [ ] provider schema 回归通过
- [ ] RGB 五路 integration holdout 完成
- [ ] integration predictions 已封存且未重新校准
- [ ] accuracy-cost-risk 验收完成
- [ ] `Trust3D_GT五路与RGB几何联合实验报告.md` 已生成
- [ ] 所有旧实验哈希保持不变

## 19. 本计划的核心防错原则

1. **先解耦再并行**：共同接口冻结后，A 验证 controller，B 验证 perception。
2. **GT 只定义上界**：GT 五路成功不能替代 RGB 联合实验。
3. **旧 30-group 不再用于新主张调参**：它只保留回归和后验诊断资格。
4. **每次只改变一层**：任务头、grounding、camera/coordinate、backbone 分层冻结。
5. **先封存再评价**：holdout inference 看不到 private GT，预测哈希冻结后 evaluator 才能打开真值。
6. **并行不争写**：共享 schema、simulator 和 private evaluator 都有唯一锁与所有者。
7. **失败也是结果**：不降低门槛、不删除难例、不把缺失输出填成默认答案。
8. **最终必须汇合**：A、B 的独立成功只有经过 C 线才能支持完整 Trust3D 五路系统。
