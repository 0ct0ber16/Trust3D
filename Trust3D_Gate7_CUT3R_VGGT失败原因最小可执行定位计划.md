# Trust3D Gate 7：CUT3R 与 VGGT 低准确率原因最小可执行定位计划

> 文档性质：后验诊断计划，尚未执行
>
> 目标：在不修改原 Gate 7 和方案 2 主结果的前提下，分离并定位“对象 grounding、相机/坐标适配、几何 backbone、donor/target 绑定、空间答案任务头”对低准确率的贡献
>
> 固定比较对象：CUT3R 56.11%、VGGT 45.00%、GT/RGB-D 100.00%
>
> 诊断输出根目录：`outputs/gate7_diagnosis/`

## 0. 结论先行

本计划采用一条最小但能够完成因果定位的路径：

1. **冻结原结果与新诊断协议**，保证任何诊断都不能覆盖 `outputs/gate7/` 和 `outputs/plan2/`。
2. **先运行纯 CPU 合成契约测试**，验证旋转、外参方向、坐标轴符号、相机前方定义、donor/target 绑定和任务答案规则。
3. **复用现有 checkpoint 做 CPU 离线审计**，先检查任务头、输入角色、相机矩阵和当前预测，不重新运行模型。
4. **每个 backbone 只进行一次可恢复的 GPU 全量前向**，在同一次 dense geometry 中同时计算 `center crop`、GT bbox 和 GT mask 三种选择，避免为每个消融重复推理。
5. **用固定的 2×2 因果矩阵**比较 `center/GT-mask × predicted/oracle-aligned pose`，并用 RGB-D/GT 两级 oracle 分离 backbone 与任务头。
6. **以 group 为统计单位进行配对 bootstrap**，按预注册阈值输出“已证实 bug、grounding 主导、相机主导、backbone 主导、任务头错误或混合原因”。

所有新分数都标记为 `diagnostic_only=true`。即使某个 oracle 干预得到高分，也不能替换已发布的 CUT3R 56.11% 或 VGGT 45.00%，更不能据此把原 Gate 7 改成通过。

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

每个 backend 都生成以下固定变体：

| 代号 | 点/深度 | grounding | query pose | 任务头 | 用途 |
|---|---|---|---|---|---|
| B0 | backend | center crop | predicted | 当前实现 | 必须复现原基线 |
| B1 | backend | GT mask | predicted | 当前实现 | grounding 干预 |
| B2 | backend | center crop | oracle-aligned | 当前实现 | 相机干预 |
| B3 | backend | GT mask | oracle-aligned | 当前实现 | grounding + 相机联合干预 |
| R0 | RGB-D | GT mask | GT | 数据定义任务头 | 几何参考上界 |
| T0 | GT object point | 不适用 | GT | 数据定义任务头 | 任务头/数据接口真值 |

GT bbox 同时计算，但只作为 center 与 GT mask 之间的辅助诊断，不进入主 2×2 判定。

### 4.2 2×2 主效应

对每个 backend 计算：

```text
grounding_effect = ((B1 - B0) + (B3 - B2)) / 2
pose_effect      = ((B2 - B0) + (B3 - B1)) / 2
interaction      = B3 - B2 - B1 + B0
backbone_gap     = R0 - B3
task_head_gap    = 1.0 - T0
```

这里的减法均指 group-level accuracy 的配对差。除点估计外，必须报告 10,000 次 group bootstrap 的 95% CI，seed 固定为 `20260730`。

### 4.3 为什么这是最小设计

- 合成和离线阶段不使用 GPU。
- center、GT bbox、GT mask 在同一次 dense 输出中提取，不重复模型前向。
- B0-B3 只重组同一份点、相机和角色，不再次运行模型。
- RGB-D 与 GT 已存在，不新增模拟器采集。
- 2×2 能分离 grounding、pose 和交互；R0/T0 能进一步分离 backbone 与任务头。
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
5. 固定 CUT3R/VGGT 权重、代码、配置和新诊断脚本的 SHA256。
6. 写入 `qa_revealed=true`、`diagnostic_only=true`、`original_results_mutable=false`。

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

- `protocol_lock.json` 完整且所有哈希验证通过。
- 原结果目录无新增、修改或删除。
- 输出根严格为 `outputs/gate7_diagnosis/`。
- 未满足任一条件立即停止，不能进入 D1。

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
- 若当前实现失败，先形成最小失败 fixture；修复必须使用新 adapter version，并保留原失败记录。
- D1 未通过不得启动 GPU。

## 8. D2：复用现有 checkpoint 的 CPU 离线分解

### 8.1 基线复现

只读取现有 checkpoint 和 routes，重新生成 B0 预测：

- CUT3R 必须复现 56.11%。
- VGGT 必须复现 45.00%。
- 360 个 Trust3D episode 的 answer、route、branch、question_type 必须逐条一致。
- 若无法复现，先定位 evaluator 或 checkpoint 读取差异，不能继续归因。

### 8.2 任务头 oracle

执行两个不需要模型的测试：

1. `GT object point + GT query pose + 当前任务头`。
2. `RGB-D object point + GT query pose + 当前任务头`。

同时用独立的 `spatial_labels`/数据构建规则作为参考实现，不能用同一个函数同时生成 expected 和 actual。

输出：

- 四类问题准确率；
- 混淆矩阵；
- yaw-only 与 full-3D-camera 的差异数；
- 决策 margin 分布；
- donor/target 交换后的变形检查。

判定：

- T0 必须为 360/360；否则确认任务头或数据接口错误。
- RGB-D 应为 360/360；若 GT 通过但 RGB-D 不通过，定位为 RGB-D 到任务头接口或数值边界问题。
- 若只有 full-3D-camera 失败、yaw-only 通过，确认标签语义与当前三维任务头不一致。

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
| `gt_mask` | 是 | grounding oracle B1/B3 |

三者必须使用同一份 dense points 和 confidence，不能分别运行模型。

### 9.3 pilot group

pilot 固定取 `sha256(group_id + seed)` 最小的 3 groups，不允许人工挑选易例或失败例。pilot 只判断工程链路，不计算原因结论。

每个 backend 依次执行，禁止并行占用两张 GPU：

```bash
bash scripts/run_gate7_failure_diagnosis.sh pilot cut3r
bash scripts/run_gate7_failure_diagnosis.sh pilot vggt
```

### 9.4 pilot 验收

- 每个 backend 3/3 groups 成功。
- 每个 group stable/stale 两个序列、三种 selector、target/donor 均完整。
- 所有点、pose 和 intrinsic 为有限值。
- mask resize 使用 nearest-neighbor，映射尺寸与模型输出一致。
- B0 的 center 点与原 checkpoint 在容差内一致；答案必须一致。
- GT mask 只出现在 `diagnostic_only` 产物，不写入公开基线预测。
- checkpoint 可重复读取，第二次 pilot 不加载模型且哈希不变。

pilot 失败只修工程问题，不查看或优化 pilot QA 分数。

## 10. D4：双后端 30-group grounding 全量诊断

### 10.1 GPU 准入

每次启动前必须满足：

- CPU、内存和磁盘安全；
- 至少一张 GPU 空闲显存不少于 60,000 MiB；
- GPU utilization 不高于 10%；
- 当前工作区没有同 backend 的重复进程；
- 原 Gate 7/方案 2 哈希仍与 D0 一致。

虽然历史峰值仅约 3.72 GB 和 6.94 GB，仍保留 60,000 MiB 准入门槛，以覆盖模型加载、临时张量、CUDA 上下文和共享服务器竞态。

### 10.2 命令

```bash
bash scripts/run_gate7_failure_diagnosis.sh full cut3r
bash scripts/run_gate7_failure_diagnosis.sh full vggt
```

若 GPU 不可用，启动唯一 watcher：

```bash
bash scripts/run_gate7_failure_diagnosis.sh watch
```

watcher 每 1 秒检查一次，命中后做一次原子准入复核；两个 backend 串行执行。不得修改已有 `scripts/watch_plan2_gpu.sh` 的状态或复用其输出目录。

### 10.3 checkpoint

每个 `backend × group` 单独写 checkpoint：

1. 先写 `<group_id>.json.tmp.<pid>`；
2. 校验 schema、fingerprint、有限值、selector 数量和输入 hash；
3. `fsync` 后原子重命名；
4. manifest 最后原子更新；
5. 恢复时只跳过 fingerprint 一致且验证成功的 group。

fingerprint 至少包含：

- backend 与模型权重 hash；
- RGB、mask 和 source context hash；
- adapter、诊断 selector 和配置 hash；
- public episodes、routes 和 group_id；
- image size、dtype、confidence quantile 和 seed。

### 10.4 全量验收

- CUT3R 30/30、VGGT 30/30 groups 成功。
- 每个 backend 只进行一轮必要模型前向。
- B0 逐 episode 复现原答案；若不复现，停止，不计算 B1-B3 贡献。
- dense maps 未被长期保存，磁盘只保留摘要和少量固定 pilot overlay。
- 恢复前后 checkpoint SHA256 一致。

## 11. D5：相机 oracle 对齐与 2×2 归因

### 11.1 oracle-aligned pose 的定义

对每个 sequence 使用五个预测相机和对应 GT 相机进行标签无关的 Sim(3) 对齐：

1. 旋转用已知相机姿态求统一坐标基变换；
2. 平移用 camera centers 做 Umeyama similarity alignment；
3. scale 只能取正值；
4. 对齐不能读取任何 QA answer；
5. 若相机中心秩不足 2，则使用非零 pair baseline 的中位尺度与旋转对齐，并记录 `alignment_degenerate=true`；
6. 对齐后的模型点进入 GT world frame，再使用精确 GT query pose 计算 B2/B3。

对齐不会修复模型点的局部深度、目标选择或形变，只用于隔离 query pose/坐标误差。

### 11.2 必须计算的指标

对 B0、B1、B2、B3、R0、T0 分别输出：

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
- T0 不能 360/360 复现数据标签；
- yaw-only 数据定义通过而 full-3D-camera 任务头系统性失败。

bug 修复必须使用新 adapter version，原 checkpoint 和原分数保留。

### 12.2 主要贡献阈值

对 grounding、pose 或 backbone 的配对提升/差距，采用：

| 级别 | 条件 |
|---|---|
| 主要贡献 | 点估计不少于 20 个百分点，且 95% CI 下界大于 0 |
| 次要/混合贡献 | 点估计 10-20 个百分点，或 CI 跨 0 |
| 当前无强证据 | 点估计小于 10 个百分点且 CI 跨 0 |

“主导原因”还要求该因素的点估计至少比第二大因素高 10 个百分点；否则结论必须写为“混合原因”。

### 12.3 充分性

- 若单一干预后准确率达到至少 90%，该因素可称为“在 oracle 诊断中足以解释 Gate 7 失败”。
- 若 B3 仍低于 90%，grounding 和 pose 的联合修复仍不充分。
- 若 R0/T0 为 100% 而 B3 明显偏低，剩余问题主要位于 backend 点/深度。
- 若 T0 低于 100%，必须先修任务头/数据接口，再讨论 backbone。

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
bash scripts/run_gate7_failure_diagnosis.sh lock
bash scripts/run_gate7_failure_diagnosis.sh unit
bash scripts/run_gate7_failure_diagnosis.sh offline
bash scripts/run_gate7_failure_diagnosis.sh pilot cut3r
bash scripts/run_gate7_failure_diagnosis.sh pilot vggt
bash scripts/run_gate7_failure_diagnosis.sh full cut3r
bash scripts/run_gate7_failure_diagnosis.sh full vggt
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

停止不是删除结果；失败 group、错误堆栈和输入 fingerprint 必须保留，修复后只重试失败单元。

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
├── unit.log
├── offline.log
├── cut3r-pilot.log
├── vggt-pilot.log
├── cut3r-full.log
├── vggt-full.log
├── attribute.log
└── report.log
```

每个日志必须记录主机、工作目录、完整命令、开始/结束时间、退出码、选中 GPU、输入/输出路径和当前 Git commit。

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
2. **T0/R0 是否为 100%？** 若否，先报告任务头或数据接口问题。
3. **B0 是否精确复现主结果？** 若否，报告复现失败而不是原因归因。
4. **grounding effect 多大？** 分别给 CUT3R/VGGT 点估计和 CI。
5. **pose effect 多大？** 分别给 CUT3R/VGGT 点估计和 CI。
6. **interaction 是否显著？** 判断 grounding 与 pose 是否相互依赖。
7. **backbone gap 多大？** 在 GT mask + oracle pose 后还剩多少。
8. **front/behind 是否由平面/三维语义差异解释？** 给混淆矩阵和 margin。
9. **两个 backend 的主因是否相同？** 不允许默认相同。
10. **哪些结论只是后验假设？** 单独列出。

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
- D1 所有合成契约测试通过，或已保存可复现失败并形成新版本修复。
- CUT3R/VGGT 的 B0 均逐 episode 复现原结果。
- 两个 backend 均完成 30/30 groups 的三 selector 单次前向。
- B0-B3、R0、T0 指标和 group bootstrap CI 完整。
- grounding、pose、backbone、任务头和角色绑定均得到独立判断。
- `checkpoint_recovery_pass=true` 且恢复前后哈希一致。
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
