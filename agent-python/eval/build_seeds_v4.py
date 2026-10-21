"""v4 种子集扩量生成器。

计划总量（evaluation/README.md v4）：
    retrieval 120 / e2e 100 / redteam 80 / intent 60 / case 40 / multturn 30 / judge 校准 20

运行示例：
    cd agent-python
    python -m eval.build_seeds_v4 --task retrieval --append
    python -m eval.build_seeds_v4 --task redteam
    python -m eval.build_seeds_v4 --task all --append --model qwen-plus
    python -m eval.build_seeds_v4 --task retrieval --dry-run   # 只看计划，不调 LLM

说明：
- `--append` 用于保留已有的手工标注种子（retrieval 的 R01-R20、e2e 的 E01-E16），
  新记录从现有最大编号 +1 开始编号。
- intent 任务的意图清单默认从 `backend-java/resources/database/seed_intent_node.sql`
  解析真实的叶子意图（level=1），确保 intent_l2 真值与系统输出可比对；
  也可用 `--intents <json>` 指定自定义清单（例如风控化重建后的意图树）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from eval.seedgen import generator as G
from eval.seedgen.corpus import (
    CASE_QUOTA,
    E2E_QUOTA,
    JUDGE_CALIBRATION_QUOTA,
    MULTITURN_QUOTA,
    REDTEAM_FAMILIES,
    REDTEAM_PER_FAMILY,
    RETRIEVAL_QUOTA,
    load_corpus,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = _REPO_ROOT / "evaluation" / "seeds"
DEFAULT_INTENT_SQL = (
    _REPO_ROOT / "backend-java" / "resources" / "database" / "seed_intent_node.sql"
)

# case 种子的业务对象类别
CASE_CATEGORIES = ["account", "order", "payment", "device", "merchant"]

# 历史手工标注种子使用的旧分类名 → v4 分类名（与校验脚本保持一致）
CATEGORY_ALIASES = {"安全知识问答": "知识问答"}

# intent 条数：按叶子数 × 2 计算（36 叶子 → 72 条；每个叶子至少 2 条，
# 否则 Top-1/Top-3 统计没有意义）。可用 --intent-total 覆盖。
INTENT_TOTAL = 72


# ===========================================================================
# 意图清单解析
# ===========================================================================

_ROW_RE = re.compile(
    r"\(\s*\d+,\s*NULL,\s*'(?P<code>[a-z0-9\-_]+)',\s*'(?P<name>[^']*)',\s*"
    r"(?P<level>\d+),\s*(?P<parent>NULL|'[a-z0-9\-_]*'),\s*"
    r"(?P<desc>NULL|'(?:[^']|'')*'|\"[^\"]*\"),\s*(?P<examples>'(?:[^']|'')*'|\"[^\"]*\")",
    re.S,
)


def _clean(v: str | None) -> str:
    if not v or v == "NULL":
        return ""
    v = v.strip()
    if v.startswith("'") and v.endswith("'"):
        v = v[1:-1].replace("''", "'")
    elif v.startswith('"') and v.endswith('"'):
        v = v[1:-1]
    return v


def load_intents_from_sql(path: Path) -> list[dict]:
    """从 seed_intent_node.sql 解析叶子意图（level>=1），作为 intent_l2 真值域。"""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    intents: list[dict] = []
    for m in _ROW_RE.finditer(text):
        level = int(m.group("level"))
        if level < 1:
            continue
        code = m.group("code")
        raw_examples = _clean(m.group("examples"))
        try:
            examples = json.loads(raw_examples) if raw_examples else []
        except json.JSONDecodeError:
            examples = []
        intents.append(
            {
                "intent_code": code,
                "name": _clean(m.group("name")),
                "level": level,
                "parent_code": _clean(m.group("parent")),
                "description": _clean(m.group("desc"))[:200],
                "examples": examples if isinstance(examples, list) else [],
            }
        )
    return intents


# ===========================================================================
# 各任务执行
# ===========================================================================


def _next_start(path: Path, prefix: str, default: int = 0) -> int:
    """追加模式下，从已有记录的最大编号 +1 开始。"""
    existing = G.load_jsonl(path)
    max_n = default
    for row in existing:
        rid = str(row.get("id", ""))
        m = re.match(rf"^{prefix}(\d+)$", rid)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n


def _remaining_quota(path: Path, append: bool, quota: dict[str, int]) -> dict[str, int]:
    """追加模式下按分层计算"还差多少条"，而不是盲目再生成一整份。

    否则每跑一次就多出一整份数据（实测 retrieval 被加到 265 条，远超目标 120）。
    """
    if not append:
        return dict(quota)
    cat = Counter()
    for row in G.load_jsonl(path):
        c = str(row.get("category", ""))
        cat[CATEGORY_ALIASES.get(c, c)] += 1
    return {k: max(0, v - cat.get(k, 0)) for k, v in quota.items()}


def _existing_texts(path: Path, append: bool, *fields: str) -> list[str]:
    """追加模式下取出已有记录的文本，供生成器去重（避免与手工标注种子重复）。"""
    if not append:
        return []
    out: list[str] = []
    for row in G.load_jsonl(path):
        for f in fields:
            v = row.get(f)
            if isinstance(v, list):
                out.extend(str(x) for x in v)
            elif v:
                out.append(str(v))
    return out


def run_retrieval(llm: G.SeedLLM, out_dir: Path, append: bool) -> int:
    path = out_dir / "retrieval-seeds.jsonl"
    start = _next_start(path, "R") if append else 0
    docs = load_corpus()
    print(f"[retrieval] 目标 {sum(RETRIEVAL_QUOTA.values())} 条，起始编号 R{start + 1:03d}", flush=True)
    quota = _remaining_quota(path, append, RETRIEVAL_QUOTA)
    print(f"[retrieval] 待补 {sum(quota.values())} 条 {quota}", flush=True)
    rows = G.gen_retrieval(
        llm,
        docs,
        quota=quota,
        start_id=start,
        existing=_existing_texts(path, append, "q"),
    )
    _dedupe_by_id(rows)
    G.write_jsonl(path, rows, append=append)
    return len(rows)


def run_e2e(llm: G.SeedLLM, out_dir: Path, append: bool) -> int:
    path = out_dir / "e2e-seeds.jsonl"
    start = _next_start(path, "E") if append else 0
    docs = load_corpus()
    print(f"[e2e] 目标 {sum(E2E_QUOTA.values())} 条，起始编号 E{start + 1:03d}", flush=True)
    quota = _remaining_quota(path, append, E2E_QUOTA)
    print(f"[e2e] 待补 {sum(quota.values())} 条 {quota}", flush=True)
    rows = G.gen_e2e(
        llm,
        docs,
        quota=quota,
        start_id=start,
        attack_families=REDTEAM_FAMILIES,
        existing=_existing_texts(path, append, "q"),
    )
    _dedupe_by_id(rows)
    G.write_jsonl(path, rows, append=append)
    return len(rows)


def run_redteam(llm: G.SeedLLM, out_dir: Path, append: bool) -> int:
    path = out_dir / "redteam-seeds.jsonl"
    docs = load_corpus()
    total = len(REDTEAM_FAMILIES) * REDTEAM_PER_FAMILY
    # 追加保护：满额即跳过，否则每跑一次就多出一整份（且 id 会重复）
    already = len(G.load_jsonl(path)) if append else 0
    if already >= total:
        print(f"[redteam] 已达目标 {total} 条（现有 {already}），跳过", flush=True)
        return 0
    start = _next_start(path, "RT") if append else 0
    print(
        f"[redteam] 目标 {total} 条（{len(REDTEAM_FAMILIES)} 族 × {REDTEAM_PER_FAMILY}），"
        f"起始编号 RT{start + 1:03d}",
        flush=True,
    )
    rows = G.gen_redteam(
        llm,
        docs,
        per_family=REDTEAM_PER_FAMILY,
        families=REDTEAM_FAMILIES,
        # 与 e2e 的攻击诱导互不去重：两者用途不同，但要与已有 e2e 问题区分开
        existing=_existing_texts(path, append, "q")
        + _existing_texts(out_dir / "e2e-seeds.jsonl", True, "q"),
        start_id=start,
    )
    _dedupe_by_id(rows)
    G.write_jsonl(path, rows, append=append)
    return len(rows)


def run_intent(llm: G.SeedLLM, out_dir: Path, append: bool, intents: list[dict]) -> int:
    path = out_dir / "intent-seeds.jsonl"
    if not intents:
        print("[intent] 跳过：没有可用意图清单", flush=True)
        return 0
    already = len(G.load_jsonl(path)) if append else 0
    remaining = max(0, INTENT_TOTAL - already)
    if remaining == 0:
        print(f"[intent] 已达目标 {INTENT_TOTAL} 条，跳过", flush=True)
        return 0
    per = max(1, remaining // len(intents))
    print(
        f"[intent] 目标 {INTENT_TOTAL} 条（已有 {already}，待补 {remaining}），"
        f"{len(intents)} 个意图 × {per}",
        flush=True,
    )
    rows = G.gen_intent(
        llm,
        intents=intents,
        per_intent=per,
        existing=_existing_texts(path, append, "q"),
    )
    _dedupe_by_id(rows)
    G.write_jsonl(path, rows, append=append)
    return len(rows)


def run_case(llm: G.SeedLLM, out_dir: Path, append: bool) -> int:
    path = out_dir / "case-seeds.jsonl"
    print(f"[case] 目标 {sum(CASE_QUOTA.values())} 条（三分类）", flush=True)
    rows = G.gen_case(
        llm,
        quota=CASE_QUOTA,
        categories=CASE_CATEGORIES,
        existing=_existing_texts(path, append, "clue", "name"),
    )
    G.write_jsonl(path, rows, append=append)
    return len(rows)


def _load_done_specs(path: Path) -> set[str]:
    """从已有种子集读回已完成的规格键（断点续传用）。

    键由 coverage 标签还原，因此与生成计划里的规格键可直接比对——重启后
    已完成的规格会被跳过，不会重复烧 LLM 调用。
    """
    from eval.coverage import spec_from_row, spec_key

    done: set[str] = set()
    for row in G.load_jsonl(path):
        spec = spec_from_row(row)
        if spec:
            done.add(spec_key(spec))
    return done


def run_case_coverage(
    llm: G.SeedLLM,
    out_dir: Path,
    append: bool,
    *,
    per_bin: int = 2,
    per_spec: int = 1,
    with_boundary: bool = True,
    limit: int = 0,
) -> int:
    """覆盖率驱动的 case 种子生成（与 run_case 并行存在，产出同一文件）。

    与 run_case（按结论配额随机出题）的区别：这里逐规格指定每个维度的取值，
    产出带 `coverage` 标签，使 eval.coverage_report 能精确统计覆盖率并做收敛判定。
    结论分布齐但维度塌，正是 run_case 的短板。
    """
    from eval.coverage import (
        build_boundary_plan,
        build_coverage_plan,
        estimate_min_samples,
    )

    path = out_dir / "case-seeds.jsonl"
    cross_plan = build_coverage_plan(per_bin=per_bin)
    boundary_plan = build_boundary_plan() if with_boundary else []
    lo, detail = estimate_min_samples()
    print(
        f"[case_cov] 交叉规格 {len(cross_plan)} 条（理论下界 {lo}），"
        f"边界规格 {len(boundary_plan)} 条，每规格 {per_spec} 条",
        flush=True,
    )
    for k, v in detail.items():
        print(f"    {k:34s} need {v}", flush=True)

    plan = cross_plan + boundary_plan
    if limit > 0:
        plan = plan[:limit]
        print(f"[case_cov] --limit 生效，仅生成前 {len(plan)} 条规格", flush=True)

    start_id = _next_start(path, "CASE-", 0) if append else 0

    if append:
        done_specs = _load_done_specs(path)
        print(
            f"[case_cov] 断点续传：已完成 {len(done_specs)} 条规格将被跳过",
            flush=True,
        )
    else:
        done_specs = set()
        if path.exists():
            # 重建模式先清空：否则增量落盘会追加到上一轮的内容后面
            path.unlink()

    rows = G.gen_case_coverage(
        llm,
        plan=plan,
        per_spec=per_spec,
        start_id=start_id,
        existing=_existing_texts(path, append, "clue", "name"),
        flush_path=path,  # 每 10 条规格增量落盘，中断不丢已完成部分
        flush_every=10,
        done_specs=done_specs,
    )
    print(f"[case_cov] 完成，本次新增 {len(rows)} 条 -> {path}", flush=True)
    return len(rows)


def run_multturn(llm: G.SeedLLM, out_dir: Path, append: bool) -> int:
    path = out_dir / "multturn-seeds.jsonl"
    docs = load_corpus()
    print(f"[multturn] 目标 {sum(MULTITURN_QUOTA.values())} 条", flush=True)
    rows = G.gen_multturn(
        llm,
        docs,
        quota=MULTITURN_QUOTA,
        existing=_existing_texts(path, append, "turns"),
    )
    _dedupe_by_id(rows)
    G.write_jsonl(path, rows, append=append)
    return len(rows)


def run_judge_calib(llm: G.SeedLLM, out_dir: Path, append: bool) -> int:
    path = out_dir / "judge-calibration.jsonl"
    docs = load_corpus()
    print(f"[judge_calib] 目标 {sum(JUDGE_CALIBRATION_QUOTA.values())} 条（golden 需人工标注）", flush=True)
    rows = G.gen_judge_calib(
        llm,
        docs,
        quota=JUDGE_CALIBRATION_QUOTA,
        existing=_existing_texts(path, append, "question"),
    )
    _dedupe_by_id(rows)
    G.write_jsonl(path, rows, append=append)
    return len(rows)


def _dedupe_by_id(rows: list[dict]) -> None:
    """保证 id 唯一（生成器里多批合并可能出现重复编号）。"""
    seen: set[str] = set()
    for r in rows:
        rid = str(r.get("id", ""))
        if rid in seen:
            prefix = re.match(r"^([A-Za-z]+)", rid)
            prefix = prefix.group(1) if prefix else "X"
            i = len(seen) + 1
            while f"{prefix}{i:03d}" in seen:
                i += 1
            r["id"] = f"{prefix}{i:03d}"
        seen.add(str(r["id"]))


# ===========================================================================
# CLI
# ===========================================================================

TASKS = {
    "retrieval": run_retrieval,
    "e2e": run_e2e,
    "redteam": run_redteam,
    "intent": run_intent,
    "case": run_case,
    "case_cov": run_case_coverage,
    "multturn": run_multturn,
    "judge_calib": run_judge_calib,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="v4 种子集扩量生成器")
    parser.add_argument(
        "--task",
        default="all",
        choices=[*TASKS.keys(), "all"],
        help="要生成的数据集（默认 all）",
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT), help="输出目录")
    parser.add_argument("--append", action="store_true", help="追加到已有种子文件（保留手工标注）")
    parser.add_argument("--backend", default="dashscope", choices=["dashscope", "openai_compat"])
    parser.add_argument("--model", default=None, help="生成模型名")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--base-url", default=None, help="openai_compat 后端用")
    parser.add_argument(
        "--intents",
        default=None,
        help="intent 任务的意图清单 JSON 文件路径；默认从 seed_intent_node.sql 解析",
    )
    # case_cov（覆盖率驱动生成）专用
    parser.add_argument("--per-bin", type=int, default=2, help="case_cov：每个交叉 bin 的目标样本数")
    parser.add_argument("--per-spec", type=int, default=1, help="case_cov：每条规格生成几条变体")
    parser.add_argument("--no-boundary", action="store_true", help="case_cov：跳过阈值边界专项样本")
    parser.add_argument("--limit", type=int, default=0, help="case_cov：只生成前 N 条规格（冒烟用）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不调用 LLM")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    docs = load_corpus()
    plan = {
        "retrieval": sum(RETRIEVAL_QUOTA.values()),
        "e2e": sum(E2E_QUOTA.values()),
        "redteam": len(REDTEAM_FAMILIES) * REDTEAM_PER_FAMILY,
        "intent": INTENT_TOTAL,
        "case": sum(CASE_QUOTA.values()),
        "multturn": sum(MULTITURN_QUOTA.values()),
        "judge_calib": sum(JUDGE_CALIBRATION_QUOTA.values()),
    }
    print(f"语料：{len(docs)} 篇（业务码 {len({d.doc_id for d in docs})} 个）", flush=True)
    print(f"计划：{plan}  合计 {sum(plan.values())} 条", flush=True)
    print(f"输出：{out_dir}（append={args.append}）", flush=True)

    if args.dry_run:
        return 0

    llm = G.SeedLLM(
        backend=args.backend,
        model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
    )
    print(f"生成模型：{llm.model}（backend={llm.backend}）", flush=True)

    if args.intents:
        intents = json.loads(Path(args.intents).read_text(encoding="utf-8"))
    else:
        intents = load_intents_from_sql(DEFAULT_INTENT_SQL)

    todo = list(TASKS.keys()) if args.task == "all" else [args.task]
    total = 0
    for t in todo:
        print(f"\n=== {t} ===", flush=True)
        if t == "intent":
            n = TASKS[t](llm, out_dir, args.append, intents)
        elif t == "case_cov":
            n = TASKS[t](
                llm,
                out_dir,
                args.append,
                per_bin=args.per_bin,
                per_spec=args.per_spec,
                with_boundary=not args.no_boundary,
                limit=args.limit,
            )
        else:
            n = TASKS[t](llm, out_dir, args.append)
        print(f"--- {t}: 写入 {n} 条", flush=True)
        total += n

    print(
        f"\nDONE 共写入 {total} 条；LLM 调用 {llm.calls} 次，失败 {llm.failures} 次",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
