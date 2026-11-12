# 洞真（dongzhen）API 服务文档

> FastAPI 探针服务：把"发 3 题挑战 → 归因 → 给判定"封装成 HTTP 接口，供编排器/人工调用。
> 监听：容器内 `0.0.0.0:8100`。镜像：`ghcr.io/qq33357486/dongzhen`（生产只拉镜像，**不要在服务器上构建**）。

## 0. 安全红线

- **api_key 只进内存**：不写日志、不写磁盘、不进 sqlite。sqlite 与一切输出只存脱敏形式（前 4 后 4，如 `sk-a***7890`）。
- 响应体任何字段都不回显 api_key。
- `/probe` 与 `/probe_batch` 的失败也留底（verdict 为空、error 有摘要，key 同样脱敏）。

## 1. 部署与本地起服务

```bash
# 本地开发
python -m pip install -r api/requirements.txt -r requirements.txt
python -m uvicorn api.main:app --host 0.0.0.0 --port 8100

# 本地构建镜像（生产不要这么做，生产只 pull）
docker build -t dongzhen:service .

# docker-compose 起（见 docker-compose.yml，sqlite 历史挂卷持久化）
docker compose up -d dongzhen-api
```

配置全部走环境变量（见 `api/config.py`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DZ_BANK_PATH` | `data/unified_bank.json` | 指纹库（**显式统一库**，绝不用默认 gpt_bank） |
| `DZ_HISTORY_DB` | `data/dongzhen_history.sqlite3` | 历史留底 sqlite |
| `DZ_HOST` / `DZ_PORT` | 0.0.0.0 / 8100 | 监听 |
| `DZ_PROBE_CONCURRENCY` | 3 | 单次探针的题间并发（继承 mt_cli：轮内一次发完不补题） |
| `DZ_BATCH_CONCURRENCY` | 4 | /probe_batch 的目标间并发 |
| `DZ_API_TOKEN` | 未设置 | AI 自助模式鉴权：设置后 /challenge、/submit 要求 `Authorization: Bearer <token>`；未设置放行（见 `api/auth.py`） |
| `DZ_CHALLENGE_SECRET` | 回退 `DZ_API_TOKEN` | AI 自助模式 challenge_id 的 HMAC 签名密钥（见 `api/config.py: challenge_secret`）；生产暴露前必须显式设置 |

换指纹库（merge 新模型后）：更新挂载的 `unified_bank.json` 并重启容器（库只在启动时加载一次）。

## 2. POST /probe — 单目标探针

请求：

```json
{
  "base_url": "https://某中转站/v1",
  "api_key": "sk-xxxx",
  "model": "gpt-6.1-sol",
  "pin": "gpt-6.1-sol@s141c592",
  "rounds": 1,
  "questions": 3,
  "timeout_s": 240,
  "temperature": null
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| base_url / api_key / model | 是 | 目标端点与模型；key 只进内存 |
| pin | 否 | 宣称身份，格式 `<宣称模型>@<样本标记>`（如 `gpt-6.1-sol@s141c592`）。库内模型做定向核对（排名/p 值），库外模型只判家族不硬归类 |
| rounds | 否，默认 1 | 轮数；每轮独立发 questions 题、独立归因（per_round） |
| questions | 否，默认 3 | 每轮题数；3 与 mt_cli 口径一致（库校准最高档） |
| timeout_s | 否，默认 240 | 单题超时（秒） |
| temperature | 否 | 留空=供应商默认（推荐，与指纹库同口径） |

响应（200）：

```json
{
  "target": "https://某中转站/v1::gpt-6.1-sol",
  "verdict": "clean",
  "verdict_reason": "top1 命中正常分支（gpt-6-astra 含 'astra'）；族内可能是 gpt-6-astra / gpt-6-sol",
  "family": "gpt",
  "family_name": "GPT",
  "family_p": 0.93,
  "candidates": [
    {"model": "gpt-6-astra", "p": 0.55},
    {"model": "gpt-6-sol", "p": 0.30}
  ],
  "candidates_tied": false,
  "top_model": "gpt-6-astra",
  "top_p": 0.55,
  "top_gap": 0.81,
  "valid_samples": 3,
  "total_questions": 3,
  "per_round": [
    {"round": 1, "valid_samples": 3, "top_model": "gpt-6-astra", "top_p": 0.55, "family": "gpt", "family_p": 0.93}
  ],
  "results": [
    {"model": "gpt-6-astra", "display_name": "gpt-6-astra", "family": "gpt", "family_name": "GPT",
     "probability": 0.55, "score": 1.21, "profile_similarity": 0.91}
  ],
  "pinned": {
    "claimed": "gpt-6.1-sol", "tag": "s141c592", "in_bank": false,
    "claimed_rank": null, "claimed_p": null, "top1_is_claimed": null,
    "note": "宣称模型不在库内；家族判定 gpt 与宣称一致，具体版本无法核对（库外不硬判）"
  },
  "cost_estimate": {"input_tokens_est": 1200, "output_tokens_est": 2673, "note": "粗估：输出 3 token/整数，输入 0.4K/题；只含已完成的题"},
  "latency_ms": 41230,
  "errors": []
}
```

### 族口径：candidates 与 candidates_tied

指纹的终点是**族**（2026-11 用户拍板的设计方向）：

- `family / family_p` 是族判定，比单个模型名更可靠；
- `candidates` 是胜出族内按概率降序的候选列表 `[{model, p}]`，收录族内全部 `p > 0.05` 的成员；同族模型（如同一分支的 sol/astra）在指纹上可能不可分，会如实并列列出；
- 族内 top1-top2 融合分差 < 0.40（同 `ambiguous_gap_score` 阈值）时 `candidates_tied=true`，表示多候选并列，`verdict_reason` 会附"可能是 X / Y / Z"；
- `top_model / top_p` 保留作向后兼容，语义 = 族内概率最高的候选，不再是"唯一真相"。

错误：全部题目失败时返回 **502**（`{"detail": "全部 N 题失败，最后错误：..."}`），同样留底。入参不合法 422。

### verdict 判定规则（可配置，`api/config.py: VERDICT_RULES`）

按序判断，命中即返回：

| 顺序 | 条件（默认值） | verdict |
|---|---|---|
| 1 | top1 模型名含 `luna` | **degraded**（GPT 线定谳：判出 luna 族=真降智） |
| 2 | top1 模型名含 `astra` | **clean**（判出 astra=正常没降智） |
| 3 | top1-top2 融合分差 < 0.40 | **unknown**（疑似库外强归，诚实协议不硬判；阈值依据见 family-branch-research.md §3.4） |
| 4 | top_p ≥ 0.85 | **clean**（自匹配成立） |
| 5 | 都不命中 | **unknown** |

规则改口径只动 `VERDICT_RULES`（阈值、子串都可调）；Claude 线目前没有 astra/luna 类分支标记，靠自匹配 p 值判 clean。

### 成本

每题输入约 0.4K token、输出约 900 token（300 整数 × 3）。3 题一次探针约 **4K token**，`cost_estimate` 字段按实际解析出的数字数估算。

## 3. POST /probe_batch — 多目标并发

```bash
curl -s -X POST http://127.0.0.1:8100/probe_batch -H "Content-Type: application/json" -d '{
  "targets": [
    {"base_url": "https://a中转/v1", "api_key": "sk-aaa", "model": "gpt-6.1-sol"},
    {"base_url": "https://b中转/v1", "api_key": "sk-bbb", "model": "gpt-6-astra", "rounds": 2}
  ]
}'
```

- 每个目标字段同 /probe；目标间并发 `DZ_BATCH_CONCURRENCY`（默认 4）。
- 单目标失败不拖垮整批：该目标返回 `{"verdict": "unknown", "error": "..."}`。
- 响应 `{"count": N, "results": [...]}`，results 顺序与请求 targets 一致。

## 4. GET /history — 历史查询

```bash
curl -s "http://127.0.0.1:8100/history?model=gpt-6.1-sol&days=7&limit=50"
```

| 参数 | 说明 |
|---|---|
| model | 可选，按目标模型名精确过滤 |
| days | 可选，只看最近 N 天（1~365） |
| limit | 可选，默认 100，最大 1000 |

返回按时间倒序的留底行：`ts / base_url / model / api_key_masked / verdict / verdict_reason / family / family_p / candidates（族内候选 JSON，旧记录为 null）/ top_model / top_p / top_gap / valid_samples / latency_ms / error`。

## 5. GET /health

```json
{
  "status": "ok",
  "service": "洞真（dongzhen）模型指纹探针服务",
  "version": "0.1.0",
  "bank": {"path": "/app/data/unified_bank.json", "models": 18, "built_at": "2026-10-09T..."},
  "history": {"total": 42, "by_verdict": {"clean": 30, "degraded": 5, "unknown": 7}}
}
```

## 6. 消费者集成示例（供应商系统编排器视角）

编排器定期巡检所有在售供应商，按 verdict 处置：

```python
import httpx

def inspect_vendor(client: httpx.Client, vendor: dict) -> dict:
    """对单个在售供应商做指纹巡检，返回编排器可用的处置建议。"""
    response = client.post(
        "http://dongzhen-api:8100/probe",
        json={
            "base_url": vendor["upstream_base_url"],
            "api_key": vendor["upstream_key"],        # 只进服务内存
            "model": vendor["claimed_model"],          # 供应商宣称的模型
            "pin": f'{vendor["claimed_model"]}@{vendor["batch"]}',
            "questions": 3,
        },
        timeout=600,
    )
    response.raise_for_status()
    probe = response.json()

    # 处置建议（编排器侧策略示例）
    if probe["verdict"] == "degraded":
        action = "下架告警"       # 判出 luna 族 = 真降智
    elif probe["verdict"] == "unknown" and probe["family"] != "gpt":
        action = "人工复核"       # 连家族都不对，宣称大概率造假
    elif probe["verdict"] == "unknown":
        action = "加题复核"       # 疑似库外强归/同名不同版，rounds 拉到 2~3 再看
    else:
        action = "通过"

    return {
        "vendor": vendor["name"],
        "verdict": probe["verdict"],
        "top_model": probe["top_model"],
        "top_p": probe["top_p"],
        "pinned_note": (probe.get("pinned") or {}).get("note"),
        "action": action,
    }
```

要点：
- 编排器只需要保存 `verdict / top_model / top_p / pinned.note`，**不要保存、不要转发 api_key**（服务不回显，编排器自己也不落盘）。
- `unknown + 家族一致` 的典型场景就是"6.1 系还没入库"：先用 pin 看家族判定，再走 docs/enrollment.md 把新模型录进库。
- 巡检节奏建议：新供应商上架必测；在售每周一轮；verdict 出现 degraded 的当天复测一次确认。

## 7. AI 自助模式（出题 + 判卷，无状态）

> 构想出处（2026-11-13）："主页里藏了给 AI 的提示词，直接讲清楚接口，让 AI 可以方便使用。我理解就 2 个接口：得到题目、提交答案。"
> 无状态定案（同日）："应该是无状态的。出题后，给出题目编号，提交答案的时候，带编号就行了。服务器没必要存东西。"
> 即：洞真退化为「出题 + 判卷」，**不碰目标模型的 key、不发外呼、不存题目状态**。AI（或任何消费者）自己拿题、自己去问目标模型、把答案交回来判。

### 端点

| 端点 | 作用 |
|---|---|
| `POST /challenge` | 得到题目：`{model_hint?, count=3}` → `{challenge_id, expires_at, challenges: [{index, prompt, expected_count}], usage_hint}` |
| `POST /submit` | 提交答案：`{challenge_id, target_model?, answers: [{index, text}]}` → 验签、重渲染题面、归因判定 |
| `GET /` | 主页 = 上游原版 web 工具页（api/webui.py 迁 FastAPI），AI 提示词内藏在 `<script type="text/markdown" id="ai-instructions">` 块（AI 可读、人不可见） |
| `GET /llms.txt` | 同内容纯文本版，方便 AI 直接抓取 |

### 无状态凭证：challenge_id

`challenge_id = base64url(json 负载) + "." + HMAC-SHA256 签名（hex）`，负载结构：

```json
{"v": 1, "t": 1760000000, "h": "gpt-6.1-sol", "q": [[模板号, expected_count], ...]}
```

- **自包含**：`(模板号, N)` 足以确定性重建全部题面（`fingerprint.render_challenge` 纯函数），服务器零存储、跨重启天然有效；客户端整段保存、原样传回即可。
- **防篡改**：HMAC-SHA256 验签，改任何字符返回 422；签名密钥解析顺序：环境变量 `DZ_CHALLENGE_SECRET` > `DZ_API_TOKEN` > 固定开发默认值（仅开发用，生产暴露前必须显式设置，见 `api/config.py: challenge_secret`）。换密钥后旧 id 立即失效。
- **防续期**：过期只看负载里的签发时间 `t`，签发 24 小时后过期，返回 404。
- 判定结果仍写历史留底（`probes` 表，`base_url=ai-mode`、`api_key_masked=****`、`model` 取 target_model 优先）；题目状态零存储。

### 调用示例

```bash
# 第一步：拿题（记下 challenge_id，整段保存）
curl -s -X POST http://127.0.0.1:8100/challenge \
  -H "Content-Type: application/json" \
  -d '{"model_hint": "gpt-6.1-sol", "count": 3}'

# 第二步：把每道题的 prompt 原样发给目标模型，收集纯文本回答，然后带同一 challenge_id 交卷
curl -s -X POST http://127.0.0.1:8100/submit \
  -H "Content-Type: application/json" \
  -d '{"challenge_id": "<上一步返回的 id>", "target_model": "gpt-6.1-sol",
       "answers": [{"index": 0, "text": "<模型回答原文>"}, {"index": 1, "text": "..."}, {"index": 2, "text": "..."}]}'
```

`/submit` 响应：`verdict / verdict_reason / family / family_name / family_p / candidates / candidates_tied / top_model / top_p / top_gap / valid_samples / per_challenge: [{index, parsed_count, valid}] / target_model / model_hint / expires_at`。判定口径与 `/probe` 完全一致（同一套 `VERDICT_RULES`：判 astra=clean、判 luna 族=degraded、分差过小=unknown），族口径字段语义同上（candidates=族内候选列表，多候选并列时 reason 附"可能是 X / Y / Z"）。

### 题面模板清单（`fingerprint.py`）

题面 = 开头句 + 动作句 + `"{N} 个 1 到 355（含端点）的整数。"` + 固定约束段 + 结尾句 + 分隔提示 + 固定输出段。4 组措辞随机各选一，编码为模板号（`encode_challenge_template`，0~499）：

| 组 | 下标 | 取值 |
|---|---|---|
| 开头句 openings | 0~4 | 这是一次独立的数值选择记录 / 请完成下面的无语义整数选择任务 / 执行一次第一反应取值记录 / 生成一组不承载语义的整数选择 / 进行一轮快速逐项取值 |
| 动作句 actions | 0~4 | 为各个位置分别凭第一反应选择 / 逐项选择 / 每次只决定当前一项，共给出 / 分别凭第一反应给出 / 逐个直接选择 |
| 结尾句 endings | 0~4 | 允许某个数字再次出现；每项写出后不要回头排序、去重或替换。/ 偶然重复是有效的；不要重新排列或修正已经写出的项目。/ 相同值可以再次出现；输出过程中不要整理或改写前面的项目。/ 重复值无需删除；不要筛选、重排或补成某种规律。/ 不必赋予数字任何含义；已经给出的值保持不变。 |
| 分隔提示 separators | 0~3 | 数字之间用逗号或空格分隔均可。/ 使用一种一致的常见分隔符即可。/ 可以用逗号、空格或换行分隔。/ 只要每个整数边界清楚，格式可自行选择。 |

N 每题独立随机取 292~332（与历史口径一致）。改模板措辞会影响与历史指纹数据的可比性，动前先对齐 docs/family-branch-research.md。

### 口径与注意

- **`target_model` 只作展示，不参与判定**；判定只看答案的数字分布。
- 答案必须**原样**提交：模型回答一字不改，解析由服务端完成（`parse_numbers`）。
- 每题有效性口径与 `/probe` 一致：解析出的数字数 ≥ max(80, ceil(expected_count × 0.55)) 才计入；`expected_count` 仅供核对样本量，绝不要发给目标模型。
- 错误语义：challenge_id 篡改/格式坏 → **422**；过期 → **404**；有效题不足 1 或 index 越界 → **422**（明确报错）。
- 题面已内置"禁止使用工具"禁令；调用方不要放宽。
- 鉴权：环境变量 `DZ_API_TOKEN` 设置后，`/challenge` 与 `/submit` 要求 `Authorization: Bearer <token>`（依赖实现见 `api/auth.py` 的 `require_token`，可复用）；未设置则放行。`GET /` 与 `/llms.txt` 不要求鉴权，方便 AI 先拿到说明。

### 主页 AI 提示词与地址红线

主页 `GET /` 是上游原版 web 工具页（模型测试/指纹库管理，`templates/` + `static/`，由 `api/webui.py` 挂到 FastAPI；出题/归因接口 `/api/challenges`、`/api/analyze` 与原 app.py 行为一致），页尾 `<script type="text/markdown" id="ai-instructions">` 块内嵌完整机器可读说明（服务是什么、无状态设计、两个接口的 curl 示例、族语义、判定口径、注意事项），AI 抓取可读、对人不可见，与 `GET /llms.txt` 内容同源（唯一事实源在 `api/ai_instructions.py`）。文案不包含任何密钥。

地址红线：对外可见文案（主页内藏说明、`/llms.txt`）只用 `https://dz.duojie.games`，禁止出现 127.0.0.1 / localhost / 内网地址（`tests/test_webui.py` 有断言）；`docs/` 目录内部文档不受此限。

## 8. 本地自测

```bash
python -m pytest tests/ -q          # mock 全套：probe/ai_mode/webui/verdict/store/auth，零网络零成本
```

全部 mock（用库内真实参考回答当上游返回），零网络零成本。
