# Trust3D Gate 7 修复实验详细报告

## 1. 实验结论总览

本报告对应 `Trust3D_GT五路路由与Gate7修复并行执行计划.md` 的 B 线。B0-B6 已全部执行，最终 runner 于 2026-08-02 07:33:13 UTC 以退出码 0 结束。

| 项目 | 结果 | 判定 |
|---|---:|---|
| B 线执行状态 | B0-B6 全部完成 | 完成 |
| 最佳后端 | CUT3R + `rgb_saliency_v1` | 仅表示两者中较优 |
| CUT3R holdout accuracy | 51.11% | 未达到门槛 |
| VGGT holdout accuracy | 49.44% | 未达到门槛 |
| CUT3R 相对 GT/RGB-D 的 QA drop | 48.89 个百分点 | 大于 20 个百分点 |
| VGGT 相对 GT/RGB-D 的 QA drop | 50.56 个百分点 | 大于 20 个百分点 |
| 几何 group 运行失败率 | CUT3R 0%，VGGT 0% | 工程通过 |
| inference 私有文件读取次数 | 0 | 防泄漏通过 |
| checkpoint 恢复 | 两个后端哈希均一致，未加载 GPU | 通过 |
| 最终科学等级 | `failed_scientific` | B 线未通过 |

这里必须区分两个概念：

- **实验执行已经完成**：所有预定阶段、前向、评价、恢复测试和结果封存均已结束。
- **修复方案没有通过科学门槛**：`report.json` 中的 `complete: false` 表示结果未达到 QA drop 门槛，不表示任务仍在运行。

最准确的结论是：

> B 线已完整证明当前 CUT3R/VGGT RGB-only 适配器可以稳定运行并接受严格评价，但其空间问答准确率仍与 GT/RGB-D 上界相差约 49-51 个百分点，因此 Gate 7 修复实验科学失败。该结果不推翻 GT 五路路由控制器的可行性，只说明 RGB 感知到空间证据接口仍未达到进入 C 线主实验的要求。

## 2. 实验目标与结论边界

B 线验证的链路为：

```text
RGB 序列
  -> RGB-only 对象 grounding
  -> 相机与坐标契约
  -> CUT3R/VGGT 几何恢复
  -> contract-valid 空间任务头
  -> 空间答案
```

本实验回答三个问题：

1. 修复已证明的任务头和坐标契约错误后，准确率能否明显恢复；
2. RGB saliency grounding 能否优于中心 crop；
3. 冻结后的 CUT3R/VGGT 适配器能否在未见 holdout 上将 QA drop 控制到 20 个百分点以内。

本实验不直接验证五路 router，也不使用 B 线结果改写 A 线的 GT 控制器结论。根据预注册规则，只有 B 线 QA drop 不高于 20 个百分点时，才能进入 C 线端到端主实验。

## 3. 协议锁与数据隔离

### 3.1 协议锁

| 字段 | 固定值 |
|---|---|
| protocol revision | `parallel-v2` |
| contract-valid task head | `gate7-planar-yaw-v1` |
| legacy task head | `gate7-camera-z-v1` |
| legacy30 用途 | 只作历史回归，不作主结果调参 |
| 配置 SHA256 | `5c1504df164224fd580f9b5bf51130a70d1ee646ca9bb36eb214e379b021e479` |
| 诊断报告 SHA256 | `6f90b03a1057298cc78f75ded2cb7442db217783c42922b8c323212c5847c9ce` |
| 锁定的历史 CUT3R accuracy | 56.11% |
| 锁定的历史 VGGT accuracy | 45.00% |

历史数字只用于确认基线和 evaluator 没有被覆盖，不能与新 holdout 混合作为主结果。

### 3.2 数据规模

| split | group 数 | episode 数 | 本次从 checkpoint 新补 group | 完成状态 |
|---|---:|---:|---:|---|
| pilot | 10 | 120 | 1 | 10/10 |
| holdout | 30 | 360 | 4 | 30/30 |
| 合计 | 40 | 480 | 5 | 完成 |

每个 group 包含两种风险分支和四类空间问题。pilot 只用于选择 grounding 和冻结置信度；holdout 只评价，不在揭示私有答案后更换候选。

### 3.3 防泄漏与去重

| 审计项 | 结果 |
|---|---:|
| pilot/holdout source overlap | 0 |
| pilot/holdout scene overlap | 0 |
| 与 legacy30 source overlap | 0 |
| 与 legacy30 scene overlap | 0 |
| CUT3R inference private open count | 0 |
| VGGT inference private open count | 0 |

模型前向只读取 public manifest 和 RGB sequence manifest。私有 `oracle_private.jsonl` 仅由独立 evaluator 在预测封存后读取。

## 4. B0-B2：契约测试、数据构建与中断修复

### 4.1 B0-B1 结果

| 阶段 | 检查内容 | 结果 |
|---|---|---|
| B0 | Gate 7、方案 2、诊断起点及配置哈希锁定 | 通过 |
| B1 | planar yaw、外参往返、轴符号、旋转、donor/target 交换 | 通过 |
| B1 | 平衡合成 case | 16/16 |
| B1 | round-trip tolerance | `<=1e-6` |
| B1 | Sim(3) 禁止进入主适配器 | 通过 |

### 4.2 B2 初始中断原因

原 B2 checkpoint 停在 pilot 9/10、holdout 26/30。日志中的 15 个失败主要为：

```text
KeyError: object not found in metadata: <objectId>
```

AI2-THOR 的 `objectId` 含物体位置。`SetObjectPoses` 或 `TeleportObject` 改变位置后，ID 会重新编码；旧实现跨场景重置和位置交换继续使用旧 ID，因而在 metadata 中找不到物体。这是数据生成器的稳定身份绑定错误，不是 GPU、显存或模型错误。

### 4.3 修复内容

| 修改点 | 修复方式 |
|---|---|
| 场景重置后的对象绑定 | 使用稳定且唯一的对象 `name` 重新解析当前 `objectId` |
| `SetObjectPoses` 后的位置误差检查 | 按 `name` 重新绑定后再检查 |
| TeleportObject 回退 | 每次位置变化后重新绑定下一次操作所需 ID |
| stale 分支可见性检查 | 使用交换后的当前对象，而不是 history ID |
| stale 分支几何验证 | 将交换后重新解析的 ID 传入验证器 |

新增回归测试覆盖缺失名称、重复名称、对象 ID 重编码和三步 TeleportObject 回退。相关测试最终为 `14 passed`。

两次退出码 1 是为了在发现新 traceback 后主动停止本任务 runner 并修复代码；所有已完成 checkpoint 均保留。第三次从相同 checkpoint 恢复后只新增 pilot 1 个、holdout 4 个 group，最终 runner 退出码为 0。

## 5. B3：pilot 因素实验

GT/RGB-D 上界在所有变体中均为 100%。pilot 结果如下：

| 后端与 grounding | contract-valid accuracy | legacy accuracy | QA drop | ECE | 几何运行失败率 |
|---|---:|---:|---:|---:|---:|
| CUT3R + center crop | 50.00% | 50.00% | 50.00 pp | 21.15% | 0% |
| CUT3R + RGB saliency | **55.00%** | 53.33% | **45.00 pp** | **15.68%** | 0% |
| VGGT + center crop | 48.33% | 47.50% | 51.67 pp | **17.28%** | 0% |
| VGGT + RGB saliency | **50.00%** | 50.83% | **50.00 pp** | 17.49% | 0% |

按预注册的 QA drop 优先规则，两个后端均选择并冻结 `rgb_saliency_v1`：

| 后端 | grounding 增益 | 冻结候选 |
|---|---:|---|
| CUT3R | +5.00 pp | `rgb_saliency_v1` |
| VGGT | +1.67 pp | `rgb_saliency_v1` |

RGB saliency 对 CUT3R 有小幅帮助，对 VGGT 帮助更小；pilot 上不存在足以接近 20 pp 门槛的候选。

## 6. B4-B5：严格 holdout 主结果

### 6.1 主结果表

| 后端 | grounding | contract-valid accuracy | legacy accuracy | GT/RGB-D | QA drop | 95% group bootstrap CI |
|---|---|---:|---:|---:|---:|---|
| CUT3R | `rgb_saliency_v1` | **51.11%** | 49.72% | 100% | **48.89 pp** | [41.67%, 60.00%] |
| VGGT | `rgb_saliency_v1` | 49.44% | 49.44% | 100% | 50.56 pp | [40.00%, 58.89%] |

CUT3R 比 VGGT 高 1.67 个百分点，但二者区间大幅重叠，不能据此声称 CUT3R 在总体上稳定优于 VGGT。更重要的是，两者都远未达到 20 个百分点门槛。

### 6.2 置信度校准

| 后端 | Brier score | ECE | 判断 |
|---|---:|---:|---|
| CUT3R | 0.2549 | 18.51% | 校准较弱 |
| VGGT | 0.2563 | 14.27% | 校准较弱 |

VGGT 的 ECE 略低，但准确率和 Brier score 没有形成足以改变主结论的优势。

### 6.3 增益分账

| 层级 | CUT3R | VGGT | 解释 |
|---|---:|---:|---|
| task head pilot 增益，center crop | 0.00 pp | +0.83 pp | 很小 |
| task head pilot 增益，RGB saliency | +1.67 pp | -0.83 pp | 很小且不稳定 |
| grounding pilot 增益 | +5.00 pp | +1.67 pp | 有帮助但不足 |
| pose/contract 记账增益 | 0.00 pp | 0.00 pp | 当前实现未带来总体提升 |
| holdout 剩余误差 | 48.89 pp | 50.56 pp | 主导项 |

这些数据说明，修正 evaluator 语义和替换 grounding 后，剩余差距仍接近 50 个百分点。当前证据更支持“backbone 几何、对象定位、相机/坐标到任务答案的联合误差仍然很大”，不能再把 Gate 7 失败主要解释为单一任务头错误。

## 7. B6：工程验收

| 验收项 | 结果 | 判定 |
|---|---|---|
| contract 单元测试 | 通过 | 通过 |
| legacy regression lock | 保持锁定 | 通过 |
| inference private open count | 0 | 通过 |
| 几何 group failure rate | 两个后端均为 0 | 通过 |
| holdout prediction seal | 两个后端 SHA256 已封存 | 通过 |
| checkpoint 恢复 | before/after 哈希一致 | 通过 |
| CPU recovery 加载 GPU | `false` | 通过 |
| QA drop `<=20 pp` | 最佳为 48.89 pp | **失败** |

预测封存哈希：

| 后端 | predictions SHA256 |
|---|---|
| CUT3R | `7495c393ad08c6072f9ca9beff4214c84d0da53cea245d5de1b1f0debafb91da` |
| VGGT | `f669beb56bd7fb1ccad4b878ce35f996ba8638f7d71c9b3b1c112129ceedcbad` |

恢复测试前后两个哈希均完全一致，且 `gpu_loaded: false`，证明 CPU 恢复没有重新加载模型或修改预测。

## 8. 最终判定

预注册等级为：

| 最佳 holdout QA drop | 等级 | 本次结果 |
|---|---|---|
| `<=10 pp` | B 线主结果通过 | 未达到 |
| `>10 pp` 且 `<=20 pp` | 现实设置受限通过 | 未达到 |
| `>20 pp` | `failed_scientific` | **48.89 pp，命中此档** |

因此：

1. B 线实验已经完整做完，但 Gate 7 修复没有成功；
2. CUT3R 和 VGGT 均能无运行失败地生成结果，不等于生成的几何或空间答案准确；
3. RGB saliency grounding 只能带来有限改善，不能补偿约 50 个百分点的剩余差距；
4. 本结果不否定 A 线已经验证的 GT 五路路由控制器可行性；
5. 按当前计划，不应在该结果上启动 C 线主实验，也不应降低门槛或使用 GT mask/bbox/pose 掩盖失败。

## 9. 后续最小建议

若继续修复 Gate 7，应保持本 holdout 和门槛不变，并另建 development split：

1. 对 CUT3R/VGGT 分别输出对象中心误差、深度误差、相机旋转误差和最终答案误差的逐 group 联合表；
2. 将 RGB grounding、camera normalization、geometry 和 task head 分别替换为 oracle，测量每层可恢复的准确率上限；
3. 优先修复能在独立 development 数据上带来至少 10 pp 改善的层，不在本 holdout 上继续选择变体；
4. 只有冻结后的新 revision 在全新 holdout 上达到 `<=20 pp`，再恢复 C 线。

## 10. 结果文件索引

| 文件 | 作用 |
|---|---|
| `outputs/parallel_v2/gate7_fix/protocol_lock.json` | B0 协议与历史基线锁 |
| `outputs/parallel_v2/gate7_fix/unit.json` | B1 合成契约测试结果 |
| `outputs/parallel_v2/gate7_fix/dataset_lock.json` | pilot/holdout 数据及哈希锁 |
| `outputs/parallel_v2/gate7_fix/adapter_lock.json` | pilot 选择后的冻结适配器 |
| `outputs/parallel_v2/gate7_fix/confidence_lock.json` | 冻结置信度配置 |
| `outputs/parallel_v2/gate7_fix/pilot_metrics.json` | 四个 pilot 变体指标 |
| `outputs/parallel_v2/gate7_fix/holdout_metrics.json` | 30-group 严格主结果 |
| `outputs/parallel_v2/gate7_fix/checkpoint_recovery.json` | CPU 断点恢复审计 |
| `outputs/parallel_v2/gate7_fix/report.json` | 最终 `failed_scientific` 判定 |
| `/224010104/Jerry/logs/parallel_v2/gate7-fix.log` | B 线完整 runner 日志 |
| `/224010104/Jerry/logs/gate7-fix-resume.log` | 本次恢复、修复与测试日志 |
