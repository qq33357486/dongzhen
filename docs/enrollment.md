# 洞真（dongzhen）新模型指纹录入 SOP

> 目标：来了一个库外新模型（例如 gpt-6.1-sol），照本文走完就能把它的指纹录进 `data/unified_bank.json`，全程本地操作。
> **训练/录入全部在本地完成是定规**：采集脚本只在你自己的机器上跑，库文件不外发。
> 前置阅读：`docs/architecture.md`（挑战/特征/库结构）。

## 0. 总流程一览

```
① 采数据   enroll_cli.py 用官方/可信直连源采 36 条回答 → data/candidates/候选文件
② 人工审核 候选文件摘要：自匹配 p 值、最近邻距离、样本量是否达标
③ 并入     tools/merge_candidate.py 把候选行 append 进族 jsonl（显式第二步，防误操作）
④ 重建     python rebuild_unified_bank.py 全量重建 unified_bank.json（先备份旧库！）
⑤ 回归验证 tools/confusion_analysis.py 对新库跑一遍：自匹配、混淆对、库外代理都要看
```

**防污染铁律**：`enroll_cli.py` 只产出候选文件，**绝不自动写 `unified_bank.json`、不 append reference.jsonl**。并入永远由人显式执行第 ③ 步。

## 1. 采样源要求（硬性）

| 项 | 要求 | 原因 |
|---|---|---|
| 来源 | **官方 API 或可信直连**（如 codex 号池直连、官方 endpoint） | 录的是"真身"基线。中转站后端可能已经是降智/换皮模型，会把假指纹录进库，之后所有判定都被带歪 |
| 禁止 | 任何中转站 / 二道贩子 / 来源不明的 endpoint | 同上 |
| 工具调用 | 必须用**不带工具执行**的端点（plain chat completions） | 题面虽已禁工具，但带 code interpreter 的会话可能让模型用工具生成"真随机数"，污染指纹（指纹要的就是模型自己的随机数偏好） |
| 温度 | **不传 temperature（供应商默认）**，与现库 36 条参考数据完全同口径 | 现库全部 `provider_default`。传了不同温度等于引入系统偏差；温度对直方图形状有实际影响 |
| max_tokens | ≥ 4096 | 期望输出 ~300 个数字，截断的回答会被有效性阈值挡掉（≥ max(80, 期望×0.55)），浪费题数 |
| 单一来源 | 36 条必须**同一个 endpoint + 同一账号类型**采完 | 混源等于把渠道差异录进指纹。上游支持多环境配对采集，我们没有那个采集矩阵，单一可信源是最优解 |
| api_format | openai 或 anthropic（auto 也行） | 与 `enrollment.py: request_completion` 一致，自动重试 3 次，429/5xx 退避 |

## 2. 需要多少题/样本

| 量 | 值 | 说明 |
|---|---|---|
| **满配（推荐，必须）** | **36 条**（= 12 环境 × 3 题，`challenge_suite.fingerprint_suite()` 全套） | 现库每模型都是 36 条。少采会导致该模型环境覆盖不均、去噪中心估计有偏，且重建时"完整环境"判定会退化 |
| 绝对下限 | 12 条（每环境 1 条） | 只够看轮廓，**不足以入库** |
| 预检（不算录入） | 3~6 条 | 只用来快速看一眼"这模型大概像谁"，结果不进任何 jsonl |
| 有效样本口径 | 每条回答解析出的 [1,355] 整数 ≥ max(80, ceil(期望数×0.55)) | 与现库一致；拒答/截断自动剔除并补采（每题最多重试 2 次） |

为什么是 36：建库的去噪基（SVD 环境方向）和交叉验证校准都按"每环境每模型 3 条"设计；`bank_builder.fit_robust_artifacts` 只有在"某环境下全部模型都有数据"时才标记 robust_ready，数据不齐会走降级路径。

## 3. 成本量级

单题 token（按任务口径估）：

- 输入：题面 + 约束 ≈ 0.4K token（中文题 ~300 字，英文/带前缀环境稍多）
- 输出：~300 个整数 ≈ 900 token
- **满配 36 题 ≈ 输入 14K + 输出 33K ≈ 47K token**

按输出单价折算（输入价忽略不计）：

| 输出价 | 满配一次 | 含重采验证（×1.3） |
|---|---|---|
| $2/M | $0.07 | $0.09 |
| $8/M | $0.26 | $0.34 |
| $15/M（Claude 档） | $0.49 | $0.64 |
| $60/M（最贵档） | $2.00 | $2.60 |

结论：**绝大多数模型满配录入 < $1，最贵档 < $3**。enroll_cli 会按实际解析出的数字数给出真实 token 估算。

## 4. 第①步：采集（enroll_cli.py）

```bash
# key 走环境变量，不进 shell history
export DZ_ENROLL_KEY="sk-xxxx"

python enroll_cli.py \
  --base-url https://官方或可信直连/v1 \
  --model gpt-6.1-sol \            # 上游 API 的模型名
  --label gpt-6.1-sol \            # 库内显示名（默认同 --model）
  --family gpt \                   # gpt / claude / 新族名（如 gemini）
  --samples 36                     # 满配；预检可给 3~6
```

行为：
1. 从 36 题环境套件等距抽 `--samples` 题，2 并发采集（每题失败/无效自动重试 1 次）。
2. 本地在内存拼"现库参考数据 + 候选数据"临时库（**不落盘**），留 6 条候选当验证集。
3. 打印：样本量、数字分布摘要、与库内最近邻 TOP3（余弦 + 自举 d）、验证集自匹配结果（top1 是否自身、p 值、top1-top2 分差）。
4. 写候选文件 `data/candidates/<label>-<时间戳>.json`（含全部回答全文 + 行记录 + 摘要 + 并入指引），文件不进 git（.gitignore 已配 `data/candidates/`）。

**读摘要的要点**：
- 自匹配：验证集每组 3 题 top1 应全是自身，p≈1，top1-top2 分差 ≥ 0.7（库内自匹配分差 p5=0.78，见 research 文档）。分差在 0.1~0.3 徘徊 → 该模型与库内某模型太像，入库会制造混淆对，见 §7。
- 最近邻 d 值（Cohen's d）：≥ 3 很稳；2~3 可入库但探测建议加到 5 题；< 2 不要入（3 题分不开）。
- 数字分布摘要：均值应落在 150~205 区间、无极端单值堆积（>8% 占比单值要人工看回答全文确认没有复读模式）。

## 5. 第②~④步：审核、并入、重建

审核通过后：

```bash
# 1) 备份（重建是全量覆盖，必须先备份）
cp data/unified_bank.json data/unified_bank.json.bak-$(date +%F-%H%M%S)

# 2) 显式并入：候选行 append 进族 jsonl（gpt→gpt_reference.jsonl，claude→claude_reference.jsonl，
#    新族→自动建 <family>_reference.jsonl），然后原地重建 unified_bank.json
python tools/merge_candidate.py data/candidates/gpt-6.1-sol-XXXX.json

# 3) 重建已完成（merge_candidate 内置），跑回归验证
python tools/confusion_analysis.py
```

`rebuild_unified_bank.py` 的作用与风险：
- **作用**：读 `gpt_reference.jsonl` + `claude_reference.jsonl`（可扩展新族 jsonl）→ `build_bank()` 全量重建统一库：每模型池化直方图、去噪工件、按 1/2/3 题档交叉验证拟合 β 温度。
- **风险一（全量重建）**：任何模型的参考数据变动都会移动**所有**模型的去噪基和中心——不是增量追加。所以并新模型前后都要跑 confusion_analysis 对比。
- **风险二（完整环境退化）**：统一库里如果新模型只覆盖部分环境，`complete_environments` 可能为空 → 全库降级为"不去环境方向"拟合。**这就是必须满配 36 条的原因。**
- **风险三（calibration fallback）**：交叉验证记录不足时 β 沿用旧库值（fallback: true），重建摘要里要瞄一眼 `calibration["3"]["fallback"]` 是否为 false。
- **新族注意**：上游 `rebuild_unified_bank.py` 的 SOURCES 写死 gpt/claude；`tools/merge_candidate.py` 自带族→jsonl 映射，支持新族（如 gemini），用哪个脚本重建就保持一致。

## 6. 第⑤步：录完如何验证（ checklist）

1. **库内自匹配**：`tools/confusion_analysis.py` 新库报告里，新模型 k=3 自举正确率应 100%，混淆对 TOP 里不该出现新模型。
2. **自匹配 p 值 ≈ 1**：enroll_cli 验证集 + 录入后隔天用 mt_cli.py 探一次真源（3 题），top1 应是自身、p>0.9。
3. **交叉验证**：重建摘要 `calibration["3"]["cv_accuracy"]` 应 ≈ 1.0（现库为 1.0），`fallback` 为 false。
4. **库外代理**：confusion_analysis 的 leave-one-model-out 部分，把新模型剔除后它应落回**族内正确分支**（如 luna 系剔除后落 luna 系）；如果落进别的族，说明它的族标签或指纹有问题。
5. **端到端**：`python mt_cli.py <新模型名>` 打真实可用源（非录入源也行），归因指向自身。

## 7. 常见失败模式

| 症状 | 判断 | 处理 |
|---|---|---|
| 自匹配分差 0.1~0.3，最近邻是同系某模型 | 新模型与库内模型指纹重叠（同系蒸馏/checkpoint 变体的典型表现） | **不要强行入库**。按 family-branch-research.md 的方案做族内细分或标 ambiguous；也可只录族标签不录模型 |
| 有效样本大量被拒 | 回答截断/模型输出解释文字/用了带工具端点 | 看 enroll_cli 的错误列表；换 plain 端点，max_tokens 拉到 4096 |
| 摘要显示单值堆积 | 模型在复读某个数字或模式 | 人工看候选文件里回答全文，确认后重采 |
| 重建后旧模型自匹配掉下来 | 完整环境退化或数据混源 | 对比新旧库 confusion 报告；检查新行的 condition/temperature 字段 |
| 429 频繁 | 采集并发太高 | enroll_cli 已限 2 并发 + 退避；还可以分批跑（--samples 12 跑三次再合并？**不行**，候选文件一次一体，改用官方更高限额的 key） |

## 8. 为什么这样设计（设计依据）

- **候选文件 + 显式并入**：库是所有判定的地基，一次误并入污染全部模型。两步走让"采集"可以随便试错，"入库"永远是深思熟虑的动作。
- **临时库内存验证**：入库前就知道"录进去之后会怎样"（自匹配/最近邻/混淆），避免录完才发现重影。
- **满配 36 与现库对齐**：所有统计口径（环境覆盖、β 校准、完整环境）都是按 36/模型设计的，对齐是阻力最小的路径。
