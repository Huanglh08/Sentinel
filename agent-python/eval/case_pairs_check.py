"""成对边界集断言（只读 case_eval 报告）。

主断言是**分流方向**：benign = 可自主结案（dismiss），其余 = 应转人工（escalate）。
成对的两条样本只差一个决定性因素，方向必须按声明翻转；同质对方向必须一致。
结论精确值只作提示，不作为失败条件（同一方向的 suspicious/malicious 都算升级）。

跑法: python -m eval.case_pairs_check eval/reports/pairs-*.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

DISMISS = "benign"


def direction(conclusion: str) -> str:
    return "dismiss" if conclusion == DISMISS else "escalate"


def load_pairs(path: Path) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            groups.setdefault(row["pair_id"], []).append(row)
    return groups


def main() -> None:
    report_path = sys.argv[1] if len(sys.argv) > 1 else ""
    if not report_path:
        print("usage: python -m eval.case_pairs_check <report.json> [pairs.jsonl]")
        raise SystemExit(2)
    pairs_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parents[2] / "evaluation" / "seeds" / "case-pairs.jsonl"
    groups = load_pairs(pairs_path)
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    got = {c["name"]: c for c in report.get("cases", [])}

    fails: list[str] = []
    print(f"{'pair':6s} {'name':44s} {'expect':12s} {'got':12s} {'dir':10s} ok")
    for pid, rows in sorted(groups.items()):
        for row in rows:
            case = got.get(row["name"])
            if not case:
                fails.append(f"{pid}/{row['name']}: missing in report")
                print(f"{pid:6s} {row['name']:44s} {row['pair_expect']:12s} {'-':12s} {'-':10s} MISS")
                continue
            want = direction(row["pair_expect"])
            have = direction(case["predicted_conclusion"])
            ok = want == have
            if not ok:
                fails.append(f"{pid}/{row['name']}: {row['pair_expect']} -> {case['predicted_conclusion']}")
            print(
                f"{pid:6s} {row['name']:44s} {row['pair_expect']:12s} "
                f"{case['predicted_conclusion']:12s} {have:10s} {'OK' if ok else 'FAIL'}",
            )

    total = sum(len(v) for v in groups.values())
    print(f"\n{total - len(fails)}/{total} pairs match direction")
    if fails:
        print("FAILED:")
        for f in fails:
            print("  -", f)
        raise SystemExit(1)
    print("all pair directions passed")


if __name__ == "__main__":
    main()
