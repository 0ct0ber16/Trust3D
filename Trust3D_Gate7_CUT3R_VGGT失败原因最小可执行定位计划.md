# Trust3D Gate 7：CUT3R 与 VGGT 低准确率原因最小可执行定位计划

> 文档性质：后验诊断计划，尚未执行
>
> 版本：v2（2026-07-30 完备性审阅后覆盖修订）
>
> 目标：在不修改原 Gate 7 和方案 2 主结果的前提下，分离并定位“对象 grounding、相机/坐标适配、几何 backbone、donor/target 绑定、空间答案任务头”对低准确率的贡献
>
> 固定比较对象：CUT3R 56.11%、VGGT 45.00%、GT/RGB-D 100.00%
>
> 诊断输出根目录：`outputs/gate7_diagnosis/`

## 0. 结论先行

本计划采用一条最小但能够完成因果定位的路径：

1. **先完成最小诊断实现，再冻结原结果与诊断协议**，避免协议锁中的代码哈希在实现阶段立即失效。
2. **先运行纯 CPU 合成契约测试**，验证旋转、外参方向、坐标轴符号、相机前方定义、donor/target 绑定和任务答案规则。
3. **复用现有 checkpoint 做 CPU 离线审计**，先检查任务头、输入角色、相机矩阵和当前预测，不重新运行模型。
4. **每个 backbone 只加载一次模型并完成一轮可恢复的 GPU 数据遍历**，先验证固定 3-group pilot，成功后在同一进程中续跑剩余 27 groups；同一份 dense geometry 同时计算 `center crop`、GT bbox 和 GT mask。
5. **用固定的 2×2 因果矩阵**比较 `center/GT-mask × predicted/oracle-aligned pose`；若任务头契约失败，则不增加 GPU 前向，条件式扩展为 `grounding × pose × legacy/contract-valid task head`。
6. **以 group 为统计单位进行配对 cluster bootstrap**，保留同一 group 内全部问题和全部变体的配对关系，按预注册阈值输出“已证实 bug、grounding 主导、相机主导、backbone 主导、任务头错误或混合原因”。

所有新分数都标记为 `diagnostic_only=true`。即使某个 oracle 干预得到高分，也不能替换已发布的 CUT3R 56.11% 或 VGGT 45.00%，更不能据此把原 Gate 7 改成通过。

本版相对初稿补齐了六个会影响执行结论的缺口：实现前锁协议、任务头修正后无法复现原基线、pilot/full 重复推理、固定 60 GiB GPU 门槛过度保守、负向 oracle 效应容易被误称为贡献，以及文件原子重命名但目录未 `fsync` 的恢复漏洞。

## 1. 当前证据与必须回答的问题

### 1.1 已知事实

| 项目 | CUT3R | VGGT |
|---|---:|---:|
| Trust3D 主准确率 | 56.11% | 45.00% |
| 相对 GT 下降 | 43.89 个百分点 | 55.00 个百分点 |
| 30-group 几何失败率 | 0 | 0 |
| 私有数据进入主适配器 | 否 | 否 |
| 峰值 GPU allocated bytes | 3,722,003,968 | 6,942,184,960 |
| 已有主 grounding | 12% center crop | 12% center crop |

补充事实：

- GT 与 RGB-D 参考均为 100%。
- VGGT 相对 CUT3R 的配对差为 -11.11 个百分点，group bootstrap 95% CI 为 [-21.39, -0.83] 个百分点。
- VGGT 的 12% crop purity 中位数为 0，但高 purity 四分位没有更高准确率，因此不能只凭 purity 把失败全部归因于 grounding。
- 当前 90 个 `front_behind` 标签全部为 `behind`。
- Trust3D-CUT3R 在这 90 题中输出 23 次 `behind`、67 次 `front`；Trust3D-VGGT 输出 10 次 `behind`、80 次 `front`。
- 当前合成测试只覆盖单位姿态、一次平移和一次对象交换，尚未覆盖非零 yaw/pitch、外参逆矩阵、轴符号和完整的答案真值表。

### 1.2 代码审计发现的最高优先级疑点

数据构建使用 `trust3d.geometry.egocentric.spatial_labels`：

- `left_right` 和 `front_behind` 由世界平面上的 query yaw 决定；
- query 的 `horizon=30°` 不参与前后标签；
- 距离使用完整三维欧氏距离。

当前 Gate 7 任务头先用完整的 4×4 `camera_to_world` 把目标转换到相机坐标，再用 `target_camera[2]` 的符号判断前后。完整相机旋转可能包含 pitch/roll，而数据标签明确使用平面 yaw。该差异是一个**必须优先验证的任务语义疑点**，但在合成契约测试通过或失败之前不能直接称为 bug。

### 1.3 必须回答的六个问题

1. CUT3R/VGGT 输出的外参究竟是 `camera_to_world` 还是 `world_to_camera`，当前代码是否正确求逆？
2. 模型相机坐标中的 x/y/z 与数据集的 right/up/forward 是否具有一致方向和符号？
3. 输入序列的 0/1/2/3/4 是否始终绑定为历史 target、历史 donor、query、当前 target、当前 donor？
4. 在 GT 点和 GT query pose 下，当前任务头能否 360/360 复现数据标签？
5. 固定 backbone 与相机后，把 center crop 换成 GT mask 能提高多少准确率？
6. 固定 grounding 后，把预测相机换成 oracle-aligned pose 能提高多少；剩余差距是否来自 backbone 的点/深度？

## 2. 诊断边界与不可违反规则

### 2.1 原结果只读

以下路径必须只读：

- `outputs/gate7/`
- `outputs/plan2/`
- `data/episodes/spatial30/`
- `external/cut3r/`
- `external/vggt/`

所有新文件只能写入：

- `outputs/gate7_diagnosis/`
- `/224010104/Jerry/checkpoints/gate7_diagnosis/`
- `/224010104/Jerry/logs/gate7-diagnosis/`
- 本计划明确列出的新代码、测试和配置文件。

runner 必须在启动时对输出目录执行 `resolve()` 检查；若目标位于 `outputs/gate7`、`outputs/plan2` 或数据目录下，立即退出 125。

### 2.2 私有信息隔离

诊断分成两个进程边界：

1. **公开主链路/基线复现**：只读取 RGB、固定 routes、模型和公开 episodes，不读取 oracle、GT depth、instance 或 mask。
2. **后验诊断链路**：协议锁完成后可以读取 GT pose、RGB-D、mask 和 oracle，但输出必须带：

```json
{
  "diagnostic_only": true,
  "qa_revealed": true,
  "uses_gt_mask": true,
  "eligible_as_main_result": false
}
```

模型权重、参数和选择规则不得根据 oracle 答案更新。GT mask 只用于测量上界和误差归因，不得成为新的 RGB-only 主方法。

### 2.3 不进行评测集调参

- 不在当前 30 groups 上搜索 crop 大小、置信度阈值或答案阈值。
- 不按 QA 准确率挑选相机轴变换。
- 相机约定只能由模型 API、合成契约和标签无关的 pose 误差确定。
- 任何实现修正必须先在合成测试中被证明，再在真实 checkpoint 上确认。
- 修正后的 30-group 分数仍是后验诊断分数；若要成为新主结果，必须另建冻结协议和独立数据。

## 3. 假设树与证据标准

| 编号 | 假设 | 可证伪证据 | 可确认到什么程度 |
|---|---|---|---|
| H1 | 外参方向或矩阵求逆错误 | 合成 pose round-trip、API mock、`T_cw @ T_wc` | 可证明实现 bug |
| H2 | 坐标轴符号/相机 forward 定义错误 | yaw/pitch 与已知世界点的真值表、OpenCV/OpenGL 基变换 | 可证明实现 bug |
| H3 | donor/target 或帧索引交换 | manifest 路径、角色不变量、交换变形测试 | 可证明绑定 bug |
| H4 | 空间答案任务头语义错误 | GT 点 + GT pose 不能复现 360 个标签；平面 yaw 与完整 3D 相机结果不一致 | 可证明任务接口 bug |
| H5 | center crop grounding 失效 | GT-mask 干预显著提高准确率和点误差 | 可确认主要贡献，但 GT mask 不是可部署方案 |
| H6 | 相机估计质量不足 | 固定 grounding 后 oracle-aligned pose 显著提高准确率 | 可确认主要贡献 |
| H7 | backbone 点/深度质量不足 | GT mask + oracle pose 后仍明显弱于 RGB-D | 可确认剩余主要贡献 |
| H8 | 数据标签不平衡放大现象 | 平衡合成集通过而真实 `front_behind` 单类；margin 分层不稳定 | 只能限制外推，不能抹掉主结果 |

“已证明 adapter bug”的最低标准是：

1. 当前实现违反一个不依赖真实 QA 标签的确定性契约；
2. 单一修正能让该契约全部通过；
3. 修正与模型官方 API 或数据标签定义一致；
4. 不是在 30-group QA 上从多个候选中挑出的最高分变换。

仅凭某个轴翻转让准确率升高，不能称为已证明 bug。

## 4. 最小因果设计

### 4.1 主诊断阶梯

每个 backend 都生成以下固定变体。`B0-B3` 始终使用发布 Gate 7 时的 legacy 任务头，以便解释原结果：

| 代号 | 点/深度 | grounding | query pose | 任务头 | 用途 |
|---|---|---|---|---|---|
| B0 | backend | center crop | predicted | 当前实现 | 必须复现原基线 |
| B1 | backend | GT mask | predicted | 当前实现 | grounding 干预 |
| B2 | backend | center crop | oracle-aligned | 当前实现 | 相机干预 |
| B3 | backend | GT mask | oracle-aligned | 当前实现 | grounding + 相机联合干预 |
| R0 | RGB-D | GT mask | GT | 数据定义任务头 | 几何参考上界 |
| TL | GT object point | 不适用 | GT | legacy 当前任务头 | 检查发布任务头是否符合标签契约 |
| T0 | GT object point | 不适用 | GT | 数据定义任务头 | 任务头/数据接口真值 |

GT bbox 同时计算，但只作为 center 与 GT mask 之间的辅助诊断，不进入主 2×2 判定。

若且仅若 D1/D2 用标签无关契约证明 legacy 任务头错误，额外在 CPU 上生成 `C0-C3`：其点、grounding 和 pose 分别与 `B0-B3` 完全相同，只把任务头替换为带新版本号的 contract-valid 实现。若 legacy 任务头通过，`C0-C3` 直接作为 `B0-B3` 的别名，不重复保存。这样既能保持 `B0` 精确复现原结果，也能在修正任务语义后继续分离 grounding、pose 和 backbone，而不把任务头修正偷偷混入原基线。

### 4.2 2×2 主效应

令 `V0-V3` 表示 contract-valid 任务头下的 2×2：存在已证明任务头修正时取 `C0-C3`，否则取 `B0-B3`。对每个 backend 计算：

```text
grounding_effect = ((V1 - V0) + (V3 - V2)) / 2
pose_effect      = ((V2 - V0) + (V3 - V1)) / 2
interaction      = V3 - V2 - V1 + V0
backbone_gap     = R0 - V3
task_head_gap    = T0 - TL
task_head_effect = mean(Ci - Bi), i in {0,1,2,3}  # 仅在任务头修正已被证明时
```

这里的减法均指 group-level accuracy 的配对差。除点估计外，必须报告 10,000 次 group cluster bootstrap 的 95% CI，seed 固定为 `20260730`。每次重采样以 group 为单位，同时携带该 group 的全部 12 个问题和全部变体，不能把 360 个问题当成独立样本。

所有效应保留符号，不截断为 0。负值表示该 oracle 干预在当前链路中反而降低准确率，只能触发适配、交互或小样本复核，不能写成“负贡献”。bootstrap CI 仅用于描述这 30 groups 上的不确定性，不作总体显著性或多重检验声明。

### 4.3 为什么这是最小设计

- 合成和离线阶段不使用 GPU。
- center、GT bbox、GT mask 在同一次 dense 输出中提取，不重复模型前向。
- B0-B3 只重组同一份点、相机和角色，不再次运行模型。
- RGB-D 与 GT 已存在，不新增模拟器采集。
- 2×2 能分离 grounding、pose 和交互；R0/TL/T0 能进一步分离 backbone、legacy 任务头与独立数据定义参考。
- 条件式 `2×2×2` 只在已证明任务头 bug 时用同一批摘要做 CPU 重组，不增加模型前向，也不会牺牲原基线复现。
- 不引入 detector、tracker、训练或新 backbone，因为这些属于后续改进，不是原因定位的最低必要条件。

## 5. 最小代码与产物范围

### 5.1 必要代码

只允许新增或最小修改以下文件：

| 文件 | 作用 |
|---|---|
| `configs/gate7_failure_diagnosis.json` | 冻结输入、模型、选择器、阈值、seed 和资源门槛 |
| `tests/test_gate7_geometry_contract.py` | 合成坐标、角色和任务语义契约测试 |
| `trust3d/geometry/diagnostic_grounding.py` | 对同一 dense map 计算 center/GT-bbox/GT-mask 点摘要 |
| `trust3d/eval/diagnose_gate7_layers.py` | 离线 pose、任务头、2×2、bootstrap 和归因 |
| `scripts/run_gate7_failure_diagnosis.sh` | 唯一阶段入口、资源检查、日志和恢复 |
| `trust3d/geometry/run_cut3r.py` | 仅增加默认关闭的诊断 hook |
| `trust3d/geometry/run_vggt.py` | 仅增加默认关闭的诊断 hook |

不得复制一套新的 CUT3R/VGGT 模型实现。两个 runner 的默认参数和原输出格式必须保持不变；诊断 hook 只有同时指定独立输出根和 `diagnostic_only` 配置时才启用。

### 5.2 输出结构

```text
outputs/gate7_diagnosis/
├── status.json
├── protocol_lock.json
├── protocol_locks/<revision>.json
├── access_audit.json
├── baseline_reproduction.json
├── synthetic_contract.json
├── offline_audit.json
├── role_binding_audit.json
├── pose_audit.json
├── task_head_audit.json
├── cut3r/
│   ├── checkpoints/<group_id>.json
│   └── manifest.json
├── vggt/
│   ├── checkpoints/<group_id>.json
│   └── manifest.json
├── factorial_predictions.jsonl
├── factorial_metrics.json
├── attribution.json
├── checkpoint_recovery.json
└── final_diagnosis.json
```

最终解释性报告建议输出为：

`Trust3D_Gate7_CUT3R_VGGT失败原因诊断报告.md`

### 5.3 P0：最小实现与锁前检查

D0 不能在诊断代码尚未创建时执行。先完成本计划 5.1 中的最小文件和默认关闭 hook，再执行：

```bash
bash scripts/run_gate7_failure_diagnosis.sh prepare
```

`prepare` 只允许做 import、JSON schema、路径边界、公开/私有模式文件访问白名单和 shell 静态检查，不读取私有答案，不运行模型，不产生实验分数。它必须验证：

- 默认 runner 行为与原 CUT3R/VGGT 输出格式不变；
- 所有输出路径经 `resolve()` 后仍位于允许目录；
- public 模式的允许读集合不含 `oracle_private.jsonl`、GT depth、instance 或 mask；
- private diagnostic 模式的全部输出自动带后验诊断标记；
- 唯一 workspace runner 锁位于 `/224010104/Jerry/checkpoints/gate7_diagnosis/runner.lock`；
- 配置、脚本和待测代码已存在，D0 可以对最终实现哈希。

P0 是工程准备，不得生成或查看 QA 效果；完成后才能冻结 revision `r000`。

## 6. D0：冻结基线和诊断协议

### 6.1 执行内容

1. 检查 CPU、内存、磁盘和 GPU 状态。
2. 确认 Git 工作树干净，记录 `HEAD`。
3. 固定现有七项 Gate 7 哈希：
   - `outputs/gate7/validation.json`
   - `outputs/gate7/predictions.jsonl`
   - `outputs/gate7/checkpoint_recovery.json`
   - `outputs/gate7/cut3r_geometry/manifest.json`
   - `data/episodes/spatial30/episodes_public.jsonl`
   - `data/episodes/spatial30/oracle_private.jsonl`
   - `outputs/gate6/routes.jsonl`
4. 固定方案 2 的 `final_decision.json`、`vggt_validation.json` 和 `vggt_predictions.jsonl` 哈希。
5. 固定 CUT3R/VGGT 权重、代码、配置和 P0 已完成诊断实现的 SHA256。
6. 写入 `qa_revealed=true`、`diagnostic_only=true`、`original_results_mutable=false`。
7. 写入数据集、group 列表、pilot group、所有决策阈值、资源阈值、软件版本和随机性设置。

当前必须保持一致的关键哈希包括：

| 文件 | SHA256 |
|---|---|
| `outputs/gate7/validation.json` | `2c640c1279c7a4724ca60287e6f3ec9f943416ce2964018d2d8397e280fe94d5` |
| `outputs/gate7/predictions.jsonl` | `8af0a03e9f4ac956dbcd5f859eb093fc5197d1c581b779eb8367c185a5df40fc` |
| `outputs/plan2/final_decision.json` | `d36a8bc74de8fc4cc0ac34cce715526f6eb5bb6d9514fece98a15a82041e897a` |
| `outputs/plan2/vggt_validation.json` | `13759a89a66b89ea129e94ef638605f28c575780ca9d9ea53a0c8f4b794cb98b` |
| `outputs/plan2/vggt_predictions.jsonl` | `27ccbf98703eba4d48217747d86695cc8a9cc4e79ea25f314cb324eedc9df3f8` |

### 6.2 命令

```bash
bash scripts/run_gate7_failure_diagnosis.sh lock
```

### 6.3 验收

- `protocol_lock.json` 指向的不可变 revision 文件完整，其自身 SHA256 和 revision 内全部哈希验证通过。
- 原结果目录无新增、修改或删除。
- 输出根严格为 `outputs/gate7_diagnosis/`。
- 未满足任一条件立即停止，不能进入 D1。

协议锁采用只增不改的 revision：`protocol_locks/r000.json` 保存完整内容，`protocol_lock.json` 只记录当前 revision 及其 SHA256。若 D1 证明实现 bug，必须先保存该 revision 的失败 fixture 和结果，再以新 adapter/task-head version 生成 `r001`；不得覆盖旧 revision，不得改变 pilot、阈值、数据或根据 QA 选择修正。每次后续 checkpoint 都记录自己的 protocol revision，混用 revision 时立即失败。

## 7. D1：合成坐标、角色和任务头契约

### 7.1 相机旋转真值表

创建不依赖模型和真实 QA 的解析场景：

| case | query yaw | query horizon | 世界中的“前方”点 | 预期相机方向 |
|---|---:|---:|---|---|
| Y0 | 0° | 0° | `camera + (0,0,+d)` | `forward > 0` |
| Y90 | 90° | 0° | `camera + (+d,0,0)` | `forward > 0` |
| Y180 | 180° | 0° | `camera + (0,0,-d)` | `forward > 0` |
| Y270 | 270° | 0° | `camera + (-d,0,0)` | `forward > 0` |
| P+30 | 0° | +30° | 平面前方目标 | 数据标签仍由 yaw 决定 |
| P-30 | 0° | -30° | 平面前方目标 | 数据标签仍由 yaw 决定 |

同时测试每个 yaw 下的右、左、后方向，距离取 1、2、5 三档，避免所有点落在单一象限。

### 7.2 外参方向与 round-trip

必须覆盖：

1. `T_camera_to_world` 与 `T_world_to_camera` 互逆。
2. `point_world -> point_camera -> point_world` 最大绝对误差不超过 `1e-6`。
3. 旋转矩阵满足 `R.T @ R = I`，误差不超过 `1e-6`。
4. `det(R)` 与 1 的误差不超过 `1e-6`。
5. 平移、yaw、pitch 组合时乘法顺序正确。
6. 用 mock CUT3R `pose_encoding_to_camera` 和 mock VGGT extrinsic 验证 adapter 对官方返回约定的解释。

### 7.3 坐标轴符号

预注册并验证以下基变换，而不是根据真实 QA 分数挑选：

- identity；
- OpenCV 与 OpenGL 常见转换 `diag(1,-1,-1)`；
- 数据集 right/up/forward 与模型 camera x/y/z 的文档映射；
- 由官方 API 明确要求的其他固定变换。

对八个象限中的点检查：

- x 正负只改变 left/right；
- forward 正负只改变 front/behind；
- 同一个全局 SE(3) 同时作用于相机和点后，camera-local 坐标与答案保持不变；
- 正比例缩放所有相对坐标后，方向标签和距离次序保持不变。

### 7.4 donor/target 绑定与交换变形

固定输入索引契约：

| index | 角色 |
|---:|---|
| 0 | historical target |
| 1 | historical donor |
| 2 | query |
| 3 | current target |
| 4 | current donor |

测试要求：

- checkpoint 中的路径、SHA256、scenario、index 与 source context 一致。
- target 与 donor 交换后，距离不相等时 `target_nearer` 必须取反。
- `which_closer` 必须在 `target/reference` 间对应交换。
- 对称构造的左右/前后点交换后，方向答案按真值表变化。
- 交换两次必须恢复原答案和原点。
- 不允许通过真实 QA 高低决定是否交换。

### 7.5 任务头语义

构造至少 32 个平衡 fixture：

- left/right 各半；
- front/behind 各半；
- target/reference closer 各半；
- `target_nearer=true/false` 各半；
- yaw 覆盖 0°、90°、180°、270° 和非直角；
- horizon 覆盖 0°、+30°、-30°；
- 包含不同高度但相同平面 bearing 的点；
- 决策边界和距离 tie 单独验证为显式拒绝或固定策略，不得静默偏向某类。

重点断言：数据定义的 `front_behind` 忽略 horizon。若完整三维 camera z 与平面 yaw 标签不同，必须把这种差异记录为 `task_semantics_mismatch`，而不是用 QA 分数决定采用哪一个。

### 7.6 命令与验收

```bash
bash scripts/run_gate7_failure_diagnosis.sh unit
```

内部必须执行：

```bash
/224010104/Jerry/.conda/envs/vggt/bin/python -m pytest -q \
  tests/test_gate7_cut3r.py \
  tests/test_plan2_vggt.py \
  tests/test_gate7_geometry_contract.py
```

验收标准：

- 所有确定性测试 100% 通过。
- round-trip、正交和行列式误差满足上述容差。
- 平衡答案 fixture 100% 正确。
- 若当前实现失败，先把最小失败 fixture、旧实现输出、堆栈和 `r000` 哈希写入不可覆盖的 revision 目录；修复必须使用新 adapter/task-head version，生成下一份协议 revision 后从 D0 重验。
- 修正只能由官方 API、数据定义或解析真值唯一决定；若存在多个等价候选而契约不能区分，结论必须为 `inconclusive`，不得查看 QA 后选择。
- D1 未通过不得启动 GPU。

## 8. D2：复用现有 checkpoint 的 CPU 离线分解

### 8.1 基线复现

只读取现有 checkpoint 和 routes，重新生成 B0 预测：

- CUT3R 必须复现 56.11%。
- VGGT 必须复现 45.00%。
- 360 个 Trust3D episode 的 answer、route、branch、question_type 必须逐条一致。
- 若无法复现，先定位 evaluator 或 checkpoint 读取差异，不能继续归因。

复现环境固定模型为 `eval()`、固定 seed、图像尺寸、dtype、置信度分位数和输入顺序，并记录 PyTorch/CUDA/cuDNN 版本。B0 的离线 checkpoint 重组要求答案逐条一致；D3/D4 的重新前向允许点坐标在预注册绝对/相对容差内浮动，但最终答案仍必须逐条一致。不得用放宽容差跨过答案决策边界。

### 8.2 任务头 oracle

执行三个不需要模型的测试：

1. `TL = GT object point + GT query pose + legacy 当前任务头`。
2. `T0 = GT object point + GT query pose + 独立数据定义任务头`。
3. `R0 = RGB-D object point + GT query pose + 独立数据定义任务头`。

独立数据定义任务头必须按 `spatial_labels`/数据构建规则重新实现解析参考，不能用同一个函数同时生成 expected 和 actual。可额外计算 RGB-D + legacy 任务头作为调试项，但不能替代 R0。

输出：

- 四类问题准确率；
- 混淆矩阵；
- yaw-only 与 full-3D-camera 的差异数；
- 决策 margin 分布；
- donor/target 交换后的变形检查。

判定：

- T0 必须为 360/360；否则是独立参考或数据接口未闭环，不能归罪于 legacy 任务头，且必须停止后续归因。
- T0 为 360/360 而 TL 低于 360/360 时，确认 legacy 任务头语义错误。
- R0 应为 360/360；若 T0 通过但 R0 不通过，定位为 RGB-D 到任务头接口、mask 或数值边界问题。
- 若只有 legacy full-3D-camera 失败、独立 yaw-only T0 通过，确认标签语义与当前三维任务头不一致。

若确认任务头不一致，保留 legacy `B0-B3`，并按 4.2 生成 contract-valid `C0-C3`；不能覆盖 legacy 预测，也不能把 `C0` 冒充原 Gate 7 复现分数。

### 8.3 角色绑定审计

对 CUT3R 和 VGGT 的 30 个 checkpoint 全量检查：

- 10 个输入记录的路径和 hash；
- stable/stale 两个五帧序列；
- index 0/1/2/3/4 的角色；
- current stable 是否正确复用历史 target/donor；
- current stale 是否使用 stale target/donor；
- evaluator 在 `trust_memory/reobserve` 下选择的 stage。

任何一条路径角色不匹配即为确定性 binding bug。

### 8.4 相机与 pose 审计

对每个 backend、group、scenario 输出：

- `R.T @ R` 最大误差和 `det(R)`；
- `camera_to_world @ world_to_camera` 残差；
- relative rotation error；
- translation direction error；
- camera center baseline 和退化标记；
- target/donor 的 cheirality；
- query-camera x/z 符号和答案 margin；
- full 3D 与 planarized heading 的答案差异。

只允许评估预注册的、由 API 定义的相机解释：

1. 当前 `camera_to_world`；
2. 将存储矩阵解释为 `world_to_camera` 后求逆；
3. 官方文档要求的 OpenCV/OpenGL 基变换。

相机解释按标签无关的 pose 指标比较，不按 QA 准确率选择。

### 8.5 命令与验收

```bash
bash scripts/run_gate7_failure_diagnosis.sh offline
```

runner 内部必须先在 public 子进程完成 B0 复现并关闭文件描述符，再启动 private diagnostic 子进程计算 TL/T0/R0 和 oracle 审计。两个子进程分别记录规范化后的实际读文件清单；`access_audit.json` 若发现 public 子进程读取私有路径，立即停止且公开复现无效。

必须生成：

- `baseline_reproduction.json`
- `task_head_audit.json`
- `role_binding_audit.json`
- `pose_audit.json`
- `offline_audit.json`

所有文件必须记录输入 hash、代码 hash、group 数、失败列表和 `diagnostic_only=true`。

## 9. D3：一次前向的 grounding 诊断 pilot

### 9.1 为什么需要重新前向

现有 CUT3R/VGGT checkpoint 只保存 center crop 的三维中位点，没有保存 dense world map。要在**完全相同的 backbone 输出**上比较 center、GT bbox 和 GT mask，至少需要重新获得一次 dense map。

不得把 dense map 全量永久保存。每个序列在显存/内存中同时计算三种选择器后，只保存以下摘要：

- selected point median、MAD、数量和置信度；
- crop/bbox/mask 像素数、purity、recall；
- query-camera 坐标；
- role、stage、frame index；
- camera matrices、intrinsics 和必要的对齐统计；
- 输入 RGB/mask/config/model/code SHA256。

### 9.2 三种选择器

| 选择器 | 是否进入因果主矩阵 | 作用 |
|---|---|---|
| `center_0.12` | 是 | 原方法 B0/B2 |
| `gt_bbox` | 否，辅助 | 判断矩形定位是否已足够 |
| `gt_mask` | 是 | grounding oracle B1/B3；若任务头修正则同时用于 C1/C3 |

三者必须使用同一份 dense points 和 confidence，不能分别运行模型。

GT bbox/mask 必须来自与 target/donor 角色及历史/当前帧逐一对应的 observation，禁止把 query mask 或另一 stage 的 mask 复用到对象帧。mask 先按原始 RGB 尺寸校验非空和目标 object id，再以 nearest-neighbor 一次映射到 dense map 尺寸；映射约定、原/目标 shape、非零像素数和 mask SHA256 均写入 checkpoint。空 mask 或角色不一致只标记该 selector 失败，不能回退到 center crop 冒充 GT mask。

### 9.3 pilot group

pilot 固定取 `sha256(group_id + seed)` 最小的 3 groups，不允许人工挑选易例或失败例。pilot 只判断工程链路，不计算原因结论。

正常路径不单独启动 pilot 进程。`gpu-all` 对每个 backend 加载模型后先运行固定 3 groups，在进程内完成工程验收，通过后直接续跑其余 27 groups。调试时允许执行 `pilot <backend>`，但其 checkpoint 必须使用与 full 完全相同的 schema 和 fingerprint，后续 full 验证后直接复用，禁止再次前向这 3 groups。

### 9.4 pilot 验收

- 每个 backend 3/3 groups 成功。
- 每个 group stable/stale 两个序列、三种 selector、target/donor 均完整。
- 所有点、pose 和 intrinsic 为有限值。
- mask resize 使用 nearest-neighbor，映射尺寸与模型输出一致。
- B0 的 center 点与原 checkpoint 在容差内一致；答案必须一致。
- GT mask 只出现在 `diagnostic_only` 产物，不写入公开基线预测。
- checkpoint 可重复读取；恢复或 full 阶段读取已通过的 pilot checkpoint 时不再次前向且哈希不变。

pilot 失败只修工程问题，不查看或优化 pilot QA 分数。

## 10. D4：双后端 30-group grounding 全量诊断

### 10.1 GPU 准入

每次启动前必须满足：

- 1 分钟 CPU load 小于逻辑 CPU 数的 75%；
- available 内存不少于 64 GiB，工作区所在文件系统可用空间不少于 100 GiB；
- CUT3R 至少有 16,384 MiB 空闲显存，VGGT 至少有 20,480 MiB 空闲显存；
- GPU utilization 不高于 10%；
- 当前工作区没有同 backend 的重复进程；
- 原 Gate 7/方案 2 哈希仍与 D0 一致。

显存门槛分别高于历史峰值 3,722 MiB 和 6,942 MiB 的两倍并额外保留约 4 GiB，兼顾安全与最小可执行性。固定 60,000 MiB 会在模型只需约 4-7 GiB 时造成不必要等待，因此不再使用。单次采样命中后只做一次无等待原子复核；复核失败立即回到 1 秒轮询，不强行启动。

### 10.2 命令

```bash
bash scripts/run_gate7_failure_diagnosis.sh gpu-all
```

`gpu-all` 由同一个持久 runner 串行执行 CUT3R 和 VGGT，禁止两个 backend 并行。每个 backend 的模型在正常路径只加载一次；从 pilot 到剩余 groups 不释放该模型。切换 backend 时允许释放前一模型并加载后一模型，这是必要计算阶段切换，不得用无效张量占住显存；切换前再做一次无等待资源复核。

若 GPU 不可用，启动唯一 watcher：

```bash
bash scripts/run_gate7_failure_diagnosis.sh watch
```

watcher 每 1 秒检查一次，命中后做一次原子准入复核；两个 backend 串行执行。不得修改已有 `scripts/watch_plan2_gpu.sh` 的状态或复用其输出目录。

共享服务器没有管理员独占调度时，runner 只能通过快速复核降低竞态，不能声称排斥其他用户。运行期间监控本进程、总显存、温度和 Xid/OOM；不得杀停、暂停或驱逐他人进程。

### 10.3 checkpoint

每个 `backend × group` 单独写 checkpoint：

1. 先写 `<group_id>.json.tmp.<pid>`；
2. 校验 schema、fingerprint、有限值、selector 数量和输入 hash；
3. 对临时文件 `fsync` 后原子重命名，并对父目录 `fsync`；
4. manifest 和 `status.json` 最后以同样方式原子更新；
5. 恢复时只跳过 fingerprint 一致且验证成功的 group。

唯一 runner 在进入任一阶段前持有 workspace `flock`；锁只防止 Jerry 自己重复启动。遗留 `.tmp.<pid>` 文件不视为完成，恢复时保留到 `failed_attempts/` 供审计后重试。

fingerprint 至少包含：

- backend 与模型权重 hash；
- RGB、mask 和 source context hash；
- adapter、诊断 selector 和配置 hash；
- public episodes、routes 和 group_id；
- image size、dtype、confidence quantile 和 seed。

### 10.4 全量验收

- CUT3R 30/30、VGGT 30/30 groups 成功。
- 每个 backend 对每个 sequence 只进行一次必要模型前向；已验证 pilot 直接计入 30 groups。
- B0 逐 episode 复现原答案；若不复现，停止，不计算 B1-B3 贡献。
- dense maps 未被长期保存，磁盘只保留摘要和少量固定 pilot overlay。
- 恢复前后 checkpoint SHA256 一致。

## 11. D5：相机 oracle 对齐与 2×2 归因

### 11.1 oracle-aligned pose 的定义

对每个 stable/stale 五帧 sequence，使用五个预测相机和一一对应的 GT 相机进行标签无关的 Sim(3) 对齐：

1. 先按 D1 已证明的 API 约定把预测 pose 统一成 `camera_to_world`，不枚举 QA 得分最高的轴变换；
2. 对每对旋转计算全局候选 `Q_i = R_gt_i @ R_pred_i.T`，对其和做 SVD 并投影到 `SO(3)`，强制 `det(Q)=+1`；
3. 对所有非零相机中心 pair 计算 `||c_gt_i-c_gt_j|| / ||c_pred_i-c_pred_j||`，scale 取固定规则的中位数且必须为正；
4. 平移取 `t = median_i(c_gt_i - scale * Q @ c_pred_i)`，并报告旋转、中心和 baseline 对齐残差；
5. 对齐不能读取 QA answer、对象点或 mask，也不能为 target/donor 分别拟合；
6. 若没有足够非零 baseline 或有限旋转，标记 `alignment_unidentifiable=true`，该 group 的 pose effect 记为缺失并单独报告，禁止直接代入 GT pose 或补 0；
7. 对齐后的模型点进入 GT world frame，再使用精确 GT query pose计算 B2/B3；若存在 contract-valid 任务头，同样计算 C2/C3。

对齐不会修复模型点的局部深度、目标选择或形变，只用于隔离 query pose/坐标误差。

### 11.2 必须计算的指标

对 B0、B1、B2、B3、R0、TL、T0 分别输出；若启用 contract-valid 任务头，再输出 C0-C3：

- 总准确率和 95% group bootstrap CI；
- 分支 × 问题类型准确率；
- 对象类型准确率，但小样本只作描述；
- front/behind、left/right 混淆矩阵；
- 决策 margin 分位数；
- target/donor 3D angular error；
- 相对距离排序准确率；
- static/movable revisit error；
- grounding purity、recall 和 3D point error；
- 失败/退化 group 列表。

所有指标同时报告有效 group 数和缺失原因。主表以全 30 groups 为目标；若 `alignment_unidentifiable` 或 selector 失败导致配对不完整，则额外给 complete-case 描述，但最终主标签降级为 `inconclusive_small_sample`，不能把缺失 group 静默删除后仍宣称主导原因。

### 11.3 donor/target 交换诊断

利用已保存的 target/donor 点在 CPU 上计算交换变体：

- 交换只用于验证角色不变量和量化“若角色绑定相反会发生什么”；
- 不进入主 2×2 矩阵；
- 即使交换后 QA 更高，若 source path 和合成契约没有证明绑定错误，也只能标记为 `posthoc_role_hypothesis`。

### 11.4 命令

```bash
bash scripts/run_gate7_failure_diagnosis.sh attribute
```

内部统计固定：

```text
bootstrap_unit = group
bootstrap_samples = 10000
bootstrap_seed = 20260730
confidence_interval = percentile_95
```

## 12. 原因判定规则

### 12.1 确定性 bug

满足以下任一项即可确认对应实现 bug：

- D1 外参 round-trip、轴符号或 API mock 失败；
- D1 donor/target 双交换不能恢复；
- D2 输入路径与角色契约不一致；
- T0 能 360/360 而 TL 不能 360/360 复现数据标签；
- yaw-only 数据定义通过而 full-3D-camera 任务头系统性失败。

bug 修复必须使用新 adapter version，原 checkpoint 和原分数保留。

### 12.2 主要贡献阈值

对 grounding、pose 或 backbone 的配对提升/差距，采用：

| 级别 | 条件 |
|---|---|
| 主要贡献 | 点估计不少于 20 个百分点，且 95% CI 下界大于 0 |
| 次要/混合贡献 | 点估计 10-20 个百分点，或 CI 跨 0 |
| 当前无强证据 | 点估计小于 10 个百分点且 CI 跨 0 |

上述“贡献”只适用于方向符合预期的正向 grounding/pose effect 和正向 backbone gap。负值不取绝对值套用此表，而标记为 `counter_directional_effect` 并检查交互、适配和样本量。“主导原因”还要求该因素的正向点估计至少比第二大正向因素高 10 个百分点；否则结论必须写为“混合原因”。

交互项绝对值不少于 10 个百分点且 95% CI 不含 0 时，标记为强交互，禁止单独解释 grounding 或 pose 主效应。当前 90 个真实 `front_behind` 全为 `behind`，因此真实集只能判断这一个标签分布下的错误，类别对称性只能由平衡合成契约支持，最终报告不得外推为一般 front/behind 能力。

### 12.3 充分性

- 若单一干预后准确率达到至少 90%，该因素可称为“在 oracle 诊断中足以解释 Gate 7 失败”。
- 若 V3 仍低于 90%，grounding 和 pose 的联合修复仍不充分。
- 若 R0/T0 为 100% 而 V3 明显偏低，剩余问题主要位于 backend 点/深度。
- 若 T0 低于 100%，必须先修独立参考或数据接口；若只有 TL 低于 100%，必须形成 contract-valid 任务头后再讨论 backbone。
- 若存在已证明任务头修正，legacy 与 contract-valid 两套结果必须并列报告；只有 contract-valid 任务头下的 V0-V3 才用于 grounding/pose/backbone 主归因，legacy B0 仍是原结果锚点。

### 12.4 最终标签

`final_diagnosis.json` 必须从以下集合选择一个主标签，并可附加次标签：

```text
proven_extrinsic_bug
proven_axis_convention_bug
proven_role_binding_bug
proven_task_head_semantics_bug
grounding_dominant
pose_dominant
backbone_geometry_dominant
mixed_grounding_pose_geometry
inconclusive_small_sample
```

CUT3R 和 VGGT 分别判定，不能因为共用下游代码就默认具有相同原因。

## 13. 最小执行顺序和停止规则

### 13.1 唯一顺序

```bash
bash scripts/run_gate7_failure_diagnosis.sh prepare
bash scripts/run_gate7_failure_diagnosis.sh lock
bash scripts/run_gate7_failure_diagnosis.sh unit
bash scripts/run_gate7_failure_diagnosis.sh offline
bash scripts/run_gate7_failure_diagnosis.sh gpu-all
bash scripts/run_gate7_failure_diagnosis.sh attribute
bash scripts/run_gate7_failure_diagnosis.sh report
```

中断或服务器重启后统一执行：

```bash
bash scripts/run_gate7_failure_diagnosis.sh resume
```

### 13.2 全局停止条件

发生以下任一情况必须停止当前阶段并保留日志：

1. 原 Gate 7 或方案 2 任一锁定哈希变化。
2. 输出路径越过 `outputs/gate7_diagnosis/`。
3. D1 确定性契约未通过。
4. B0 不能逐 episode 复现原主答案。
5. GT mask、depth 或 oracle 被公开基线进程读取。
6. checkpoint fingerprint 不一致或恢复前后哈希变化。
7. 30 groups、360 questions 或固定 routes 数量变化。
8. GPU、CPU、内存或磁盘状态可能影响服务器稳定。
9. 同一 workspace 出现重复 runner 或重复模型进程。
10. checkpoint、manifest、status 或 protocol revision 之间的 fingerprint 不一致。

停止不是删除结果；失败 group、错误堆栈和输入 fingerprint 必须保留，修复后只重试失败单元。

每个阶段开始前都重新执行 CPU、内存、磁盘和 GPU 状态记录；CPU-only 阶段不要求 GPU 空闲，但必须记录 GPU 状态。只有 GPU 阶段进入 watcher，不能因 GPU 忙碌阻塞 P0-D2。

## 14. 服务器、tmux、日志与恢复

### 14.1 tmux

固定使用：

```text
/224010104/Jerry/.conda/tools/bin/tmux
session = trust3d-<hostname>
window  = gate7-diagnosis
```

所有 Python、pytest、Git、GPU watcher 和统计命令必须在该 window 中运行。tmux 断开不能终止任务；主机重启后由 `resume` 和持久化 checkpoint 恢复。

### 14.2 日志

```text
/224010104/Jerry/logs/gate7-diagnosis/
├── runner.log
├── gpu-watch.log
├── prepare.log
├── unit.log
├── offline.log
├── cut3r-pilot.log
├── vggt-pilot.log
├── cut3r-full.log
├── vggt-full.log
├── gpu-all.log
├── attribute.log
└── report.log
```

每个日志必须记录主机、工作目录、完整命令、开始/结束时间、退出码、选中 GPU、输入/输出路径和当前 Git commit。

`status.json` 也必须原子写入，至少包含 `protocol_revision`、`stage`、`backend`、`completed_groups`、`pending_groups`、`last_valid_checkpoint`、`runner_pid`、`hostname`、`gpu_uuid`、`updated_at` 和最近错误。主机重启后若旧 PID 不存在，`resume` 先校验 checkpoint，再接管状态，不能仅凭旧状态中的 `running` 拒绝恢复。

### 14.3 状态查看

```bash
bash scripts/run_gate7_failure_diagnosis.sh status
jq . outputs/gate7_diagnosis/status.json
tail -n 100 /224010104/Jerry/logs/gate7-diagnosis/runner.log
/224010104/Jerry/.conda/tools/bin/tmux capture-pane \
  -p -t trust3d-$(hostname):gate7-diagnosis -S -120
```

状态文件必须区分：

```text
waiting_for_gpu
running
recovering
complete
failed_engineering
complete_failure_analysis
```

## 15. 结果解释模板

最终报告必须按以下顺序回答，不能只给一个总准确率：

1. **确定性契约是否通过？** 若否，具体是哪一个矩阵、轴或角色失败。
2. **TL/T0/R0 是否为 100%？** T0/R0 若否，先报告独立参考、RGB-D 或数据接口问题；TL 若否而 T0 通过，则报告已证明的 legacy 任务头错误。
3. **B0 是否精确复现主结果？** 若否，报告复现失败而不是原因归因；C0 不能替代 B0 回答这一项。
4. **grounding effect 多大？** 在 contract-valid V0-V3 上分别给 CUT3R/VGGT 点估计、有效 group 数和 CI。
5. **pose effect 多大？** 在 contract-valid V0-V3 上分别给 CUT3R/VGGT 点估计、有效 group 数和 CI。
6. **interaction 是否显著？** 判断 grounding 与 pose 是否相互依赖。
7. **backbone gap 多大？** 在 GT mask + oracle pose + contract-valid task head 后还剩多少。
8. **front/behind 是否由平面/三维语义差异解释？** 给混淆矩阵和 margin。
9. **两个 backend 的主因是否相同？** 不允许默认相同。
10. **哪些结论只是后验假设？** 单独列出。
11. **是否存在缺失、负向效应或单类标签限制？** 不得省略这些结论边界。

建议的结论示例：

> CUT3R 的主要损失来自 A，VGGT 的主要损失来自 B；C 是两者共享的下游问题。该判断基于固定 30-group 后验诊断，原 Gate 7 与方案 2 分数保持不变，任何修正均需在新冻结数据上重新验证。

## 16. 不属于本计划的工作

以下内容在原因定位完成前不得加入：

- 训练或微调 CUT3R/VGGT；
- 引入 detector、tracker、SAM 或外部 VLM；
- 搜索 crop fraction、confidence quantile 或答案阈值；
- 扩展到 Gate 8；
- 在当前 30 groups 上选择最高分坐标变换并作为主结果；
- 修改 routes、导航成本或 freshness 策略；
- 重新生成数据以掩盖 `front_behind` 标签不平衡。

原因定位结束后，若确认 grounding 主导，下一份独立方案才评估 RGB-only bbox/track；若确认 pose/adapter bug，下一份方案用新 adapter version 和新数据确认；若确认 backbone 主导，再讨论其他冻结模型或训练。

## 17. Git 与完成定义

### 17.1 提交顺序

建议分阶段提交：

1. `增加Gate7坐标与任务头合成契约测试`
2. `增加Gate7双后端分层诊断与恢复入口`
3. `记录Gate7双后端grounding诊断结果`
4. `补充Gate7失败原因诊断报告`

每次提交前执行：

```bash
git diff --check
/224010104/Jerry/.conda/envs/vggt/bin/python -m pytest -q \
  tests/test_gate7_cut3r.py \
  tests/test_plan2_vggt.py \
  tests/test_gate7_geometry_contract.py
```

不得提交模型权重、dense maps、缓存或日志。

### 17.2 完成定义

只有同时满足以下条件，原因定位任务才算完成：

- 原 Gate 7 和方案 2 锁定文件哈希未变化。
- P0 静态边界检查通过，协议锁包含最终诊断代码且 revision 链完整不可覆盖。
- D1 所有合成契约测试通过，或已保存可复现失败并形成新版本修复。
- CUT3R/VGGT 的 B0 均逐 episode 复现原结果。
- 两个 backend 均完成 30/30 groups 的三 selector 单次前向。
- B0-B3、R0、TL、T0 指标和 group cluster bootstrap CI 完整；若任务头被证明有误，C0-C3 及 task-head effect 也完整。
- grounding、pose、backbone、任务头和角色绑定均得到独立判断。
- `checkpoint_recovery_pass=true` 且恢复前后哈希一致。
- pilot checkpoint 被 full 直接复用，没有重复模型前向；所有缺失、退化和负向效应均显式报告。
- `final_diagnosis.json` 和中文诊断报告生成。
- 报告明确区分“已证明 bug、主要贡献、弱证据和未知项”。
- 新结果全部标记为后验诊断，没有覆盖原 Gate 7，也没有进入 Gate 8。

## 18. 预期价值

本计划结束后，不再只得到“CUT3R 56.11%、VGGT 45.00%”这一总体结果，而应得到每个 backend 的可审计误差分解：

```text
总误差
├── 确定性 adapter/坐标/角色错误
├── 任务标签与答案头语义错误
├── center crop grounding 损失
├── query pose/坐标对齐损失
├── grounding × pose 交互
└── 在 GT mask + oracle pose 后仍存在的 backbone 几何损失
```

这足以决定下一步应优先修 adapter、替换 grounding、改任务头，还是更换/训练三维 backbone，同时避免在原评测集上通过后验调参制造虚假的 Gate 7 通过结果。
