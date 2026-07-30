# Trust3D 服务器最小可执行实验方案 2 详细实验报告

> 实验主题：冻结 VGGT `depth-unprojection` 后端与 CUT3R 的严格同协议比较
> 实验日期：2026-07-30（UTC）
> 执行主机：`c0ahe0i1kk7lo-0`
> 主实验代码提交：`6f8514de5dfef93e162e1a7a07e2d5ff728ab501`
> 最终结果提交：`db3a18935cc4178ab002de78cc03decf059077d4`
> 最终状态：流程执行成功，工程检查通过，Gate 7 质量门槛未通过，禁止进入 Gate 8

## 1. 摘要

本实验按照《Trust3D 服务器最小可执行实验方案 2》完成了冻结 VGGT 后端的环境审计、协议锁定、单组 smoke、30-group 全量推理、同协议评测、组级 bootstrap、条件诊断和断点恢复验证。30 个 group 全部完成，共产生并评测 360 个问题，程序最终退出码为 0；几何 group 失败率、问题几何失败率和私有数据泄漏计数均为 0。

核心质量结果如下：

| 后端/参考 | 准确率 | 相对 GT 下降 | Gate 7 |
|---|---:|---:|---|
| GT 3D / RGB-D 参考 | 100.00% | 0.00 个百分点 | 参考上界 |
| CUT3R | 56.11% | 43.89 个百分点 | 未通过 |
| VGGT | 45.00% | 55.00 个百分点 | 未通过 |

VGGT 相对 CUT3R 的配对准确率差为 **-11.11 个百分点**。以 group 为重采样单位、固定随机种子 `20260730`、执行 10,000 次 bootstrap 后，95% 置信区间为 **[-21.39, -0.83] 个百分点**。该区间完全低于 0，说明在本次固定的 30-group 协议上，VGGT 不仅没有达到与 GT 相差不超过 10 个百分点的 Gate 7 门槛，而且显著弱于 CUT3R。

因此，本实验的结论不是“VGGT 程序无法运行”，而是：

1. 冻结 VGGT 后端在工程上可以稳定执行、持久化恢复并保持无私有数据泄漏。
2. 当前 `RGB-only + 12% center crop + depth-unprojection` 的任务映射不能提供合格的空间问答质量。
3. VGGT 在当前协议下不能替换 CUT3R，更不能据此规划或执行 Gate 8。

## 2. 实验目标与预注册问题

方案 2 的目标不是重新优化 Gate 7，也不是在评测集上寻找更高分参数，而是在固定既有变量的条件下回答以下问题：

1. 冻结 VGGT 能否仅根据公开 RGB 输入恢复可供 Trust3D 使用的几何信息？
2. 在不改变 30-group 数据、路由、导航成本和评测器的前提下，VGGT 能否公平替换 CUT3R？
3. VGGT 相对 GT 3D 参考的准确率下降是否不超过 10 个百分点？
4. VGGT 与 CUT3R 的配对差异在 group 级重采样下是否具有稳定方向？
5. 若主结果失败，现有证据能否把失败定位到模型、坐标适配、目标 grounding 或其他环节？

预注册的主判定规则为：所有工程条件通过，且 `GT accuracy - VGGT accuracy <= 0.10`，方可判定 `gate7_vggt_pass=true`。评测门槛、30-group 主结果和原 Gate 7 结果均不得通过后验调参或覆盖输出的方式修改。

## 3. 与方案 1 的边界及未改变部分

方案 2 是一个独立的冻结后端比较实验，没有覆盖或修改方案 1 已完成的 Gate 0-7 结果。其边界如下：

- 复用 Gate 6 固定的 freshness 路由、导航轨迹与成本定义。
- 复用同一份 30-group 公开 episode 和同一评测器。
- 只将在线 RGB 几何后端由 CUT3R 替换为 VGGT 适配器。
- 不读取 GT mask、GT depth 或私有答案来生成 VGGT 预测。
- 私有标签只在协议锁定后由评测器用于计分和诊断。
- 不修改原 Gate 7 门槛，不覆盖 CUT3R 结果，不将条件诊断写回主结果。

特别需要说明：**VGGT 在本实验中没有负责导航规划**。Trust3D、Always-Reobserve 和 Persistent-3D 的路由逻辑、移动步数计算及观测次数定义均来自既有 Gate 6 协议；VGGT 只提供 RGB 几何恢复及其到问题答案的映射。因此，本报告不能据此声称 VGGT 已经完成自主导航或路径规划。

## 4. 数据、样本单位与固定变量

### 4.1 数据规模

- group 数：30。
- 每个 group 的问题数：12。
- 总问题数：360。
- 分支：`fresh_stable`、`risk_stable`、`risk_stale`。
- 问题类型：`front_behind`、`left_right`、`target_nearer`、`which_closer`。
- 每个“分支 × 问题类型”组合：30 个问题。
- bootstrap 单位：group，而不是把同一 group 内的 12 个相关问题当作独立样本。

### 4.2 固定变量

| 项目 | 固定值 |
|---|---|
| 主输入 | RGB-only |
| 输入尺寸 | 518 |
| 预处理 | `pad` |
| 推理精度 | `bfloat16` |
| 几何路径 | `depth_unprojection` |
| grounding | 12% center crop |
| 置信度阈值 | 中位数分位数 `0.5` |
| 随机种子 | `20260730` |
| bootstrap 次数 | 10,000 |
| GPU 准入门槛 | 空闲显存不少于 60,000 MiB，利用率不高于 10% |

## 5. 环境、模型与协议锁

### 5.1 VGGT 环境

| 项目 | 值 |
|---|---|
| Python | `3.11.9` |
| PyTorch | `2.3.1+cu121` |
| VGGT repository commit | `a288dd0f14786c93483e45524328726ab7b1b4ce` |
| Hugging Face revision | `860abec7937da0a4c03c41d3c269c366e82abdf9` |
| 权重大小 | 5,026,367,224 bytes |
| 权重 SHA256 | `f164acf60724910d8fe1578bb499d800850c7bb0948db7555c413f9fbe60467e` |
| License SHA256 | `b94e46746792d1adfa0ebdbccada5d4576d244ae230be9de8a46b5868fff001b` |
| README SHA256 | `56426b9aea25174ccbc4c2c37a3a22107fd8b5ac9dc5a8bf008e7457402ed3bd` |
| 适配器版本 | `plan2-vggt-depth-v1` |

`outputs/plan2/vggt_environment.json` 的 `environment_complete=true`，`outputs/plan2/vggt_weights.json` 的 `verified=true`。模型来源、版本和权重均被固定，排除了运行期间静默更换权重的情况。

### 5.2 协议锁

协议锁创建于 `2026-07-30T07:15:03+00:00`，当时 `qa_revealed=false`。锁定内容如下：

| 文件/对象 | SHA256 |
|---|---|
| 主配置 | `8ea3e131dc315da8bcd34c8173ae0e0af1d04c93e749b63b96a85a41250d9556` |
| VGGT 适配器 | `db0cad14a715a9ab544dd28b519a2e7cd01f65469aaa6a49cd9d478df278e4a1` |
| 评测器 | `29fbcef76c539dd636ebcbfa60b375b19938441054f451a752191b538b0bc3e6` |
| 公开 episodes | `9d5cd67479fb1e1c8291effb0d77ed876b0cc27d6b7f9cd02eb43045f3022263` |
| 固定 routes | `057c60055eaa0ee59cf8d4d76d1db22a900c3697e2667c495e05ec07336cf888` |
| VGGT 权重 | `f164acf60724910d8fe1578bb499d800850c7bb0948db7555c413f9fbe60467e` |

该锁保证主配置、适配器、评测器、公开输入、路由和权重均先于答案揭示固定。它不能保证模型在其他数据集上的效果，但能保证本次主结果没有因看到答案而修改协议。

## 6. VGGT 适配器及评测链路

冻结适配器的工作链路为：读取公开 RGB 序列，经固定的 518 像素 `pad` 预处理送入 VGGT，使用预测 depth、相机参数和置信度执行 unprojection，再从固定 12% 中心区域提取 donor/target 的候选几何，最后交给未修改的空间关系评测逻辑生成答案。

关键限制如下：

1. 适配器采用 `rgb_only_centered_verification_frame`，没有使用 GT 目标 mask 做定位。
2. 12% center crop 是冻结的弱 grounding 假设，不等价于真实目标检测或跟踪。
3. `depth_unprojection` 能生成数值几何，不代表生成的点必然属于被询问的目标。
4. 相机/深度的重访一致性只检查几何稳定性，不直接保证空间问答答案正确。
5. 路由、导航和观测成本不由 VGGT 学习或规划。

## 7. 执行过程与时间线

所有长任务均在主机本地 tmux 持久会话中运行，并把 stdout/stderr 写入 `/224010104/Jerry/logs/plan2/`。关键时间线如下：

| UTC 时间 | 事件 | 结果 |
|---|---|---|
| 07:15:03 | 首次 runner 启动，绑定 GPU 0；创建协议锁 | 成功 |
| 07:16:43 | smoke group checkpoint 写入 | 1/1 group 成功 |
| 07:16:58 | smoke 阶段及其检查完成 | 成功 |
| 07:16:58 后 | smoke 后 GPU 资源发生竞态，原 runner 的原子复核返回资源不足 | 未破坏 checkpoint |
| 07:17:17 | 从已验证 checkpoint 恢复，第二次 runner 启动，绑定 GPU 1 | 成功 |
| 07:18:52 | 30-group VGGT manifest 完成 | 30/30 成功 |
| 07:19:20 | `full` 与 `diagnose` 完成 | 成功 |
| 07:19:21 | `decide` 完成，runner 退出 | 退出码 0 |
| 07:24:30 | 最终结果提交 | `db3a189...` |
| 07:24:35 | 推送 GitHub 完成 | 成功 |

第一次运行后的资源不足不是模型崩溃，也不是实验从头重跑。第二次运行校验已有 smoke checkpoint 后继续处理剩余 group，证明了方案的断点恢复路径确实参与了真实执行。

## 8. M0：CUT3R 基线复核与诊断

基线复核结果：

- 30 groups、360 questions 均存在。
- CUT3R 准确率为 56.11%。
- GT 3D 准确率为 100.00%。
- GT 到 CUT3R 的下降为 43.89 个百分点。
- 七项输入/输出哈希一致。
- 评测器回归结果一致。
- `original_gate7_modified=false`。

CUT3R 本身仍未通过原 Gate 7 的 10 个百分点门槛，但它是本次 VGGT 替换实验的冻结比较基线。诊断文件记录了 30/30 groups 成功、失败数为 0、`private_data_used_by_adapter=false`，且所有诊断均标记为 `diagnostic_only=true`。

### 8.1 CUT3R 错误分层

| 分层 | 问题数 | 错误率 |
|---|---:|---:|
| `branch/fresh_stable` | 120 | 44.17% |
| `branch/risk_stable` | 120 | 49.17% |
| `branch/risk_stale` | 120 | 38.33% |
| `question_type/front_behind` | 90 | 74.44% |
| `question_type/left_right` | 90 | 36.67% |
| `question_type/target_nearer` | 90 | 32.22% |
| `question_type/which_closer` | 90 | 32.22% |

`front_behind_current_labels` 中 `behind=90`、`front=0`，说明该问题类型存在严重标签不平衡。该事实限制了对 front/behind 泛化能力的解释，也说明后续应另建平衡诊断集；但不能因此修改或丢弃本次固定主集结果。

此外，CUT3R 诊断记录 `dense_reprojection_reported=false`、`dense_selected_point_mask_available=false`。因此，现有 CUT3R 诊断不足以证明某个具体坐标适配器 bug，也不能把其全部错误归因到 crop、相机外参或 backbone 中的任一单点。

## 9. M1：环境和权重审计结果

M1 完成了以下检查：

- VGGT 源码提交固定且可审计。
- Python、Torch 与 CUDA 版本满足适配器运行要求。
- 模型权重大小和 SHA256 与配置一致。
- License 与 README 的哈希已记录。
- 所有模型、环境和生成文件均位于 `/224010104/Jerry` 工作区内。
- 未修改 `/224010104/Jerry` 之外的其他用户文件。

结论：环境和权重审计通过，可以进入 smoke；这一结论只表示依赖和模型身份可信，不表示模型质量达标。

## 10. M2：smoke、协议锁与恢复准备

预注册 smoke group 为 `s_02195803c5ebd4d3dcb170a7`，包含 12 个问题。其 checkpoint 状态为 `success`，私有文件打开计数为 0。

| smoke 指标 | 值 |
|---|---:|
| 总帧数 | 10 |
| stable 序列耗时 | 5.4702 秒 |
| stale 序列耗时 | 0.7833 秒 |
| checkpoint 记录总耗时 | 6.2534 秒 |
| 峰值 GPU allocated bytes | 6,924,430,848 |
| 输入/输出 group | 1/1 成功 |

smoke 中 stable 序列包含首次执行的冷启动影响，不能直接与全量阶段的中位序列耗时等同。smoke 的主要作用是验证输入、输出、几何结构、无私有读取和 checkpoint 写入链路，而不是估计最终吞吐量。

## 11. M3：30-group 主实验结果

### 11.1 路由策略总体结果

| 策略 | 准确率 | stale 准确率 | 移动步数 | 新观测数 | 几何失败数 |
|---|---:|---:|---:|---:|---:|
| Trust3D-GT | 100.00% | 100.00% | 2,422 | 360 | 0 |
| Trust3D-RGB-D | 100.00% | 100.00% | 2,422 | 360 | 0 |
| Trust3D-VGGT | 45.00% | 41.67% | 2,422 | 360 | 0 |
| Always-Reobserve-VGGT | 45.00% | 41.67% | 3,724 | 540 | 0 |
| Persistent-3D-VGGT | 42.22% | 33.33% | 0 | 0 | 0 |

Trust3D-VGGT 与 Always-Reobserve-VGGT 的准确率相同，但 Trust3D 使用更少的移动和新观测。这说明既有 freshness 路由仍保持了成本优势；它不说明 VGGT 几何质量足够，因为两者的绝对准确率都只有 45%。Persistent-3D-VGGT 的 stale 准确率降至 33.33%，与“陈旧状态需要重观察”的预期一致。

### 11.2 Gate 7 判定

| 条件 | 实际值 | 是否通过 |
|---|---:|---|
| 请求的 30 groups 全部完成 | 30/30 | 是 |
| camera geometry group failure rate | 0 | 是 |
| query geometry failure rate | 0 | 是 |
| private leakage count | 0 | 是 |
| GT 到 VGGT 的准确率下降不超过 10 个百分点 | 55.00 个百分点 | **否** |

最终 `gate7_vggt_pass=false`、`result_status=failure_analysis`、`gate8_may_be_planned=false`。唯一失败的是预注册质量条件，但该条件是决定性条件，不能因其他工程条件通过而忽略。

## 12. 分支与问题类型结果

### 12.1 分支 × 问题类型

| 分支 | `front_behind` | `left_right` | `target_nearer` | `which_closer` | 分支均值 |
|---|---:|---:|---:|---:|---:|
| `fresh_stable` | 10.00% | 63.33% | 56.67% | 56.67% | 46.67% |
| `risk_stable` | 10.00% | 63.33% | 56.67% | 56.67% | 46.67% |
| `risk_stale` | 13.33% | 33.33% | 60.00% | 60.00% | 41.67% |

最明显的失败是 `front_behind`，三个分支均接近 10%。`left_right` 在 stable 分支为 63.33%，但在 stale 分支降至 33.33%；两个距离类问题维持在 56.67%-60.00%。这表明错误并非均匀分布：方向关系尤其脆弱，而距离关系相对较好，但仍不足以满足总体验收门槛。

`fresh_stable` 与 `risk_stable` 的四类结果完全一致，说明在稳定场景中 freshness 路由选择没有引入额外答案损失。`risk_stale` 的主要额外下降来自 `left_right`，而不是两个距离类问题。

### 12.2 对象类型

| 对象类型 | 问题数 | 准确率 |
|---|---:|---:|
| Box | 12 | 66.67% |
| CellPhone | 60 | 40.00% |
| CreditCard | 36 | 8.33% |
| DishSponge | 12 | 66.67% |
| Egg | 24 | 58.33% |
| Glassbottle | 24 | 41.67% |
| HandTowel | 12 | 58.33% |
| Newspaper | 12 | 16.67% |
| PepperShaker | 12 | 58.33% |
| SaltShaker | 12 | 75.00% |
| SoapBottle | 12 | 41.67% |
| SprayBottle | 12 | 16.67% |
| Statue | 72 | 61.11% |
| Vase | 48 | 39.58% |

CreditCard、Newspaper 和 SprayBottle 的准确率最低，而 SaltShaker、Box 和 DishSponge 较高。不过多数对象只有 12-24 个问题，样本量过小，不能把对象间差异解释为稳定的类别能力排序。

## 13. VGGT 与 CUT3R 的公平比较

同一 30 groups 上：

- `VGGT accuracy = 0.4500`。
- `CUT3R accuracy = 0.561111...`。
- 配对差 `VGGT - CUT3R = -0.111111...`。
- 10,000 次 group bootstrap 95% CI 为 `[-0.213888..., -0.008333...]`。

换算为百分点后，VGGT 比 CUT3R 低 11.11 个百分点，区间表示在本数据和 group 重采样定义下，差异大致位于 VGGT 低 21.39 至 0.83 个百分点之间。区间没有跨过 0，因此“VGGT 在当前协议下至少不差于 CUT3R”不受数据支持。

这个统计结论只适用于本次冻结适配器和 30-group 评测分布。它不能推出 VGGT 架构在所有三维任务中普遍差于 CUT3R，也不能排除更好的 RGB grounding、坐标适配或任务头能够改善 VGGT。但在提交本实验主结论时，不能用这些尚未验证的可能性覆盖当前负结果。

## 14. 几何一致性、时延与显存

### 14.1 几何一致性

| 指标 | count | mean | median | p95 |
|---|---:|---:|---:|---:|
| static structure camera revisit error | 120 | 0.009656 | 0.000126 | 0.023213 |
| movable object revisit error | 120 | 0.014218 | 0.002646 | 0.052767 |

两类重访误差的统计值较低，且没有触发 `diagnostic_failures`。但这些指标仅表示适配器输出在其评测定义下具有一定重访一致性。一个稳定落在背景上的 crop 也可能产生低重访误差，因此**低几何一致性误差不等于目标身份正确，更不等于空间问答正确**。

### 14.2 时延

| 指标 | 值 |
|---|---:|
| 模型加载时间 | 13.9015 秒 |
| stable 五帧序列 mean / median / p95 | 0.9664 / 0.7933 / 1.0687 秒 |
| stale 五帧序列 mean / median / p95 | 0.7906 / 0.7907 / 0.8239 秒 |
| 状态复用总耗时 | 52.7119 秒 |
| 摊销到每个问题的耗时 | 0.1464 秒 |
| 按问题重复推理的估算耗时字段 | 326.8179 秒 |
| 状态复用节省比例 | 83.87% |

状态按 group 复用明显降低了重复推理成本。这里的时延来自当前 A100、`bfloat16` 和批处理实现，不能直接外推到其他 GPU 或实时机器人平台。

### 14.3 显存

全量评测记录的峰值 allocated bytes 为 `6,942,184,960`，约 6.94 GB（十进制）或 6.47 GiB。smoke checkpoint 为 `6,924,430,848` bytes。实际 GPU 准入仍按 60,000 MiB 空闲显存执行，因为准入门槛还需要覆盖模型加载、CUDA 上下文、临时张量和共享服务器上的并发风险，不能只按 `peak_allocated_bytes` 反推最低安全显存。

## 15. 断点续传与完整性审计

全量任务按 group 保存 30 个独立 checkpoint。恢复审计结果如下：

- `checkpoint_recovery_pass=true`。
- 恢复前后 32 个受检文件的 SHA256 全部一致。
- checkpoint 数量为 30。
- 纯恢复运行的模型加载时间为 0 秒。
- 恢复前哈希：`/224010104/Jerry/logs/plan2/recovery-before.sha256`。
- 恢复后哈希：`/224010104/Jerry/logs/plan2/recovery-after.sha256`。

这说明已验证完成的 group 在恢复时被正确跳过，恢复过程没有改写既有结果。tmux 只能保证当前主机上的终端断开不终止进程；真正应对主机重启的是工作区内的 checkpoint、manifest、状态文件和哈希审计，而不是 tmux 本身。

## 16. M4：条件诊断结果

主实验失败后，方案按预注册条件检查是否需要运行 8% 和 18% crop 变体。结果如下：

| 触发条件 | 值 | 满足 |
|---|---:|---|
| 主结果 QA drop > 10 个百分点 | 55.00 个百分点 | 是 |
| 12% crop purity median < 0.90 | 0.0000 | 是 |
| 高低 purity 四分位可分 | 0.02587 对 0.0000 | 是 |
| 高低 purity 准确率差至少 +20 个百分点 | -7.29 个百分点 | **否** |

高 purity 四分位与低 purity 四分位各含 8 groups：

- 高 purity 四分位准确率：36.46%。
- 低 purity 四分位准确率：43.75%。
- `high - low = -7.29` 个百分点。

由于最后一个条件方向相反且没有达到 +20 个百分点，联合触发条件不成立，`triggered=false`，`variant_results={}`。因此没有运行 8%/18% crop 变体，12% 主结果也没有被覆盖。

crop purity 中位数为 0 是重要警报，表明中心 crop 对目标的覆盖很弱；但是 purity 更高的四分位并未获得更高准确率，所以当前数据**不能证明中心 crop 是唯一主因或充分主因**。GT mask 只用于主结果完成后的诊断 purity 计算，没有进入 VGGT 适配器预测链路。

## 17. Gate 7 失败原因分析

### 17.1 已被直接证实的事实

1. 失败不是 group 缺失、几何异常退出、私有数据泄漏或恢复损坏造成的。
2. VGGT 主准确率为 45%，比 GT 低 55 个百分点，比 CUT3R 低 11.11 个百分点。
3. `front_behind` 是最弱问题类型，三个分支只有 10.00%-13.33%。
4. 12% center crop purity 中位数为 0，说明当前 grounding 与真实目标位置经常不一致。
5. purity 较高 group 没有表现出更高准确率，因此尚不能把全部失败归咎于 crop。
6. 几何重访误差较低但问答准确率低，证明“输出自洽”与“任务语义正确”之间仍有明显差距。

### 17.2 最可能的误差链路

综合现有证据，失败更可能来自多个环节叠加，而不是一个已经被定位的单点故障：

1. **RGB 目标 grounding 较弱。** 固定中心 crop 不知道目标实例身份，容易抽取背景或错误物体的深度点。
2. **几何到任务关系的适配不足。** VGGT 的通用相机/深度输出需要经过坐标约定、外参方向、尺度和 donor/target 角色映射；`front_behind` 的极低准确率提示方向关系链路尤其脆弱。
3. **冻结通用几何与小物体任务存在域差距。** CreditCard、Newspaper、SprayBottle 等类别表现很低，可能受到目标尺寸、纹理、遮挡和中心偏移共同影响。
4. **一致性指标无法验证语义身份。** 即使同一背景区域在重访时非常稳定，也不能回答目标物体之间的关系。
5. **诊断分辨率仍不足。** 当前没有密集 selected-point mask 与完整 dense reprojection 归因，无法把误差严格分解为 backbone、相机姿态、crop 或规则头的独立贡献。

因此，最严谨的表述是：**当前冻结 VGGT 到 Trust3D 空间问答的适配链路整体不达标，中心 crop 是明确风险点，但尚未被证明为唯一原因；现有结果也没有证明一个确定的相机坐标 adapter bug。**

## 18. 运维协议偏差及其影响

原方案文档曾采用较慢的 GPU 轮询和连续多次空闲确认。执行前根据用户明确要求，监测器调整为：

- 轮询间隔由 60 秒改为 1 秒。
- 删除“连续 3 次确认”。
- 单次命中后立即执行一次原子准入复核，再启动任务。

这是已披露的运维偏差。它改变的是空闲 GPU 的获取时机和竞态窗口，不改变公开输入、模型权重、适配器、评测器、随机种子、30-group 样本、主门槛或 bootstrap 协议，因此不改变科学比较定义。

实际执行中，smoke 后确实发生一次资源竞态；runner 没有强行覆盖其他任务，而是返回资源不足并保留 checkpoint，随后在 GPU 1 可用时恢复。这一行为符合服务器安全边界，也验证了原子复核和断点续传的必要性。

## 19. 局限与不可支持的结论

### 19.1 局限

- 主集只有 30 groups，虽有 360 个问题，但组内问题相关，不能当作 360 个独立场景。
- `front_behind` 当前标签全部为 `behind`，类别不平衡限制了方向关系的外推。
- 只测试一个冻结 VGGT 版本、一个 12% center crop 和一个固定任务适配器。
- 没有独立开发集用于选择 crop、坐标约定或任务头参数。
- 没有目标检测、实例跟踪或 RGB-only bbox grounding。
- CUT3R 与 VGGT 的通用三维输出形式不同，公平性来自下游协议固定，不代表适配器成熟度完全相同。
- 评测在单一服务器环境完成，时延和显存不能直接外推到所有硬件。

### 19.2 本实验不支持的结论

- 不支持“VGGT 可以替换 CUT3R”。
- 不支持“VGGT 已完成导航规划”。
- 不支持“VGGT 架构普遍差于 CUT3R”。
- 不支持“Gate 7 失败完全由 center crop 引起”。
- 不支持“低重访误差说明空间问答几何正确”。
- 不支持在当前 30 groups 上后验选择 8%/18% crop 并覆盖 45% 主结果。
- 不支持进入 Gate 8 或把当前结果包装为通过。

## 20. 最终结论与后续建议

方案 2 已完整执行并达到了失败分析目的：冻结 VGGT 后端能够在服务器上稳定运行，30/30 groups 和 360/360 questions 全部完成，工程失败率和私有泄漏均为 0，断点恢复与哈希审计通过；但其主准确率只有 45.00%，相对 GT 下降 55.00 个百分点，并以组级 bootstrap 显著低于 CUT3R 11.11 个百分点。

最终判定为：

- **执行状态：成功完成。**
- **工程状态：通过。**
- **质量状态：Gate 7 失败。**
- **替换结论：当前 VGGT 适配器不能替换 CUT3R。**
- **后续 Gate：Gate 8 不得进入。**

后续工作应建立新的、与本次主结果隔离的协议，不覆盖现有文件，并按以下优先级推进：

1. 增加相机旋转、外参方向、坐标轴符号和 donor/target 交换的合成单元测试，先排除可证明的适配器错误。
2. 在独立开发集上实现 RGB-only detector/tracker/bbox grounding，替换无语义的 center crop；GT mask 只能用于诊断和离线评价。
3. 增加 selected-point 可视化、密集重投影和实例覆盖率报告，把 backbone 几何与目标定位误差分开。
4. 建立 front/behind 标签平衡的诊断集，同时保留当前 30-group 主结果不变。
5. 冻结新协议后重新比较 VGGT 与 CUT3R；只有 GT 到后端的下降不超过 10 个百分点，才可重新讨论 Gate 7 通过和 Gate 8。

## 21. 结果文件、日志与复现索引

### 21.1 机器可读结果

| 内容 | 路径 |
|---|---|
| 主配置 | `configs/plan2_vggt.json` |
| CUT3R 基线 | `outputs/plan2/baseline.json` |
| CUT3R 诊断 | `outputs/plan2/cut3r_diagnostics.json` |
| VGGT 环境 | `outputs/plan2/vggt_environment.json` |
| VGGT 权重审计 | `outputs/plan2/vggt_weights.json` |
| 协议锁 | `outputs/plan2/protocol_lock.json` |
| 30-group checkpoint | `outputs/plan2/vggt_geometry/checkpoints/` |
| 几何 manifest | `outputs/plan2/vggt_geometry/manifest.json` |
| 预测 | `outputs/plan2/vggt_predictions.jsonl` |
| 主评测 | `outputs/plan2/vggt_validation.json` |
| 条件诊断 | `outputs/plan2/conditional_diagnostics.json` |
| 恢复审计 | `outputs/plan2/checkpoint_recovery.json` |
| 最终判定 | `outputs/plan2/final_decision.json` |
| 当前状态 | `outputs/plan2/status.json` |

### 21.2 最终关键文件 SHA256

| 文件 | SHA256 |
|---|---|
| `outputs/plan2/final_decision.json` | `d36a8bc74de8fc4cc0ac34cce715526f6eb5bb6d9514fece98a15a82041e897a` |
| `outputs/plan2/vggt_validation.json` | `13759a89a66b89ea129e94ef638605f28c575780ca9d9ea53a0c8f4b794cb98b` |
| `outputs/plan2/vggt_predictions.jsonl` | `27ccbf98703eba4d48217747d86695cc8a9cc4e79ea25f314cb324eedc9df3f8` |

### 21.3 执行入口与日志

| 内容 | 路径 |
|---|---|
| 主 runner | `scripts/run_plan2.sh` |
| GPU 监测启动器 | `scripts/start_plan2_gpu_watch.sh` |
| GPU 监测器 | `scripts/watch_plan2_gpu.sh` |
| GPU 监测日志 | `/224010104/Jerry/logs/plan2/gpu-watch.log` |
| 首次运行日志 | `/224010104/Jerry/logs/plan2/20260730T071503Z-resume.log` |
| 恢复运行日志 | `/224010104/Jerry/logs/plan2/20260730T071717Z-resume.log` |
| 状态审计日志 | `/224010104/Jerry/logs/plan2/status-inspection.log` |
| 本报告编写审计 | `/224010104/Jerry/logs/plan2/report-authoring.log` |

### 21.4 Git 追踪

- 主实验冻结提交：`6f8514de5dfef93e162e1a7a07e2d5ff728ab501`。
- 最终结果提交：`db3a18935cc4178ab002de78cc03decf059077d4`。
- 远端仓库：`git@github.com:0ct0ber16/Trust3D.git`。

本报告中的所有主指标均来自上述机器可读结果；解释性结论遵守主结果不可后验覆盖、私有数据不可进入预测链路和 Gate 8 不得提前进入的约束。
