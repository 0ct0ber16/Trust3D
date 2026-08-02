# Trust3D GT 五路路由可行性验证详细报告 v3

## 1. 实验结论总览

| 项目 | 结果 |
|---|---|
| 最终状态 | `complete` |
| 五路路由可行性验收 | **通过** |
| 协议版本 | `gt-five-route-v3` |
| 主确认集 | 100 groups（sealed60 + replication40） |
| 五路支持量 | 每路 20 groups |
| 路由与独立 oracle 一致率 | 100/100 = **100.00%** |
| Trust3D 总体准确率 | 80/100 = **80.00%** |
| Trust3D coverage | 80/100 = **80.00%** |
| Avoidable route regret | 0/100 |
| 在线验证 | 20 个 REOBSERVE 真实执行通过；其余 80 个路由副作用检查通过 |
| Checkpoint 恢复 | 700/700 单元完整，恢复前后哈希一致 |
| 冻结代码 commit | `e9abc400b5ddb158764bc8cc05aee72410baa224` |
| 结果归档 commit | `d84b502b619a0a8b7c4a45af651175fb12449726` |

**结论：**在 current view、history、GT 3D memory、在线重观察和 abstain 所需证据可靠且语义明确时，Trust3D 五路控制器能够稳定选择独立 oracle 给出的最低损失路线，在风险受控的同时，相对 always-reobserve 保持更高准确率并显著降低成本。

该结论只证明**可靠 GT 证据条件下的五路控制器可行性和上界**。它不证明 CUT3R/VGGT 驱动的 RGB-only 端到端系统已经成立，也不改写原 Gate 7 的失败结果。

## 2. 实验目标与评价口径

### 2.1 核心问题

本实验回答以下问题：

1. 当五类证据路线都可被独立构造时，控制器能否正确选择 `USE_CURRENT_VIEW`、`RETRIEVE_HISTORY`、`QUERY_3D_MEMORY`、`REOBSERVE` 或 `ABSTAIN`？
2. 与固定使用某一路线或旧二路策略相比，五路控制器能否同时改善 accuracy、coverage、risk 和 cost？
3. 离线路由能否在 AI2-THOR 中产生正确的在线执行副作用，并在中断后从 checkpoint 恢复？

### 2.2 “准确率 80%”与“路由命中 100%”为什么不矛盾

确认集预先平衡为每路 20 groups。独立 oracle 在 20 个不可安全作答的 group 上选择 `ABSTAIN`；这些 group 的正确控制行为是“不输出答案”，但总体 answer accuracy 仍将未答计为不正确。因此：

| 指标 | 计分对象 | Trust3D 结果 |
|---|---|---:|
| Route match | 是否选择独立 oracle 的最佳动作 | 100/100 = 100.00% |
| Answer accuracy | 是否输出正确答案；ABSTAIN 计为未答 | 80/100 = 80.00% |
| Coverage | 实际输出答案的比例 | 80/100 = 80.00% |
| Selective error | 已回答样本中的错误比例 | 0/80 = 0.00% |
| False abstain | 本可安全回答却 abstain | 0/80 = 0.00% |

因此，80% 不是 20 个路由错误，而是确认集中预先设置了 20 个应当拒答的安全压力样本。

## 3. 数据、协议与防泄漏

### 3.1 数据分层

| 数据层 | Group 数 | 每路支持量 | 用途 | 是否进入主指标 |
|---|---:|---:|---|---|
| development20 | 20 | 4 | 开发路由契约和实现检查 | 否 |
| sealed60 | 60 | 12 | 未揭示主确认层 | 是 |
| replication40 | 40 | 8 | 全新 source 独立复现层 | 是 |
| confirmatory100 | 100 | 20 | sealed60 + replication40 汇总主结果 | 是 |

三层 source 隔离审计结果：

| 检查项 | 结果 |
|---|---:|
| development 与 sealed source overlap | 0 |
| development 与 replication source overlap | 0 |
| sealed 与 replication source overlap | 0 |
| confirmatory100 内 source overlap | 0 |
| sealed60 未揭示资格 | `eligible_unrevealed` |
| Inference 打开 private GT 次数 | 0 |
| Evaluator 打开 private GT 次数 | 1 |

### 3.2 五路动作定义

| 路由 | 使用的证据 | 适用条件 | 主要成本 | 安全语义 |
|---|---|---|---|---|
| `USE_CURRENT_VIEW` | 当前第一视角观测 | 当前视图足以回答且风险低 | 当前感知/VLM | 不读取不可见对象状态 |
| `RETRIEVE_HISTORY` | 历史事实或非几何证据 | 历史事实仍新鲜且无需三维计算 | 历史检索 | 不把 stale 事实当作当前事实 |
| `QUERY_3D_MEMORY` | 带时间和坐标系的历史三维证据 | 问题需要空间计算且几何仍有效 | 一次几何查询 | 不使用失效或未观测几何 |
| `REOBSERVE` | 新传感器观测 | 旧证据风险过高且目标可重观察 | 移动、观察、感知 | 到达可见位姿后才更新记忆 |
| `ABSTAIN` | 无充分可靠证据 | 所有可答路线风险均不可接受 | 0 | 不生成默认答案或猜测 |

### 3.3 v2 停止原因与 v3 修订

v2 pilot 的路由契约通过，但最坏情形功效公式得到所需样本量 `N=4958`，因此旧结果保留为 `inconclusive_underpowered`。v3 不覆盖该结果，也不降低门槛；它使用固定 100-group 确认集、每路平衡支持、有限样本精确区间、配对 Tango 区间和 scene-cluster bootstrap 检验实际主张。

## 4. 路由契约与独立 Oracle

| 检查 | 样本量 | 结果 |
|---|---:|---:|
| 五个能力开关全因子组合 | 32 cases | 32/32 通过 |
| 预登记 mutation test | 7 mutations | 7/7 被测试杀死 |
| Development 路由匹配 | 20 groups | 20/20 |
| Confirmatory 路由匹配 | 100 groups | 100/100 |
| 独立 oracle 存储损失复算不一致 | 100 groups | 0 |
| 独立 oracle near tie | 100 groups | 0 |
| Avoidable route regret | 100 groups | 0 |

独立 oracle 不调用 Trust3D 路由器，而是分别枚举五路可行性、答案损失、风险和真实成本后选择最低 loss 路线。路由器在 confirmatory100 上与该 oracle 完全一致。

## 5. 离线五路路由结果

### 5.1 七种策略对比

| 方法 | Oracle 路由命中 | 总体准确率 | Coverage | Selective error | Unsafe answer | Avoidable regret | 平均成本 |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Trust3D 五路主策略** | **100/100** | **80.00%** | **80.00%** | **0/80 = 0.00%** | **0/100** | **0** | **0.040922** |
| Legacy 二路策略 | 60/100 | 60.00% | 60.00% | 0/60 = 0.00% | 0/100 | 40 | 0.152061 |
| Always-Current | 20/100 | 20.00% | 20.00% | 0/20 = 0.00% | 0/100 | 80 | 0.020005 |
| Always-History | 20/100 | 20.00% | 40.00% | 20/40 = 50.00% | 20/100 | 80 | 0.000001 |
| Always-3D-Memory | 20/100 | 20.00% | 20.00% | 0/20 = 0.00% | 0/100 | 80 | 0.030003 |
| Always-Reobserve | 20/100 | 20.00% | 20.00% | 0/20 = 0.00% | 0/100 | 80 | 0.230300 |
| Always-Abstain | 20/100 | 0.00% | 0.00% | 不适用 | 0/100 | 80 | 0.000000 |

这张表应按“平衡路线压力测试”理解：每个固定单路策略只在属于自身路线的 20 个 group 上适用，因此 Always-Reobserve 在本实验中的 20% 准确率不能与原 Gate 4 自然二路数据上的 100% 直接横向比较。

主要观察：

- 五路策略相对 Legacy 二路策略将 route match 从 60% 提高到 100%，总体准确率从 60% 提高到 80%，并将平均成本从 0.152061 降至 0.040922。
- Always-History 在 stale group 上产生 20 个 unsafe answer，selective error 达到 50%；五路策略的 unsafe answer 和 selective error 均为 0。
- Always-Abstain 虽然没有 unsafe answer，但产生 80/80 false abstain，无法满足 coverage 要求。
- 五路策略是唯一同时做到五路完全命中、零 avoidable regret、80% coverage 和零已答错误的策略。

### 5.2 五条路由的确认支持

| 路由 | Oracle 支持量 | Trust3D 选择量 | 路由命中 | 预期执行结果 |
|---|---:|---:|---:|---|
| `USE_CURRENT_VIEW` | 20 | 20 | 20/20 | 使用当前视图回答 |
| `RETRIEVE_HISTORY` | 20 | 20 | 20/20 | 检索仍新鲜的历史事实 |
| `QUERY_3D_MEMORY` | 20 | 20 | 20/20 | 查询仍有效的三维记忆 |
| `REOBSERVE` | 20 | 20 | 20/20 | 移动到可见位姿并获取新观察 |
| `ABSTAIN` | 20 | 20 | 20/20 | 不输出不安全答案 |

### 5.3 准确率、风险与成本门槛

| 指标 | 预注册门槛 | 实测结果 | 判定 |
|---|---:|---:|---|
| Coverage | >= 75% | 80.00% | 通过 |
| 配对准确率差（Trust3D - Always-Reobserve） | 点估计 >= -2 个百分点 | +60.00 个百分点 | 通过 |
| Tango 单侧 95% 下界 | >= -2 个百分点 | +60.00 个百分点 | 通过 |
| Selective error | 0，且单侧 95% 上界 <= 5% | 0/80；上界 3.68% | 通过 |
| Unsafe answer | 0，且单侧 95% 上界 <= 5% | 0/100；上界 2.95% | 通过 |
| False abstain | 0，且单侧 95% 上界 <= 5% | 0/80；上界 3.68% | 通过 |
| Avoidable route regret | 0 | 0/100 | 通过 |
| 相对 Always-Reobserve 成本下降 | 点估计 >= 20% | 82.23% | 通过 |
| 成本下降 scene-cluster bootstrap 单侧 95% 下界 | > 0 | 64.84% | 通过 |

成本下降使用 59 个 scene 聚类、100,000 次固定 seed bootstrap。成本下降双侧 95% 区间为 [62.27%, 91.13%]。

### 5.4 分层复现

| 数据层 | Groups | 每路支持量 | Route match | 总体准确率 | Coverage | Selective error | Unsafe answer | 平均成本 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| sealed60 | 60 | 12 | 60/60 | 80.00% | 80.00% | 0/48 | 0/60 | 0.040855 |
| replication40 | 40 | 8 | 40/40 | 80.00% | 80.00% | 0/32 | 0/40 | 0.041022 |
| confirmatory100 | 100 | 20 | 100/100 | 80.00% | 80.00% | 0/80 | 0/100 | 0.040922 |

sealed60 与全新 replication40 得到相同的路由命中率、准确率、coverage 和零错误结果，说明主结论不是由单一数据层驱动。

## 6. AI2-THOR 在线执行与恢复

### 6.1 在线执行结果

| 在线检查 | 覆盖量 | 结果 |
|---|---:|---|
| `REOBSERVE` 真实 AI2-THOR 执行 | 20 groups | 全部完成 |
| 其余四路确定性副作用检查 | 80 groups | 全部通过 |
| Confirmatory 在线覆盖 | 100 groups | 100/100 |
| 在线 route 与封存离线 route 一致 | 20 个真实执行 source | 全部一致 |
| 新观察是否真实写入 | 20 个 REOBSERVE | 全部写入 |
| 实际移动成本是否匹配 source ledger | 20 个 REOBSERVE | 全部匹配 |
| 在线动作失败 | 20 个 REOBSERVE | 0 |
| 重复 trace | 全部在线 trace | 0 |

在线后端为 `ai2thor_shortest_visible_pose`。REOBSERVE 必须恢复场景、重放历史、施加需要的隐藏干预、执行最短可见路径，并在目标重新可见后才产生新观察和更新事实。

### 6.2 成本估计与真实执行账本

| 项目 | 结果 |
|---|---:|
| 公开候选成本估计与实际 source ledger 一致 | 16/20 |
| 公开估计与实际 ledger 不一致 | 4/20 |
| 实际执行与 source checkpoint ledger 一致 | 20/20 |

4/20 的差异来自公开候选成本是离线路由使用的估计值，而在线 source checkpoint 保存的是实际可执行路径成本。在线验收没有用公开估计掩盖真实执行，而是以 source checkpoint 的真实 ledger 为准；20 个 REOBSERVE 的执行成本全部匹配。该差异已保留在 `online_validation.json`，不能解释为估计器已经精确预测所有真实导航成本。

### 6.3 中断恢复

| 恢复检查 | 结果 |
|---|---:|
| 人为中断退出码 | 124 |
| 中断记录 | 完整 |
| 从 checkpoint 继续 | 成功 |
| CPU 推理单元 checkpoint | 700/700 |
| 恢复前后 predictions 哈希 | 一致 |
| 恢复前后 metrics 哈希 | 一致 |
| GPU 模型加载 | 否 |

该结果证明 tmux 断连或进程中断后，可以从已验证 checkpoint 继续，不需要覆盖重跑已经完成的单元。

## 7. 工程问题与处理记录

在线阶段首次运行暴露了以下兼容性问题，均保留失败日志并在不修改确认集、路由门槛、统计方法或预测结果的前提下修复：

| 问题 | 原因 | 处理 | 科学结果影响 |
|---|---|---|---|
| 单问题 source manifest 缺少 `questions_per_branch` | 既有 builder 在值为 1 时省略该字段 | 按既有 schema 默认值 1，并增加回归测试 | 无 |
| Fresh source checkpoint 未声明 full cache modalities | 单问题 source 使用 RGB-only cache schema | 校验逻辑跟随 manifest 声明 | 无 |
| 历史证据字段存在两种公开 schema | 旧数据使用 `history_observation.rgb`，新数据使用 `history_frames` | 统一公开证据路径适配器 | 无 |
| 在线证据路径仍指向旧固定目录 | 新 replication source 位于独立目录 | 根据实际 source/online root 生成路径 | 无 |
| 公开估计成本与真实路径成本不完全一致 | 估计成本不是执行 ledger | 在线验收改为匹配 source checkpoint 的真实成本，并同时报告 4/20 估计差异 | 不改变离线预测或门槛 |

修复后重新冻结代码；predictions SHA256 保持为 `e29812f17f56570685983bed92f7199501f31434d4d3def71013f3b3b73c5fbb`，已有 evaluator 结果按相同预测哈希复用，没有再次打开 private GT。

## 8. 逐项验收结论

| 验收项 | 中文含义 | 结果 |
|---|---|---|
| `confirmatory_group_count` | 主确认集为固定 100 groups | 通过 |
| `five_routes_twenty_each` | 五路各有 20 个确认 group | 通过 |
| `router_matches_oracle_100_of_100` | 路由与独立 oracle 100/100 一致 | 通过 |
| `sealed_and_replication_route_match` | sealed 与 replication 均完全匹配 | 通过 |
| `avoidable_route_regret_zero` | 无可避免的路由损失 | 通过 |
| `coverage` | Coverage 不低于 75% | 通过 |
| `accuracy_noninferiority_point` | 配对准确率点估计满足非劣 | 通过 |
| `accuracy_noninferiority_tango_lower` | Tango 单侧下界满足非劣 | 通过 |
| `selective_error_exact_upper` | 已答错误精确上界受控 | 通过 |
| `unsafe_answer_exact_upper` | 不安全答案精确上界受控 | 通过 |
| `false_abstain_exact_upper` | 错误拒答精确上界受控 | 通过 |
| `stale_error_below_unconditional_history` | Stale 错误低于无条件历史策略 | 通过 |
| `cost_reduction_point` | 成本下降点估计至少 20% | 通过 |
| `cost_reduction_cluster_lower` | 聚类 bootstrap 成本下降下界大于 0 | 通过 |

## 9. 最终结论与适用边界

本次实验支持以下结论：

1. 在可靠证据条件下，五个动作都有独立实验支持，Trust3D 能够在 100 个平衡压力 group 上 100% 匹配独立 oracle。
2. 五路策略相对旧二路策略提高 route coverage，并相对 always-reobserve 获得显著的 accuracy-cost 优势。
3. 风险指标受控：已回答样本零错误、零 unsafe answer、零 false abstain，所有单侧 95% 精确上界均低于 5%。
4. 20 个 REOBSERVE group 完成真实 AI2-THOR 执行，另外 80 个 group 的非移动副作用检查通过，checkpoint 中断恢复有效。

本次实验**不支持**以下更强结论：

1. 不证明 CUT3R 或 VGGT 已经能够从 RGB 准确恢复任务所需几何。
2. 不证明对象 grounding、相机坐标约定和几何到答案任务头已经解决。
3. 不证明完整 RGB-only 五路 Trust3D 已经通过 Gate 7 或联合 Gate。
4. 不应把平衡路线压力集中的 Always-Reobserve 20% 准确率与原 Gate 4 自然二路数据中的 100% 直接比较。
5. 公开成本估计在 4/20 个在线 source 上与真实路径 ledger 不同，因此不能声称离线成本估计已经完全校准。

因此，最准确的表述是：

> **GT 五路路由控制器的可行性已经得到确认；RGB-only 感知到五路控制器的完整链路仍需由 Gate 7 修复和后续联合实验验证。**

## 10. 结果文件

| 文件 | 作用 |
|---|---|
| `outputs/parallel_v3/gt_five_route/metrics.json` | 离线主指标、分层结果、区间和验收项 |
| `outputs/parallel_v3/gt_five_route/online_validation.json` | AI2-THOR 在线执行、真实成本和恢复检查 |
| `outputs/parallel_v3/gt_five_route/checkpoint_recovery.json` | 700 个 CPU 单元的断点恢复审计 |
| `outputs/parallel_v3/gt_five_route/final_decision.json` | 最终 `complete` 判定 |
| `logs/parallel-v3/gt-five-route-v3.log` | 完整实验过程日志 |
| `Trust3D_GT五路路由可行性验证报告_v3.md` | 本中文详细报告 |
