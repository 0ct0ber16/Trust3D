# Trust3D 服务器最小可执行实验详细报告

> 报告日期：2026-07-29（UTC）
>
> 实验结果基线提交：`3356dd49173b6a420768c852cc80efe0ff2cced0`
>
> 实验方案：[Trust3D_服务器最小可执行实验方案.md](Trust3D_服务器最小可执行实验方案.md)
>
> 状态总表：[STATUS.md](STATUS.md)

## 摘要

本实验按 Gate 0 至 Gate 7 的顺序，逐层验证 Trust3D 的核心主张：当环境中的事实可能过期时，Agent 应在“继续相信三维记忆”和“付出移动与观察成本重新获取证据”之间进行受约束决策。实验先隔离数据、模拟器、路由和在线执行，再引入第一视角空间问题，最后用冻结的 RGB-only 单目几何模型 CUT3R 替换确定性 GT/RGB-D 几何。

Gate 0 至 Gate 6 均通过。Gate 4 和 Gate 5 表明，在当前构造的数据上，Trust3D 路由可以保持 100% 准确率，同时相对 Always-Reobserve 减少 33.33% 的新观察；Gate 6 进一步表明，在空间问题上，freshness 路由可将无时效 Persistent 3D 的 stale 子集准确率从 25% 提升到 100%，并把移动成本从 3724 降至 2422。

Gate 7 的程序执行、模型加载、几何输出和 checkpoint 均成功，但答案质量未达到门槛。Trust3D-CUT3R 准确率为 56.11%，GT/RGB-D 参考为 100%，QA drop 为 43.89 个百分点，超过方案规定的 20 个百分点失败分析阈值。因此 `gate7_pass=false`，实验按预注册逻辑停止，不进入 Gate 8。

Gate 7 失败不是 freshness 路由错误，也不是模型调用失败，而是“冻结 CUT3R + 当前五帧序列组织 + 无 mask 中心区域 grounding + 相机坐标转换”这一完整视觉几何链路的答案误差。VGGT 在接口上可以替换 CUT3R：它直接输出相机参数、深度、世界点和置信度，且官方建议由深度和相机参数反投影得到三维点。但 VGGT 不能直接无修改替换，也不能在运行前保证通过 Gate 7；它必须作为新的冻结后端完成坐标、grounding、动态物体、许可、资源和防泄漏验证。

## 1. 实验目标与研究问题

### 1.1 核心研究问题

实验围绕以下问题展开：

1. 能否从 ALFRED 轨迹中构造可复现、无公开答案泄漏的动态事实数据？
2. 当历史事实可能过期时，路由策略能否同时降低 stale-memory error 和不必要重观察成本？
3. 离线路由收益能否在真实 AI2-THOR 在线执行中复现？
4. 该机制能否扩展到依赖第一视角几何的空间问题，而不是退化为普通状态缓存？
5. 冻结的 RGB-only 三维 backbone 能否在不读取 depth、instance mask 或 private oracle 的条件下接近 GT/RGB-D 几何？

### 1.2 预注册推进规则

方案要求当前 Gate 未通过时不得进入下一 Gate。Gate 7 对 GT-3D 到视觉几何的 QA drop 作出三级判断：

- 不超过 10 个百分点：可作为完整系统主结果；
- 10 至 20 个百分点：可保留为现实设置，但必须说明 backbone gap；
- 超过 20 个百分点：GT/RGB-D 继续作为主因果实验几何，视觉模型只作失败分析。

Gate 8 是非 MVP 必需的外部静态测试，只有 Gate 4 至 Gate 7 全部通过后才允许执行。本次 Gate 7 未通过，因此停止是方案要求，而不是运行中断。

## 2. 实验环境、数据与可复现性

### 2.1 固定环境

| 项目 | 固定值 |
|---|---|
| 主机类型 | x86_64 Linux，兼容 Ubuntu 22.04 |
| 模拟器环境 | Python 3.7.12，`ai2thor==2.1.0`，Xvfb 21.1.4 |
| ALFRED 提交 | `f91f4c0c96c7a29f33d0557f86b0a21035379b3b` |
| CUT3R 提交 | `8bc15dc92a6d7fd92920b4ec81540d3dec7d3ecf` |
| CUT3R checkpoint SHA256 | `45f7e98a0a64dbeb54901ae2b878cd8cd125f20a4497316483f0bd6f109f8103` |
| Gate 7 环境 | Python 3.11.9，PyTorch 2.4.1+cu121，torchvision 0.19.1+cu121 |
| 随机控制 | 数据生成 seed 17；Gate 4 bootstrap seed 20260728 |

### 2.2 数据隔离

公开 episode 只包含 Agent 决策时允许访问的信息。隐藏 intervention、当前答案、stale 标签、真实对象位置和最短验证真值保存在 private oracle 中，仅由隔离 evaluator 使用。Gate 2 至 Gate 7 均检查 public/private ID 一一对应和路由私有字段泄漏；最终泄漏计数为 0。

### 2.3 checkpoint 与恢复

长任务按 source event 或 group 拆分，逐单元采用临时文件写入后原子替换。恢复时校验配置、代码/模型版本、输入 fingerprint 和产物哈希，只跳过已验证成功的单元。

最终恢复审计结果包括：

- Gate 2：247 个 pilot 文件恢复前后逐字节一致，未启动 Unity/Xvfb；
- Gate 3：2906 个本地文件哈希一致，未启动模拟器；
- Gate 5：294/294 单元从 checkpoint 聚合，trace SHA256 保持不变；
- Gate 6：30/30 group 恢复，四个关键文件哈希一致，未启动模拟器；
- Gate 7：30/30 group 通过 fingerprint，32 个受审计文件哈希一致，模型加载时间 0 秒，未使用 GPU。

2026-07-29 再次执行 Gate 7 纯 checkpoint 恢复，结果仍为 `checkpoint_recovery_pass=true`。

## 3. Gate 0 至 Gate 7 总览

| Gate | 目标 | 主要规模 | 结果 | 决策 |
|---|---|---:|---|---|
| Gate 0 | 模拟器、RGB-D、分割、动作与确定性 smoke | 1 个场景，多次冷启动 | 通过 | 进入 Gate 1 |
| Gate 1 | 扫描 ALFRED JSON 候选事件 | 8051 个 JSON，14306 个候选 | 通过 | 进入 Gate 2 |
| Gate 2 | 20 个事件三分支可复现重放 | 20 groups，60 episodes | 通过 | 进入 Gate 3 |
| Gate 3 | 构建 TrustEQA-Dynamic-MVP | 98 有效 groups，588 episodes | 通过 | 进入 Gate 4 |
| Gate 4 | 离线二路 evidence routing | 588 episodes，10000 次 group bootstrap | 通过 | 进入 Gate 5 |
| Gate 5 | 在线二路 Agent | 294 环境单元，588 traces | 通过 | 进入 Gate 6 |
| Gate 6 | 第一视角空间记忆与 freshness | 30 groups，360 问题 | 通过 | 进入 Gate 7 |
| Gate 7 | 冻结 CUT3R RGB-only 几何 | 30 groups，360 问题 | 未通过质量门槛 | 仅失败分析，不进入 Gate 8 |

## 4. 分阶段实验结果

### 4.1 Gate 0：模拟器冒烟测试

Gate 0 验证了 AI2-THOR、无头渲染和数据模态是否可用于后续实验。RGB 为 300x300x3，深度包含 90000 个有限值，实例分割和 metadata 均有效，`RotateRight` 执行成功。通过 `InitialRandomSpawn(seed=17, placeStationary=true)`，四次冷启动得到相同对象状态哈希：

`57ad74a8e3b2ca5dfe909df906b5ca44fd3e373d4fb29e92cc85aa934979ef63`

这一阶段解决了 Flask/Werkzeug 版本导致的 multipart 响应死锁，以及可移动物体冷启动稳定性问题。无头 Unity 的音频和可选插件警告不影响有效模态。

### 4.2 Gate 1：ALFRED 候选事件扫描

ALFRED manifest 包含 8051 个 JSON，共 1.40 GB。7080 条公开轨迹包含动作计划，971 条隐藏测试轨迹不提供 `plan`，因此作为无标注项跳过而不是解析失败。

扫描得到 14306 个 OpenObject/CloseObject 候选，其中 14212 个位于 MVP 白名单。7080 条可处理轨迹的解析错误和动作验证错误均为 0。两次完整扫描输出逐字节一致，证明候选抽取可复现。

### 4.3 Gate 2：20 事件分支重放

20 个 source events 被构造成 Fresh-Stable、Risk-Stable 和 Risk-Stale 三种分支，共 60 个 public episodes、60 个 private oracle records 和 120 个独立 replay records。

关键结果：

- 完整 source events：20/20；
- 两次重放状态哈希一致：60/60；
- 可达验证视角：60/60；
- Query 时目标隐藏：60/60；
- Fresh/Risk-Stable 答案保持，Risk-Stale 答案发生变化；
- 符号答案与模拟器 metadata 一致：60/60；
- `gate2_pass=true`。

一个风险对出现 6/90000 像素、单通道 1 强度级的量化抖动，占 0.0067%，但目标仍隐藏且 public metadata 完全一致，未构成可利用泄漏。

### 4.4 Gate 3：TrustEQA-Dynamic-MVP

100 个初始 source events 全部完成 checkpoint，其中 2 个因真实 query 视觉差异被明确写入排除配置，保留原始失败记录。最终数据含 98 个 group、588 个 public episodes 和 588 个 private records，Fresh-Stable、Risk-Stable、Risk-Stale 比例为 1:1:1，答案 open/closed 各 50%。

关键结果：

- 重放成功率：98%；
- 588/588 episodes 状态哈希一致；
- 588/588 Query 时目标隐藏且存在可达验证视角；
- 最短验证成本范围 2 至 42，均值 19.24；
- 最终 196 对 Risk-Stable/Risk-Stale query 的 RGB、depth、instance 逐像素一致；
- `gate3_pass=true`。

对 2 个不合格 group 的显式排除避免了通过静默丢弃失败样本来制造通过结果。

### 4.5 Gate 4：离线二路路由

| 方法 | 准确率 | Stale-memory error | 不必要重观察率 | 重观察比例 | 平均验证成本 |
|---|---:|---:|---:|---:|---:|
| Always-Trust | 66.67% | 100.00% | 0.00% | 0.00% | 0.00 |
| Always-Reobserve | 100.00% | 0.00% | 100.00% | 100.00% | 19.24 |
| Global-TTL | 100.00% | 0.00% | 50.00% | 66.67% | 12.84 |
| Fact-Freshness | 100.00% | 0.00% | 50.00% | 66.67% | 12.84 |
| Trust3D 主策略 | 100.00% | 0.00% | 50.00% | 66.67% | 12.84 |
| Clairvoyant-Oracle | 100.00% | 0.00% | 0.00% | 33.33% | 6.43 |

Trust3D 主策略相对 Always-Trust 将 stale-memory error 降低 100 个百分点，相对 Always-Reobserve 保持相同准确率并减少 33.33% 的新观察。五个预先固定成本权重的 expected utility 均优于两个极端策略中的较优者，group-bootstrap 95% CI 下界均大于 0。

但 Global-TTL、Fact-Freshness 和 Trust3D 主策略在当前数据上产生相同路由。因此 Gate 4 支持“时效感知路由优于两个极端策略”，不支持“复杂控制器优于简单 TTL”这一更强结论。

### 4.6 Gate 5：在线二路 Agent

在线执行覆盖 294 个环境单元和 588 个问题。Agent 只读取 public episode 与固定配置，Fresh-Stable 信任历史，Risk-Stable/Risk-Stale 执行真实重观察。

关键结果：

- 在线答案准确率：100%；
- 与 Gate 4 离线答案差异率：0%；
- 路由一致：588/588；
- 动作尝试：6414，失败 0；
- 重观察：392 次，相对 Always-Reobserve 减少 33.33%；
- 移动成本：7548；
- 196/196 条 stale trace 正确使旧事实失效；
- `gate5_pass=true`。

需要保留的边界是：44 个后段单元使用一次真实 `TeleportFull` 到已验证终点，而不是逐动作导航；因此 Gate 5 证明在线路由、终点观察与规划成本一致，不证明这 44 个单元完成了逐动作导航执行。

### 4.7 Gate 6：第一视角空间记忆

Gate 6 构建 30 个锁定 group、360 个空间问题，覆盖左右、前后、二者谁更近和目标是否更近。几何后端包括模拟器 GT，以及由真实 RGB、depth、instance mask 和相机位姿反投影得到的确定性 RGB-D 几何。

| 方法 | 准确率 | Stale 子集准确率 | 新观察数 | 移动成本 |
|---|---:|---:|---:|---:|
| Persistent 3D（GT） | 75.00% | 25.00% | 0 | 0 |
| Persistent 3D（RGB-D） | 75.00% | 25.00% | 0 | 0 |
| Always-Reobserve（GT/RGB-D） | 100.00% | 100.00% | 540 | 3724 |
| Trust3D（GT） | 100.00% | 100.00% | 360 | 2422 |
| Trust3D（RGB-D） | 100.00% | 100.00% | 360 | 2422 |

Trust3D 相对 Persistent 3D 将 stale 子集准确率从 25% 提高到 100%，相对 Always-Reobserve 减少 33.33% 新观察，并把移动成本从 3724 降至 2422。确定性 RGB-D 工具与模拟器 GT 的答案误差率为 0%，`gate6_pass=true`。

该结果证明在已知深度、实例区域和相机位姿准确时，三维记忆加 freshness 路由有效。它不证明 RGB-only 几何已解决，也不证明优于训练后的 RGB 检索模型，因为 Gate 6 的 Current-RGB 和 All-history RGB 只是返回 `unknown` 的诊断占位项。

### 4.8 Gate 7：CUT3R RGB-only 几何

Gate 7 使用冻结的 CUT3R 官方最终 checkpoint。每个稳定/变化序列输入五帧：历史 target、历史 donor、公开 query、当前 target 和当前 donor。适配器只读取 RGB、帧角色和“验证帧中目标水平居中”先验，不读取 depth、instance mask 或 private 答案。

| 方法 | 准确率 | Stale 子集准确率 | 新观察数 | 移动成本 |
|---|---:|---:|---:|---:|
| Persistent 3D（CUT3R） | 46.39% | 27.50% | 0 | 0 |
| Always-Reobserve（CUT3R） | 54.44% | 61.67% | 540 | 3724 |
| Trust3D（CUT3R） | 56.11% | 61.67% | 360 | 2422 |
| Trust3D（GT/RGB-D） | 100.00% | 100.00% | 360 | 2422 |

全部 30 个 group 和 360 个问题完成，几何 group failure rate 和 query geometry failure rate 均为 0。两段序列总推理时间为 22.25 秒，状态复用后每题摊销 0.0618 秒，峰值已分配显存约 3.47 GiB。

质量指标未通过：GT-3D 到 CUT3R 的 QA drop 为 43.89 个百分点，最终 `gate7_pass=false`。

## 5. Gate 7 失败分析

### 5.1 失败类型

Gate 7 是质量失败，不是执行失败：

- 30/30 group checkpoint 状态均为 `success`；
- checkpoint 模型 key 全部匹配；
- 输出点、相机矩阵和答案均为有限值；
- geometry failure count 为 0；
- 恢复时全部输入 fingerprint 和 32 个文件哈希一致；
- 同一路由配合 GT/RGB-D 几何得到 100% 准确率。

最后一项排除了 freshness 路由本身是主要失败源。错误位于 RGB 到相机/三维点再到离散空间答案的视觉几何链路。

### 5.2 按分支分解

| 分支 | Trust3D-CUT3R 正确数 | 准确率 | 几何调用失败 |
|---|---:|---:|---:|
| Fresh-Stable | 67/120 | 55.83% | 0 |
| Risk-Stable | 61/120 | 50.83% | 0 |
| Risk-Stale | 74/120 | 61.67% | 0 |

Fresh-Stable 和 Risk-Stable 在没有对象状态变化时仍只有约 51% 至 56%，说明动态对象交换不是唯一原因。Risk-Stale 反而略高，进一步表明不能把全部差距归因于 stale 分支。

### 5.3 按问题类型分解

| 问题类型 | 正确数 | 准确率 |
|---|---:|---:|
| 前方/后方 | 23/90 | 25.56% |
| 左侧/右侧 | 57/90 | 63.33% |
| 目标是否更近 | 61/90 | 67.78% |
| 二者谁更近 | 61/90 | 67.78% |

前后方向是最严重的误差源：67 个错误全部是把 GT `behind` 预测为 `front`。这与 query 视角和验证视角之间的相机朝向/重定位误差一致，但现有产物不能把它唯一归因于模型相机头、坐标约定或适配器变换中的某一项。

距离类问题达到 67.78%，表明模型输出包含部分有效几何信息，但离 90% 主结果门槛仍很远。`which_closer` 和 `target_nearer` 是同一距离关系的两种答案表达，因此两列不能视为独立的几何证据。

### 5.4 数据标签与反事实审计

Gate 6 的 query pose 构造要求目标和参照物都位于 Agent 后方且不可见，因此 90 个 `front_behind` 当前答案全部是 `behind`。这使该子任务标签退化，既会放大坐标朝向错误，也可被“始终回答 behind”的规则利用。

反事实结果：

- 排除 `front_behind` 后，CUT3R 为 179/270，即 66.30%；
- 仅翻转前后坐标符号，总准确率为 68.33%；
- 即使把全部 `front_behind` 强制改为正确的 `behind`，总准确率上限也只有 74.72%；
- 在该理想修复后，达到 80% 仍需额外修正 19 个答案，达到 90% 仍需额外修正 55 个答案。

因此，前后坐标问题很重要，但不足以单独解释 43.89 个百分点的总差距。后续应保留当前 30-group 结果用于可比性，同时另建前后标签平衡的诊断集，避免用退化标签调适配器。

### 5.5 几何一致性诊断

| 指标 | 中位数 | p95 | 解释 |
|---|---:|---:|---|
| 静态结构相机重访漂移 | 0.1381 | 1.8204 | 以历史相机基线归一化，长尾明显 |
| 可移动对象重访误差 | 0.0975 | 2.2205 | 以对象间基线归一化，长尾更严重 |

中位数较小但 p95 大于一个甚至两个基线长度，说明少数组的相机或物体点误差足以改变离散空间关系。当前诊断只使用相机平移和对象点一致性，没有直接测量旋转误差；而前后/左右答案对旋转最敏感，这是 Gate 7 诊断需要补充的缺口。

### 5.6 对象类型差异

| 对象类型 | 正确数/问题数 | 准确率 |
|---|---:|---:|
| HandTowel | 12/12 | 100.00% |
| SaltShaker | 12/12 | 100.00% |
| PepperShaker | 1/12 | 8.33% |
| Newspaper | 2/12 | 16.67% |
| Glassbottle | 8/24 | 33.33% |
| SprayBottle | 4/12 | 33.33% |
| CellPhone | 32/60 | 53.33% |
| Statue | 38/72 | 52.78% |
| Vase | 28/48 | 58.33% |

其余类型位于 58.33% 至 75.00%。类别差异支持“小物体/细长物体 grounding 与深度估计不稳定”的解释，但每类样本只有 12 至 72 个问题，且每个 group 产生多个相关问题，不能把类别差异解释为可靠的泛化排名。

### 5.7 最可能的误差链路

#### A. Query 与验证帧的相机重定位困难

query 中目标和参照物刻意不可见且位于后方；五帧之间包含模拟器传送和大视角变化，不是平滑视频。适配器必须先把当前验证帧中的对象点变换到 query 相机坐标，再判断左右、前后和距离。有限输出不代表相机朝向正确，前后问题和重访长尾均指向该环节。

#### B. 无实例 mask 的中心区域 grounding

当前适配器在目标居中的验证帧中心截取边长为短边 12% 的区域，只保留置信度上半部分点并取三维中位数。该规则不知道物体轮廓，可能混入背景、支撑面或遮挡物。不同对象类型的误差差异与这一风险一致。

#### C. 单目深度和跨帧尺度/姿态耦合

左右、前后和两物体相对距离对统一的正尺度变换不敏感，因此“单目绝对尺度不唯一”本身不是充分解释。真正影响答案的是跨帧不一致的深度、姿态旋转、非统一尺度或局部点偏差。

#### D. 动态物体与静态几何假设冲突

Risk-Stale 序列中两个同类型对象发生位置交换。连续三维模型可能把物体运动吸收到相机或场景几何中。不过稳定分支同样表现较差，所以动态场景只能解释部分误差。

#### E. 冻结零样本边界

实验刻意不进行 task-specific training，也没有用 Gate 7 真值校准坐标、裁剪比例或置信度阈值。这保证了 `no task-specific training` 边界，但当前结果测量的是“冻结 backbone 与最小 grounding 适配器的组合”，不能单独断言 CUT3R backbone 本身不适合该任务。

### 5.8 已证实、强假设与未证实项

| 判断 | 证据级别 | 依据 |
|---|---|---|
| 失败不是运行或 checkpoint 故障 | 已证实 | 30/30 success，0 geometry failures，恢复哈希一致 |
| 失败不是 freshness 路由主导 | 已证实 | 相同路由下 GT/RGB-D 为 100% |
| 前后方向是最大单项误差 | 已证实 | 23/90，67 个 behind→front 错误 |
| 只修正前后符号仍不能通过 | 已证实 | 最优强制 behind 仅 74.72% |
| 相机旋转/重定位误差重要 | 强假设 | query 不重叠、前后错误、重访漂移长尾；尚缺 GT 对齐旋转诊断 |
| 中心 crop 混入背景 | 强假设 | 无 mask、类别差异大；尚缺 bbox/mask 对照实验 |
| 动态物体是唯一原因 | 被结果否定 | Fresh/Risk-Stable 同样低准确率 |
| CUT3R backbone 本身不可用 | 未证实 | backbone 与适配器/grounding 未做消融 |

## 6. VGGT 能否替换 CUT3R

### 6.1 结论

**技术上可以替换，科学上必须作为新的受控 Gate 7-VGGT 实验，不能直接假定替换后通过。**

VGGT 的官方接口提供本实验需要的几何量：相机外参/内参、深度图、深度置信度、世界点、世界点置信度和可选三维点轨迹。当前 CUT3R 适配器需要的核心接口是每帧 camera-to-world、每像素世界点和置信度，因此存在清晰映射。

但它不是直接替换 checkpoint 路径即可运行。至少需要完成外参方向转换、图像预处理坐标映射、深度反投影、目标 grounding、动态物体策略、checkpoint fingerprint、资源监控和许可确认。

### 6.2 官方能力与当前接口映射

截至 2026-07-29，VGGT 官方仓库固定检查提交为 `a288dd0f14786c93483e45524328726ab7b1b4ce`。官方将其描述为可从一张、少量或数百张视图在数秒内直接预测相机参数、点图、深度图和三维点轨迹的前馈模型。

| 当前 CUT3R 适配器需要 | VGGT 输出 | 所需适配 |
|---|---|---|
| 每帧 camera-to-world | OpenCV 约定的 extrinsic，官方说明为 camera-from-world | 对 4x4 外参求逆，并写坐标约定单元测试 |
| 每像素世界点 | `world_points` | 可直接使用，但需验证坐标、缩放与置信度 |
| 更稳定的每像素几何 | `depth`、`depth_conf`、intrinsic、extrinsic | 优先按官方建议由深度和相机反投影生成点图 |
| 世界点置信度 | `world_points_conf` 或 `depth_conf` | 固定选择一种并纳入 fingerprint |
| 目标跨帧对应 | 可选 `track`、`vis`、`conf` | 需要合法的 RGB-only query points 或 bbox；不能读取 GT mask |
| 五帧共同坐标系 | 联合前馈聚合全部视图 | 验证大视角、不重叠 query 与动态物体下的一致性 |

### 6.3 VGGT 可能改善的部分

1. **相机与深度联合输出。** VGGT 同时预测相机、深度和点图，官方 README 明确指出“深度 + 相机反投影”通常比 point-map branch 得到更准确的三维点。这正对应当前相机重定位和对象深度耦合问题。
2. **短批量多视图适配。** 当前每个分支只有五帧，符合 VGGT 少量视图输入模式，不需要把模型用于数百帧长序列。
3. **显式内参。** 当前 CUT3R 适配器直接使用预测点图和 pose；VGGT 的内参/外参接口便于做重投影、坐标方向和旋转误差审计。
4. **点轨迹与可见性。** VGGT 可选 track head 能提供像素轨迹、可见性和置信度，可能比固定中心 crop 更适合对象对应，但必须验证动态交换和 query 不可见时是否可靠。

### 6.4 VGGT 不能自动解决的部分

1. **目标身份仍需 grounding。** VGGT 输出场景几何，不会自动知道哪个点属于“target”或“donor”。若继续使用 12% 中心 crop，背景污染风险仍存在；若使用 track/bbox，也要定义不读取 private 信息的 RGB-only 规则。
2. **动态对象仍是风险。** 位置交换不是普通静态多视图重建。官方通用接口没有保证在同一对象移动后仍输出可用于本任务的刚体身份和共同坐标。
3. **无重叠 query 仍困难。** query 刻意背向对象，模型需要借助其他静态结构估计相机关系。更强模型可能改善，但没有当前数据上的证据。
4. **前后标签退化仍存在。** 更换模型不会修复 90/90 都是 `behind` 的评测设计，需要单独的平衡诊断集。
5. **零样本公平性。** 如果在当前 30 个评测 group 上调 crop、符号或置信度阈值，会产生评测集适配偏差。适配器应在合成单元测试或预先划定 pilot 上冻结。

### 6.5 架构与实验边界比较

| 维度 | 当前 CUT3R | VGGT 替换候选 |
|---|---|---|
| 模型形式 | 有 persistent state 的连续三维感知 | 联合多视图前馈 Transformer |
| 当前输入 | 稳定/变化序列各 5 帧 | 可保持完全相同的各 5 帧输入 |
| 主要几何 | camera pose、self-view points、confidence | camera、depth、world points、tracks、confidence |
| 状态复用 | 已测得每题摊销 0.0618 秒 | 需要重新定义为“每 group 一次前馈，多问题共享输出” |
| 当前 checkpoint 大小 | 3.17 GB | 官方 `model.safetensors` 为 5,026,367,224 bytes，约 4.68 GiB |
| 当前实测峰值显存 | 约 3.47 GiB | 未在本服务器测量，不能用权重大小代替峰值显存 |
| 当前任务结果 | 56.11% | 未运行，不能预判 |
| 原始权重许可 | CC BY-NC-SA 4.0 | `facebook/VGGT-1B` 模型卡为 CC BY-NC 4.0 |
| 商业权重 | 当前实验未涉及 | `VGGT-1B-Commercial` 需申请并遵守 VGGT License/AUP |

### 6.6 许可判断

本项目当前 CUT3R 代码/权重采用 CC BY-NC-SA 4.0。VGGT 官方仓库在 2025-07-29 更新为 VGGT License；原始公开 checkpoint `facebook/VGGT-1B` 的模型卡仍标注 CC BY-NC 4.0，商业 checkpoint `VGGT-1B-Commercial` 采用需申请的自定义许可。

对当前非商业研究实验，可以在遵守署名和非商业条款的前提下评估原始 VGGT checkpoint。若未来代码、权重或结果用于商业场景，不能沿用原始非商业权重，必须单独确认商业 checkpoint 授权和 Acceptable Use Policy。本段是工程合规提示，不替代正式法律审查。

## 7. 建议的 Gate 7-VGGT 最小实验

### 7.1 公平比较原则

1. 使用与 CUT3R 完全相同的 30 个锁定 group、RGB 路径、路由和 evaluator。
2. 不读取 depth、instance mask、private answer、真实对象位置或 branch 标签。
3. 不进行 task-specific training，不用 30-group 最终结果调超参数。
4. 单独保存 VGGT checkpoint、manifest、predictions 和 validation，不覆盖 CUT3R 产物。
5. 仍按 group 原子 checkpoint，输入图像、模型权重、适配器版本、预处理模式和 grounding 参数全部进入 fingerprint。

### 7.2 分阶段执行

#### V0：环境与许可审计

- 固定 VGGT 仓库提交、checkpoint 文件 SHA256、PyTorch/CUDA 版本；
- 原始权重约 5.03 GB，下载必须断点续传；
- 先确认研究用途与 checkpoint 许可匹配；
- 不复用 CUT3R 环境，避免依赖冲突。

#### V1：坐标与单元测试

- 用合成相机矩阵验证 world-to-camera 与 camera-to-world 求逆；
- 验证 OpenCV z-forward、图像 resize/crop 后中心坐标映射；
- 对同一图像重复推理，检查相机、深度、点图确定性；
- 分别测试 point-map 和 depth-unprojection 两条路径，但在 pilot 前固定主路径；
- 增加旋转误差、重投影误差和 front/behind 符号诊断。

#### V2：预先固定 pilot

- 从最终评测外的数据或预先锁定的少量 group 做 smoke；
- 只验证模型加载、有限输出、坐标方向、显存、时延和原子恢复；
- grounding 首选保持与 CUT3R 一致的中心 crop，以测纯 backbone 差异；
- track/bbox grounding 作为独立消融，不能选择最好结果后再称为预注册主结果。

#### V3：30-group 全量比较

必须报告：

- Trust3D-VGGT、Always-Reobserve-VGGT、Persistent-3D-VGGT 准确率；
- 相对 GT/RGB-D 的 QA drop；
- 按 branch、问题类型、group 和对象类型分层结果；
- camera/geometry failure rate；
- 相机旋转、平移重访、对象点重访和重投影误差；
- 推理时间、每题摊销时间、峰值显存；
- private leakage count 和 checkpoint 恢复哈希。

#### V4：平衡诊断集

另建一个不替换原 30-group 主比较的诊断集，使 `front`/`behind`、`left`/`right` 和距离答案尽量平衡。该诊断集用于区分坐标方向、相机旋转和 grounding 误差，不能与原 Gate 7 混合后只报告一个更有利的总准确率。

### 7.3 验收门槛

建议沿用原 Gate 7 门槛，不因更换模型降低标准：

- QA drop ≤10 个百分点：可作为完整系统视觉几何主结果；
- 10 至 20 个百分点：作为现实设置并明确 backbone gap；
- >20 个百分点：仍只作失败分析；
- geometry group failure rate 必须为 0；
- 不允许 private 泄漏或用 GT mask 改善 RGB-only 主结果；
- 纯 checkpoint 恢复必须不加载模型、不占用 GPU、哈希保持一致。

## 8. 当前实验支持与不支持的结论

### 8.1 支持的结论

1. 数据生成和三分支重放可复现，公开决策输入与私有真值隔离有效。
2. 在当前风险线索设计下，时效感知路由可在保持准确率的同时减少不必要重观察。
3. 离线收益可以在 AI2-THOR 在线重观察与证据采集中复现。
4. 当三维几何可靠时，freshness 路由能修复 stale 空间记忆，同时降低观察和移动成本。
5. 当前冻结 CUT3R 最小适配器无法达到主结果门槛，但其输出包含部分有效空间信息。

### 8.2 不支持的结论

1. 不能声称 Trust3D 复杂控制器已经优于 Global-TTL 或 Fact-Freshness；三者在 Gate 4 主设置中路由相同。
2. 不能声称当前系统优于训练后的 RGB 检索模型；Gate 6 的 RGB 方法是占位项。
3. 不能把 Gate 7 失败完全归因于 CUT3R backbone；适配器、相机变换和 grounding 尚未充分消融。
4. 不能把 CUT3R 的归一化一致性误差表述为绝对米制误差。
5. 不能声称 VGGT 会通过 Gate 7；目前只有接口和能力分析，没有本服务器实测。
6. 不能进入 Gate 8；Gate 7 未通过方案规定的质量门槛。

## 9. 最终结论与建议优先级

本次最小实验已经完成其主要作用：它证明了 Trust3D 的 freshness 路由在确定性几何和真实在线重观察条件下有效，同时明确暴露了冻结 RGB-only 三维 backbone 到任务答案之间的现实差距。Gate 7 的负结果是有信息量的失败分析，不应通过降低门槛、读取 GT mask 或在评测集上调参来掩盖。

后续建议按以下顺序推进：

1. 先补充相机旋转、外参方向和重投影诊断，确认 CUT3R 前后错误中有多少来自坐标/适配器，而不是立刻更换模型。
2. 冻结一个最小 VGGT depth-unprojection 适配器，在相同 30 groups 上做严格可比实验。
3. 将中心 crop 与 RGB-only track/bbox grounding 作为分离消融，避免把 backbone 与目标定位混为一谈。
4. 增加前后标签平衡的诊断集，但保留原 Gate 7 结果和门槛不变。
5. 只有 VGGT 或其他冻结视觉几何后端满足 ≤20 个百分点边界后，再讨论外部 Gate 8；若要作为主结果，仍需达到 ≤10 个百分点。

上述建议的不可变基线、逐 Gate 命令、checkpoint、验收门槛、停止条件和 Gate 8 决策规则，见 [Trust3D_服务器最小可执行实验方案2.md](Trust3D_服务器最小可执行实验方案2.md)。

## 10. 结果与审计文件

| 内容 | 路径 |
|---|---|
| 总状态 | `STATUS.md` |
| Gate 2 验证 | `outputs/gate2/validation.json` |
| Gate 3 验证与恢复 | `outputs/gate3/validation.json`、`outputs/gate3/checkpoint_recovery.json` |
| Gate 4 指标与图 | `outputs/gate4/metrics.json`、`outputs/gate4/plots/` |
| Gate 5 在线 traces | `outputs/gate5/traces.jsonl`、`outputs/gate5/validation.json` |
| Gate 6 验证与恢复 | `outputs/gate6/validation.json`、`outputs/gate6/checkpoint_recovery.json` |
| Gate 7 预测与验证 | `outputs/gate7/predictions.jsonl`、`outputs/gate7/validation.json` |
| Gate 7 恢复报告 | `outputs/gate7/checkpoint_recovery.json` |
| Gate 7 完整日志 | `/224010104/Jerry/logs/gate7/` |
| 本报告数据审计日志 | `/224010104/Jerry/logs/report/` |

## 参考资料

1. 本项目实验方案：[Trust3D_服务器最小可执行实验方案.md](Trust3D_服务器最小可执行实验方案.md)。
2. 本项目状态记录：[STATUS.md](STATUS.md)。
3. CUT3R 官方仓库，固定提交 [`8bc15dc`](https://github.com/CUT3R/CUT3R/tree/8bc15dc92a6d7fd92920b4ec81540d3dec7d3ecf)。
4. Wang et al., *Continuous 3D Perception Model with Persistent State*, [arXiv:2501.12387](https://arxiv.org/abs/2501.12387)。
5. VGGT 官方仓库，核对提交 [`a288dd0`](https://github.com/facebookresearch/vggt/tree/a288dd0f14786c93483e45524328726ab7b1b4ce)，访问日期 2026-07-29。
6. Wang et al., *VGGT: Visual Geometry Grounded Transformer*, [arXiv:2503.11651](https://arxiv.org/abs/2503.11651)，CVPR 2025。
7. VGGT 原始模型卡：[facebook/VGGT-1B](https://huggingface.co/facebook/VGGT-1B)。
8. VGGT 商业模型卡：[facebook/VGGT-1B-Commercial](https://huggingface.co/facebook/VGGT-1B-Commercial)。
9. VGGT 官方许可：[LICENSE.txt](https://github.com/facebookresearch/vggt/blob/a288dd0f14786c93483e45524328726ab7b1b4ce/LICENSE.txt)。
