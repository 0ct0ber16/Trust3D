# Trust3D 服务器最小可执行实验方案 2

## CUT3R 诊断、VGGT 几何替换与最小消融

> 方案日期：2026-07-30（UTC）
>
> 状态：待执行
>
> 起点提交：`88914e0c8ee33771922927f4b59e5c4cdc292619`
>
> 原方案：[Trust3D_服务器最小可执行实验方案.md](Trust3D_服务器最小可执行实验方案.md)
>
> 结果报告：[Trust3D_最小可执行实验详细报告.md](Trust3D_最小可执行实验详细报告.md)

---

# 第一部分：执行目标与边界

## 1. 为什么需要方案 2

原实验已经完成 Gate 0 至 Gate 7：

- Gate 0 至 Gate 5 建立了模拟器、数据、freshness 路由和在线重观察流程；
- Gate 6 使用模拟器 GT 和确定性 RGB-D 几何，证明几何可靠时 Trust3D 路由可以保持 100% 准确率，并相对 Always-Reobserve 减少 33.33% 新观察；
- Gate 7 保持路由、问题、移动成本和评测不变，只把几何后端替换为冻结 CUT3R；
- CUT3R 30/30 group 均成功输出，但 Trust3D-CUT3R 准确率只有 56.11%，相对 GT/RGB-D 的 QA drop 为 43.89 个百分点，因此 `gate7_pass=false`。

方案 2 不重做已经通过的 Gate 0 至 Gate 6。它只回答两个后续问题：

1. Gate 7 的差距中，有多少来自 CUT3R 坐标约定、相机重定位或当前中心区域 grounding 适配器？
2. 在不训练、不读取 GT mask 和不改变路由/导航的条件下，冻结 VGGT 能否把 RGB-only 几何的 QA drop 降到 20 个百分点以内，最好降到 10 个百分点以内？

## 2. 必须避免的概念混淆

CUT3R 和 VGGT 在本方案中都**不是导航规划器**。它们只替换以下模块：

```text
RGB 序列
  -> 相机参数与每像素几何
  -> target/donor 三维点
  -> query 相机坐标中的左右、前后和相对距离答案
```

以下内容固定不变：

- Gate 6 的 30 个 `group_id`；
- 360 个 public episodes 和 360 个 private oracle records；
- Gate 6 主路由 `outputs/gate6/routes.jsonl`；
- 是否重观察的决策；
- 已验证的移动成本和导航终点；
- 问题文本、答案定义和 Gate 7 evaluator；
- GT/RGB-D 参考结果。

因此，方案 2 测量的是视觉几何后端和目标 grounding，不重新评价导航算法。

## 3. 主假设、辅助假设与非目标

### 3.1 主假设

在相同 30 groups、相同 RGB、相同路由和相同 evaluator 下，VGGT 的 camera + depth-unprojection 几何比当前 CUT3R 最小适配器更接近 GT/RGB-D，并降低空间问答 QA drop。

### 3.2 辅助假设

1. CUT3R 的前后错误部分来自相机相对旋转或坐标转换，而不是 freshness 路由。
2. 无实例 mask 的固定中心区域会混入背景，造成对象类别相关的长尾误差。
3. 即使 VGGT 改善相机和深度，动态物体交换与无重叠 query 仍可能限制性能。

### 3.3 非目标

本方案不做以下工作：

- 不重新生成或扩大原 30-group 主评测集；
- 不修改 Gate 4/5/6 路由与导航规划；
- 不训练或微调 CUT3R、VGGT、检测器或 Controller；
- 不用 private answer、GT depth、GT pose 或 instance mask 生成主方法答案；
- 不接入 VGGT-Omega、HM3D 或 Sequential-EQA；
- 不通过降低 Gate 7 门槛把失败结果改写为通过；
- 不在主评测 30 groups 上枚举坐标轴、crop 比例或阈值并选择最高准确率。

---

# 第二部分：不可变基线与安全规则

## 4. 原 Gate 7 产物冻结

以下文件是方案 2 的只读基线，不得覆盖：

| 文件 | SHA256 |
|---|---|
| `outputs/gate7/validation.json` | `2c640c1279c7a4724ca60287e6f3ec9f943416ce2964018d2d8397e280fe94d5` |
| `outputs/gate7/predictions.jsonl` | `8af0a03e9f4ac956dbcd5f859eb093fc5197d1c581b779eb8367c185a5df40fc` |
| `outputs/gate7/checkpoint_recovery.json` | `83a10737770129b97cf36c3b72b616501c9d5910ef49a74b01c446cfe690694c` |
| `outputs/gate7/cut3r_geometry/manifest.json` | `426fde42f5ed78d1bca75575ff727e972b9441d335cb5107474128fc5094be76` |
| `data/episodes/spatial30/episodes_public.jsonl` | `9d5cd67479fb1e1c8291effb0d77ed876b0cc27d6b7f9cd02eb43045f3022263` |
| `data/episodes/spatial30/oracle_private.jsonl` | `e81df1510a292be065dd8db93d155ee8afa798f435e6064f80ea08b2d484cf21` |
| `outputs/gate6/routes.jsonl` | `057c60055eaa0ee59cf8d4d76d1db22a900c3697e2667c495e05ec07336cf888` |

原 Gate 7 固定指标：

```text
groups                         30
questions                      360
Trust3D-CUT3R accuracy         56.11%
Trust3D-GT/RGB-D accuracy      100.00%
QA drop                        43.89 个百分点
geometry group failure rate    0%
query geometry failure rate    0%
```

任何修正结果必须写入 `outputs/plan2/`，原始结果始终保留并在最终报告中并列展示。

## 5. 服务器与工作空间安全

1. 所有命令必须在 `trust3d-<hostname>` tmux 会话的独立 window 中运行。
2. 每次 Gate 启动前记录 CPU、内存、磁盘、GPU、工作目录、完整命令、开始时间和日志路径。
3. 所有新增环境、代码、权重、数据、输出和日志只能写入 `/224010104/Jerry`。
4. 不停止、修改或向其他用户进程发送信号。
5. CPU 数据任务默认最多 16 线程；模型 smoke 默认最多 8 CPU 线程和 1 张 GPU。
6. GPU 推理至少连续 3 次检查通过，检查间隔 10 秒：
   - 空闲显存不少于 60000 MiB；
   - GPU 利用率不高于 10%；
   - 没有本工作区重复的 CUT3R/VGGT 进程。
7. 任一检查失败时只等待或退出，不抢占 GPU，不自动切换到另一张同样繁忙的卡。
8. 发生 CUDA OOM 时保存失败记录并停止；不得自动降低分辨率后继续主实验。

2026-07-30 03:34 UTC 的资源快照显示两张 A100 各已占用约 78665 MiB 显存，因此当前只允许执行 CPU 诊断和文档/代码准备，不允许启动模型推理。后续必须重新检查，不能沿用这次快照。

## 6. 防泄漏与公平比较

主适配器只允许读取：

- `episodes_public.jsonl` 中的 RGB 路径、问题和公开对象标识；
- Gate 6 成功 context 中为构造当前 RGB 序列所需的图像路径；
- 冻结模型权重；
- 预先固定的图像预处理、置信度和 grounding 配置。

主适配器禁止读取：

- `oracle_private.jsonl` 中的答案、分支标签、真实位置和 stale 标签；
- simulator depth、instance segmentation、对象世界坐标或 query pose 真值；
- 任何根据 30-group QA 准确率后验选择的参数。

private 数据和 GT mask 只允许进入隔离诊断/evaluator，且输出文件必须带 `diagnostic_only=true`，不得被 Agent 或主适配器重新读取。

---

# 第三部分：目录、配置和最小代码范围

## 7. 新增目录约定

```text
configs/
└── plan2_vggt.yaml

trust3d/
├── data/
│   ├── build_spatial_diagnostic.py
│   └── validate_spatial_diagnostic.py
├── eval/
│   ├── diagnose_visual_geometry.py
│   └── evaluate_visual_geometry.py
└── geometry/
    ├── backend_schema.py
    └── run_vggt.py

scripts/
├── freeze_plan2_baseline.sh
├── run_plan2_cut3r_diagnostics.sh
├── run_plan2_balanced_diagnostic.sh
├── bootstrap_plan2_vggt.sh
├── download_plan2_vggt_weights.sh
├── run_plan2_vggt.sh
└── verify_plan2_vggt.sh

outputs/plan2/
├── baseline.json
├── cut3r_diagnostics.json
├── balanced_diagnostic_validation.json
├── vggt_environment.json
├── vggt_weights.json
├── vggt_geometry/
│   ├── manifest.json
│   └── checkpoints/<group_id>.json
├── vggt_predictions.jsonl
├── vggt_validation.json
├── grounding_ablation.json
├── checkpoint_recovery.json
└── final_decision.json

data/episodes/spatial_diagnostic/
├── selection.json
├── episodes_public.jsonl
├── oracle_private.jsonl
└── checkpoints/<group_id>/context.json
```

本地大文件：

```text
external/vggt/                         # 官方仓库，不提交 Git
external/vggt/model.safetensors        # 约 5.03 GB，不提交 Git
/224010104/Jerry/.conda/envs/vggt/     # 独立环境
/224010104/Jerry/logs/plan2/           # 完整过程日志
/224010104/Jerry/checkpoints/plan2/     # 下载和阶段完成状态
```

## 8. 唯一主配置

在任何 VGGT QA 结果产生前提交 `configs/plan2_vggt.yaml`。至少固定：

```yaml
schema_version: 1
seed: 20260730

baseline:
  commit: 88914e0c8ee33771922927f4b59e5c4cdc292619
  public_sha256: 9d5cd67479fb1e1c8291effb0d77ed876b0cc27d6b7f9cd02eb43045f3022263
  routes_sha256: 057c60055eaa0ee59cf8d4d76d1db22a900c3697e2667c495e05ec07336cf888

vggt:
  repository: https://github.com/facebookresearch/vggt.git
  commit: a288dd0f14786c93483e45524328726ab7b1b4ce
  model_id: facebook/VGGT-1B
  image_size: 518
  image_preprocess: pad
  dtype: bfloat16
  geometry_source: depth_unprojection
  grounding: center_crop
  center_crop_fraction: 0.12
  confidence_quantile: 0.5

pilot:
  group_id: s_02195803c5ebd4d3dcb170a7
  reveal_qa: false

evaluation:
  bootstrap_seed: 20260730
  bootstrap_samples: 10000
  main_drop_threshold_pp: 10
  realistic_drop_threshold_pp: 20

resources:
  min_free_gpu_mib: 60000
  max_gpu_utilization_percent: 10
  stable_checks: 3
  stable_check_interval_seconds: 10
  cpu_threads: 8
```

`image_preprocess=pad` 用于保留完整方形 AI2-THOR 图像；`geometry_source=depth_unprojection` 根据 VGGT 官方建议，使用预测 depth、intrinsic 和 extrinsic 反投影世界点。`center_crop_fraction=0.12` 与当前 CUT3R 主适配器保持一致，用于先隔离 backbone 差异。

---

# 第四部分：逐 Gate 最小执行方案

## P0：冻结基线与执行环境

### 目标

在任何实现修改前验证原 Gate 7 文件、Git 提交和输入 manifest，生成机器可读基线。

### 实现

新增 `scripts/freeze_plan2_baseline.sh`，完成：

1. 检查 `$TMUX` 非空；
2. 记录服务器资源；
3. 校验第 4 节全部 SHA256；
4. 校验 `HEAD` 不早于起点提交且工作树没有意外修改；
5. 读取 Gate 7 指标；
6. 以临时文件写入 `outputs/plan2/baseline.json` 后原子替换。

### 固定命令

```bash
scripts/freeze_plan2_baseline.sh
```

### 验收

- 所有基线哈希一致；
- `group_count=30`、`episode_count=360`；
- CUT3R accuracy 56.11%、GT accuracy 100%、QA drop 43.89pp；
- 输出包含代码提交、主机、时间、输入哈希和 Git 状态；
- 脚本退出码 0。

### 停止条件

任一基线文件哈希不一致时停止，不自动用当前文件更新本方案中的期望哈希。

## P1：CUT3R 坐标、相机与 grounding 诊断

### 目标

先判断现有 Gate 7 中可由适配器修复的问题，避免在未定位错误时直接归因于 backbone 或更换模型。

### P1.1 纯函数坐标测试

扩展 `tests/test_gate7_cut3r.py`，至少覆盖：

1. identity camera-to-world 下 z 正/负对应前后标签；
2. 已知 yaw 旋转下左右、前后符号；
3. world-to-camera 与 camera-to-world 互逆；
4. CUT3R 官方 pose 解码输出到本项目坐标的约定；
5. 统一正尺度变化不改变左右、前后和两物体距离排序；
6. 边界点必须报错或标记 `unknown`，不能静默选边。

禁止通过在 30 groups 上枚举轴交换/符号组合并选择准确率最高者来确定坐标约定。任何坐标修正必须同时满足官方约定和合成测试。

### P1.2 只读 checkpoint 诊断

新增 `trust3d.eval.diagnose_visual_geometry`，只读 CUT3R checkpoint 和隔离 GT，输出：

- query 到历史/当前验证帧的相对旋转 geodesic error（degree）；
- query 坐标中的相对平移方向误差（degree），不要求绝对尺度；
- 相机平移重访误差和旋转重访误差；
- center crop 中属于 GT instance mask 的像素比例，仅作为 `diagnostic_only`；
- 预测对象点到 RGB-D 参考点的归一化误差；
- 按 branch、question type、object type 和 GT decision margin 的错误率；
- `front_behind` 全部为 `behind` 的标签退化审计；
- 坐标符号翻转和始终回答 `behind` 的反事实，但不写入主预测。

相对旋转比较使用 query 与验证帧之间的相对旋转，避免预测世界坐标系和 simulator 世界坐标系的全局旋转任意性。

### 固定命令

```bash
/224010104/Jerry/.conda/envs/cut3r/bin/python -m pytest -q \
  tests/test_gate7_cut3r.py

scripts/run_plan2_cut3r_diagnostics.sh
```

### 输出

```text
outputs/plan2/cut3r_diagnostics.json
/224010104/Jerry/logs/plan2/cut3r-diagnostics.log
```

### 验收

- 诊断覆盖 30/30 groups 和 360/360 问题；
- 不修改原 Gate 7 checkpoint、predictions、validation 或 manifest；
- `private_data_used_by_agent=false`；
- 旋转、平移、grounding purity 和答案错误均有 group-level 记录；
- 诊断文件原子写入；
- 单元测试和脚本退出码均为 0。

### 决策

1. 若发现由官方坐标约定和合成测试共同证明的适配器 bug，可新增 `cut3r-adapter-v2`，但必须写入 `outputs/plan2/cut3r_corrected_*`，原 Gate 7 不覆盖；修正结果标记为 post-hoc。
2. 若只是某个轴变换在 30-group QA 上更好，但没有独立依据，不实施该变换。
3. 无论是否发现 bug，均继续 P2；P1 不把 `gate7_pass` 改为 true。

## P2：前后标签平衡的独立诊断集

### 目标

消除原数据 90/90 `front_behind=behind` 的退化，提供不污染原 30-group 主评测的适配器诊断集。

### 最小规模

构造 12 个新 group，共 144 个问题。每个 group 仍包含 Fresh-Stable、Risk-Stable、Risk-Stale 和四类空间问题。

聚合标签必须满足：

- `front` 与 `behind` 各占 40% 至 60%；
- `left` 与 `right` 各占 40% 至 60%；
- target-nearer true/false 各占 40% 至 60%；
- target 在 left-front、right-front、left-behind、right-behind 四个象限中每类至少 2 个 group；
- query 时 target 和 donor 均不可见；
- 历史证据和重观察证据来自真实可达视角；
- Risk-Stale 至少改变一个空间答案。

### 实现

新增独立构造器 `trust3d.data.build_spatial_diagnostic`，不得修改或复用原 `spatial30` 输出目录。候选搜索过程逐项记录拒绝原因；达不到 12 groups 时保留 checkpoint 并失败退出，不降低标签平衡标准。

每个 source event 继续使用原子 context checkpoint，selection 和 public/private manifest 都记录 seed、构造器版本、源候选哈希和标签配额。

### 固定命令

```bash
scripts/run_plan2_balanced_diagnostic.sh

/224010104/miniconda3/envs/trust3d-sim/bin/python -m trust3d.data.validate_spatial_diagnostic \
  --public data/episodes/spatial_diagnostic/episodes_public.jsonl \
  --private data/episodes/spatial_diagnostic/oracle_private.jsonl \
  --selection data/episodes/spatial_diagnostic/selection.json \
  --report outputs/plan2/balanced_diagnostic_validation.json
```

### 验收

- 12/12 groups 和 144/144 问题完成；
- 标签比例和四象限配额全部满足；
- public/private ID 一一对应；
- query RGB 不泄漏实际 stable/stale 分支；
- target/donor query 时不可见；
- GT 与 RGB-D 答案误差率低于 2%；
- 纯 checkpoint 恢复不启动 Unity/Xvfb，恢复前后哈希一致。

### 停止条件

若固定候选预算内不能构造至少 12 个合格 group，输出失败分析并停止 P2 数据扩展。不得删除困难类别、放宽不可见性或把原 30-group 标签混入以达到配额。

## P3：VGGT 环境、权重与统一输出接口

### P3.1 固定版本与许可

使用：

```text
repository  https://github.com/facebookresearch/vggt.git
commit      a288dd0f14786c93483e45524328726ab7b1b4ce
model       facebook/VGGT-1B
weight      model.safetensors
size        5,026,367,224 bytes（约 4.68 GiB）
license     模型卡标注 CC BY-NC 4.0
```

只用于符合许可的非商业研究。商业用途必须另行申请 `VGGT-1B-Commercial` 并审查 VGGT License/AUP。本方案不自动接受任何新的服务条款。

### P3.2 环境引导

新增 `scripts/bootstrap_plan2_vggt.sh`：

- 环境固定在 `/224010104/Jerry/.conda/envs/vggt`；
- Python ≥3.10；
- 依赖首先按官方固定版本建立，不与 CUT3R 环境混用；
- 仓库只克隆到 `external/vggt` 并 checkout 固定提交；
- 每个阶段写入 `/224010104/Jerry/checkpoints/plan2/vggt_environment/`；
- 已验证阶段恢复时跳过，不重复安装；
- 输出 `outputs/plan2/vggt_environment.json`。

### P3.3 权重下载

新增 `scripts/download_plan2_vggt_weights.sh`：

- 使用支持续传的 Hugging Face 下载或 `curl -C -`；
- 先写 `.part`，完成大小和 SHA256 校验后原子重命名；
- checkpoint SHA256 首次成功后写入配置锁文件和 `vggt_weights.json`；
- 后续恢复只校验，不重复下载；
- 权重不提交 Git。

### 固定命令

```bash
scripts/bootstrap_plan2_vggt.sh
scripts/download_plan2_vggt_weights.sh
```

### P3.4 统一几何 schema

新增 `trust3d.geometry.backend_schema`，使 CUT3R/VGGT checkpoint 的 evaluator 可读取共同字段：

```text
schema_version
backend_id
backend_commit
checkpoint_sha256
fingerprint
group_id
status
historical.target/donor.world
historical.target/donor.query_camera
stable_reobserve.target/donor.world
stable_reobserve.target/donor.query_camera
stale_reobserve.target/donor.world
stale_reobserve.target/donor.query_camera
camera_trajectories
timing
peak_allocated_bytes
```

现有 CUT3R JSON 不迁移、不重写。通用 evaluator 必须有回归测试，证明读取原 CUT3R checkpoint 后生成的指标与当前 `validation.json` 逐字段一致。

### 验收

- 环境、仓库提交、模型大小、SHA256 和许可来源均有机器可读报告；
- 不在 `/224010104/Jerry` 外写文件；
- checkpoint 能离线加载到 CPU 并核对 key，但不做完整 CPU 推理；
- CUT3R evaluator 回归结果不变；
- 所有 shell 脚本通过 `bash -n`。

## P4：VGGT 最小适配器、smoke 与配置冻结

### P4.1 主几何路径

`trust3d.geometry.run_vggt` 对 stable/stale 各读取与 CUT3R 相同的五帧 RGB：

```text
历史 target
历史 donor
公开 query
当前 target
当前 donor
```

固定处理：

1. 用官方 `load_and_preprocess_images(..., mode="pad")` 变换到 518；
2. 以 bfloat16 在单张 A100 上前馈；
3. 读取 `pose_enc`、`depth`、`depth_conf`；
4. 按官方 OpenCV camera-from-world 约定解码 extrinsic/intrinsic；
5. 对 extrinsic 求逆得到 camera-to-world；
6. 用官方 depth + camera unprojection 得到共同坐标系世界点；
7. 在 target/donor 验证帧中心短边 12% 区域内，选择 depth confidence 高于中位数的有限点并取三维中位数；
8. 把 target/donor 世界点变换到 query 相机坐标；
9. 使用与 CUT3R 完全相同的 `spatial_answers`；
10. 每 group 原子写 checkpoint。

`world_points` branch 只作为诊断，不作为主路径，避免在看到主评测 QA 后选择更有利分支。

### P4.2 fingerprint

每个 group fingerprint 必须包含：

- adapter/schema 版本；
- VGGT 仓库提交和权重 SHA256；
- 10 个 stable/stale 输入 RGB 的路径与 SHA256；
- 图像预处理模式、尺寸和 dtype；
- geometry source；
- crop fraction 和 confidence quantile；
- 公共 episode 和 routes SHA256。

任一字段变化时不得跳过旧 checkpoint。

### P4.3 smoke

smoke group 固定为按字典序最小的原 group：

```text
s_02195803c5ebd4d3dcb170a7
```

smoke 只检查：

- 模型加载；
- 输出 shape 和有限值；
- camera/extrinsic 求逆；
- depth-unprojection；
- target/donor 点与四类答案齐全；
- 显存、时延和 checkpoint 恢复。

smoke 阶段不得读取该 group 的 QA accuracy，并在输出中记录 `qa_revealed=false`。

### 固定命令

```bash
scripts/run_plan2_vggt.sh smoke
scripts/verify_plan2_vggt.sh smoke
```

### 验收

- GPU 资源门槛连续 3 次通过后才启动；
- 1/1 group success；
- 所有相机、深度、世界点和答案有限；
- private leakage count 为 0；
- 无 OOM，峰值显存和耗时已记录；
- 第二次执行通过 fingerprint 跳过推理，模型加载时间为 0；
- smoke 后提交并冻结 `configs/plan2_vggt.yaml` 的 SHA256。

### 停止条件

任何 shape、坐标、有限值、泄漏、OOM 或恢复失败都停止，不进入 P5。不得为了让 smoke 通过而临时降分辨率并沿用旧配置。

## P5：相同 30 groups 的 VGGT 主比较

### 目标

只改变几何后端，完成与原 CUT3R 严格可比的 30-group 实验。

### 固定命令

```bash
scripts/run_plan2_vggt.sh full
scripts/verify_plan2_vggt.sh full
```

### 输出

```text
outputs/plan2/vggt_geometry/checkpoints/*.json
outputs/plan2/vggt_geometry/manifest.json
outputs/plan2/vggt_predictions.jsonl
outputs/plan2/vggt_validation.json
outputs/plan2/checkpoint_recovery.json
/224010104/Jerry/logs/plan2/vggt-full.log
```

### 必须报告

1. Persistent-3D-VGGT、Always-Reobserve-VGGT、Trust3D-VGGT 准确率；
2. stale 子集准确率；
3. 相对 GT/RGB-D 的 QA drop；
4. 相对 CUT3R 的配对 group accuracy 差和 10000 次 group-bootstrap 95% CI；
5. branch × question type 分层；
6. object type 分层，但明确小样本限制；
7. geometry group/query failure rate；
8. 相机旋转/平移、对象点和重投影诊断；
9. 模型加载、两段五帧前馈、每 group、每题摊销时延；
10. 峰值显存和输入分辨率；
11. 新观察数和移动成本，确认它们与相同路由下 GT/CUT3R 一致；
12. private leakage count；
13. checkpoint 恢复与哈希结果。

### 验收与状态分类

先要求工程条件全部满足：

- 30/30 groups 完成；
- geometry group/query failure rate 均为 0；
- 360 个问题和全部方法预测齐全；
- private leakage count 为 0；
- 纯 checkpoint 恢复不加载模型、不使用 GPU，哈希一致。

再按 QA drop 分类：

| QA drop | 状态 | 后续动作 |
|---:|---|---|
| ≤10pp | `main_result`，`gate7_vggt_pass=true` | 可作为视觉几何主结果，进入 P6 并具备讨论 Gate 8 的条件 |
| >10pp 且 ≤20pp | `realistic_setting`，`gate7_vggt_pass=false` | 保留现实设置和限制；可做 P6 诊断，但不能自动进入 Gate 8 |
| >20pp | `failure_analysis`，`gate7_vggt_pass=false` | 只做失败分析；P6 仅在预设触发条件满足时执行，不进入 Gate 8 |

门槛使用点估计，与原 Gate 7 保持一致；bootstrap CI 必须报告，但不得后验替换门槛定义。

## P6：最小 grounding 消融

### 原则

主结果始终是 P5 预先固定的 12% center crop。P6 不能回写 P5，也不能把 30-group 上最好的消融包装成预注册主结果。

### 触发条件

仅在以下任一条件满足时执行 P6：

1. P5 QA drop >10pp；
2. P1/P5 隔离诊断显示 center crop GT-mask purity 中位数 <90%；
3. 对象类别最高与最低准确率相差 >20 个百分点，且差异与 grounding purity 同向。

### 最小消融 A：crop 稳健性

不重新选择主参数，只并列运行预先固定的：

```text
center crop fraction = 0.08 / 0.12 / 0.18
confidence quantile  = 0.5（固定）
```

这是 post-hoc 诊断，用于判断背景混入敏感性。所有设置单独 fingerprint 和 checkpoint。

### 可选消融 B：RGB-only track/bbox grounding

只有在平衡诊断集上先冻结完整规则后才允许运行。优先使用 VGGT 自带 track/visibility/confidence，避免再引入第二个大型视觉 backbone。规则必须满足：

- query points 仅来自公开的目标居中验证帧中心网格；
- 不读取 instance mask、depth GT 或 private object position；
- track visibility/confidence 阈值在平衡诊断集上固定；
- 不能跟踪时输出明确 failure，不回退到 GT mask；
- 在原 30 groups 上只运行一次冻结配置。

如果官方 track API 不能在当前帧顺序中无歧义地指定 target/donor query frame，则本最小方案不临时加入 GroundingDINO、SAM 等额外模型；记录接口限制，把 bbox grounding 延后到独立方案。

### 输出与报告

```text
outputs/plan2/grounding_ablation.json
```

必须并列报告 center crop 主结果和所有消融，不只报告最佳项。P6 即使达到 ≤10pp，也只能标为 post-hoc candidate；要升级为主结果，必须在新的未使用 holdout 上复验。

## P7：最终决策与 Gate 8 边界

新增 `outputs/plan2/final_decision.json`，由固定脚本读取 P0 至 P6 产物生成，禁止手工只填写有利结果。

### 决策规则

1. **VGGT 主结果 QA drop ≤10pp 且全部工程条件通过：**
   - 记录 `gate7_vggt_pass=true`；
   - CUT3R 原失败仍保留；
   - 可以单独提出 Gate 8 执行方案，但仍需先做服务器资源、外部仓库许可和数据存储审计。
2. **VGGT 主结果 QA drop 在 10–20pp：**
   - 记录为现实设置；
   - 可以讨论是否值得做静态外测，但按原 Gate 8“Gate 4–7 均通过”的规则，不自动执行；
   - 若用户决定修改研究门槛，必须另写方案并说明是 protocol amendment。
3. **VGGT 主结果 QA drop >20pp：**
   - 保持 GT/RGB-D 为主因果实验几何；
   - VGGT 和 CUT3R 都只作失败分析；
   - 不进入 Gate 8，不继续堆叠更多 backbone。
4. **只有 P6 post-hoc 消融 ≤10pp：**
   - 不标记通过；
   - 先建立新 holdout 并冻结 grounding 规则后复验。

### 最终报告必须回答

- CUT3R 错误中有多少可由坐标/适配器解释？
- VGGT 是否显著改善相同 30 groups 的准确率？
- 改善来自相机、深度还是 grounding？
- 动态物体和无重叠 query 还剩多少误差？
- 结果是否满足 ≤10pp、≤20pp 或仅失败分析？
- 是否具备讨论 Gate 8 的条件？

---

# 第五部分：checkpoint、恢复与验证细则

## 9. group checkpoint schema

每个 VGGT group checkpoint 至少包含：

```json
{
  "schema_version": 1,
  "adapter_version": "plan2-vggt-depth-v1",
  "backend_id": "vggt-1b",
  "group_id": "...",
  "status": "success",
  "fingerprint": "...",
  "repository_commit": "a288dd0f14786c93483e45524328726ab7b1b4ce",
  "checkpoint_sha256": "...",
  "preprocess": {
    "mode": "pad",
    "image_size": 518,
    "dtype": "bfloat16"
  },
  "geometry_source": "depth_unprojection",
  "grounding": {
    "method": "center_crop",
    "crop_fraction": 0.12,
    "confidence_quantile": 0.5
  },
  "inputs": [],
  "historical": {},
  "stable_reobserve": {},
  "stale_reobserve": {},
  "camera_trajectories": {},
  "timing": {},
  "peak_allocated_bytes": 0,
  "completed_at": "..."
}
```

失败 checkpoint 保留 `error_type`、`error`、traceback、资源快照和失败时间。成功重试后不得删除旧失败记录，应在审计目录中关联 attempt ID。

## 10. 原子写入与恢复

1. JSON/JSONL 先写同目录 `.tmp.<pid>`，`fsync` 后原子替换。
2. manifest 只聚合 fingerprint 验证成功的 group。
3. 恢复时先校验模型、配置、输入图像和 routes 哈希。
4. 只要 fingerprint 不一致，就把旧 checkpoint 标记为 stale，不覆盖原文件，写入新 adapter 版本目录。
5. `verify_plan2_vggt.sh` 强制 `CUDA_VISIBLE_DEVICES=''`，只做 fingerprint、聚合、evaluator 和哈希审计。
6. 恢复审计必须证明 `model_load_seconds_this_run=0`，且没有新增 VGGT/CUT3R/Unity/Xvfb 进程。
7. manifest 的创建时间不得在纯恢复时无语义刷新；纯恢复前后 Git 工作树应保持干净。

## 11. 测试最小集合

### 单元测试

- camera extrinsic 求逆；
- OpenCV 坐标方向；
- depth unprojection；
- resize/pad 后像素坐标映射；
- center crop/置信度选择；
- 空/NaN/低置信度输入失败；
- fingerprint 任一字段变化即失效；
- private forbidden keys；
- 通用 evaluator 对 CUT3R 的回归一致性；
- QA drop 和状态分类边界 0.10/0.20。

### 集成测试

- 1-group fake VGGT 输出到 evaluator；
- smoke checkpoint 二次恢复跳过模型；
- 失败 group 保留且 full 模式继续其他 group；
- 30-group manifest 缺一个 group 时不得标记 complete；
- evaluator 预期质量失败返回非零，但恢复脚本能区分“预期门槛失败”和“程序故障”。

### 固定验证命令

```bash
/224010104/Jerry/.conda/envs/vggt/bin/python -m pytest -q

for script in scripts/*plan2*.sh; do
  bash -n "$script"
done

scripts/verify_plan2_vggt.sh full
```

---

# 第六部分：最小执行顺序与停止条件

## 12. 唯一执行顺序

```text
P0 冻结基线
  -> P1 CUT3R 无 GPU 诊断
  -> P2 独立平衡诊断集
  -> P3 VGGT 环境、权重和统一 schema
  -> P4 单 group smoke，冻结配置
  -> P5 相同 30 groups 主比较
  -> 按触发条件决定 P6 grounding 消融
  -> P7 最终决策
```

禁止跳过 P1 直接用主评测 QA 调 VGGT 适配器；禁止 P4 未完成就运行 30 groups；禁止 P5 后修改主配置并覆盖结果。

## 13. 全局停止条件

发生以下任一情况立即停止当前 Gate并保留日志/checkpoint：

- tmux 不存在或 `$TMUX` 为空；
- CPU、内存、磁盘或 GPU 资源不满足安全门槛；
- 检测到另一工作区 VGGT/CUT3R 任务正在使用同一 GPU；
- 原 Gate 7 基线哈希变化；
- public/private 泄漏检查失败；
- 主适配器读取 GT depth、mask、pose 或 private answer；
- 模型/权重许可不适用于当前用途；
- 配置未在 QA 揭示前冻结；
- checkpoint fingerprint、原子恢复或哈希审计失败；
- 30 groups 主实验出现 OOM 后需要改变分辨率或方法；
- 需要通过任务专用训练才能继续。

## 14. 预计工作量单位

不预先承诺墙钟时间，以可恢复单元计：

| 阶段 | 最小单元 | 是否需要 GPU | 可恢复边界 |
|---|---:|---:|---|
| P0 | 1 次基线审计 | 否 | 整体原子报告 |
| P1 | 30 个只读 group 诊断 | 否 | 每 group 诊断记录 |
| P2 | 12 个新 group | 否 | 每 source event context/branch |
| P3 | 环境、仓库、权重 3 阶段 | 否 | 每阶段完成标记，权重续传 |
| P4 | 1 个 smoke group | 是 | 单 group checkpoint |
| P5 | 30 个主 group | 是 | 单 group checkpoint |
| P6 | 每个 grounding 配置 30 groups | 是 | 配置 × group checkpoint |
| P7 | 1 次聚合 | 否 | 原子决策报告 |

P6 是条件执行项。最小主路径在 P5 完成后已经能够回答“VGGT 是否优于 CUT3R以及是否达到门槛”。

---

# 第七部分：最终交付与检查清单

## 15. 必须提交 Git 的文件

只提交已审核的小文件：

- 方案、配置、源码、脚本和测试；
- 环境/权重 manifest，不提交环境和权重本体；
- baseline、diagnostics、validation、final decision；
- 体积可控的 predictions 和 group checkpoint JSON；
- 中文状态与最终报告。

不提交：

- Conda 环境；
- VGGT/CUT3R 仓库副本；
- 5 GB 权重；
- RGB/depth/mask 大文件；
- 全量运行日志。

每个 Gate 验收后执行：

```bash
git status --short
git diff --check
git add <本 Gate 明确文件>
git diff --cached --name-status
git commit -m '<本 Gate 中文或规范化提交说明>'
git push origin main
```

暂存范围必须逐文件验证，禁止 `git add .`。

## 16. 执行检查清单

### P0

- [ ] tmux、资源和 Git 状态已记录
- [ ] 七个基线哈希一致
- [ ] `outputs/plan2/baseline.json` 原子生成

### P1

- [ ] 坐标/旋转/尺度单元测试通过
- [ ] 30-group CUT3R 诊断完成
- [ ] 旋转、平移、grounding purity 和 margin 分层已报告
- [ ] 原 Gate 7 产物未修改

### P2

- [ ] 12-group 独立诊断集完成
- [ ] 前后、左右和距离标签满足配额
- [ ] 不可见性、可达性和泄漏检查通过
- [ ] 纯 checkpoint 恢复哈希一致

### P3

- [ ] VGGT 仓库提交和许可已固定
- [ ] 独立环境验证完成
- [ ] 权重续传、大小和 SHA256 校验完成
- [ ] 通用 schema 和 CUT3R 回归测试通过

### P4

- [ ] GPU 连续资源检查通过
- [ ] 固定 smoke group 推理成功
- [ ] QA 未揭示
- [ ] 二次运行未加载模型并跳过推理
- [ ] 主配置 SHA256 已冻结并提交

### P5

- [ ] 30/30 VGGT group 完成
- [ ] 360 个问题和所有方法预测齐全
- [ ] 私有泄漏为 0
- [ ] QA drop、CI、时延、显存和诊断均已报告
- [ ] 纯 checkpoint 恢复通过

### P6

- [ ] 触发条件已记录
- [ ] 主结果未覆盖
- [ ] 所有消融配置并列报告
- [ ] post-hoc 结果未标记为预注册通过

### P7

- [ ] `final_decision.json` 由固定脚本生成
- [ ] `main_result` / `realistic_setting` / `failure_analysis` 分类正确
- [ ] Gate 8 边界按原规则执行
- [ ] 中文最终报告与 GitHub 同步完成

## 17. 最小成功定义

方案 2 的工程成功不要求 VGGT 一定达到 90%。最小成功是：

1. 原 Gate 7 保持可复现且未被覆盖；
2. CUT3R 的坐标、相机和 grounding 误差得到可量化诊断；
3. VGGT 在相同 30 groups 上完成冻结、无泄漏、可恢复的严格比较；
4. 无论结果好坏，都能根据预设门槛明确判断是主结果、现实设置还是失败分析；
5. 不影响服务器稳定性，不修改他人进程或文件，不通过后验调参掩盖负结果。

只有 VGGT 主配置在全部工程条件通过且 QA drop ≤10 个百分点时，才把新的 RGB-only 几何视为 Gate 7 主结果。QA drop 在 10–20 个百分点时只作为现实设置；超过 20 个百分点时停止继续堆叠视觉 backbone。
