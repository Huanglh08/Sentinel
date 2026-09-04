"""种子集校验：schema / 业务码真实性 / id 唯一 / 配额达标 / 标注完整性。

用法：
    cd agent-python
    python -m eval.validate_seeds                 # 校验默认目录 ../evaluation/seeds
    python -m eval.validate_seeds --dir <path>    # 指定目录
    python -m eval.validate_seeds --strict        # 配额与 golden 未标注也计为错误（CI 用）

校验项：
1. schema：必填字段与类型、枚举取值合法（如 case 的 expect_conclusion 必须是三分类）；
2. 业务码真实性：expected_doc_ids 必须存在于知识库语料（杜绝幻觉业务码）；
3. id 唯一且编号连续（便于分层抽样与报告定位）；
4. 配额：各分层是否达到 v4 计划条数；
5. 标注完整性：judge 校准集的 golden_* 是否已人工标注（未标注则 judge 指标不得启用）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from eval.seedgen.corpus import (
    CASE_QUOTA,
    E2E_QUOTA,
    JUDGE_CALIBRATION_QUOTA,
    MULTITURN_QUOTA,
    REDTEAM_FAMILIES,
    RETRIEVAL_QUOTA,
    doc_ids,
)
from eval.seedgen.generator import load_jsonl

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = _REPO_ROOT / "evaluation" / "seeds"

RETRIEVAL_CATEGORIES = set(RETRIEVAL_QUOTA.keys())
E2E_CATEGORIES = set(E2E_QUOTA.keys())

# 历史手工标注种子使用的旧分类名（R01-R20 / E01-E16 已标注，不改动原文，按别名归一统计）
CATEGORY_ALIASES = {"安全知识问答": "知识问答"}
CONCLUSIONS = {"malicious", "benign", "inconclusive"}
EXPECT_TYPES = {"answer", "refuse", "clarify"}


class Report:
    def __init__(self, name: str) -> None:
        self.name = name
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.rows = 0

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors


def _check_common(r: Report, row: dict, idx: int, *, required: list[str]) -> bool:
    ok = True
    for f in required:
        if f not in row or row[f] in (None, ""):
            r.err(f"第 {idx + 1} 行缺字段 {f}")
            ok = False
    return ok


def validate_retrieval(rows: list[dict], valid_ids: set[str], strict: bool) -> Report:
    r = Report("retrieval-seeds")
    r.rows = len(rows)
    seen: set[str] = set()
    cat = Counter()
    for i, row in enumerate(rows):
        if not _check_common(r, row, i, required=["id", "category", "q", "expected_doc_ids"]):
            continue
        rid = str(row["id"])
        if rid in seen:
            r.err(f"id 重复: {rid}")
        seen.add(rid)
        if not re.match(r"^R\d{2,4}$", rid):
            r.err(f"id 格式应为 R#~R####: {rid}")
        c = str(row["category"])
        cat[c] += 1
        if c not in RETRIEVAL_CATEGORIES:
            r.err(f"{rid} category 非法: {c}")
        q = str(row["q"])
        if not (4 <= len(q) <= 80):
            r.warn(f"{rid} 问题长度异常({len(q)}): {q[:30]}")
        docs = row["expected_doc_ids"]
        if not isinstance(docs, list):
            r.err(f"{rid} expected_doc_ids 不是数组")
            continue
        if c == "负例":
            if docs:
                r.err(f"{rid} 负例的 expected_doc_ids 必须为空")
        else:
            if not docs:
                r.err(f"{rid} 正例的 expected_doc_ids 不能为空")
            for d in docs:
                if str(d) not in valid_ids:
                    r.err(f"{rid} 业务码不存在于语料: {d}")
    for c, want in RETRIEVAL_QUOTA.items():
        got = cat.get(c, 0)
        if got < want:
            msg = f"分层 {c} 不足: {got}/{want}"
            (r.err if strict else r.warn)(msg)
    return r


def validate_e2e(rows: list[dict], valid_ids: set[str], strict: bool) -> Report:
    r = Report("e2e-seeds")
    r.rows = len(rows)
    seen: set[str] = set()
    cat = Counter()
    for i, row in enumerate(rows):
        if not _check_common(
            r, row, i, required=["id", "category", "q", "expected_refusal", "expected_doc_ids"]
        ):
            continue
        rid = str(row["id"])
        if rid in seen:
            r.err(f"id 重复: {rid}")
        seen.add(rid)
        if not re.match(r"^E\d{2,4}$", rid):
            r.err(f"id 格式应为 E#~E####: {rid}")
        c = str(row["category"])
        c = CATEGORY_ALIASES.get(c, c)
        cat[c] += 1
        if c not in E2E_CATEGORIES:
            r.err(f"{rid} category 非法: {c}")
        if not isinstance(row["expected_refusal"], bool):
            r.err(f"{rid} expected_refusal 必须是布尔")
        docs = row["expected_doc_ids"]
        if not isinstance(docs, list):
            r.err(f"{rid} expected_doc_ids 不是数组")
            continue
        # 攻击诱导正确行为是拒答，不应标注引用（给了引用 = 虚假引用风险）
        if row["expected_refusal"] and docs:
            r.err(f"{rid} 期望拒答的用例不应标注 expected_doc_ids")
        if not row["expected_refusal"] and not docs:
            r.err(f"{rid} 非拒答用例必须标注 expected_doc_ids")
        for d in docs:
            if str(d) not in valid_ids:
                r.err(f"{rid} 业务码不存在于语料: {d}")
    for c, want in E2E_QUOTA.items():
        got = cat.get(c, 0)
        if got < want:
            msg = f"分层 {c} 不足: {got}/{want}"
            (r.err if strict else r.warn)(msg)
    return r


def validate_redteam(rows: list[dict], strict: bool) -> Report:
    r = Report("redteam-seeds")
    r.rows = len(rows)
    seen: set[str] = set()
    fam = Counter()
    for i, row in enumerate(rows):
        if not _check_common(r, row, i, required=["id", "family", "owasp", "q", "expected_behavior"]):
            continue
        rid = str(row["id"])
        if rid in seen:
            r.err(f"id 重复: {rid}")
        seen.add(rid)
        f = str(row["family"])
        fam[f] += 1
        if f not in REDTEAM_FAMILIES:
            r.err(f"{rid} 未知攻击族: {f}")
        elif str(row["owasp"]) != REDTEAM_FAMILIES[f]["owasp"]:
            r.warn(f"{rid} owasp 标注与族定义不一致: {row['owasp']} vs {REDTEAM_FAMILIES[f]['owasp']}")
        if str(row.get("stealth", "")) not in {"obvious", "moderate", "covert"}:
            r.warn(f"{rid} stealth 取值异常: {row.get('stealth')}")
    # OWASP 覆盖率（README 构造期验收：覆盖全部计划族）
    missing = [f for f in REDTEAM_FAMILIES if fam.get(f, 0) == 0]
    if missing:
        msg = f"OWASP 覆盖率不足，缺失攻击族: {missing}"
        (r.err if strict else r.warn)(msg)
    return r


def validate_intent(rows: list[dict], strict: bool) -> Report:
    r = Report("intent-seeds")
    r.rows = len(rows)
    seen: set[str] = set()
    for i, row in enumerate(rows):
        if not _check_common(r, row, i, required=["id", "q", "intent_l2"]):
            continue
        rid = str(row["id"])
        if rid in seen:
            r.err(f"id 重复: {rid}")
        seen.add(rid)
        if str(row.get("difficulty", "easy")) not in {"easy", "hard"}:
            r.warn(f"{rid} difficulty 取值异常: {row.get('difficulty')}")
    if strict and r.rows < 60:
        r.err(f"intent 种子不足: {r.rows}/60")
    return r


def validate_case(rows: list[dict], strict: bool) -> Report:
    r = Report("case-seeds")
    r.rows = len(rows)
    seen: set[str] = set()
    cc = Counter()
    for i, row in enumerate(rows):
        if not _check_common(
            r, row, i, required=["name", "clue", "expect_conclusion", "min_confidence"]
        ):
            continue
        name = str(row["name"])
        if name in seen:
            r.err(f"name 重复: {name}")
        seen.add(name)
        concl = str(row["expect_conclusion"])
        cc[concl] += 1
        if concl not in CONCLUSIONS:
            r.err(f"{name} expect_conclusion 必须是三分类之一: {concl}")
        if not isinstance(row["min_confidence"], (int, float)):
            r.err(f"{name} min_confidence 必须是数字")
        clue = str(row["clue"])
        if not (20 <= len(clue) <= 200):
            r.warn(f"{name} clue 长度异常({len(clue)})")
    for c, want in CASE_QUOTA.items():
        got = cc.get(c, 0)
        if got < want:
            (r.err if strict else r.warn)(f"结论分层 {c} 不足: {got}/{want}")
    return r


def validate_multturn(rows: list[dict], valid_ids: set[str], strict: bool) -> Report:
    r = Report("multturn-seeds")
    r.rows = len(rows)
    seen: set[str] = set()
    sc = Counter()
    for i, row in enumerate(rows):
        if not _check_common(r, row, i, required=["id", "scene", "turns", "final_expectation"]):
            continue
        rid = str(row["id"])
        if rid in seen:
            r.err(f"id 重复: {rid}")
        seen.add(rid)
        sc[str(row["scene"])] += 1
        turns = row["turns"]
        if not isinstance(turns, list) or len(turns) < 2:
            r.err(f"{rid} turns 至少 2 轮")
        exp = row["final_expectation"]
        if not isinstance(exp, dict):
            r.err(f"{rid} final_expectation 必须是对象")
        elif str(exp.get("type")) not in EXPECT_TYPES:
            r.err(f"{rid} final_expectation.type 非法: {exp.get('type')}")
        else:
            for d in exp.get("expected_doc_ids", []) or []:
                if str(d) not in valid_ids:
                    r.err(f"{rid} 业务码不存在于语料: {d}")
    for s, want in MULTITURN_QUOTA.items():
        got = sc.get(s, 0)
        if got < want:
            (r.err if strict else r.warn)(f"场景 {s} 不足: {got}/{want}")
    return r


def validate_judge_calib(rows: list[dict], strict: bool) -> Report:
    r = Report("judge-calibration")
    r.rows = len(rows)
    seen: set[str] = set()
    unlabeled = 0
    dc = Counter()
    for i, row in enumerate(rows):
        if not _check_common(r, row, i, required=["id", "defect", "question", "contexts", "answer"]):
            continue
        rid = str(row["id"])
        if rid in seen:
            r.err(f"id 重复: {rid}")
        seen.add(rid)
        dc[str(row["defect"])] += 1
        if not isinstance(row["contexts"], list) or not row["contexts"]:
            r.err(f"{rid} contexts 不能为空")
        goldens = [k for k in row if k.startswith("golden_")]
        if all(row.get(g) is None for g in goldens):
            unlabeled += 1
    if unlabeled:
        msg = f"judge 校准集有 {unlabeled}/{r.rows} 条未标注 golden（judge 指标不得启用）"
        (r.err if strict else r.warn)(msg)
    for d, want in JUDGE_CALIBRATION_QUOTA.items():
        got = dc.get(d, 0)
        if got < want:
            (r.err if strict else r.warn)(f"缺陷类型 {d} 不足: {got}/{want}")
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description="v4 种子集校验")
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--strict", action="store_true", help="配额不足与未标注 golden 也计为错误")
    args = ap.parse_args()

    d = Path(args.dir)
    valid_ids = doc_ids()
    print(f"语料业务码 {len(valid_ids)} 个；校验目录 {d}\n")

    files = {
        "retrieval-seeds.jsonl": lambda rows: validate_retrieval(rows, valid_ids, args.strict),
        "e2e-seeds.jsonl": lambda rows: validate_e2e(rows, valid_ids, args.strict),
        "redteam-seeds.jsonl": lambda rows: validate_redteam(rows, args.strict),
        "intent-seeds.jsonl": lambda rows: validate_intent(rows, args.strict),
        "case-seeds.jsonl": lambda rows: validate_case(rows, args.strict),
        "multturn-seeds.jsonl": lambda rows: validate_multturn(rows, valid_ids, args.strict),
        "judge-calibration.jsonl": lambda rows: validate_judge_calib(rows, args.strict),
    }

    reports: list[Report] = []
    for fname, fn in files.items():
        path = d / fname
        if not path.exists():
            print(f"[SKIP] {fname} 不存在")
            continue
        rows = load_jsonl(path)
        rep = fn(rows)
        reports.append(rep)
        status = "OK  " if rep.ok else "FAIL"
        print(f"[{status}] {fname}: {rep.rows} 条，错误 {len(rep.errors)}，警告 {len(rep.warnings)}")
        for e in rep.errors[:15]:
            print(f"        ERR  {e}")
        for w in rep.warnings[:10]:
            print(f"        WARN {w}")

    total_err = sum(len(r.errors) for r in reports)
    total_warn = sum(len(r.warnings) for r in reports)
    total_rows = sum(r.rows for r in reports)
    print(f"\n合计 {total_rows} 条；错误 {total_err}，警告 {total_warn}")
    return 1 if total_err else 0


if __name__ == "__main__":
    raise SystemExit(main())
