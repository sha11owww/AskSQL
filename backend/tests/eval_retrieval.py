"""检索层离线评测：算 Recall / All-Hit / MRR / 误召率。

用法（在 backend 目录下执行）：
    ./.venv/Scripts/python.exe -m tests.eval_retrieval --validate
    ./.venv/Scripts/python.exe -m tests.eval_retrieval --threshold 0.55
    ./.venv/Scripts/python.exe -m tests.eval_retrieval --thresholds 0.45,0.55,0.65

--validate 只校验数据集本身（不联网）；其余模式需要 LLM_API_KEY。
注意：首次运行若索引签名变化，会触发全量重建 embedding（422 个字段，约 40 次请求）。
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
LEGACY_PATH = ROOT / "data" / "eval" / "retrieval_eval.jsonl"
EVAL_PATH = ROOT / "data" / "eval" / "benchmark_120.jsonl"
STORE_PATH = ROOT / "data" / "schema_store.json"


def load_cases(path: Path = EVAL_PATH) -> list[dict]:
    cases: list[dict] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path.name} 第 {line_no} 行不是合法 JSON：{exc}")
    return cases


def known_doc_ids() -> set[str]:
    payload = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    return {item["doc_id"] for item in payload["documents"]}


def validate(cases: list[dict]) -> list[str]:
    known = known_doc_ids()
    problems: list[str] = []
    seen: set[str] = set()
    for case in cases:
        cid = case.get("id", "<无编号>")
        if cid in seen:
            problems.append(f"{cid} 编号重复")
        seen.add(cid)
        slots = case.get("required_slots") or []
        expect = case.get("expect", "answer")
        if expect == "answer" and not slots:
            problems.append(f"{cid} 缺少 required_slots")
        if expect != "answer" and slots:
            problems.append(f"{cid} expect={expect} 不应声明 required_slots")
        for slot in slots:
            if not slot.get("any_of"):
                problems.append(f"{cid} 槽位「{slot.get('name')}」没有候选字段")
            for doc_id in slot.get("any_of") or []:
                if doc_id not in known:
                    problems.append(f"{cid} 引用不存在的字段 {doc_id}")
        for doc_id in case.get("forbidden") or []:
            if doc_id not in known:
                problems.append(f"{cid} forbidden 引用不存在的字段 {doc_id}")
    return problems


def score(case: dict, ranked: list[str]) -> dict:
    """按「槽位」判分：每个必需槽位命中任一候选字段即算召回。"""
    ranks: list[int] = []
    missing: list[str] = []
    for slot in case["required_slots"]:
        position = next(
            (i + 1 for i, doc_id in enumerate(ranked) if doc_id in slot["any_of"]),
            None,
        )
        if position is None:
            missing.append(slot["name"])
        else:
            ranks.append(position)

    forbidden = case.get("forbidden") or []
    harmful = [doc_id for doc_id in ranked if doc_id in forbidden]
    total = len(case["required_slots"])
    return {
        "recall": len(ranks) / total,
        "all_hit": 0.0 if missing else 1.0,
        "mrr": 1.0 / min(ranks) if ranks else 0.0,
        "harmful": 1.0 if harmful else 0.0,
        "missing": missing,
        "harmful_fields": harmful,
        "ranked": ranked,
        "slots_hit": len(ranks),
        "slots_total": total,
    }


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "recall": 0.0, "all_hit": 0.0, "mrr": 0.0, "harmful": 0.0,
                "micro": 0.0, "slots_hit": 0, "slots_total": 0}
    hit = sum(r["slots_hit"] for r in rows)
    total = sum(r["slots_total"] for r in rows)
    return {
        "n": len(rows),
        "recall": mean(r["recall"] for r in rows),
        "all_hit": mean(r["all_hit"] for r in rows),
        "mrr": mean(r["mrr"] for r in rows),
        "harmful": mean(r["harmful"] for r in rows),
        "micro": hit / total if total else 0.0,
        "slots_hit": hit,
        "slots_total": total,
    }


def format_summary(label: str, s: dict) -> str:
    return (
        f"{label:<24} n={s['n']:<3} "
        f"Micro={s['micro']:.3f}({s['slots_hit']}/{s['slots_total']})  "
        f"Macro={s['recall']:.3f}  AllHit={s['all_hit']:.3f}  "
        f"MRR={s['mrr']:.3f}  误召={s['harmful']:.3f}"
    )


def evaluate(threshold: float, cases: list[dict], index) -> list[dict]:
    rows: list[dict] = []
    for case in cases:
        if case.get("expect", "answer") != "answer":
            continue  # 负样本不参与检索评测，由护栏层单独测
        result = index.retrieve(case["query"], threshold=threshold)
        ranked = [hit["doc_id"] for hit in result.get("hits") or []]
        rows.append({"case": case, **score(case, ranked)})
    return rows


def report(rows: list[dict], threshold: float) -> None:
    print(f"\n=== 阈值 {threshold} ===")
    print(format_summary("总体", summarize(rows)))

    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        case = row["case"]
        groups[case.get("layer") or case.get("category", "unknown")].append(row)
    print("\n按分层：")
    for layer, items in sorted(groups.items()):
        print("  " + format_summary(layer, summarize(items)))

    by_difficulty: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_difficulty[row["case"].get("difficulty", "unknown")].append(row)
    print("\n按难度：")
    for difficulty, items in sorted(by_difficulty.items()):
        print("  " + format_summary(difficulty, summarize(items)))

    failures = [r for r in rows if r["missing"] or r["harmful_fields"]]
    print(f"\n有问题样本：{len(failures)} / {len(rows)}")
    for row in failures:
        case = row["case"]
        print(f"  [{case['id']}] {case['query']}")
        if row["missing"]:
            print(f"      未召回槽位：{'、'.join(row['missing'])}")
        if row["harmful_fields"]:
            print(f"      误召字段：{', '.join(row['harmful_fields'])}")
        print(f"      实际召回：{', '.join(row['ranked'][:8]) or '（空）'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="检索层离线评测")
    parser.add_argument("--validate", action="store_true", help="只校验数据集，不联网")
    parser.add_argument("--threshold", type=float, help="单个 rerank 阈值")
    parser.add_argument("--thresholds", help="多个阈值，逗号分隔，如 0.45,0.55,0.65")
    parser.add_argument("--eval-file", help="自定义评测集路径")
    parser.add_argument("--layer", help="只评测某一层，如 L4_multi_table")
    parser.add_argument("--legacy", action="store_true", help="使用旧的 28 条评测集")
    args = parser.parse_args()

    default_path = LEGACY_PATH if args.legacy else EVAL_PATH
    cases = load_cases(Path(args.eval_file) if args.eval_file else default_path)
    if args.layer:
        cases = [c for c in cases if c.get("layer") == args.layer]
    print(f"评测集：{len(cases)} 条")

    layers: dict[str, int] = {}
    for case in cases:
        key = case.get("layer") or case.get("category", "unknown")
        layers[key] = layers.get(key, 0) + 1
    if len(layers) > 1:
        print("  分层分布：" + "  ".join(f"{k}={v}" for k, v in sorted(layers.items())))
    expects: dict[str, int] = {}
    for case in cases:
        expects[case.get("expect", "answer")] = expects.get(case.get("expect", "answer"), 0) + 1
    print("  期望行为：" + "  ".join(f"{k}={v}" for k, v in sorted(expects.items())))

    problems = validate(cases)
    if problems:
        print(f"\n数据集校验失败（{len(problems)} 项）：")
        for item in problems:
            print("  " + item)
        raise SystemExit(1)
    print("数据集校验通过：所有字段引用均存在")
    if args.validate:
        return

    from app.config import settings
    from app.model_client import ModelClient
    from app.retrieval.service import SchemaIndex

    if not settings.api_key:
        raise SystemExit(
            "未配置 LLM_API_KEY：检索需要在线算查询向量与 rerank 分数。"
            "可先用 --validate 只校验数据集。"
        )

    if args.thresholds:
        thresholds = [float(x) for x in args.thresholds.split(",") if x.strip()]
    elif args.threshold is not None:
        thresholds = [args.threshold]
    else:
        thresholds = [settings.schema_recall_threshold]

    index = SchemaIndex(ModelClient(settings), settings)
    index.ensure_built()

    table: list[tuple[float, dict, list[dict]]] = []
    for threshold in thresholds:
        rows = evaluate(threshold, cases, index)
        report(rows, threshold)
        table.append((threshold, summarize(rows), rows))

    if len(table) > 1:
        print("\n=== 阈值对比 ===")
        print(f"{'阈值':<8}{'Recall':>9}{'AllHit':>9}{'MRR':>8}{'误召':>8}{'平均召回数':>11}")
        for threshold, s, rows in table:
            avg_selected = mean(len(r["ranked"]) for r in rows)
            print(
                f"{threshold:<8.2f}{s['recall']:>9.3f}{s['all_hit']:>9.3f}"
                f"{s['mrr']:>8.3f}{s['harmful']:>8.3f}{avg_selected:>11.1f}"
            )


if __name__ == "__main__":
    main()
