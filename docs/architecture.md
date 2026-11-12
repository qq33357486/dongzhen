# 洞真（dongzhen）架构理解

> 本文是 service 分支的架构梳理：挑战怎么发、指纹长什么样、怎么比对、怎么定家族/模型、库怎么建。
> 依据源码：`fingerprint.py` / `bank_builder.py` / `enrollment.py` / `challenge_suite.py` / `rebuild_unified_bank.py` / `app.py`。
> 配套文档：新模型录入见 `docs/enrollment.md`，HTTP 服务见 `docs/api.md`，族/分支研究见 `docs/family-branch-research.md`。

## 0. 改名记录（已执行）

本项目已定名**洞真（dongzhen）**。仓库已由 GitHub 侧改名为 `qq33357486/dongzhen`，本地 origin 已指向新地址。本轮在代码/文档中执行的字符串替换：

| 触点 | 处理 |
|---|---|
| GitHub 仓库名 | 已改为 `dongzhen`（协调者操作，本地无需动） |
| `README.md` 标题与正文品牌名 | service 分支已更新为「洞真（dongzhen）」 |
| `docs/architecture.md` / `docs/enrollment.md` / `docs/api.md` / `docs/family-branch-research.md` | 新建文档直接用洞真名 |
| FastAPI 服务名 / API 自我介绍（`/health`、OpenAPI title） | `api/main.py` 用「洞真（dongzhen）指纹探针服务」 |
| Docker 镜像名 | `ghcr.io/qq33357486/dongzhen`（见根目录 Dockerfile / docker-compose.yml） |
| sqlite 库名 | 服务历史库文件 `dongzhen_history.sqlite3` |
| 环境变量前缀 | 新代码用 `DZ_*`（`DZ_BANK_PATH` 等）；旧的 `MT_*` / `MODELTRACE_*` 仅保留兼容读取，未删 |
| `mt_cli.py` / `mt_debug.py` 文件名 | **保持不动**（运维口径与肌肉记忆引用它们），只在文件头注释加「洞真（dongzhen）探针 CLI」 |
| `fingerprint.py` 等核心模块内 `MODELTRACE_USER_AGENT` 等既有字符串 | 未动（上游共有代码，改名收益低、冲突成本高） |
| `codex-plugin/`（ModelTrace Guard） | 本轮未动；如需改名属独立改造 |

## 1. 一句话总览

给模型发"生成 300 个左右、范围 [1,355] 的随机整数、禁用工具"的挑战，把回答解析成数字后提取两类统计特征（一阶值直方图 + 有序块结构），投影掉环境干扰方向后与库内 18 个模型的指纹中心算相似度，经 softmax 温度校准得到概率，输出**家族（GPT/Claude）**与**具体模型**两级判定。

## 2. 挑战如何生成

两条生成路径，对应两种使用方式：

**A. 快速探针（`fingerprint.py: generate_challenges`，mt_cli / 服务 /probe 用这条）**
- 每次 `random.SystemRandom()` 现场生成，题数默认 3。
- 题目长度从 292~333 均匀抽样（期望输出个数），配中文提示词模板随机拼接（开头/动作/结尾/分隔符各从一个池子里随机选）。
- 关键约束都写在题面里：值域 [1,355] 含端点、禁止工具与代码执行、禁止等差/循环/区块等规则化模式、允许重复、写完不回头改。
- 每题带 `expected_count`，回验时要求解析出的数字数 ≥ max(80, ceil(expected×0.55)) 才算有效样本。

**B. 建库级环境套件（`challenge_suite.py: fingerprint_suite`，enrollment 自动采集用这条）**
- 12 个"环境"（environment-01~12）：不同 transport（clean / system 前缀 / user 前缀）、不同语言风格（json 数组 / 英文 / 中文）、不同前缀长度（96/512/2048 词）、每环境 3 个不同期望长度（合计 36 题）。
- 目的：让参考数据覆盖多种包装/语言/长度条件，建库时才能估计"环境方向"（见 §4 去噪），并按环境分组做交叉验证校准。
- 这就是为什么每个库内模型恰好 36 条参考回答（12 环境 × 3 题）。

## 3. 回答解析

`fingerprint.py: parse_numbers`：
- 正则抓出文本里所有 `\d+` 数字串；按"分隔符里是否含字母"切分数字段（run）——字母会把连续数字切开（比如 `a1b2` 里的 1、2 属于不同段）。
- 只保留落在 [1,355] 的值；取**最长的一段**作为回答序列。
- 含义：如果模型输出了 JSON 数组或逗号分隔序列，就取那个完整序列；夹杂解释文字时取最长的纯数字块。有效样本数 ≥ 80 或期望数的 55% 才计入（`analyze_outputs` 里的阈值）。

## 4. 指纹特征是什么

每个回答（一段数字序列）提取**两类特征**，融合权重 0.75 : 0.25：

**特征一：一阶值直方图（Hellinger，权重 0.75）**
- 355 维计数向量：每个值在 [1,355] 内出现的次数（`count_numbers`）。
- 加 0.5 平滑后开根号归一化（`hellinger_feature`，ALPHA=0.5）——这就是 Hellinger 嵌入，让计数分布可做欧氏/余弦比较。
- **粒度：单值一级**，355 个 bin，不管顺序、不管位置。

**特征二：有序块特征（ordered blocks，权重 0.25）**
- 74 维：把序列按位置切成 4 块，每块做 16 bin 的直方图（值域 [1,356) 等宽）＝64 维；再加尾数分布 10 维（值 mod 10 的直方图）。
- 捕捉的信息：序列前段/中段/后段的取值偏好差异（位置相关性），以及个位数的偏好（模型对 0-9 结尾数字的隐性偏好）。
- **粒度：4 块粗位置 × 16 bin 值域 + 尾数**。

**去噪（nuisance projection）——两类特征共用同一套路**
- 建库时：12 个环境在各特征维上的均值偏移构成"环境偏移矩阵"，对它做 SVD 取前 2 个奇异方向作为"环境干扰基"（`fit_robust_artifacts`）。物理含义：把"提示词语言/包装方式/长度"带来的分布漂移投影掉，只留模型固有偏好。
- 归因时：新回答的特征标准化（减均值除标准差）→ 减掉在干扰基上的投影 → L2 归一化 → 与每个模型的单位化中心做点积（即余弦相似度）→ 得到 18 维原始相似度 → 再标准化成 z 分数。
- 有序块特征额外有一套"环境模板"：按完整环境算的每模型中心，取跨环境最大相似度再标准化（对"目标恰好处于某个已见环境"的情形更灵敏），最后与 nuisance 通道五五开融合。

**打分融合（`robust_score_numbers`）**
```
fused = 0.75 × hellinger_z + 0.25 × ordered_z
```
注意 0.75 来自 `hellinger` 通道是主通道这一事实，`ORDERED_BLOCK_WEIGHT=0.25` 只作用于有序块部分（`bank["robust"]["ordered_blocks"]["weight"]`）。

## 5. 比对算法与两级判定

**单回答 → 18 维得分**：§4 的融合 z 分数。

**多回答聚合（`analyze_outputs`）**：
- 把全部有效回答的 18 维得分**逐维取平均**（等权平均，不是投票）。
- 用库内置校准温度 β（按本轮有效样本数取 1/2/3 档，`bank["calibration"][k]["beta"]`）缩放后 softmax：
  `P(模型_i) = softmax(β × mean_score)_i`
- β 与 cv_accuracy 是建库时用"留题组交叉验证"拟合的（见 §6），当前统一库 3 题档 β≈12、cv 准确率 100%。
- 附带 `profile_similarity`：汇总计数与各模型池化计数的 JS 相似度（`js_similarity`，1−sqrt(JS/log2)），是独立于打分的直观参照。

**两级判定（`analyze_global_outputs`）**：
- 家族概率 = 该家族内各模型概率之和（P(GPT) = Σ P(gpt-*)）。
- 模型级判定 = 全局 argmax；家族级判定 = 家族概率 argmax。
- 每个模型还带 `conditional_probability`（家族内条件概率 = P(模型)/P(家族)）。
- **判定口径（我方定谳，勿改）**：GPT 线测中判出 astra 系 = 正常没降智；判出 luna 族（gpt-5.6-luna / gpt-6-luna）= 真降智（中转站偷偷换成了廉价分支）；**库外模型会被强行归到最像的库内模型，这个归类不可信（垃圾）**——服务的 verdict 规则就是围绕这三条设计的（见 docs/api.md）。

**重要边界**：以上全部是"库内互斥归因"——softmax 强制把概率分给 18 个库内模型，没有"都不像"的出口。库外模型（如 6.1 系）的得分与 top1-top2 分差特征见 family-branch-research.md 的量化分析。

## 6. 库的数据结构与构建流程

**文件**：`data/unified_bank.json`（18 模型：GPT 5.4~6.x 八款 + Claude 4.5~5.5 十款），另有 `data/gpt_bank.json`、`data/claude_bank.json` 两个单族库。

**警告（已踩过的坑）**：`fingerprint.py: DEFAULT_BANK` 指向 `gpt_bank.json`（纯 GPT 库），`load_bank()` 不传参会拿错库。所有新代码必须显式传 `data/unified_bank.json`（mt_cli.py 已修，api/ 服务继承此修复）。

**库内容三层**：
1. `models[]`：每模型的池化计数直方图（`counts`，355 维）、回答数、来源环境分布、family 标签。这是"指纹库"的本体。
2. `robust`：打分工件——两类特征的 `feature_mean/feature_scale`（标准化参数）、`nuisance_basis`（环境干扰基）、`centroids`（去噪后每模型单位中心）、有序块的环境模板。归因时只用这一层 + models 的 counts（算 JS 相似度用）。
3. `calibration`：1/2/3 题三档的 β 温度与交叉验证准确率。

**构建流程（数据 → 库）**：
```
采集：每模型 × 12环境 × 3题 = 36 条回答
  → data/{gpt,claude}_reference.jsonl（每行含全文、环境、挑战id、有效性等）
rebuild_unified_bank.py：
  读两份 jsonl → 打上 family 标签 → build_bank()：
    a) 每模型池化 counts（进 models[]）
    b) fit_robust_artifacts()：标准化参数 + 环境偏移 SVD 去噪基 + 每模型去噪中心（hellinger 355维 + ordered 74维两套）
    c) calibration_records()：按环境分组留题交叉验证——每次留 q 题（q=1,2,3）当验证集、其余重建工件给验证集打分，
       产生 (得分, 真值) 记录
    d) fit_beta()：在 [0.05,12] 网格上搜使验证集 NLL 最小的温度 β
  → data/unified_bank.json
```
`enrollment.py: rebuild_bank` 是同一 `build_bank` 的单族版本（写回 gpt_bank/claude_bank）。**风险**：rebuild 是"全量重建"，新模型的数据一旦 append 进 reference.jsonl，库就变了——旧模型的中心和校准都会移动，所以正式库更替前要留备份、并跑回归验证（见 enrollment.md §5）。

## 7. 现有入口盘点

| 入口 | 说明 |
|---|---|
| `mt_cli.py` | 我方主力 CLI：3 题并发、一次发完不补题、按有效样本归因，显式用 unified_bank |
| `mt_debug.py` | 逐题串行调试：存全文、逐题单独归因，用于定位污染来源 |
| `app.py` | 上游原 Flask 网页（7860 端口）：手动粘贴 / API 自动测试 / 指纹库管理 / enrollment 入口 |
| `api/`（本轮新增） | FastAPI 服务：/probe、/probe_batch、/history、/health，见 docs/api.md |
| `enroll_cli.py`（本轮新增） | 新模型录入采集 CLI：采集→候选指纹文件→人工审核，不自动写库 |
| `tools/confusion_analysis.py`（本轮新增） | 库内混淆度/功效离线分析，零成本 |
| `codex-plugin/` | Codex 任务内监测插件，独立子系统，本轮未动 |

## 8. 关键常量速查

| 常量 | 值 | 位置 |
|---|---|---|
| 值域 | [1, 355]，DIMENSION=355 | fingerprint.py |
| 平滑 | ALPHA=0.5（hellinger/js 相似度），ordered 用 0.5 | fingerprint.py |
| 有序块权重 | 0.25 | fingerprint.py |
| 直方图特征权重 | 0.75 | fingerprint.py |
| 有效样本下限 | max(80, ceil(expected×0.55)) 个数字 | fingerprint.py / enrollment.py |
| 推荐题数 | 3（calibration 最高档） | unified_bank.json |
| 统一库 | data/unified_bank.json，built 2026-10-09 | data/ |
| 采集环境 | 12 环境 × 3 题 = 36 答/模型 | challenge_suite.py |
