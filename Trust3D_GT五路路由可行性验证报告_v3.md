# Trust3D GT 五路路由可行性验证报告 v3

## 1. 最终结果

- 最终状态：`complete`
- 五路路由可行性验收：通过
- 协议版本：`gt-five-route-v3`
- 确认集：100 groups（sealed60 + replication40），每路 20 groups
- Git commit：`e9abc400b5ddb158764bc8cc05aee72410baa224`

通过仅表示：在可靠 GT 证据和预注册平衡压力测试下，完整五路控制器风险受控，且相对 always-reobserve 保持准确率并降低成本。该结论不等于 RGB-only 端到端系统已经成立，也不改写原 Gate 7 失败。

## 2. v2 停止原因与 v3 修订

v2 pilot 的路由契约通过，但最坏情形功效公式得到 N=4958，因此状态为 `inconclusive_underpowered`。v3 保留该结果，只将该公式降为敏感性分析，并用固定 100-group 确认集、有限样本精确区间和独立复现集检验实际主张。

## 3. 数据与防泄漏

- sealed60 未揭示审计：`eligible_unrevealed`
- development20：仅用于开发，不进入主指标
- replication40：五路各 8，来源与 development/sealed 不重叠
- confirmatory100 source overlap：0
- inference private open count：0
- evaluator private open count：1

## 4. 契约和独立 oracle

- 全因子 case：32，全部通过
- mutation：7，全部杀死
- development route match：20/20
- confirmatory route match：100/100
- avoidable route regret：0

## 5. 准确率、风险与成本

- Trust3D 总体准确率：80/100 = 0.8000
- always-reobserve 总体准确率：20/100 = 0.2000
- 配对差：0.6000；Tango 单侧 95% 下界：0.6000
- coverage：0.8000
- selective error：0/80；单侧 95% 上界：0.0368
- unsafe answer：0/100；单侧 95% 上界：0.0295
- false abstain：0/80；单侧 95% 上界：0.0368
- 成本下降：0.8223；scene-cluster bootstrap 单侧 95% 下界：0.6484

## 6. 分层复现

- sealed60 route match：60/60
- replication40 route match：40/40

## 7. 在线执行与恢复

- AI2-THOR REOBSERVE：20 groups，全部通过
- 实际 source ledger 与公开成本估计不一致：4/20；验收按实际执行 ledger，不用估计值替代
- 其余四路确定性副作用：80 groups，全部通过
- confirmatory100 在线覆盖：100/100
- 人为中断退出码：124，随后从 checkpoint 恢复
- CPU 单元 checkpoint：700/700，重复恢复哈希不变

## 8. 逐项验收

- `accuracy_noninferiority_point`：通过
- `accuracy_noninferiority_tango_lower`：通过
- `avoidable_route_regret_zero`：通过
- `confirmatory_group_count`：通过
- `cost_reduction_cluster_lower`：通过
- `cost_reduction_point`：通过
- `coverage`：通过
- `false_abstain_exact_upper`：通过
- `five_routes_twenty_each`：通过
- `router_matches_oracle_100_of_100`：通过
- `sealed_and_replication_route_match`：通过
- `selective_error_exact_upper`：通过
- `stale_error_below_unconditional_history`：通过
- `unsafe_answer_exact_upper`：通过

## 9. 结论边界

本实验隔离验证的是 controller：当 current/history/3D/reobserve/abstain 所需证据可靠且语义明确时，路由器可以稳定选择正确路线，并获得 accuracy-cost-risk 优势。CUT3R/VGGT 的 RGB 几何误差仍由 Gate 7/B 线单独评价；只有 A、B 均通过且 C 线联合实验通过，才能支持完整 RGB 五路 Trust3D 主结果。
