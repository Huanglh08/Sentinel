"""judge 校准集 golden 预标注。

校准集每条样本自带 defect 标签（设计时的正/负例意图），
据此在对应维度填入极值锚点分（0.0 / 1.0），用于校准 judge：
正例应得高分、负例应得低分，偏差大说明 rubric 或 judge 有问题。

规则（保守）：
- 只填与 defect 对应的那一个维度，其余维度留给人工按需补充；
- 已有任何非 null golden 的条目一律跳过（不覆盖人工标注）；
- defect 不在映射表里的条目报错退出（避免静默漏标）。

用法：
    cd agent-python
    python -m eval.label_judge_calib
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SEEDS = Path(__file__).resolve().parents[2] / "evaluation" / "seeds" / "judge-calibration.jsonl"

# defect → (golden 字段, 预期锚点分)
LABELS: dict[str, tuple[str, float]] = {
    "faithfulness_负例（含幻觉）": ("golden_faithfulness", 0.0),
    "faithfulness_正例": ("golden_faithfulness", 1.0),
    "citation_precision_负例（挂羊头）": ("golden_citation_precision", 0.0),
    "citation_coverage_负例（无引用结论）": ("golden_citation_coverage", 0.0),
    "answer_relevancy_负例（答非所问/被劫持）": ("golden_answer_relevancy", 0.0),
}


def main() -> int:
    rows = [
        json.loads(line)
        for line in SEEDS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    labeled = 0
    skipped = 0
    unknown: list[str] = []
    for row in rows:
        goldens = [k for k in row if k.startswith("golden_")]
        if any(row.get(g) is not None for g in goldens):
            skipped += 1
            continue
        label = LABELS.get(str(row.get("defect", "")))
        if label is None:
            unknown.append(str(row.get("id")))
            continue
        field, score = label
        row[field] = score
        labeled += 1
    SEEDS.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    print(f"标注 {labeled} 条；跳过已有人工标注 {skipped} 条；未知 defect {len(unknown)} 条 {unknown}")
    return 1 if unknown else 0


if __name__ == "__main__":
    sys.exit(main())
