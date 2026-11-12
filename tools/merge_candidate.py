# -*- coding: utf-8 -*-
"""洞真（dongzhen）候选指纹显式并入工具：enroll_cli.py 的候选文件 -> 参考数据 -> 重建统一库。
这是防污染的第二步，必须由人显式执行；绝不自动跑。
用法:
  cp data/unified_bank.json data/unified_bank.json.bak-$(date +%F-%H%M%S)   # 先备份！
  python tools/merge_candidate.py data/candidates/<label>-<时间戳>.json
行为:
  1) 读候选文件，校验 schema 与自匹配指标（自匹配未全中要 --force 才放行）
  2) 候选行 append 进族 jsonl（gpt/claude 用现文件；新族自动建 <family>_reference.jsonl）
  3) 全量重建 data/unified_bank.json（含新族，不再依赖 rebuild_unified_bank.py 的写死 SOURCES）
  4) 打印重建摘要与验证提示
"""
import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bank_builder import build_bank, read_rows
from fingerprint import count_numbers, parse_numbers

ROOT = Path(__file__).resolve().parents[1]
UNIFIED_BANK = ROOT / "data" / "unified_bank.json"
FAMILY_NAMES = {"gpt": "GPT", "claude": "Claude"}


def enrich_rows(rows: list[dict]) -> None:
    """候选行补 numbers/counts（make_row 采集时不带，建库需要）。"""
    for row in rows:
        row["numbers"] = parse_numbers(row["text"])
        row["counts"] = count_numbers(row["numbers"])


def family_jsonl(family: str) -> Path:
    if family in ("gpt", "claude"):
        return ROOT / "data" / f"{family}_reference.jsonl"
    return ROOT / "data" / f"{family}_reference.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser(description="洞真候选指纹显式并入（append jsonl + 重建统一库）")
    parser.add_argument("candidate", type=Path, help="enroll_cli.py 产出的候选文件")
    parser.add_argument("--force", action="store_true", help="自匹配未全中时仍强行并入（不建议）")
    parser.add_argument("--dry-run", action="store_true", help="只校验与预览，不写任何文件")
    args = parser.parse_args()

    payload = json.loads(args.candidate.read_text(encoding="utf-8"))
    if payload.get("schema") != "dongzhen-enrollment-candidate":
        sys.exit(f"错误：{args.candidate} 不是洞真候选文件（schema 不符）")
    label = payload["label"]
    family = payload["family"]
    rows = payload["rows"]
    if len(rows) < 12:
        sys.exit(f"错误：候选仅 {len(rows)} 条（<12），不足以入库；请满配 36 条重采")

    # 自匹配指标守门
    self_match = payload.get("self_match", {})
    hits, total = self_match.get("hits", 0), self_match.get("total", 0)
    if total and hits < total and not args.force:
        sys.exit(
            f"错误：候选自匹配 {hits}/{total} 未全中（与库内模型重叠）。"
            "先人工研判 docs/enrollment.md §7；确要并入加 --force（不建议）"
        )

    target = family_jsonl(family)
    family_name = FAMILY_NAMES.get(family, family)
    enrich_rows(rows)
    tagged = [{**row, "family_id": family, "family_name": family_name} for row in rows]
    print(f"候选 {label}（族 {family}，{len(tagged)} 条）-> {target}")

    # 全量数据 = 现有三族参考数据 + 本候选
    all_rows = []
    for fam in ("gpt", "claude"):
        path = ROOT / "data" / f"{fam}_reference.jsonl"
        if path.exists():
            all_rows.extend({**row, "family_id": fam, "family_name": FAMILY_NAMES[fam]} for row in read_rows(path))
    if family not in ("gpt", "claude") and target.exists():
        all_rows.extend({**row, "family_id": family, "family_name": family_name} for row in read_rows(target))
    all_rows.extend(tagged)
    print(f"重建输入合计 {len(all_rows)} 条（含本候选 {len(tagged)} 条）")

    if args.dry_run:
        print("dry-run：未写任何文件。")
        return

    if not UNIFIED_BANK.exists():
        sys.exit("错误：找不到 data/unified_bank.json，拒绝在无备份的情况下写入")
    backup = UNIFIED_BANK.with_name(f"unified_bank.json.bak-merge")
    backup.write_text(UNIFIED_BANK.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"已留滚动备份：{backup.name}")

    # append 候选行（jsonl 追加，一行一条）
    with target.open("a", encoding="utf-8") as handle:
        for row in tagged:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    print("正在全量重建 unified_bank.json（约需十几秒）...")
    bank = build_bank(all_rows)
    UNIFIED_BANK.write_text(json.dumps(bank, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(UNIFIED_BANK),
                "models": len(bank["models"]),
                "responses": sum(m["response_count"] for m in bank["models"]),
                "calibration_3": bank["calibration"].get("3"),
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    print("\n并入完成。请立刻跑回归验证：")
    print("  python tools/confusion_analysis.py        # 新模型不应出现在混淆对 TOP，自举正确率应 100%")
    print("  python tools/confusion_analysis.py data/unified_bank.json.bak-merge   # 与旧库对比")
    print("  docs/enrollment.md §6 的 checklist 逐项过一遍")


if __name__ == "__main__":
    # 仅脚本直跑时重包 stdout 为 UTF-8；import 时不动
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
