# Trust3D 服务器最小可执行实验方案 2

## CUT3R 合规诊断与 VGGT 冻结几何替换

> 方案版本：2.1
>
> 修订日期：2026-07-30（UTC）
>
> 状态：待执行
>
> Gate 7 基线提交：`3356dd4`
>
> 本方案修订前提交：`00e31aaa2b4eae96310b5eb0eee680064b4c6081`
>
> 原方案：[Trust3D_服务器最小可执行实验方案.md](Trust3D_服务器最小可执行实验方案.md)
>
> 实验报告：[Trust3D_最小可执行实验详细报告.md](Trust3D_最小可执行实验详细报告.md)

---

## 0. 结论先行

本方案只回答一个主问题：

> 在完全复用 Gate 7 的 30 个 groups、RGB 输入、freshness 路由、问题、答案定义和 evaluator 时，仅将冻结的 CUT3R 几何后端替换为冻结的 VGGT，能否把 Trust3D 相对 GT/RGB-D 的 QA drop 从 43.89 个百分点降至不超过 10 个百分点？

方案不重跑 Gate 0 至 Gate 6，不重新规划导航，不训练模型，不生成新的主评测集，也不在 30 groups 上选择坐标轴、crop、阈值或模型分支。CUT3R 和 VGGT 都只负责从 RGB 估计相机与三维几何，不负责导航规划。

### 0.1 最小必经路径

```text
M0 冻结基线并做 CUT3R 无 GPU 诊断
  -> M1 建立 VGGT 独立环境、权重锁和最小适配器
  -> M2 固定 1-group smoke，冻结协议
  -> M3 原 30 groups 一次性主比较
  -> M4 生成结论；仅在预注册条件满足时做离线诊断
```

M0 至 M4 都有独立 checkpoint。服务器重启后执行 `scripts/run_plan2.sh resume`，只恢复未通过哈希验证的最小单元。

### 0.2 “最优且最小”的操作性定义

本方案不声称 VGGT 在所有模型中理论最优。“最优”限定为当前实验约束下同时满足以下五项：

1. **因果可解释：**主比较只改变视觉几何后端，路由、导航、数据和评测不变。
2. **统计上诚实：**主参数在 QA 揭示前冻结，负结果不靠后验调参改写。
3. **工程量最小：**复用现有 checkpoint schema、空间答案函数和 evaluator，不建设通用 backend 框架。
4. **GPU 开销最小：**smoke group 直接复用于 full；full 只补剩余 29 groups；crop 诊断在同一次前向中预计算，不重复加载模型。
5. **结果闭合：**无论 VGGT 成功、改善但未通过或失败，都能按固定规则停止并形成结论。

以下工作从主路径删除：重跑 Gate 0 至 Gate 6、先建 12-group 平衡数据集、通用 schema 重构、额外检测/分割模型、VGGT 训练或微调、大规模 grounding 搜索。它们都不是回答主问题的必要条件。

---

## 1. 固定实验协议

### 1.1 不可变基线

以下文件只读，任何一个哈希不一致都停止，不得用当前文件重新计算“期望哈希”：

| 文件 | SHA256 |
|---|---|
| `outputs/gate7/validation.json` | `2c640c1279c7a4724ca60287e6f3ec9f943416ce2964018d2d8397e280fe94d5` |
| `outputs/gate7/predictions.jsonl` | `8af0a03e9f4ac956dbcd5f859eb093fc5197d1c581b779eb8367c185a5df40fc` |
| `outputs/gate7/checkpoint_recovery.json` | `83a10737770129b97cf36c3b72b616501c9d5910ef49a74b01c446cfe690694c` |
| `outputs/gate7/cut3r_geometry/manifest.json` | `426fde42f5ed78d1bca75575ff727e972b9441d335cb5107474128fc5094be76` |
| `data/episodes/spatial30/episodes_public.jsonl` | `9d5cd67479fb1e1c8291effb0d77ed876b0cc27d6b7f9cd02eb43045f3022263` |
| `data/episodes/spatial30/oracle_private.jsonl` | `e81df1510a292be065dd8db93d155ee8afa798f435e6064f80ea08b2d484cf21` |
| `outputs/gate6/routes.jsonl` | `057c60055eaa0ee59cf8d4d76d1db22a900c3697e2667c495e05ec07336cf888` |

固定基线指标：

```text
groups                         30
questions                      360
Trust3D-CUT3R accuracy         56.11%
Trust3D-GT/RGB-D accuracy      100.00%
QA drop                        43.89 个百分点
geometry group failure rate    0%
query geometry failure rate    0%
```

所有新结果只能写入 `outputs/plan2/`。原 Gate 7 文件不得覆盖、迁移或格式化。

### 1.2 主比较中固定与改变的变量

| 项目 | 处理 |
|---|---|
| 30 个 `group_id`、360 个问题 | 固定 |
| public episodes、private oracle | 固定 |
| `outputs/gate6/routes.jsonl` | 固定 |
| 重观察决策、导航终点、移动成本 | 固定 |
| stable/stale 每组各 5 张源 RGB | 路径和 SHA256 必须与 Gate 7 checkpoint 一致 |
| 对象居中先验 | 固定 |
| 归一化中心 crop 短边比例 | 固定为 `0.12` |
| 三维点聚合 | 有限点中置信度不低于中位数，再取逐轴中位数 |
| 空间答案函数、evaluator | 复用现有实现 |
| 唯一主变量 | `CUT3R` 替换为 `VGGT-1B` |

模型官方要求的输入尺寸和预处理属于后端实现：CUT3R 保持现有 512 处理；VGGT 固定 518 和 `pad`。二者必须读取完全相同的原始 RGB，不得通过裁掉原图内容改善 VGGT。

### 1.3 数据隔离与防泄漏

VGGT 适配器只允许读取：

- public episode 中的 RGB 路径、问题和公开对象角色；
- 已冻结 context 中构造相同 RGB 序列所需的图像路径；
- Gate 6 路由；
- 冻结模型、配置和权重。

VGGT 适配器禁止读取：

- private answer、stable/stale 标签、真实对象位置；
- simulator depth、instance mask、GT pose；
- 根据 30-group QA 结果选择的轴、crop、confidence、VGGT 输出分支或阈值。

private oracle 和 GT mask 只允许由 evaluator/诊断模块在全部几何 checkpoint 写完后单向读取。主适配器不得导入 evaluator，也不得读取 `outputs/plan2/*validation*.json`。测试用文件访问审计断言这一边界。

### 1.4 主结果与停止阈值

定义：

```text
QA drop (pp) = 100 * (Trust3D-GT accuracy - Trust3D-VGGT accuracy)
```

先检查工程条件，再按点估计分类：

| 条件 | 状态 | Gate 8 边界 |
|---|---|---|
| 工程条件任一失败 | `engineering_failure` | 不进入 |
| QA drop `<=10pp` | `main_result`，`gate7_vggt_pass=true` | 可以另写 Gate 8 方案 |
| `10pp <` QA drop `<=20pp` | `realistic_setting`，`gate7_vggt_pass=false` | 不自动进入 |
| QA drop `>20pp` | `failure_analysis`，`gate7_vggt_pass=false` | 不进入，停止堆叠 backbone |

工程条件为：30/30 groups 完成、360/360 问题齐全、几何 group/query failure rate 均为 0、private leakage count 为 0、路由和成本逐项一致、纯 checkpoint 恢复成功。

10000 次按 group 配对 bootstrap 的 95% CI 必须报告，但只用于表达不确定性，不替代上述点估计门槛。

### 1.5 VGGT 主配置

唯一配置使用标准库可直接解析的 `configs/plan2_vggt.json`，避免为 YAML 新增依赖：

```json
{
  "schema_version": 1,
  "seed": 20260730,
  "adapter_version": "plan2-vggt-depth-v1",
  "repository": "https://github.com/facebookresearch/vggt.git",
  "repository_commit": "a288dd0f14786c93483e45524328726ab7b1b4ce",
  "model_id": "facebook/VGGT-1B",
  "model_file": "model.safetensors",
  "model_file_bytes": 5026367224,
  "model_sha256": "LOCK_BEFORE_SMOKE",
  "image_size": 518,
  "image_preprocess": "pad",
  "dtype": "bfloat16",
  "geometry_source": "depth_unprojection",
  "grounding": "center_crop",
  "center_crop_fraction": 0.12,
  "diagnostic_crop_fractions": [0.08, 0.18],
  "confidence_quantile": 0.5,
  "smoke_group_id": "s_02195803c5ebd4d3dcb170a7",
  "bootstrap_seed": 20260730,
  "bootstrap_samples": 10000
}
```

下载完成后，`model_sha256` 必须在 smoke 前替换为实际 SHA256 并提交。模型固定使用 `depth + camera unprojection` 主路径；VGGT `world_points` 只可记录 shape/有限值，不生成主答案。

`facebook/VGGT-1B` 模型卡当前标注 CC BY-NC 4.0，本方案只用于符合许可的非商业研究。环境锁必须记录仓库 `LICENSE.txt` 和模型卡快照的哈希；用途或许可不匹配时停止。

---

## 2. 服务器安全、日志与恢复

### 2.1 每阶段启动门槛

所有命令必须在 `trust3d-<hostname>` tmux 会话中执行，脚本入口首先断言 `$TMUX` 非空。每阶段记录：主机、工作目录、完整命令、Git 提交、开始/结束时间、退出码、CPU、内存、`/224010104/Jerry` 磁盘和 GPU 状态。

CPU 任务最多 8 线程；GPU 任务只用 1 张卡。GPU 启动前连续检查 3 次，间隔 10 秒，每次必须满足：

- 空闲显存 `>=60000 MiB`；
- GPU 利用率 `<=10%`；
- 不存在本工作区重复的 VGGT/CUT3R 进程。

检查失败只记录并退出码 75，不等待占卡、不抢占、不杀进程。CUDA OOM 后保留失败 checkpoint 并停止；不得自动降低分辨率或改 dtype 继续主实验。

2026-07-30 的检查显示两张 A100 各占用约 78.6 GiB。该快照下只能做文档、CPU 诊断和代码准备，不能启动 VGGT；实际执行仍须重新检查。

### 2.2 唯一入口与状态文件

只新增一个 shell 入口：

```text
scripts/run_plan2.sh <baseline|bootstrap|smoke|full|diagnose|decide|resume|status>
```

入口统一管理日志、资源检查、退出码和 checkpoint，避免多个脚本的安全逻辑漂移。状态布局：

```text
/224010104/Jerry/logs/plan2/<UTC>-<stage>.log
/224010104/Jerry/logs/plan2/<UTC>-<stage>.exit.json
/224010104/Jerry/checkpoints/plan2/stages/<stage>.json
/224010104/Jerry/checkpoints/plan2/cut3r_diagnostics/<group_id>.json
outputs/plan2/status.json
```

`status.json` 只汇总阶段、完成数、失败数、当前日志和最后更新时间，不作为恢复依据。恢复只信任带输入/配置/代码 fingerprint 且输出哈希验证成功的 stage/group checkpoint。

### 2.3 原子 checkpoint 规则

1. JSON/JSONL 先写同目录 `.tmp.<pid>`，`flush + fsync` 后 `os.replace`。
2. 每个 group fingerprint 包含代码提交、adapter 版本、配置 SHA256、权重 SHA256、routes SHA256，以及 10 张输入 RGB 的路径和 SHA256。
3. fingerprint 不一致的旧结果移入同目录 `stale/<attempt_id>/`，不得覆盖或误跳过。
4. 失败记录保留异常类型、traceback、资源快照、时间和 attempt ID；成功重试不得删除失败历史。
5. 聚合 manifest 只接收 `status=success` 且 fingerprint 通过的 checkpoint。
6. `resume` 按 `baseline -> bootstrap -> smoke -> full -> diagnose -> decide` 检查，仅启动第一个未完成的必要阶段。
7. 纯恢复设置 `CUDA_VISIBLE_DEVICES=''`，必须在导入 torch/VGGT 和加载模型前完成 checkpoint 检查。

服务器重启后，tmux 本身不会跨主机恢复，但磁盘 checkpoint 会保留。重新创建 `trust3d-<hostname>` 会话后执行：

```bash
cd /224010104/Jerry/trust3d
scripts/run_plan2.sh resume
```

### 2.4 查看状态

```bash
scripts/run_plan2.sh status
jq . outputs/plan2/status.json
tail -f "$(jq -r .current_log outputs/plan2/status.json)"
```

`status` 不导入模型、不访问 GPU、不修改实验产物。

---

## 3. 最小代码与产物范围

### 3.1 必要代码改动

| 文件 | 最小职责 |
|---|---|
| `configs/plan2_vggt.json` | 唯一冻结配置 |
| `scripts/run_plan2.sh` | 所有阶段的资源、日志、恢复和命令编排 |
| `trust3d/geometry/run_vggt.py` | VGGT 前向、反投影、对象点和兼容 checkpoint |
| `trust3d/eval/diagnose_cut3r.py` | 只读 CUT3R 合规诊断 |
| `trust3d/eval/evaluate_cut3r.py` | 增加可选 `--backend-id vggt`，默认 CUT3R 行为不变 |
| `tests/test_plan2_vggt.py` | 坐标、反投影、fingerprint、泄漏和恢复测试 |

先一次性实现上述最小代码，代码编辑本身不算实验阶段；实现期间不运行模型、不读取 private oracle。随后严格按 M0 至 M4 执行，M0 先验证 evaluator 回归，验证失败则不进入后续阶段。

不新增 `backend_schema.py`，不复制 evaluator。VGGT checkpoint 直接兼容现有 CUT3R 的 `historical`、`stale_sequence_historical`、`stable_reobserve`、`stale_reobserve`、`camera_trajectories` 和 `answers` 字段；额外模型元数据由 evaluator 忽略。

修改后的 `evaluate_cut3r.py` 在不传 `--backend-id` 时，必须对原 CUT3R 产出与 1.1 节相同的 predictions/validation SHA256 和原退出语义。VGGT 模式只参数化方法名、几何目录和报告字段，不改路由、答案或指标公式。

### 3.2 必要输出

```text
outputs/plan2/
├── status.json
├── baseline.json
├── cut3r_diagnostics.json
├── vggt_environment.json
├── vggt_weights.json
├── protocol_lock.json
├── vggt_geometry/
│   ├── manifest.json
│   └── checkpoints/<group_id>.json
├── vggt_predictions.jsonl
├── vggt_validation.json
├── checkpoint_recovery.json
├── conditional_diagnostics.json
└── final_decision.json
```

大文件只放在 `/224010104/Jerry`：

```text
external/vggt/                         # 固定官方提交，不提交 Git
external/vggt/model.safetensors        # 约 5.03 GB，不提交 Git
/224010104/Jerry/.conda/envs/vggt/     # 独立环境
/224010104/Jerry/logs/plan2/           # 完整日志
/224010104/Jerry/checkpoints/plan2/     # 持久化恢复状态
```

下载前先只读检查 `/224010104` 是否已有相同大小和哈希的权重；如复用，只引用且禁止写入源文件。所有 cache、临时文件、`HOME`、`TMPDIR` 和 Hugging Face cache 都显式指向 `/224010104/Jerry`。

---

## 4. M0：冻结基线与 CUT3R 无 GPU 诊断

### 4.1 目的

证明原 Gate 7 可复现，并量化现有 checkpoint 能支持的 CUT3R 坐标、相机和中心 crop 问题。此阶段不加载 CUT3R/VGGT，不修改 Gate 7。

### 4.2 执行内容

1. 校验 1.1 节七个文件的 SHA256、30 groups 和 360 questions。
2. 用修改后的 evaluator 在临时输出目录回归 CUT3R，确认 predictions/validation 与原文件哈希相同。
3. 对每条已保存相机轨迹计算相邻帧及 query-to-reference 的相对旋转 geodesic error。
4. 在对应相机坐标中计算相对平移方向角误差；只比较方向，不比较单目绝对尺度。
5. 用 Gate 7 的相同 resize 规则把隔离 GT instance mask 映射到已保存 `crop_xyxy` 的坐标系，计算 `crop_purity` 和 `mask_recall` 代理。
6. 按 branch、question type、object type 和 GT decision margin 报告错误率，并显式记录原数据 `front_behind` 标签是否退化。

相对姿态使用相机间相对变换，消除预测世界坐标系的全局旋转、平移和尺度任意性。

现有 checkpoint 没有保存“被选中稠密点 mask”，因此本阶段不得报告该 mask 的 purity，也不得声称完成未保存深度图上的稠密重投影。GT mask 只进入 `diagnostic_only=true` 的诊断输出，不回流主适配器。

### 4.3 命令

```bash
scripts/run_plan2.sh baseline
```

### 4.4 验收

- 七个基线哈希、30 groups、360 questions 和固定指标全部一致；
- CUT3R evaluator 回归哈希一致；
- 30/30 groups 均有相对旋转、平移方向和 crop 代理统计，无法计算项带明确原因；
- `private_data_used_by_adapter=false`、`diagnostic_only=true`；
- 原 Gate 7 文件修改时间和 SHA256 均未变化；
- `baseline` stage checkpoint 原子生成，退出码 0。

若官方坐标约定与合成测试共同证明适配器存在 bug，只记录 `proven_adapter_bug=true`。修正只能作为 M4 的 post-hoc 结果，不能覆盖原 Gate 7，也不阻塞 VGGT 主路径。仅凭 30-group QA 更高的轴变换不算证据。

---

## 5. M1：VGGT 环境、权重锁与最小适配器

### 5.1 环境和权重

固定：

```text
repository  https://github.com/facebookresearch/vggt.git
commit      a288dd0f14786c93483e45524328726ab7b1b4ce
model       facebook/VGGT-1B/model.safetensors
size        5,026,367,224 bytes
environment /224010104/Jerry/.conda/envs/vggt
```

`bootstrap` 必须支持分阶段恢复：环境创建、依赖安装、仓库提交、许可快照、权重下载、大小校验、SHA256、CPU 只读 key 检查。下载使用 `.part` 和 `curl -C -`/等价续传；大小和 SHA256 通过后原子改名。不得进行完整 CPU 推理。

### 5.2 唯一 VGGT 几何路径

每个 group 对 stable/stale 各使用与 CUT3R 相同的五帧顺序：

```text
historical target -> historical donor -> public query
-> current target -> current donor
```

每个序列固定执行：

1. 官方 `pad` 预处理到 518，记录原图到模型坐标的映射；
2. 单张 A100、`bfloat16`、`torch.inference_mode()` 前向；
3. 解码 `pose_enc`、`depth`、`depth_conf`；
4. 按官方 OpenCV world-to-camera 约定获得 extrinsic/intrinsic；
5. 用 depth、intrinsic、extrinsic 反投影到同一世界坐标；
6. 在 target/donor 各自验证帧中心短边 12% 区域取有限且置信度不低于中位数的点，再取三维逐轴中位数；
7. 把对象世界点变换到 query 相机坐标；
8. 调用现有 `spatial_answers`，写兼容 checkpoint。

同一次前向仅额外汇总 8% 和 18% crop 的对象点到 `diagnostic_candidates`，不生成替代主答案，不读取 QA，也不增加模型前向。主结果永远使用 12%。

stable 与 stale 序列可能给历史帧不同的联合重建，因此必须像 CUT3R 一样分别保存 `historical` 与 `stale_sequence_historical`，不能错误复用一套历史世界点。

### 5.3 最小测试

必须覆盖：

- OpenCV extrinsic 方向和求逆；
- depth unprojection 的合成数值例；
- `pad` 后 crop 像素映射；
- 空点、NaN、低置信度显式失败；
- 四类空间答案与现有函数一致；
- fingerprint 任一字段变化即失效；
- forbidden private/GT key 与文件访问审计；
- fake 1-group checkpoint 可被现有 evaluator 的 VGGT 模式读取；
- 默认 CUT3R evaluator 回归哈希不变；
- 所有 shell 通过 `bash -n`。

### 5.4 命令

```bash
scripts/run_plan2.sh bootstrap
```

### 5.5 验收与停止

- 环境、仓库提交、许可、模型大小和 SHA256 均写入机器可读报告；
- 配置中的 `model_sha256` 已锁定，不再是占位符；
- 模型可通过 `safetensors.safe_open` 离线核对 metadata 和 key，不加载全部 tensor；
- 最小测试全部通过；
- `/224010104/Jerry` 外无新增或修改文件；
- stage checkpoint 和 Git 提交已记录。

网络中断走续传；许可不符、权重大小/哈希不符、CUT3R 回归变化或工作区越界时停止。

---

## 6. M2：单 group smoke 与协议冻结

### 6.1 固定输入和边界

smoke group 固定为：

```text
s_02195803c5ebd4d3dcb170a7
```

smoke 只验证模型加载、shape、有限值、相机方向、反投影、对象点、四类答案、显存、耗时和恢复。此阶段禁止传入 private oracle，禁止计算或显示该 group 的 QA accuracy。

运行前生成 `outputs/plan2/protocol_lock.json`，至少包含：

- Git 提交和 diff 状态；
- 配置、权重、public episodes、routes 的 SHA256；
- adapter/evaluator 源码 SHA256；
- smoke group 10 张 RGB 的路径和 SHA256；
- `qa_revealed=false`。

### 6.2 命令

```bash
scripts/run_plan2.sh smoke
```

### 6.3 验收

- GPU 门槛连续 3 次通过后才启动；
- 1/1 group 成功，所有必需字段有限且完整；
- 无 OOM，峰值显存和 stable/stale 前向耗时已记录；
- `private_file_open_count=0`、`qa_revealed=false`；
- 立即纯 checkpoint 重跑时不导入 VGGT、不加载模型、不使用 GPU，输出哈希不变；
- 协议锁、配置和实现已提交并推送，full 前不得修改。

工程 bug 可以修复，但必须提升 `adapter_version`、使旧 fingerprint 失效、重新运行测试和 smoke、重新提交协议锁。不得因为查看 QA 修改方法；一旦 smoke QA 被意外揭示，改用预先固定的下一个字典序 group，并在报告中披露 protocol deviation。

---

## 7. M3：原 30 groups 的一次性主比较

### 7.1 执行原则

full 首先复用 M2 的 smoke checkpoint，只对剩余 29 groups 进行 GPU 前向。每个 group 完成即原子落盘；失败后继续其他 group，最终非零退出并保留失败清单。只允许在相同 fingerprint 下重试失败 group。

30/30 几何 checkpoint 完成后才允许 evaluator 一次性读取 private oracle。不得在中途计算累计 QA，不得根据错误修改配置后覆盖结果。

### 7.2 命令

```bash
scripts/run_plan2.sh full
```

### 7.3 必须输出的指标

1. Persistent-3D-VGGT、Always-Reobserve-VGGT、Trust3D-VGGT 总体准确率；
2. Risk-Stale 子集准确率；
3. 相对 GT/RGB-D 的 QA drop；
4. 相对 CUT3R 的配对 group accuracy 差和 10000 次 group-bootstrap 95% CI；
5. branch × question type 分层；
6. geometry group/query failure rate；
7. 相对旋转、平移方向和 crop 代理诊断；
8. 模型加载、每段五帧前向、每 group 和每问题摊销时延；
9. 峰值显存、dtype、输入尺寸；
10. 新观察数和移动成本与 Gate 6/CUT3R 的逐项一致性；
11. private leakage、fingerprint 和恢复审计。

对象类别分层可以报告，但必须同时给出样本数，不从小样本排名推导主结论。

### 7.4 验收与恢复审计

```bash
CUDA_VISIBLE_DEVICES='' scripts/run_plan2.sh full
```

在 30/30 完成后第二次调用 `full`，必须在导入模型前识别全部 checkpoint 已完成，并执行纯恢复审计：

- 设置 `CUDA_VISIBLE_DEVICES=''`；
- 不导入模型、不加载权重、不启动 VGGT/CUT3R/Unity/Xvfb；
- 验证 30 个 checkpoint fingerprint 和 manifest；
- 重建 predictions/validation 并确认 SHA256 不变；
- 记录 `model_load_seconds_this_run=0`；
- 生成 `outputs/plan2/checkpoint_recovery.json`。

质量门槛未通过是有效实验结果，不等同程序故障。脚本必须把 `result_status` 与 `execution_exit_code` 分开记录。

---

## 8. M4：条件诊断与最终决策

### 8.1 必做决策

```bash
scripts/run_plan2.sh decide
```

固定脚本只读取已冻结产物，生成 `final_decision.json`，不得手工选择有利指标。无论结果好坏，都并列保留：GT/RGB-D、原 CUT3R、VGGT 12% 主结果和所有 post-hoc 结果。

### 8.2 条件诊断 A：CUT3R 坐标修正

仅当 M0 同时满足以下条件才执行：

1. 官方坐标/pose 约定给出唯一修正；
2. 独立合成测试在不读取 30-group QA 时证明当前实现错误；
3. 修正可明确版本化且不改变输入、grounding、路由和 evaluator。

修正只允许从现有 checkpoint 离线变换，结果写为 `cut3r_adapter_v2_posthoc`，永不改写原 Gate 7。若已保存字段不足以完成修正，则记录 `requires_separate_cut3r_rerun=true` 并停止该诊断；CUT3R GPU 重推理必须另写方案，不加入本最小路径。

### 8.3 条件诊断 B：center crop 敏感性

只有同时满足以下条件才评测已预计算的 8% 和 18% crop：

1. VGGT 12% 主结果 QA drop `>10pp`；
2. 12% crop 的 GT-mask purity 中位数 `<0.90`；
3. purity 最低四分位的准确率比最高四分位低至少 20 个百分点。

该诊断只从现有 checkpoint 重算答案，不重新运行 VGGT。0.08、0.12、0.18 三项必须并列报告，不选择最佳项覆盖主结果。即使替代 crop 达到 `<=10pp`，也只能标记 `posthoc_candidate`，必须在新的未使用 holdout 上冻结复验后才能讨论 Gate 8。

### 8.4 不在本方案自动执行的扩展

若 crop 诊断证明 grounding 是主要瓶颈，才另写扩展方案，构造前后/左右/距离标签平衡的独立诊断集，并在该集合上冻结 RGB-only track/bbox 规则。原 30 groups 只能对冻结规则运行一次；需要 GroundingDINO、SAM、训练或新 holdout 时均属于新实验，不加入本最小方案。

这样保留了 track/bbox grounding 的研究方向，同时避免它阻塞 VGGT 与 CUT3R 的直接比较，或把多个 backbone 的误差混进主结论。

### 8.5 最终决策必须回答

- CUT3R 已保存结果中有多少误差可由相对姿态或中心 crop 代理解释？
- VGGT 在相同 30 groups 上比 CUT3R 改善多少，CI 是否跨 0？
- VGGT 主结果属于 `main_result`、`realistic_setting`、`failure_analysis` 还是 `engineering_failure`？
- 主要剩余问题更符合相机、深度、grounding、动态物体还是无重叠视角？
- 是否具备另写 Gate 8 方案的条件？

---

## 9. 唯一执行顺序与全局停止条件

### 9.1 命令顺序

```bash
scripts/run_plan2.sh baseline
scripts/run_plan2.sh bootstrap
scripts/run_plan2.sh smoke
scripts/run_plan2.sh full
scripts/run_plan2.sh diagnose
scripts/run_plan2.sh decide
scripts/run_plan2.sh resume
```

`diagnose` 根据 M4 的预注册条件自动决定执行或写入 `not_triggered`。禁止跳过 smoke 直接 full，禁止 full 后修改主配置并覆盖 checkpoint，禁止从 `diagnose` 反向更新主结果。

### 9.2 全局停止条件

出现以下任一项立即停止当前阶段并保留日志/checkpoint：

- `$TMUX` 为空；
- CPU、内存、磁盘或 GPU 不满足安全门槛；
- 检测到重复任务或 GPU 被占用；
- 七个基线哈希任一变化；
- 主适配器读取 private、GT depth/mask/pose/position；
- 许可、仓库提交、权重大小或 SHA256 不匹配；
- 协议未在 QA 揭示前冻结；
- fingerprint、原子写入或纯恢复失败；
- 需要改变分辨率、dtype、主 crop 或模型分支才能完成 full；
- 需要训练、微调或修改路由才能达到门槛。

---

## 10. Git、交付与完成定义

### 10.1 分阶段提交

每个阶段验收后，只显式暂存本阶段已审核的小文件：

```bash
git status --short
git diff --check
git add <逐项文件>
git diff --cached --name-status
git commit -m '<阶段说明>'
git push origin main
```

禁止 `git add .`。不得提交 Conda 环境、外部仓库、模型权重、缓存、RGB/depth/mask 大文件和完整运行日志。必须提交配置、源码、测试、机器可读 manifest、体积可控的 checkpoint/predictions、中文状态和最终报告。

### 10.2 最小工作量

| 阶段 | 必要单元 | GPU | 恢复边界 |
|---|---:|---:|---|
| M0 | 1 次基线审计 + 30 个离线诊断 | 否 | group 诊断 |
| M1 | 环境/仓库/权重/测试 | 否 | 子阶段 checkpoint + 下载续传 |
| M2 | 1 个固定 smoke group | 是 | group checkpoint |
| M3 | 剩余 29 个主 groups | 是 | group checkpoint |
| M4 | 1 次决策 + 条件离线诊断 | 否 | 原子报告 |

除非 M4 的 CUT3R 修正满足严格触发条件，本方案只需要 VGGT 的 30 个唯一 group 推理，不额外运行模拟器或训练任务。

### 10.3 完成定义

本方案完成不要求 VGGT 一定通过。完成必须同时满足：

1. 原 Gate 7 基线未被修改且可复现；
2. CUT3R 诊断只报告现有 checkpoint 真正支持的指标；
3. VGGT 在相同输入、路由和 evaluator 上完成冻结、无泄漏、可恢复的 30-group 比较；
4. 主结果和 post-hoc 诊断明确分离；
5. 按固定门槛生成 `final_decision.json` 和中文实验报告；
6. 全过程日志、checkpoint 和 GitHub 提交可审计；
7. 未影响服务器稳定性，未修改 `/224010104/Jerry` 外的任何文件或他人进程。

只有预冻结的 VGGT 12% center-crop 主配置在全部工程条件通过且 QA drop `<=10pp` 时，才把它视为 Gate 7 的新主结果。`10–20pp` 只作为现实设置；`>20pp` 时停止继续堆叠视觉 backbone。任何 post-hoc 方案即使达到门槛，也必须在新 holdout 上复验。
