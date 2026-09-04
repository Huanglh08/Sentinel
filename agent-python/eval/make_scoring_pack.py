# -*- coding: utf-8 -*-
"""把 golden jsonl 转成自包含人工/AI 打分包 markdown（盲评版：不含 judge 分）。

用法（agent-python 目录下）：
  python -m eval.make_scoring_pack            # 默认读 evaluation/seeds/judge-calibration.jsonl
  python -m eval.make_scoring_pack --golden <path>

产出：evaluation/reports/scoring-pack-<ts>.md
打分者只需该文件即可工作：question / answer / 参考片段全部内嵌，
不依赖知识库与系统访问权限。参考片段 = 判 faithfulness 的唯一事实依据。

自检：产出条数必须等于 golden 条数（assert）。
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLDEN = REPO_ROOT / "evaluation" / "seeds" / "judge-calibration.jsonl"
REPORT_DIR = REPO_ROOT / "evaluation" / "reports"

HEADER = """# Sentinel 评测打分包（盲评）

你将看到若干条问答记录。每条含三部分：**问题**、**回答**（被测系统输出）、**参考片段**（系统检索到的知识库文档节选）。

请对每条独立打两个分（0~1 小数，可到两位）：

| 指标 | 判断内容 | 口径 |
|---|---|---|
| `rel` | 回答是否回应了问题的字面诉求 | 完整回应=1.0；答非所问=0.0；部分回应/夹带冗余按比例给中间值 |
| `faith` | 回答中的事实断言有多大比例被**参考片段**支撑 | 逐条数断言：被支撑的条数 / 断言总条数。常识性推论（由片段可直接推出）算支撑；片段里没有的数字、结论、细节一律算无支撑 |

注意：
- 只依据文件内信息判断，**参考片段是唯一事实依据**，不要用你自己知道的知识补判。
- 拒答/答非所问类回答没有事实断言时，faith 按「无断言即无虚构成分」给 1.0（rel 另行判断）。
- 在每条末尾的打分行填写分数与可选备注，格式保持不变：
  - `rel:` / `faith:` 后直接写数字
- 请先通读一条的参考片段再读回答，避免被回答带偏。

---
"""


def _fmt_ctx(contexts: list[dict]) -> str:
    lines = []
    for i, c in enumerate(contexts, 1):
        doc = c.get("doc_id") or f"chunk_{i}"
        text = str(c.get("text", "")).strip()
        lines.append(f"[{i}] `{doc}`\n\n> " + text.replace("\n", "\n> "))
    return "\n\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="golden jsonl -> 自包含打分包 markdown")
    parser.add_argument("--golden", default=str(DEFAULT_GOLDEN))
    args = parser.parse_args()

    golden_path = Path(args.golden)
    records = [
        json.loads(line)
        for line in golden_path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    assert records, f"golden 为空: {golden_path}"

    md = [HEADER]
    for g in records:
        md.append(f"## {g['id']}\n")
        md.append(f"### 问题\n\n{g['question']}\n")
        md.append(f"### 回答（被测系统输出）\n\n{g['answer']}\n")
        ctxs = g.get("contexts") or []
        assert ctxs, f"{g['id']} 无 contexts，打分者无法判 faith"
        md.append(f"### 参考片段（{len(ctxs)} 段，判 faith 的唯一依据）\n\n{_fmt_ctx(ctxs)}\n")
        md.append("### 打分\n")
        md.append(f"- {g['id']} rel: \n- {g['id']} faith: \n- {g['id']} 备注: \n")
        md.append("---\n")

    ts = time.strftime("%Y%m%d-%H%M%S")
    out = REPORT_DIR / f"scoring-pack-{ts}.md"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md), encoding="utf-8")

    # 自检：文件内条目数与 golden 对齐
    body = out.read_text(encoding="utf-8")
    n_marks = sum(1 for g in records if f"- {g['id']} rel: " in body)
    assert n_marks == len(records), f"打分行数 {n_marks} != golden {len(records)}"
    print(f"[make_scoring_pack] {len(records)} 条 -> {out} ({out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
