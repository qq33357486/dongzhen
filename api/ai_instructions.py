# -*- coding: utf-8 -*-
"""AI 自助模式的说明文案与主页 HTML。
AI_INSTRUCTIONS 是唯一事实源：/llms.txt 原样输出它，主页 HTML 把它转义后嵌进
<pre id="ai-instructions">，方便 AI 抓取结构化说明。不放任何密钥。
"""
from __future__ import annotations

import html

AI_INSTRUCTIONS = """\
洞真（dongzhen）模型指纹探针 — AI 自助模式使用说明
================================================================

【这是什么】
洞真是模型指纹探针服务，只做两件事：出题（生成整数挑战）和判卷（把回答的
数字分布与指纹库比对，归因出目标模型真身 / 是否降智）。
洞真不接触你的 API key，也不向目标模型发任何请求——你自己去问目标模型，
把回答原样交回来判。

【流程】
1) POST /challenge 拿题；
2) 你把每道题的 prompt 原样发给目标模型（不要改写题面，不要允许它用工具）；
3) POST /submit 交答案，拿判定。

【接口一：拿题】POST /challenge
curl 示例：
    curl -s -X POST http://127.0.0.1:8100/challenge \\
      -H "Content-Type: application/json" \\
      -d '{"model_hint": "gpt-6.1-sol", "count": 3}'
入参：model_hint 可选（宣称的模型名，只作展示与结果解读提示）；count 可选，默认 3，范围 1~10。
响应：{"challenge_id", "expires_at", "challenges": [{"index", "prompt", "expected_count"}], "usage_hint"}

【接口二：交卷】POST /submit
curl 示例：
    curl -s -X POST http://127.0.0.1:8100/submit \\
      -H "Content-Type: application/json" \\
      -d '{"challenge_id": "<上一步返回的 id>", "target_model": "gpt-6.1-sol",
           "answers": [{"index": 0, "text": "<模型回答原文>"}, {"index": 1, "text": "..."}, {"index": 2, "text": "..."}]}'
入参：challenge_id 必填；target_model 可选（只作展示，不参与判定）；answers 必填，index 对应题目编号（0 起）。
响应关键字段：verdict / verdict_reason / family / family_p / top_model / top_p / valid_samples / per_challenge。

【判定口径 verdict】
- clean：正常。指纹自匹配成立，或判出 astra 分支。
- degraded：降智。top1 命中 luna 族。
- unknown：无法判定。top1-top2 分差过小（疑似库外强归）或 p 值不满足任何规则，诚实协议不硬判。
典型读法（宣称 gpt 系时）：判出 astra = 正常没降智；判出 luna 族 = 真降智；unknown = 存疑，建议加题复测。

【注意事项】
- 答案必须原样提交：模型的原始文本一字不改（保留标点、换行、前后缀），解析由服务端完成。
- 不要让目标模型使用任何工具（代码执行器、计算器、搜索等）；题面已内置禁令，你不要放宽。
- expected_count 是期望数字数，仅供你核对样本量，绝不要发给目标模型。
- 挑战 24 小时过期；过期或不存在的 challenge_id 返回 404。
- 有效题（解析出的数字数达标）不足 1 时返回 422，无法归因。
- target_model 只作展示，不参与判定；判定只看答案的数字分布。
- 本服务可能启用鉴权：设置 DZ_API_TOKEN 后，请求需带 Authorization: Bearer <token>。
- 本说明的纯文本版：GET /llms.txt
"""

USAGE_HINT = (
    "把每道题的 prompt 原样发给目标模型：不要改写题面，不要告诉它 expected_count，"
    "也不要允许它使用任何工具；收集模型的纯文本回答，一字不改地作为该题 text。"
    '全部回答就绪后 POST /submit：{"challenge_id": "...", "target_model": "<宣称模型>", '
    '"answers": [{"index": 0, "text": "<回答原文>"}, ...]}'
)

_PAGE_CSS = """\
body{font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
     max-width:920px;margin:0 auto;padding:24px;line-height:1.65;color:#1f2328}
h1{font-size:1.5em;border-bottom:2px solid #d0d7de;padding-bottom:8px}
h2{font-size:1.15em;margin-top:1.6em}
code,pre{background:#f6f8fa;border-radius:6px;font-size:0.92em}
code{padding:2px 6px}
pre{padding:14px;overflow-x:auto;border:1px solid #d0d7de}
table{border-collapse:collapse;margin:8px 0}
th,td{border:1px solid #d0d7de;padding:6px 12px;text-align:left}
th{background:#f6f8fa}
.note{color:#57606a;font-size:0.9em}
"""

_PAGE_BODY = """\
<h1>洞真（dongzhen）模型指纹探针</h1>
<p>对中转站 / 上游市场的模型做真身判定：整数生成挑战 + 数字分布指纹归因。
本页同时提供 <strong>AI 自助模式</strong>：洞真只出题和判卷，不碰你的 key、不向目标模型发请求。</p>

<h2>AI 自助模式（两步）</h2>
<ol>
  <li><code>POST /challenge</code> 拿题（返回题目与 challenge_id，24 小时有效）；</li>
  <li>你自己把每道题的 prompt 原样发给目标模型，收集纯文本回答；</li>
  <li><code>POST /submit</code> 交答案，返回 verdict（clean / degraded / unknown）。</li>
</ol>

<h2>接口一：拿题</h2>
<pre><code>curl -s -X POST http://127.0.0.1:8100/challenge \\
  -H "Content-Type: application/json" \\
  -d '{"model_hint": "gpt-6.1-sol", "count": 3}'</code></pre>

<h2>接口二：交卷</h2>
<pre><code>curl -s -X POST http://127.0.0.1:8100/submit \\
  -H "Content-Type: application/json" \\
  -d '{"challenge_id": "&lt;上一步返回的 id&gt;", "target_model": "gpt-6.1-sol",
       "answers": [{"index": 0, "text": "&lt;模型回答原文&gt;"}, {"index": 1, "text": "..."}, {"index": 2, "text": "..."}]}'</code></pre>

<h2>判定口径</h2>
<table>
  <tr><th>verdict</th><th>含义</th></tr>
  <tr><td>clean</td><td>正常：指纹自匹配成立，或判出 astra 分支（宣称 gpt 系时判 astra = 没降智）</td></tr>
  <tr><td>degraded</td><td>降智：top1 命中 luna 族（判出 luna 族 = 真降智）</td></tr>
  <tr><td>unknown</td><td>无法判定：分差过小疑似库外强归，诚实协议不硬判；建议加题复测</td></tr>
</table>

<h2>注意事项</h2>
<ul>
  <li>答案<strong>原样</strong>提交：模型回答一字不改，解析由服务端完成。</li>
  <li>别让目标模型使用任何工具（代码执行器、计算器、搜索等）。</li>
  <li><code>expected_count</code> 仅供核对样本量，绝不要发给目标模型。</li>
  <li>挑战 24 小时过期；有效题不足 1 时返回 422。</li>
  <li>服务可能启用鉴权（环境变量 <code>DZ_API_TOKEN</code>）：请求带 <code>Authorization: Bearer &lt;token&gt;</code>。</li>
</ul>

<h2>其他端点</h2>
<p>
<code>POST /probe</code>（服务代调目标模型的完整探针）、<code>POST /probe_batch</code>、
<code>GET /history</code>、<code>GET /health</code>、<code>GET /docs</code>（OpenAPI 交互文档）、
<code>GET /llms.txt</code>（本说明纯文本版）。
完整文档见仓库 <code>docs/api.md</code>。
</p>

<h2>给 AI 的使用说明（机器可读）</h2>
<pre id="ai-instructions">{instructions}</pre>

<p class="note">洞真 dongzhen — 模型指纹探针服务</p>
"""


def build_home_html() -> str:
    """主页：人类可读版面 + 内嵌机器可读说明块（id="ai-instructions"）。"""
    body = _PAGE_BODY.replace("{instructions}", html.escape(AI_INSTRUCTIONS))
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>洞真（dongzhen）模型指纹探针 — AI 自助模式</title>\n"
        f"<style>{_PAGE_CSS}</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )
