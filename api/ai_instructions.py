# -*- coding: utf-8 -*-
"""AI 自助模式的说明文案（唯一事实源）。
/llms.txt 原样输出 AI_INSTRUCTIONS；主页（上游原版 web 工具页，templates/index.html）
把它内藏进 <script type="text/markdown" id="ai-instructions"> 块——对 AI 抓取可读、
对人不可见。地址红线：对外文案只用 https://dz.duojie.games，
禁止 127.0.0.1 / localhost / 内网地址（tests/test_webui.py 有红线断言）。不放任何密钥。
"""
from __future__ import annotations

# 对外唯一公开域名（地址红线）
PUBLIC_BASE_URL = "https://dz.duojie.games"

AI_INSTRUCTIONS = """\
洞真（dongzhen）模型指纹探针 — AI 自助模式使用说明
================================================================

【这是什么】
洞真是模型指纹探针服务，只做两件事：出题（生成整数挑战）和判卷（把回答的
数字分布与指纹库比对，归因出目标模型真身 / 是否降智）。
洞真不接触你的 API key，也不向目标模型发任何请求——你自己去问目标模型，
把回答原样交回来判。

【无状态设计】
服务端不存任何题目状态：challenge_id 本身自带题目信息（签名防篡改）。
拿题后把 challenge_id 整段保存、原样传回即可，无需会话、跨重启有效。

【流程】
1) POST /challenge 拿题（返回 challenge_id 与题目）；
2) 你把每道题的 prompt 原样发给目标模型（不要改写题面，不要允许它用工具）；
3) POST /submit 交答案（带同一个 challenge_id），拿判定。

【接口一：拿题】POST /challenge
curl 示例：
    curl -s -X POST https://dz.duojie.games/challenge \\
      -H "Content-Type: application/json" \\
      -d '{"model_hint": "gpt-6.1-sol", "count": 3}'
入参：model_hint 可选（宣称的模型名，只作展示与结果解读提示）；count 可选，默认 3，范围 1~10。
响应：{"challenge_id", "expires_at", "challenges": [{"index", "prompt", "expected_count"}], "usage_hint"}

【接口二：交卷】POST /submit
curl 示例：
    curl -s -X POST https://dz.duojie.games/submit \\
      -H "Content-Type: application/json" \\
      -d '{"challenge_id": "<上一步返回的 id>", "target_model": "gpt-6.1-sol",
           "answers": [{"index": 0, "text": "<模型回答原文>"}, {"index": 1, "text": "..."}, {"index": 2, "text": "..."}]}'
入参：challenge_id 必填；target_model 可选（只作展示，不参与判定）；answers 必填，index 对应题目编号（0 起）。
响应关键字段：verdict / verdict_reason / family / family_p / candidates / candidates_tied /
top_model / top_p / valid_samples / per_challenge。

【族与候选列表（重要读法）】
指纹的终点是"族"：family 是族判定（如 gpt / claude），比单个模型名更可靠。
candidates 是族内按概率降序的候选列表 [{"model", "p"}]，p > 0.05 的族内成员都会列出。
同族模型（例如同一分支的 sol / astra）在数字指纹上可能不可分：此时 top_model 只是
族内概率最高的候选，不是唯一真相；族内 top1-top2 分差过小时 candidates_tied=true，
表示多候选并列。读结论请以 family + candidates 为准，表述成"可能是 X / Y / Z"，
不要硬塞单个模型名；同族模型分不开时我们会如实并列列出。

【判定口径 verdict】
- clean：正常。指纹自匹配成立，或判出 astra 分支。
- degraded：降智。top1 命中 luna 族。
- unknown：无法判定。top1-top2 分差过小（疑似库外强归）或 p 值不满足任何规则，诚实协议不硬判。
verdict 的判定规则不变（基于族与分支规则），但 verdict_reason 表述附族内候选：
"可能是 X / Y / Z"。
典型读法（宣称 gpt 系时）：判出 astra = 正常没降智；判出 luna 族 = 真降智；unknown = 存疑，建议加题复测。

【注意事项】
- 答案必须原样提交：模型的原始文本一字不改（保留标点、换行、前后缀），解析由服务端完成。
- 不要让目标模型使用任何工具（代码执行器、计算器、搜索等）；题面已内置禁令，你不要放宽。
- expected_count 是期望数字数，仅供你核对样本量，绝不要发给目标模型。
- challenge_id 是自包含凭证（服务器不存题目状态）：整段保存、原样传回，勿改任何字符；
  签发 24 小时后过期（过期返回 404），被篡改返回 422。
- 有效题（解析出的数字数达标）不足 1 时返回 422，无法归因。
- target_model 只作展示，不参与判定；判定只看答案的数字分布。
- 本服务可能启用鉴权：设置 DZ_API_TOKEN 后，请求需带 Authorization: Bearer <token>。
- 本说明的纯文本版：GET /llms.txt
"""

USAGE_HINT = (
    "把每道题的 prompt 原样发给目标模型：不要改写题面，不要告诉它 expected_count，"
    "也不要允许它使用任何工具；收集模型的纯文本回答，一字不改地作为该题 text。"
    "challenge_id 自带题目信息且服务器不存状态，请整段保存、交卷时原样传回。"
    '全部回答就绪后 POST /submit：{"challenge_id": "...", "target_model": "<宣称模型>", '
    '"answers": [{"index": 0, "text": "<回答原文>"}, ...]}'
)

assert "</script" not in AI_INSTRUCTIONS.lower(), "AI_INSTRUCTIONS 会破坏内藏 <script> 块"
