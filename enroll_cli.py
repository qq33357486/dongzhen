# -*- coding: utf-8 -*-
"""洞真（dongzhen）新模型指纹录入 CLI：采集 -> 候选文件 -> 打印建议。
铁律：不写 data/unified_bank.json、不 append 任何 reference jsonl。
候选文件由人审核后，用 tools/merge_candidate.py 显式并入（见 docs/enrollment.md）。

用法（满配录入）:
  export DZ_ENROLL_KEY=sk-xxxx
  python enroll_cli.py --base-url https://官方或可信直连/v1 --model gpt-6.1-sol --family gpt --samples 36

预检（快速看一眼像谁，结果不作数）:
  python enroll_cli.py --base-url ... --model xxx --samples 3 --no-candidate

环境变量: DZ_ENROLL_KEY（API key，不落盘不进日志）
"""
import argparse
import json
import math
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

from challenge_suite import fingerprint_suite
from bank_builder import build_bank, read_rows
from enrollment import make_row, request_completion
from fingerprint import (
    analyze_global_outputs,
    count_numbers,
    js_similarity,
    parse_numbers,
    robust_score_numbers,
)

ROOT = Path(__file__).resolve().parent
UNIFIED_BANK = ROOT / "data" / "unified_bank.json"
REFERENCE_FILES = {
    "gpt": ("GPT", ROOT / "data" / "gpt_reference.jsonl"),
    "claude": ("Claude", ROOT / "data" / "claude_reference.jsonl"),
}
CANDIDATE_DIR = ROOT / "data" / "candidates"
VALIDATE_HOLDOUT = 6          # 留作自匹配验证的候选条数
NEIGHBOR_TOP = 3              # 最近邻报告条数
# token 估算口径：输出每整数约 3 token（含分隔符），输入每题约 0.4K
TOKEN_PER_NUMBER = 3
INPUT_TOKEN_PER_TASK = 400


def guess_family(label: str) -> str:
    lowered = label.lower()
    if lowered.startswith("gpt") or "openai" in lowered:
        return "gpt"
    if lowered.startswith("claude") or "anthropic" in lowered:
        return "claude"
    return ""


def select_tasks(samples: int) -> list[dict]:
    """与 enrollment.enroll_automatic 同口径：在 36 题套件上等距抽样。"""
    suite = fingerprint_suite()
    if samples < 3 or samples > len(suite):
        raise ValueError(f"题数必须在 3 到 {len(suite)} 之间")
    return [suite[int(index * len(suite) / samples)] for index in range(samples)]


def collect(base_url: str, api_key: str, api_model: str, temperature: float | None,
            api_format: str, tasks: list[dict], concurrency: int,
            reasoning_effort: str | None = None,
            checkpoint_path: Path | None = None) -> tuple[list[dict], list[str]]:
    """并发采集，每题失败或无效重试 1 次，返回 (行列表, 错误列表)。

    checkpoint_path 给定时启用断点续传：启动时恢复已采行并跳过对应题，
    每采到一条即追加落盘（进程被杀也不丢进度），全部完成后由调用方删断点文件。
    """
    rows: list[dict] = []
    errors: list[str] = []
    done_ids: set[str] = set()

    if checkpoint_path is not None and checkpoint_path.exists():
        for line in checkpoint_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                done_ids.add(record["challenge_id"])
                rows.append(record["row"])
        if done_ids:
            print(f"  断点续传：从 {checkpoint_path.name} 恢复 {len(rows)} 条，跳过对应题目", flush=True)
    if checkpoint_path is not None:
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    def run(task: dict) -> tuple[dict | None, list[str]]:
        if task["challenge_id"] in done_ids:
            return None, []
        task_errors = []
        prompt = task["prompt"]
        if task["user_prefix"]:
            prompt = task["user_prefix"] + "\n\nFinal task:\n" + prompt
        for _ in range(2):
            try:
                text = request_completion(
                    base_url, api_key, api_model, prompt, temperature, api_format, task["system"],
                    reasoning_effort=reasoning_effort,
                )
                row = make_row(
                    model_label=api_model,
                    text=text,
                    condition=task["condition"],
                    challenge_id=task["challenge_id"],
                    expected_count=task["expected_count"],
                    temperature=temperature if temperature is not None else "provider_default",
                    bank_id="candidate",
                    wrapper_transport=task["transport"],
                    provider="enroll_cli",
                    prompt=prompt,
                    base_prompt=task["prompt"],
                    system_prompt=task["system"],
                    user_prefix=task["user_prefix"],
                )
                if row["strict_valid"]:
                    return row, task_errors
                task_errors.append(
                    f"{task['challenge_id']} 有效数字不足：{row['parsed_count']}/{row['strict_threshold']}"
                )
            except Exception as error:
                task_errors.append(f"{task['challenge_id']}: {error}")
        return None, task_errors

    pending = [task for task in tasks if task["challenge_id"] not in done_ids]
    if checkpoint_path is not None and pending:
        print(f"  待采 {len(pending)}/{len(tasks)} 题（其余已由断点恢复）", flush=True)
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(run, task) for task in pending]
        for future in as_completed(futures):
            row, task_errors = future.result()
            errors.extend(task_errors)
            if row is not None:
                rows.append(row)
                if checkpoint_path is not None:
                    with checkpoint_path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps({"challenge_id": row["challenge_id"], "row": row}, ensure_ascii=False) + "\n")
                print(f"  已采集 {len(rows)}/{len(tasks)}（{row['condition_id']}，{row['parsed_count']} 个数字）", flush=True)
    return rows, errors


def load_existing_rows() -> list[dict]:
    """现库参考数据，与 rebuild_unified_bank.py 同口径打族标签。"""
    rows = []
    for family_id, (family_name, path) in REFERENCE_FILES.items():
        if path.exists():
            rows.extend({**row, "family_id": family_id, "family_name": family_name} for row in read_rows(path))
    return rows


def enrich_rows(rows: list[dict]) -> None:
    """make_row 不带 numbers/counts，建库前补上（与 read_rows 同口径）。"""
    for row in rows:
        row["numbers"] = parse_numbers(row["text"])
        row["counts"] = count_numbers(row["numbers"])


def tagged_candidate_rows(rows: list[dict], label: str, family: str) -> list[dict]:
    family_names = {"gpt": "GPT", "claude": "Claude"}
    return [
        {
            **row,
            "source": label,
            "family_id": family,
            "family_name": family_names.get(family, family),
        }
        for row in rows
    ]


def build_temp_bank(fit_rows: list[dict]) -> dict:
    """现库数据 + 候选拟合集 -> 内存临时库（不落盘）。"""
    bank = build_bank(load_existing_rows() + fit_rows)
    return bank


def neighbor_report(bank: dict, label: str) -> list[dict]:
    """新模型与库内模型的两两距离：JS 相似度 + 打分空间中心余弦。"""
    models = bank["models"]
    index = {model["id"]: i for i, model in enumerate(models)}
    if label not in index:
        return []
    centroids = np.asarray(bank["robust"]["hellinger"]["centroids"], dtype=np.float64)
    own = models[index[label]]
    report = []
    for model in models:
        if model["id"] == label:
            continue
        report.append(
            {
                "model": model["id"],
                "js_sim": js_similarity(own["counts"], model["counts"]),
                "centroid_cos": float(centroids[index[label]] @ centroids[index[model["id"]]]),
            }
        )
    report.sort(key=lambda item: -item["centroid_cos"])
    return report[:NEIGHBOR_TOP]


def self_match_report(bank: dict, holdout_rows: list[dict], label: str) -> dict:
    """验证集每 3 条一组做归因，看 top1 是否自身。"""
    groups = [holdout_rows[i : i + 3] for i in range(0, len(holdout_rows) - 2, 3)]
    results = []
    for group in groups:
        outputs = [{"text": row["text"], "expected_count": row["requested_count"]} for row in group]
        try:
            result = analyze_global_outputs(outputs, bank)
        except ValueError as error:
            results.append({"error": str(error)})
            continue
        top2 = result["results"][1] if len(result["results"]) > 1 else None
        results.append(
            {
                "top1": result["prediction"],
                "p": result["probability"],
                "gap": result["probability"] - (top2["probability"] if top2 else 0.0),
            }
        )
    hits = sum(1 for item in results if item.get("top1") == label)
    return {"groups": results, "hits": hits, "total": len(results)}


def bootstrap_d(bank: dict, candidate_rows: list[dict], neighbor_id: str, label: str, rng) -> float | None:
    """新模型与某近邻的 Cohen's d（融合得分差/合并标准差），与 confusion_analysis 同口径。"""
    models = bank["models"]
    index = {model["id"]: i for i, model in enumerate(models)}
    if label not in index or neighbor_id not in index:
        return None
    ia, ib = index[label], index[neighbor_id]
    own_scores = np.stack([
        np.asarray(robust_score_numbers(row["numbers"], bank)["fused"]) for row in candidate_rows
    ])
    # 近邻的参考回答要从现库参考数据里找
    neighbor_rows = [row for row in load_existing_rows() if row["source"] == neighbor_id]
    if not neighbor_rows:
        return None
    other_scores = np.stack([
        np.asarray(robust_score_numbers(row["numbers"], bank)["fused"]) for row in neighbor_rows
    ])
    da = own_scores[:, ia] - own_scores[:, ib]
    db = other_scores[:, ib] - other_scores[:, ia]
    pooled = math.sqrt((da.var(ddof=1) + db.var(ddof=1)) / 2.0)
    return float((da.mean() + db.mean()) / 2.0 / max(pooled, 1e-12))


def distribution_summary(rows: list[dict]) -> dict:
    """数字分布摘要：均值/标准差/最热单值占比。"""
    all_numbers = [n for row in rows for n in row["numbers"]]
    if not all_numbers:
        return {}
    counts = Counter(all_numbers)
    top_value, top_count = counts.most_common(1)[0]
    mean = sum(all_numbers) / len(all_numbers)
    variance = sum((n - mean) ** 2 for n in all_numbers) / len(all_numbers)
    return {
        "numbers": len(all_numbers),
        "mean": round(mean, 1),
        "std": round(math.sqrt(variance), 1),
        "top_value": top_value,
        "top_value_share": round(top_count / len(all_numbers), 4),
        "per_row_counts": [row["parsed_count"] for row in rows],
    }


def token_estimate(rows: list[dict], tasks: int) -> dict:
    out_tokens = sum(row["parsed_count"] for row in rows) * TOKEN_PER_NUMBER
    in_tokens = tasks * INPUT_TOKEN_PER_TASK
    return {"input_tokens_est": in_tokens, "output_tokens_est": out_tokens, "note": "粗估：输出 3 token/整数，输入 0.4K/题"}


def main() -> None:
    parser = argparse.ArgumentParser(description="洞真新模型指纹录入（采集->候选文件，不写库）")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True, help="上游 API 模型名")
    parser.add_argument("--label", default=None, help="库内显示名，默认同 --model")
    parser.add_argument("--family", default=None, help="gpt / claude / 新族名；不传则按名字猜，猜不出要报错")
    parser.add_argument("--samples", type=int, default=36, help="采集题数，满配 36（3~36）")
    parser.add_argument("--temperature", type=float, default=None, help="留空=供应商默认（推荐，与现库同口径）")
    parser.add_argument("--reasoning-effort", default=None, choices=["low", "high", "max"],
                        help="思考档（OpenAI 兼容 reasoning_effort 字段）；留空=供应商默认。换档后旧断点须 --fresh 重采")
    parser.add_argument("--format", default="openai", choices=["openai", "anthropic", "auto"])
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--fresh", action="store_true", help="忽略并删除断点文件，从头重采")
    parser.add_argument("--no-candidate", action="store_true", help="预检模式：不写候选文件（也不写断点）")
    parser.add_argument("--out-dir", type=Path, default=CANDIDATE_DIR)
    args = parser.parse_args()

    api_key = os.environ.get("DZ_ENROLL_KEY", "").strip()
    if not api_key:
        sys.exit("错误：请先 export DZ_ENROLL_KEY=...（key 只进内存，不落盘）")
    label = args.label or args.model
    family = args.family or guess_family(label)
    if not family:
        sys.exit(f"错误：无法从 {label} 猜族名，请显式传 --family gpt|claude|<新族名>")

    if not UNIFIED_BANK.exists():
        sys.exit(f"错误：找不到现库 {UNIFIED_BANK}")

    print(f"== 洞真录入采集：{label}（族 {family}，{args.samples} 题，源 {args.base_url}）==")
    if args.reasoning_effort:
        print(f"  思考档：reasoning_effort={args.reasoning_effort}（非供应商默认，注意与库口径一致性）")
    checkpoint_path = None if args.no_candidate else args.out_dir / f"checkpoint-{label}.jsonl"
    if checkpoint_path is not None and args.fresh and checkpoint_path.exists():
        checkpoint_path.unlink()
        print("  已删旧断点，从头重采")
    tasks = select_tasks(args.samples)
    started = time.time()
    rows, errors = collect(args.base_url, api_key, args.model, args.temperature, args.format, tasks,
                           args.concurrency, reasoning_effort=args.reasoning_effort,
                           checkpoint_path=checkpoint_path)
    elapsed = time.time() - started
    print(f"采集完成：有效 {len(rows)}/{len(tasks)}，用时 {elapsed:.0f}s")
    if errors:
        print("错误列表：")
        for error in errors[:10]:
            print(f"  {error}")
    if len(rows) < 3:
        sys.exit("错误：有效样本不足 3 条，无法评估。请检查端点/max_tokens 后重试。")
    enrich_rows(rows)

    # 拆验证集：留最多 6 条做自匹配，其余进拟合
    rng = np.random.default_rng(int(time.time()))
    order = rng.permutation(len(rows))
    holdout_n = min(VALIDATE_HOLDOUT, max(0, len(rows) - 3))
    holdout = [rows[i] for i in order[:holdout_n]]
    fit = [rows[i] for i in order[holdout_n:]]

    print("正在内存中拼临时库并验证（不落盘，约需十几秒）...")
    tagged = tagged_candidate_rows(fit, label, family)
    bank = build_temp_bank(tagged)
    neighbors = neighbor_report(bank, label)
    for item in neighbors:
        item["d"] = bootstrap_d(bank, tagged, item["model"], label, rng)
    self_match = self_match_report(bank, holdout, label) if holdout else {"groups": [], "hits": 0, "total": 0}
    summary = distribution_summary(rows)
    tokens = token_estimate(rows, len(tasks))

    # ---- 打印建议 ----
    print("\n-- 数字分布摘要 --")
    print(f"  总数字 {summary['numbers']}，均值 {summary['mean']}，标准差 {summary['std']}，最热值 {summary['top_value']}（占比 {summary['top_value_share']:.1%}）")
    print("\n-- 与现库最近邻（centroid_cos 越大越近）--")
    for item in neighbors:
        d_str = f"{item['d']:.2f}" if item["d"] is not None else "-"
        print(f"  {item['model']:<28} cos={item['centroid_cos']:+.3f}  js_sim={item['js_sim']:.3f}  d={d_str}")
    print("\n-- 自匹配验证（留出样本每 3 条归因）--")
    print(f"  命中自身 {self_match['hits']}/{self_match['total']} 组")
    for i, item in enumerate(self_match["groups"], 1):
        if "error" in item:
            print(f"  组{i}: 失败 {item['error']}")
        else:
            print(f"  组{i}: top1={item['top1']} p={item['p']:.4f} top1-top2分差={item['gap']:.3f}")
    print(f"\n-- token 估算 -- {tokens}")
    print("\n-- 建议 --")
    advice = []
    if args.samples < 36:
        advice.append(f"本次仅 {args.samples} 题，属预检量级，不可直接入库；正式录入请满配 36 题")
    if self_match["total"] and self_match["hits"] < self_match["total"]:
        worst = min((item["gap"] for item in self_match["groups"] if "gap" in item), default=0)
        advice.append(f"自匹配未全中（最小组间分差 {worst:.3f}），与最近邻 {neighbors[0]['model'] if neighbors else '?'} 重叠，慎入库")
    for item in neighbors:
        if item["d"] is not None and item["d"] < 2:
            advice.append(f"与 {item['model']} 的 d={item['d']:.2f} < 2：3 题分不开，不建议直接入库（见 docs/enrollment.md §7）")
    if summary.get("top_value_share", 0) > 0.08:
        advice.append(f"单值 {summary['top_value']} 占比 {summary['top_value_share']:.1%} 偏高，人工检查回答全文是否有复读")
    if not advice:
        advice.append("各项指标达标，可人工审核候选文件后按 docs/enrollment.md §5 并入")
    for line in advice:
        print(f"  * {line}")

    if args.no_candidate:
        print("\n预检模式：未写候选文件。")
        return

    # ---- 写候选文件 ----
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate_path = args.out_dir / f"{label}-{stamp}.json"
    payload = {
        "schema": "dongzhen-enrollment-candidate",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "label": label,
        "family": family,
        "base_url": args.base_url,
        "temperature": args.temperature if args.temperature is not None else "provider_default",
        "reasoning_effort": args.reasoning_effort or "provider_default",
        "requested_samples": args.samples,
        "accepted_samples": len(rows),
        "errors": errors,
        "summary": summary,
        "neighbors": neighbors,
        "self_match": self_match,
        "token_estimate": tokens,
        "merge_instructions": [
            "1) 人工审核本文件：看 neighbors/self_match/summary 是否达标（docs/enrollment.md §4）",
            "2) 备份现库：cp data/unified_bank.json data/unified_bank.json.bak-$(date +%F-%H%M%S)",
            f"3) 显式并入：python tools/merge_candidate.py {candidate_path.name}",
            "4) 回归验证：python tools/confusion_analysis.py，并按 docs/enrollment.md §6 走完 checklist",
        ],
        "rows": rows,
    }
    candidate_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if checkpoint_path is not None and checkpoint_path.exists():
        checkpoint_path.unlink()
        print("断点文件已清理（候选文件已完整落盘）")
    print(f"\n候选文件已写：{candidate_path}")
    print("该文件不会自动进入任何库；审核后按 merge_instructions 并入。")


if __name__ == "__main__":
    # 仅脚本直跑时重包 stdout 为 UTF-8（避免 Windows 控制台中文乱码）；import 时不动，兼容 pytest
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
