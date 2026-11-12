# -*- coding: utf-8 -*-
"""洞真（dongzhen）库内混淆度分析：零成本本地计算，不发任何 API 请求。
用途：
  1) 计算库内全部模型对的指纹距离/相似度矩阵，找最易混淆的模型对 TOP；
  2) 用参考数据做留出自举（bootstrap），估计 k=1..6 题下的归因正确率与混淆对；
  3) 对最近邻对做统计功效分析：要分清它们需要几题。
用法: python tools/confusion_analysis.py [bank路径，默认 data/unified_bank.json]
输出: 控制台报告（中文），数据可直接粘进 docs/family-branch-research.md。
"""
import io
import itertools
import math
import os
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from fingerprint import (
    DIMENSION,
    js_similarity,
    load_bank,
    robust_score_numbers,
)
from bank_builder import read_rows

ROOT = Path(__file__).resolve().parents[1]


def hellinger_distance(counts_a: list[int], counts_b: list[int]) -> float:
    """两条计数直方图的 Hellinger 距离（0=完全一致，1=支撑不相交）。"""
    a = np.asarray(counts_a, dtype=np.float64) + 0.5
    b = np.asarray(counts_b, dtype=np.float64) + 0.5
    p = np.sqrt(a / a.sum())
    q = np.sqrt(b / b.sum())
    return float(np.linalg.norm(p - q) / math.sqrt(2.0))


def pairwise_metrics(bank: dict) -> list[dict]:
    """库内所有模型对的四种距离指标。"""
    models = bank["models"]
    hel_centroids = np.asarray(bank["robust"]["hellinger"]["centroids"], dtype=np.float64)
    ordered = bank["robust"].get("ordered_blocks")
    ord_centroids = np.asarray(ordered["centroids"], dtype=np.float64) if ordered else None
    pairs = []
    for i, j in itertools.combinations(range(len(models)), 2):
        a, b = models[i], models[j]
        pairs.append(
            {
                "pair": (a["id"], b["id"]),
                "family_pair": (a.get("family"), b.get("family")),
                "js_sim": js_similarity(a["counts"], b["counts"]),
                "hellinger": hellinger_distance(a["counts"], b["counts"]),
                "centroid_cos": float(hel_centroids[i] @ hel_centroids[j]),
                "ordered_cos": float(ord_centroids[i] @ ord_centroids[j]) if ord_centroids is not None else None,
            }
        )
    return pairs


def score_all_rows(bank: dict, sources: dict) -> dict[str, np.ndarray]:
    """对每个模型的每条参考回答算 18 维融合得分，返回 {模型: (行数, 模型数) 得分矩阵}。"""
    model_ids = [m["id"] for m in bank["models"]]
    out = {}
    for model_id, rows in sources.items():
        if model_id not in model_ids:
            continue
        scores = np.stack([np.asarray(robust_score_numbers(row["numbers"], bank)["fused"]) for row in rows])
        out[model_id] = scores
    return out


def bootstrap_accuracy(scores: dict[str, np.ndarray], model_ids: list[str], k: int, trials: int, rng: np.random.Generator) -> tuple[float, dict]:
    """每模型有放回抽 k 条回答取均值分、全局 argmax，统计正确率与混淆计数。"""
    correct = 0
    confusion: dict[tuple[str, str], int] = {}
    total = 0
    for truth_id in model_ids:
        own = scores[truth_id]
        for _ in range(trials):
            pick = rng.choice(own.shape[0], size=k, replace=True)
            mean = own[pick].mean(axis=0)
            pred = model_ids[int(np.argmax(mean))]
            total += 1
            if pred == truth_id:
                correct += 1
            else:
                key = (truth_id, pred)
                confusion[key] = confusion.get(key, 0) + 1
    return correct / total, confusion


def margin_stats(scores: dict[str, np.ndarray], model_ids: list[str], k: int, trials: int, rng: np.random.Generator) -> dict[str, float]:
    """每模型抽 k 条，算 top1-top2 分差分布（诚实协议阈值要用）。"""
    gaps = []
    for truth_id in model_ids:
        own = scores[truth_id]
        for _ in range(trials):
            pick = rng.choice(own.shape[0], size=k, replace=True)
            mean = np.sort(own[pick].mean(axis=0))[::-1]
            gaps.append(mean[0] - mean[1])
    arr = np.asarray(gaps)
    return {"p5": float(np.percentile(arr, 5)), "p25": float(np.percentile(arr, 25)), "median": float(np.median(arr))}


def effect_size(a_scores: np.ndarray, b_scores: np.ndarray) -> float:
    """两个模型在彼此维度上的得分差 / 合并标准差（Cohen's d 近似）。
    取 A 的 (A分-B分) 与 B 的 (B分-A分) 两组差值样本。"""
    da = a_scores[:, 0] - a_scores[:, 1]
    db = b_scores[:, 0] - b_scores[:, 1]
    pooled = math.sqrt((da.var(ddof=1) + db.var(ddof=1)) / 2.0)
    return float((da.mean() + db.mean()) / 2.0 / max(pooled, 1e-12))


def questions_needed(d: float, power: float = 0.8, alpha: float = 0.05) -> float:
    """双侧 t 检验近似样本量：n = 2*(z_{1-a/2}+z_power)^2 / d^2。"""
    z_alpha = 1.959964
    z_power = {0.8: 0.841621, 0.9: 1.281552}[power]
    return 2.0 * (z_alpha + z_power) ** 2 / (d ** 2)


def main() -> None:
    args = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
    if len(sys.argv) > 1 and any(arg.startswith("-") and arg not in ("-",) for arg in sys.argv[1:]) and not args:
        print(__doc__)
        return
    bank_path = Path(args[0]) if args else ROOT / "data" / "unified_bank.json"
    bank = load_bank(bank_path)
    model_ids = [m["id"] for m in bank["models"]]

    # 参考数据：把 gpt/claude 两份 jsonl 都读进来（与 rebuild_unified_bank 同口径）
    rows = []
    for family_id, (family_name, path) in {
        "gpt": ("GPT", ROOT / "data" / "gpt_reference.jsonl"),
        "claude": ("Claude", ROOT / "data" / "claude_reference.jsonl"),
    }.items():
        rows.extend({**row, "family_id": family_id, "family_name": family_name} for row in read_rows(path))
    sources: dict[str, list] = {}
    for row in rows:
        sources.setdefault(row["source"], []).append(row)

    print(f"== 洞真库内混淆度分析（{bank_path.name}，built {bank['built_at'][:10]}）==")
    print(f"模型数 {len(model_ids)}，参考回答 {sum(len(v) for v in sources.values())} 条\n")

    # --- 1) 模型对距离矩阵 ---
    pairs = pairwise_metrics(bank)
    print("## 模型对距离指标（同族内按 centroid_cos 降序，中心余弦越接近 1 越难分）")
    same_family = [p for p in pairs if p["family_pair"][0] == p["family_pair"][1]]
    cross_family = [p for p in pairs if p["family_pair"][0] != p["family_pair"][1]]
    same_family.sort(key=lambda p: -p["centroid_cos"])
    print(f"同族模型对 {len(same_family)} 个，跨族 {len(cross_family)} 个")
    print(f"跨族 js_sim 范围: [{min(p['js_sim'] for p in cross_family):.4f}, {max(p['js_sim'] for p in cross_family):.4f}]")
    print(f"跨族 centroid_cos 范围: [{min(p['centroid_cos'] for p in cross_family):.4f}, {max(p['centroid_cos'] for p in cross_family):.4f}]")
    print("\n打分空间最易混淆 TOP12（centroid_cos 降序）：")
    print(f"{'模型对':<48} {'js_sim':>8} {'hellinger':>10} {'cen_cos':>8} {'ord_cos':>8}")
    for p in same_family[:12]:
        a, b = p["pair"]
        ord_cos = f"{p['ordered_cos']:.4f}" if p["ordered_cos"] is not None else "-"
        print(f"{a+' / '+b:<48} {p['js_sim']:>8.4f} {p['hellinger']:>10.4f} {p['centroid_cos']:>8.4f} {ord_cos:>8}")
    print("\n打分空间最可分 TOP5（centroid_cos 升序，族内可分的参照）：")
    for p in same_family[-5:]:
        a, b = p["pair"]
        ord_cos = f"{p['ordered_cos']:.4f}" if p["ordered_cos"] is not None else "-"
        print(f"{a+' / '+b:<48} {p['js_sim']:>8.4f} {p['hellinger']:>10.4f} {p['centroid_cos']:>8.4f} {ord_cos:>8}")
    gpt_same = [p for p in same_family if p["family_pair"][0] == "gpt"]
    claude_same = [p for p in same_family if p["family_pair"][0] == "claude"]
    print(f"\nGPT 系内部 {len(gpt_same)} 对 centroid_cos 范围: [{min(p['centroid_cos'] for p in gpt_same):.4f}, {max(p['centroid_cos'] for p in gpt_same):.4f}]")
    print(f"Claude 系内部 {len(claude_same)} 对 centroid_cos 范围: [{min(p['centroid_cos'] for p in claude_same):.4f}, {max(p['centroid_cos'] for p in claude_same):.4f}]")

    # --- 2) 逐条回答打分 + bootstrap 归因 ---
    if not sources:
        print("\n（未找到参考 jsonl，跳过 bootstrap 部分）")
        return
    scores = score_all_rows(bank, sources)
    rng = np.random.default_rng(20261113)
    trials = 200
    print("\n## 自举归因正确率（每模型 200 次有放回抽题，全局 argmax）")
    confusion_all = {}
    for k in (1, 2, 3, 4, 5, 6):
        acc, confusion = bootstrap_accuracy(scores, model_ids, k, trials, rng)
        confusion_all[k] = confusion
        print(f"  k={k} 题: 正确率 {acc:.4f}")

    print("\n## k=3 时混淆对 TOP10（真值→误判 次数 / 200）")
    top_conf = sorted(confusion_all[3].items(), key=lambda kv: -kv[1])[:10]
    for (truth, pred), n in top_conf:
        print(f"  {truth} -> {pred}: {n}")

    # --- 3) 最近邻对的功效分析（同族全部对都算，按 d 升序取最难的 15 对） ---
    print("\n## 同族模型对功效分析（Cohen's d 升序，最难的 15 对）")
    stats_lines = []
    for p in same_family:
        a, b = p["pair"]
        ia, ib = model_ids.index(a), model_ids.index(b)
        # d = 两模型在彼此两维上的融合得分差 / 合并 std
        da = scores[a][:, ia] - scores[a][:, ib]
        db = scores[b][:, ib] - scores[b][:, ia]
        pooled = math.sqrt((da.var(ddof=1) + db.var(ddof=1)) / 2.0)
        d = (da.mean() + db.mean()) / 2.0 / max(pooled, 1e-12)
        stats_lines.append((f"{a} / {b}", d))
    stats_lines.sort(key=lambda x: x[1])
    for label, d in stats_lines[:15]:
        n80 = questions_needed(d, 0.8)
        n90 = questions_needed(d, 0.9)
        print(f"  {label:<48} d={d:6.3f}  80%功效需 {n80:5.1f} 题  90%功效需 {n90:5.1f} 题")

    # --- 4) top1-top2 分差分布（诚实协议阈值） ---
    print("\n## top1-top2 分差分布（自有模型自举，k=3）")
    for k in (1, 2, 3):
        m = margin_stats(scores, model_ids, k, 200, rng)
        print(f"  k={k}: p5={m['p5']:.4f} p25={m['p25']:.4f} 中位={m['median']:.4f}")

    # --- 5) 库外代理：剔除单个模型后给它自己的回答打分（模拟 6.1 系等库外模型） ---
    print("\n## 库外代理（leave-one-model-out）：剔除该模型后，它的 36 条回答被判成谁")
    for truth in model_ids:
        rest = [m for m in model_ids if m != truth]
        own = scores[truth]
        wins: dict[str, int] = {}
        gaps = []
        for _ in range(200):
            pick = rng.choice(own.shape[0], size=3, replace=True)
            mean = own[pick].mean(axis=0)
            idx = [model_ids.index(m) for m in rest]
            sub = mean[idx]
            order = np.argsort(sub)[::-1]
            pred = rest[int(order[0])]
            wins[pred] = wins.get(pred, 0) + 1
            gaps.append(float(sub[order[0]] - sub[order[1]]))
        top3 = sorted(wins.items(), key=lambda kv: -kv[1])[:3]
        top3_str = "、".join(f"{m}:{n/2:.0f}%" for m, n in top3)
        print(f"  {truth:<32} -> {top3_str}   (top1-top2 分差 p5={np.percentile(gaps,5):.3f} 中位={np.median(gaps):.3f})")


if __name__ == "__main__":
    main()
