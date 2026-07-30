# Trust3D Gate 7：CUT3R 与 VGGT 失败原因诊断报告

> 生成时间：2026-07-30T12:22:38.254438+00:00
> 文档性质：固定 30-group 评测集上的后验失败归因，仅用于定位问题
> 原 Gate 7 与方案 2 结果保持不变，本报告中的 oracle 结果不得作为主结果

## 1. 执行结论

本计划已按 P0、D0、D1、D2、D3/D4、D5 顺序执行完成。两个后端均复现原基线并完成 30/30 groups，checkpoint 恢复验证通过。一个 group 的 GT 相机中心零 baseline，未对齐指标仍覆盖 30 groups，pose-aligned 因果效应按预注册规则只在其余 29 个完整 group 上计算并降级为缺失敏感结论。Gate 7 的低准确率不是单一的‘模型前向失败’，而是任务头语义、对象 grounding、相机/坐标与 backbone 几何误差共同作用的结果。

- **CUT3R**：主标签 `proven_task_head_semantics_bug`，缺失敏感几何标签 `inconclusive_small_sample`，29-group 条件式标签 `backbone_geometry_dominant`；grounding +4.31 个百分点，pose +11.21 个百分点，backbone gap +29.89 个百分点。
- **VGGT**：主标签 `proven_task_head_semantics_bug`，缺失敏感几何标签 `inconclusive_small_sample`，29-group 条件式标签 `backbone_geometry_dominant`；grounding +3.74 个百分点，pose +12.07 个百分点，backbone gap +39.94 个百分点。

## 2. 协议、数据与完整性

- 协议 revision：`r001`，SHA256：`bc7564daa3a374c5cb54edc183ad12c70e3b3f917d805dd25252dd69a9c94d44`。
- 归因实现 revision：`r001-analysis-a1`；因零 baseline 缺失处理修正形成不可覆盖 amendment：`outputs/gate7_diagnosis/protocol_locks/r001-analysis-a1.json`（SHA256：`e4cd621f1bdca4ccb1c42f9c20b58706b7f0362be5073476719328af6839d37e`）。
- Git commit：`0d7c6fd53007eb27eec87d20b2d6023bfa9c498c`。
- 数据规模：30 groups，360 questions。
- 原结果锚点：10/10 哈希一致。
- 角色绑定：检查 600 个输入，失败 0 个。
- 恢复验证：`checkpoint_recovery_pass=true`，检查 60 个 checkpoint。

## 3. 基线复现

| 后端 | 复现准确率 | 发布准确率 | 逐题答案匹配 |
|---|---|---|---|
| CUT3R | 56.11% | 56.11% | 360/360 |
| VGGT | 45.00% | 45.00% | 360/360 |

CUT3R 复现 56.11%，VGGT 复现 45.00%。因此后续差异不是 evaluator 漂移或 checkpoint 读取错误造成的。

## 4. 任务头与标签语义

| 变体 | 含义 | 准确率 |
|---|---|---|
| TL | GT 点 + GT pose + legacy 完整相机 z 任务头 | 98.06% |
| T0 | GT 点 + GT pose + 数据定义平面 yaw 任务头 | 100.00% |
| R0 | RGB-D 点 + GT pose + 数据定义任务头 | 100.00% |

legacy 与数据契约共有 7 个问题答案不一致，`task_semantics_mismatch=true`。数据标签的 `front_behind` 忽略 horizon，而 legacy 任务头使用包含 pitch 的完整 camera z；这是确定性语义错误，不是通过 QA 调参选择出的坐标变换。

真实 `front_behind` 标签计数：`{'behind': 90}`。由于当前真实集单类不平衡，类别对称性只能由合成契约证明，不能从本 30-group 结果外推。

## 5. 因果矩阵结果

### 5.1 CUT3R

| 变体 | 准确率 | 95% group bootstrap CI | 有效 groups |
|---|---|---|---|
| B0 | 56.11% | [48.06%, 64.17%] | 30 |
| B1 | 59.72% | [50.56%, 68.33%] | 30 |
| B2 | 67.24% | [58.91%, 75.29%] | 29 |
| B3 | 69.83% | [59.77%, 79.31%] | 29 |
| C0 | 56.11% | [48.05%, 64.17%] | 30 |
| C1 | 60.56% | [51.67%, 68.89%] | 30 |
| C2 | 66.09% | [57.47%, 74.14%] | 29 |
| C3 | 70.11% | [60.34%, 79.03%] | 29 |
| TL | 98.06% | [96.67%, 99.17%] | 30 |
| T0 | 100.00% | [100.00%, 100.00%] | 30 |
| R0 | 100.00% | [100.00%, 100.00%] | 30 |

| 效应 | 点估计 | 95% CI | 方向 |
|---|---|---|---|
| task_head_effect | -0.00 个百分点 | [-1.22, +1.36] 个百分点 | negative |
| grounding_effect | +4.31 个百分点 | [-4.60, +13.22] 个百分点 | positive |
| pose_effect | +11.21 个百分点 | [+5.75, +17.10] 个百分点 | positive |
| interaction | -0.57 个百分点 | [-8.05, +6.32] 个百分点 | negative |
| backbone_gap | +29.89 个百分点 | [+20.40, +39.37] 个百分点 | positive |

2x2 效应使用 29/30 个完整 group；缺失 group：`['s_39ad630766a4d0f9dcbe9445']`。由于存在不可识别 alignment，几何因素主标签按计划降级为 `inconclusive_small_sample`，表中效应仅为 complete-case 条件式描述。

### 5.2 VGGT

| 变体 | 准确率 | 95% group bootstrap CI | 有效 groups |
|---|---|---|---|
| B0 | 45.00% | [36.11%, 53.33%] | 30 |
| B1 | 48.61% | [39.17%, 57.78%] | 30 |
| B2 | 54.60% | [44.83%, 63.79%] | 29 |
| B3 | 58.05% | [48.28%, 67.24%] | 29 |
| C0 | 44.72% | [35.28%, 53.61%] | 30 |
| C1 | 48.61% | [38.89%, 58.06%] | 30 |
| C2 | 56.61% | [46.84%, 65.80%] | 29 |
| C3 | 60.06% | [50.29%, 69.25%] | 29 |
| TL | 98.06% | [96.67%, 99.17%] | 30 |
| T0 | 100.00% | [100.00%, 100.00%] | 30 |
| R0 | 100.00% | [100.00%, 100.00%] | 30 |

| 效应 | 点估计 | 95% CI | 方向 |
|---|---|---|---|
| task_head_effect | +0.93 个百分点 | [-0.07, +2.23] 个百分点 | positive |
| grounding_effect | +3.74 个百分点 | [-2.44, +9.77] 个百分点 | positive |
| pose_effect | +12.07 个百分点 | [+3.88, +21.12] 个百分点 | positive |
| interaction | -0.57 个百分点 | [-8.05, +8.05] 个百分点 | negative |
| backbone_gap | +39.94 个百分点 | [+30.75, +49.71] 个百分点 | positive |

2x2 效应使用 29/30 个完整 group；缺失 group：`['s_39ad630766a4d0f9dcbe9445']`。由于存在不可识别 alignment，几何因素主标签按计划降级为 `inconclusive_small_sample`，表中效应仅为 complete-case 条件式描述。

## 6. Grounding 与三维点误差

| 后端 | selector | purity 中位数 | recall 中位数 | target 3D error 中位数 | donor 3D error 中位数 |
|---|---|---|---|---|---|
| CUT3R | center_0.12 | 0.00% | 0.00% | 6.6502 | 6.6809 |
| CUT3R | gt_bbox | 64.11% | 100.00% | 5.5215 | 5.4240 |
| CUT3R | gt_mask | 100.00% | 100.00% | 5.5259 | 5.4648 |
| VGGT | center_0.12 | 0.00% | 0.00% | 3.0607 | 3.3662 |
| VGGT | gt_bbox | 64.50% | 100.00% | 3.0722 | 3.0515 |
| VGGT | gt_mask | 100.00% | 100.00% | 3.0772 | 3.1099 |

GT bbox/mask 只用于后验 oracle 归因。它们若提高准确率，只能证明 center crop grounding 对误差有贡献，不能成为 RGB-only 可部署方案。

## 7. 相机与坐标诊断

- **CUT3R**：60 个 sequence 中失败 2 个；Sim(3) 后相机中心残差中位数 1.2730，旋转残差中位数 66.15°。
- **VGGT**：60 个 sequence 中失败 2 个；Sim(3) 后相机中心残差中位数 0.9295，旋转残差中位数 71.53°。

oracle-aligned pose 使用相机对应关系拟合单一全局 Sim(3)，不读取 QA 答案，也不按对象分别对齐。它只能消除全局 gauge/pose 误差，不能修复局部深度、对象选择或形变。

CUT3R 与 VGGT 共同缺失 group：`['s_39ad630766a4d0f9dcbe9445']`。该 group 的 GT 五帧相机中心最大 baseline 为 0，尺度不可识别；没有使用对象点、答案或补零伪造尺度。

## 8. 最终原因判断

### 8.1 CUT3R

- 主标签：`proven_task_head_semantics_bug`。
- 条件式几何标签：`inconclusive_small_sample`。
- 29-group complete-case 条件标签：`backbone_geometry_dominant`。
- 完整 group：29/30；缺失：`['s_39ad630766a4d0f9dcbe9445']`。
- 次标签：`inconclusive_small_sample`。
- 解释：先用 contract-valid 任务头消除已证明的标签语义错误，再依据 V0-V3 归因 grounding、pose 与 backbone，避免把下游任务头错误错误归给三维 backbone。

### 8.2 VGGT

- 主标签：`proven_task_head_semantics_bug`。
- 条件式几何标签：`inconclusive_small_sample`。
- 29-group complete-case 条件标签：`backbone_geometry_dominant`。
- 完整 group：29/30；缺失：`['s_39ad630766a4d0f9dcbe9445']`。
- 次标签：`inconclusive_small_sample`。
- 解释：先用 contract-valid 任务头消除已证明的标签语义错误，再依据 V0-V3 归因 grounding、pose 与 backbone，避免把下游任务头错误错误归给三维 backbone。

## 9. 结论边界

1. 所有 B1-B3、C1-C3、R0、T0 均为后验 oracle 诊断，不具备主结果资格。
2. 未对齐变体统计单位是 30 个 group；涉及 pose alignment 的 2x2 效应只覆盖 29 个完整 group。95% CI 是配对 group bootstrap 描述，不是总体显著性或多重检验结论。
3. 当前 `front_behind` 真实标签全为 behind，不能据此证明对 front 类别的泛化能力。
4. 原 Gate 7 的 CUT3R 56.11% 与方案 2 的 VGGT 45.00% 均保持不变，Gate 7 仍失败，禁止进入 Gate 8。
5. 下一步必须按已定位的最大正向因素分别设计独立冻结协议；不得在当前 30 groups 上调参后宣称修复成功。

## 10. 可审计产物

- `outputs/gate7_diagnosis/protocol_lock.json`
- `outputs/gate7_diagnosis/protocol_locks/r001-analysis-a1.json`
- `outputs/gate7_diagnosis/baseline_reproduction.json`
- `outputs/gate7_diagnosis/task_head_audit.json`
- `outputs/gate7_diagnosis/pose_audit.json`
- `outputs/gate7_diagnosis/factorial_predictions.jsonl`
- `outputs/gate7_diagnosis/factorial_metrics.json`
- `outputs/gate7_diagnosis/attribution.json`
- `outputs/gate7_diagnosis/checkpoint_recovery.json`
- `outputs/gate7_diagnosis/final_diagnosis.json`
