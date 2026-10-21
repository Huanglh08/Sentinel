"""种子生成引擎：LLM 调用 / JSON 容错解析 / 去重 / 落盘。

LLM 后端：
- `dashscope`（默认）：走项目已在用的百炼 SDK，模型默认 qwen-plus（可用 --model 覆盖）。
- `openai_compat`：任意 OpenAI 兼容端点（便于换 deepseek / 本地模型）。

注意：**生成模型可以与 judge 模型同族**，因为生成阶段不存在"自恋偏差"
（judge 才是判定方）。README 的"不同家族"要求只约束 judge。
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

from eval.seedgen.corpus import Doc, load_corpus
from eval.seedgen import prompts


# ===========================================================================
# LLM 后端
# ===========================================================================


class SeedLLM:
    """统一的文本生成入口。"""

    def __init__(
        self,
        backend: str = "dashscope",
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.9,
        max_retries: int = 3,
    ) -> None:
        self.backend = backend
        self.temperature = temperature
        self.max_retries = max_retries
        self.calls = 0
        self.failures = 0

        if backend == "dashscope":
            import dashscope
            from dashscope import Generation

            self._gen = Generation
            dashscope.api_key = api_key or os.getenv("BAILIAN_API_KEY") or os.getenv(
                "DASHSCOPE_API_KEY", ""
            )
            self.model = model or os.getenv("SEED_MODEL", "qwen-plus")
            if not dashscope.api_key:
                raise RuntimeError("缺少 BAILIAN_API_KEY / DASHSCOPE_API_KEY")
        elif backend == "openai_compat":
            self.model = model or os.getenv("SEED_MODEL", "deepseek-v4-flash")
            self.base_url = base_url or os.getenv(
                "SEED_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
            )
            self.api_key = api_key or os.getenv("SEED_API_KEY") or os.getenv(
                "BAILIAN_API_KEY", ""
            )
            if not self.api_key:
                raise RuntimeError("缺少 SEED_API_KEY / BAILIAN_API_KEY")
        else:
            raise ValueError(f"未知 backend: {backend}")

    def complete(self, prompt: str) -> str:
        """生成一次，返回文本。失败重试。"""
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                self.calls += 1
                if self.backend == "dashscope":
                    resp = self._gen.call(
                        model=self.model,
                        messages=[{"role": "user", "content": prompt}],
                        result_format="message",
                        temperature=self.temperature,
                        max_tokens=4096,
                    )
                    if resp.status_code != 200:
                        raise RuntimeError(f"[{resp.status_code}] {resp.message}")
                    return resp.output.choices[0].message.content or ""
                # openai_compat
                import httpx

                r = httpx.post(
                    f"{self.base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": self.temperature,
                        "stream": False,
                    },
                    timeout=120.0,
                )
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"] or ""
            except Exception as e:  # noqa: BLE001
                last_err = e
                self.failures += 1
                time.sleep(1.5**attempt)
        raise RuntimeError(f"LLM 生成失败（重试 {self.max_retries} 次）: {last_err}")


def _salvage_truncated(text: str) -> str | None:
    """抢救被 max_tokens 截断的 JSON 数组：丢弃最后一条不完整对象后补上 `]`。

    实测 qwen-plus 在要求一次出 5 条时偶发输出截断，直接丢弃整批会浪费一次调用，
    这里尽量捞回已完整的部分。
    """
    idx = text.rfind("},")
    if idx == -1:
        idx = text.rfind("}\n")
        if idx == -1:
            return None
        return text[: idx + 1] + "]"
    return text[: idx + 1] + "]"


def parse_json_array(content: str) -> list[dict[str, Any]]:
    """容错解析 LLM 输出的 JSON 数组。"""
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            salvaged = _salvage_truncated(text)
            if salvaged:
                try:
                    data = json.loads(salvaged)
                except json.JSONDecodeError:
                    raise ValueError(f"无法解析为 JSON 数组: {text[:200]}") from None
            else:
                raise ValueError(f"无法解析为 JSON 数组: {text[:200]}") from None
        else:
            data = json.loads(m.group(0))
    if not isinstance(data, list):
        raise ValueError(f"LLM 输出不是数组: {type(data)}")
    return [d for d in data if isinstance(d, dict)]


# ===========================================================================
# 去重与落盘
# ===========================================================================


def _norm_q(q: str) -> str:
    """归一化问题文本用于去重：去空白与常见标点。"""
    return re.sub(r"[\s，。？?！!、；;：:\"'“”‘’（）()\[\]【】]", "", q or "")


class Deduper:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def add(self, existing: Iterable[str]) -> None:
        for q in existing:
            self._seen.add(_norm_q(q))

    def is_dup(self, q: str) -> bool:
        key = _norm_q(q)
        return not key or key in self._seen

    def mark(self, q: str) -> None:
        self._seen.add(_norm_q(q))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def write_jsonl(path: Path, rows: list[dict[str, Any]], *, append: bool = False) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as fp:
        for row in rows:
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


# ===========================================================================
# 各任务生成器
# ===========================================================================


def _gen_batch(
    llm: SeedLLM,
    dedup: Deduper,
    prompt: str,
    *,
    want: int,
    make_row,
    key_fn=None,
) -> list[dict[str, Any]]:
    """调用一次 LLM 生成一批，过滤重复后转成规范记录。

    `key_fn` 指定从 item 里取哪个字段做去重键：retrieval/e2e/redteam 用 `q`，
    但 case 用的是 `clue`、multturn 是 `turns`、judge 校准是 `question`。
    （若不区分统一取 `q`，case 会取到空串而被去重器判为重复，整批丢失。）

    解析失败（常见：值里出现未转义的引号）时带强约束提示重试一次，
    否则该批样本直接丢失 —— 实测不重试会丢掉约 20% 的批次。
    """
    key_fn = key_fn or (lambda it: str(it.get("q", "")))
    try:
        items = parse_json_array(llm.complete(prompt))
    except Exception as e:  # noqa: BLE001
        print(f"    ! 解析失败({str(e)[:60]})，带强约束重试", flush=True)
        try:
            retry_prompt = (
                prompt
                + "\n\n【重要】上一条输出不是合法 JSON。请只输出 JSON 数组，"
                "字符串值内部若出现引号必须转义为 \\\" ，不要使用换行符。只输出 JSON，不要任何解释。"
            )
            items = parse_json_array(llm.complete(retry_prompt))
        except Exception as e2:  # noqa: BLE001
            print(f"    ! 重试仍失败: {str(e2)[:60]}", flush=True)
            return []
    rows: list[dict[str, Any]] = []
    for it in items:
        q = str(key_fn(it) or "").strip()
        if not q or dedup.is_dup(q):
            continue
        dedup.mark(q)
        row = make_row(it, q)
        if row:
            rows.append(row)
        if len(rows) >= want:
            break
    return rows


def gen_retrieval(
    llm: SeedLLM,
    docs: list[Doc],
    *,
    quota: dict[str, int],
    start_id: int,
    batch: int = 5,
    existing: list[str] | None = None,
) -> list[dict[str, Any]]:
    """生成 retrieval 种子：直接术语 / 场景描述 / 跨文档 / 负例。"""
    rows: list[dict[str, Any]] = []
    dedup = Deduper()
    dedup.add(existing or [])
    counter = start_id

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"R{counter:03d}"

    scope = _scope_text(docs)

    # --- 直接术语 & 场景描述：按文档轮转 + 每篇限量，保证覆盖全部文档 ---
    # 关键：不能"批量填满就退出"（那样只会覆盖排序靠前的几篇，指标会失真），
    # 必须为每篇设上限并遍历全部文档。
    pos_docs = [d for d in docs if d.domain != "E"]
    for category, tmpl in (
        ("直接术语", prompts.RETRIEVAL_TERM),
        ("场景描述", prompts.RETRIEVAL_SCENARIO),
    ):
        want = quota.get(category, 0)
        got_cat = 0
        for _round in range(4):
            if got_cat >= want:
                break
            # 第一轮每篇只出 1 条 → 先保证覆盖全部文档；后续轮次再按缺口补量
            remaining = want - got_cat
            cap = 1 if _round == 0 else max(1, math.ceil(remaining / max(1, len(pos_docs))))
            for doc in pos_docs:
                if got_cat >= want:
                    break
                need = min(cap, want - got_cat)
                got = _gen_batch(
                    llm,
                    dedup,
                    tmpl.format(title=doc.title, excerpt=doc.excerpt(), n=batch),
                    want=need,
                    make_row=lambda it, q, doc=doc, category=category: {
                        "id": next_id(),
                        "category": category,
                        "q": q,
                        "expected_doc_ids": [doc.doc_id],
                        "answer_hint": str(it.get("answer_hint", ""))[:80],
                    },
                )
                rows.extend(got)
                got_cat += len(got)
                if got:
                    print(f"    [{category}] {doc.doc_id} -> +{len(got)}", flush=True)
        covered = len({r["expected_doc_ids"][0] for r in rows if r["category"] == category})
        print(f"    [{category}] 共 {got_cat} 条，覆盖 {covered}/{len(pos_docs)} 篇", flush=True)

    # --- 跨文档关联 ---
    want = quota.get("跨文档关联", 0)
    pos_docs = [d for d in docs if d.domain != "E"]
    i = 0
    while len([r for r in rows if r["category"] == "跨文档关联"]) < want and i < 60:
        k = 2 if i % 2 == 0 else 3
        group = [pos_docs[(i + j) % len(pos_docs)] for j in range(k)]
        i += 1
        blocks = "\n\n".join(prompts.doc_block(d, j + 1) for j, d in enumerate(group))
        got = _gen_batch(
            llm,
            dedup,
            prompts.RETRIEVAL_CROSS.format(doc_blocks=blocks, k=k, n=3),
            want=3,
            make_row=lambda it, q, group=group: {
                "id": next_id(),
                "category": "跨文档关联",
                "q": q,
                "expected_doc_ids": _valid_ids(it.get("docs"), group) or [d.doc_id for d in group],
                "answer_hint": str(it.get("answer_hint", ""))[:80],
            },
        )
        rows.extend(got)
        if got:
            print(f"    [跨文档关联] +{len(got)}", flush=True)

    # --- 负例 ---
    want = quota.get("负例", 0)
    while len([r for r in rows if r["category"] == "负例"]) < want and i < 80:
        i += 1
        got = _gen_batch(
            llm,
            dedup,
            prompts.RETRIEVAL_NEGATIVE.format(scope=scope, n=batch),
            want=batch,
            make_row=lambda it, q: {
                "id": next_id(),
                "category": "负例",
                "q": q,
                "expected_doc_ids": [],
                "answer_hint": "",
                "why_out_of_scope": str(it.get("why_out_of_scope", ""))[:80],
            },
        )
        rows.extend(got)
        print(f"    [负例] +{len(got)}", flush=True)

    return rows


def gen_e2e(
    llm: SeedLLM,
    docs: list[Doc],
    *,
    quota: dict[str, int],
    start_id: int,
    attack_families: dict[str, dict[str, str]],
    batch: int = 5,
    existing: list[str] | None = None,
) -> list[dict[str, Any]]:
    """生成 e2e 种子：知识问答 / 攻击诱导 / 综合多源。"""
    rows: list[dict[str, Any]] = []
    dedup = Deduper()
    dedup.add(existing or [])
    counter = start_id
    scope = _scope_text(docs)

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"E{counter:03d}"

    # --- 知识问答（同样按文档限量遍历，保证覆盖全部文档）---
    pos_docs = [d for d in docs if d.domain != "E"]
    want = quota.get("知识问答", 0)
    got_cat = 0
    for _round in range(4):
        if got_cat >= want:
            break
        remaining = want - got_cat
        cap = 1 if _round == 0 else max(1, math.ceil(remaining / max(1, len(pos_docs))))
        for doc in pos_docs:
            if got_cat >= want:
                break
            got = _gen_batch(
                llm,
                dedup,
                prompts.E2E_QA.format(title=doc.title, excerpt=doc.excerpt(), n=batch),
                want=min(cap, want - got_cat),
                make_row=lambda it, q, doc=doc: {
                    "id": next_id(),
                    "category": "知识问答",
                    "q": q,
                    "expected_refusal": False,
                    "expected_doc_ids": [doc.doc_id],
                    "answer_points": _as_list(it.get("answer_points")),
                },
            )
            rows.extend(got)
            got_cat += len(got)
            if got:
                print(f"    [知识问答] {doc.doc_id} -> +{len(got)}", flush=True)
    covered = len({r["expected_doc_ids"][0] for r in rows if r["category"] == "知识问答"})
    print(f"    [知识问答] 共 {got_cat} 条，覆盖 {covered}/{len(pos_docs)} 篇", flush=True)

    # --- 攻击诱导（按攻击族轮转）---
    want = quota.get("攻击诱导", 0)
    fam_items = list(attack_families.items())
    i = 0
    while len([r for r in rows if r["category"] == "攻击诱导"]) < want and i < 60:
        key, fam = fam_items[i % len(fam_items)]
        i += 1
        got = _gen_batch(
            llm,
            dedup,
            prompts.E2E_ATTACK.format(
                scope=scope,
                family_name=fam["name"],
                family_desc=fam["desc"],
                family_key=key,
                n=3,
            ),
            want=3,
            make_row=lambda it, q, key=key: {
                "id": next_id(),
                "category": "攻击诱导",
                "q": q,
                "expected_refusal": True,
                "expected_doc_ids": [],
                "attack_family": str(it.get("attack_family", key)),
            },
        )
        rows.extend(got)
        print(f"    [攻击诱导/{key}] +{len(got)}", flush=True)

    # --- 综合多源 ---
    want = quota.get("综合多源", 0)
    i = 0
    while len([r for r in rows if r["category"] == "综合多源"]) < want and i < 60:
        k = 2 if i % 2 == 0 else 3
        group = [pos_docs[(i + j) % len(pos_docs)] for j in range(k)]
        i += 1
        blocks = "\n\n".join(prompts.doc_block(d, j + 1) for j, d in enumerate(group))
        got = _gen_batch(
            llm,
            dedup,
            prompts.E2E_MULTI.format(doc_blocks=blocks, k=k, n=3),
            want=3,
            make_row=lambda it, q, group=group: {
                "id": next_id(),
                "category": "综合多源",
                "q": q,
                "expected_refusal": False,
                "expected_doc_ids": _valid_ids(it.get("docs"), group) or [d.doc_id for d in group],
                "answer_points": _as_list(it.get("answer_points")),
            },
        )
        rows.extend(got)
        print(f"    [综合多源] +{len(got)}", flush=True)

    return rows


def gen_redteam(
    llm: SeedLLM,
    docs: list[Doc],
    *,
    per_family: int,
    families: dict[str, dict[str, str]],
    existing: list[str] | None = None,
    start_id: int = 0,
) -> list[dict[str, Any]]:
    """生成红队种子（每族固定条数，带 OWASP 与隐蔽度标注）。"""
    rows: list[dict[str, Any]] = []
    dedup = Deduper()
    dedup.add(existing or [])
    scope = _scope_text(docs)
    n = 1
    for key, fam in families.items():
        got_total = 0
        guard = 0
        while got_total < per_family and guard < 10:
            guard += 1
            got = _gen_batch(
                llm,
                dedup,
                prompts.REDTEAM.format(
                    family_name=fam["name"],
                    family_key=key,
                    family_desc=fam["desc"],
                    owasp=fam["owasp"],
                    scope=scope,
                    n=4,
                ),
                want=per_family - got_total,
                make_row=lambda it, q, key=key, fam=fam: {
                    "id": f"RT{start_id + n:03d}",
                    "family": str(it.get("attack_family", key)),
                    "family_name": fam["name"],
                    "owasp": str(it.get("owasp", fam["owasp"])),
                    "severity": _pick(it.get("severity"), ("high", "medium", "low"), "medium"),
                    "stealth": _pick(it.get("stealth"), ("obvious", "moderate", "covert"), "moderate"),
                    "q": q,
                    "expected_behavior": "refuse_or_safe_answer",
                },
            )
            # id 需要单独递增，避免 lambda 闭包拿不到自增
            for r in got:
                r["id"] = f"RT{start_id + n:03d}"
                n += 1
            rows.extend(got)
            got_total += len(got)
        print(f"    [{key}] +{got_total}", flush=True)
    return rows


def gen_intent(
    llm: SeedLLM,
    *,
    intents: list[dict[str, Any]],
    per_intent: int,
    existing: list[str] | None = None,
) -> list[dict[str, Any]]:
    """生成意图识别种子：按意图平均分配，含易混样本。"""
    rows: list[dict[str, Any]] = []
    dedup = Deduper()
    dedup.add(existing or [])
    n = 1
    for it in intents:
        n_hard = max(1, per_intent // 3)
        n_easy = per_intent - n_hard
        got = _gen_batch(
            llm,
            dedup,
            prompts.INTENT.format(
                intent_code=it["intent_code"],
                intent_name=it.get("name", ""),
                intent_desc=it.get("description") or it.get("name", ""),
                examples=json.dumps(it.get("examples") or [], ensure_ascii=False),
                n=per_intent,
                n_easy=n_easy,
                n_hard=n_hard,
            ),
            want=per_intent,
            make_row=lambda item, q, it=it: {
                "id": f"I{n:03d}",
                "q": q,
                "intent_l2": str(item.get("intent_l2") or it["intent_code"]),
                "difficulty": _pick(item.get("difficulty"), ("easy", "hard"), "easy"),
                "confusable_with": str(item.get("confusable_with", "")),
            },
        )
        for r in got:
            r["id"] = f"I{n:03d}"
            n += 1
        rows.extend(got)
        print(f"    [{it['intent_code']}] +{len(got)}", flush=True)
    return rows


def gen_case(
    llm: SeedLLM,
    *,
    quota: dict[str, int],
    categories: list[str],
    existing: list[str] | None = None,
) -> list[dict[str, Any]]:
    """生成案件调查种子（三分类）。"""
    rows: list[dict[str, Any]] = []
    dedup = Deduper()
    dedup.add(existing or [])
    for conclusion, want in quota.items():
        got_total = 0
        guard = 0
        while got_total < want and guard < 12:
            guard += 1
            category = categories[got_total % len(categories)]
            got = _gen_batch(
                llm,
                dedup,
                prompts.CASE.format(
                    conclusion=conclusion,
                    category=category,
                    n=5,
                ),
                want=want - got_total,
                key_fn=lambda it: str(it.get("clue", "")),
                make_row=lambda it, q, conclusion=conclusion, category=category: {
                    "name": str(it.get("name") or f"case-{len(rows) + 1}"),
                    "clue": str(it.get("clue", q)).strip() or q,
                    "expect_conclusion": conclusion,
                    "min_confidence": 0.0 if conclusion == "inconclusive" else 0.6,
                    "category": category,
                    "key_evidence": _as_list(it.get("key_evidence")),
                },
            )
            rows.extend(got)
            got_total += len(got)
        print(f"    [{conclusion}] +{got_total}", flush=True)
    return rows


# ===========================================================================
# case-seeds（覆盖率驱动版）
# ===========================================================================


def _render_case_spec_prompt(spec: dict[str, Any]) -> str:
    """把一条覆盖规格渲染成生成 prompt（含阈值边界要求）。"""
    boundary_block = ""
    bk = spec.get("boundary_key")
    if bk:
        from eval.coverage import BOUNDARY_BY_KEY

        b = BOUNDARY_BY_KEY.get(str(bk))
        if b:
            boundary_block = (
                f"\n- **阈值边界要求**：线索中必须出现「{b.metric}」的取值 **"
                f"{spec.get('boundary_point')}**"
                f"（该阈值的业务定义是「{b.threshold}」，依据 {b.source}）\n"
            )
    return prompts.CASE_COVERAGE.format(
        conclusion=spec.get("conclusion", ""),
        technique=spec.get("technique", ""),
        evidence_strength=spec.get("evidence_strength", ""),
        info_completeness=spec.get("info_completeness", ""),
        score_band=spec.get("score_band", ""),
        score_band_desc=prompts.SCORE_BAND_DESC.get(str(spec.get("score_band", "")), ""),
        risk_level=spec.get("risk_level", ""),
        disposal_level=spec.get("disposal_level", ""),
        disposal_desc=prompts.DISPOSAL_DESC.get(str(spec.get("disposal_level", "")), ""),
        disturbance=spec.get("disturbance", ""),
        subject_type=spec.get("subject_type", ""),
        boundary_block=boundary_block,
    )


def gen_case_coverage(
    llm: SeedLLM,
    *,
    plan: list[dict[str, Any]],
    per_spec: int = 1,
    start_id: int = 0,
    existing: list[str] | None = None,
    flush_path: Path | None = None,
    flush_every: int = 10,
    done_specs: set[str] | None = None,
) -> list[dict[str, Any]]:
    """按覆盖计划逐规格定向生成 case 种子。

    与 gen_case() 的关键区别：gen_case 只按「结论」配额随机出题，结论分布能齐
    但维度是塌的（证据强度、评分档、干扰类型一条都测不到）。本函数**逐规格指定
    每个维度的取值**，产出后写入 `coverage` 字段，使 coverage_report 能做精确
    统计与收敛判定。

    规格由 eval.coverage.build_coverage_plan / build_boundary_plan 产出；
    规格之间不能混批（混了 LLM 会忽略规格），因此每个规格单独一次调用。
    `per_spec` 控制每个规格生成几条变体（1 = 刚好达标，2 = 留冗余）。

    长任务的两个保障（跑满 148 条规格需要一小时以上，中断一次代价很大）：
    - **增量落盘**（flush_path）：每 flush_every 条规格追加写一次，避免中途异常
      导致已生成的成果全部丢失（原来的 write_jsonl 是最后统一写的）。
    - **断点续传**（done_specs）：已完成的规格直接跳过，重启不重复烧 LLM 调用。
    """
    from eval.coverage import spec_key

    rows: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    dedup = Deduper()
    dedup.add(existing or [])
    done = done_specs or set()
    total = len(plan)
    calls_before = llm.calls
    skipped = 0

    for i, spec in enumerate(plan):
        if spec_key(spec) in done:
            skipped += 1
            continue
        idx = start_id + i + 1
        prompt = _render_case_spec_prompt(spec)
        concl = str(spec.get("conclusion", ""))
        cov = {k: v for k, v in spec.items() if k not in ("boundary_key", "boundary_point")}
        if spec.get("boundary_key"):
            cov["boundary_keys"] = [str(spec["boundary_key"])]

        def _make_row(
            it: dict[str, Any],
            q: str,
            *,
            idx: int = idx,
            concl: str = concl,
            cov: dict[str, Any] = cov,
            spec: dict[str, Any] = spec,
        ) -> dict[str, Any]:
            return {
                "id": f"CASE-{idx:04d}",
                "name": str(it.get("name") or f"case-cov-{idx:04d}"),
                "clue": str(it.get("clue", q)).strip() or q,
                "expect_conclusion": concl,
                "min_confidence": 0.0 if concl == "inconclusive" else 0.6,
                "key_evidence": _as_list(it.get("key_evidence")),
                "coverage": dict(cov),
                "boundary_point": spec.get("boundary_point"),
            }

        got = _gen_batch(
            llm,
            dedup,
            prompt,
            want=per_spec,
            key_fn=lambda it: str(it.get("clue", "")),
            make_row=_make_row,
        )
        rows.extend(got)
        pending.extend(got)

        if flush_path is not None and pending and (i + 1) % flush_every == 0:
            write_jsonl(flush_path, pending, append=True)
            pending = []

        if (i + 1) % 10 == 0 or i == total - 1:
            print(
                f"    [coverage] {i + 1}/{total} 规格 -> {len(rows)} 条"
                f"（LLM 调用 {llm.calls - calls_before}，跳过 {skipped}）",
                flush=True,
            )

    if flush_path is not None and pending:
        write_jsonl(flush_path, pending, append=True)
    return rows


def gen_multturn(
    llm: SeedLLM,
    docs: list[Doc],
    *,
    quota: dict[str, int],
    existing: list[str] | None = None,
) -> list[dict[str, Any]]:
    """生成多轮会话种子。"""
    rows: list[dict[str, Any]] = []
    dedup = Deduper()
    dedup.add(existing or [])
    scope = _scope_text(docs)
    valid_ids = {d.doc_id for d in docs}
    n = 1
    for scene, want in quota.items():
        got_total = 0
        guard = 0
        while got_total < want and guard < 10:
            guard += 1
            got = _gen_batch(
                llm,
                dedup,
                prompts.MULTTURN.format(
                    scope=scope,
                    scene=scene,
                    scene_desc=prompts.MULTTURN_SCENES.get(scene, ""),
                    n=4,
                ),
                want=want - got_total,
                key_fn=lambda it: " ".join(str(t) for t in (it.get("turns") or [])),
                make_row=lambda it, q, scene=scene: _multturn_row(it, q, scene, valid_ids),
            )
            for r in got:
                r["id"] = f"MT{n:03d}"
                n += 1
            rows.extend(got)
            got_total += len(got)
        print(f"    [{scene}] +{got_total}", flush=True)
    return rows


def gen_judge_calib(
    llm: SeedLLM,
    docs: list[Doc],
    *,
    quota: dict[str, int],
    existing: list[str] | None = None,
) -> list[dict[str, Any]]:
    """生成 judge 校准集（回答样本；golden 由人工标注，脚本只负责产出素材）。

    产出后需要人工填写 golden_* 字段，judge 一致率才有比对基准。
    """
    rows: list[dict[str, Any]] = []
    dedup = Deduper()
    dedup.add(existing or [])
    scope = _scope_text(docs)
    n = 1
    for defect, want in quota.items():
        got_total = 0
        guard = 0
        while got_total < want and guard < 10:
            guard += 1
            got = _gen_batch(
                llm,
                dedup,
                prompts.JUDGE_CALIB.format(
                    defect=defect,
                    defect_desc=prompts.JUDGE_CALIB_DEFECTS.get(defect, ""),
                    scope=scope,
                    n=4,
                ),
                want=want - got_total,
                key_fn=lambda it: str(it.get("question", "")),
                make_row=lambda it, q, defect=defect: {
                    "id": f"JC{n:03d}",
                    "defect": defect,
                    "question": str(it.get("question", q)).strip(),
                    "contexts": _as_contexts(it.get("contexts")),
                    "answer": str(it.get("answer", "")).strip(),
                    # 以下 golden 字段由人工标注后填充（judge 一致率的比对基准）
                    "golden_faithfulness": None,
                    "golden_citation_precision": None,
                    "golden_citation_coverage": None,
                    "golden_answer_relevancy": None,
                },
            )
            for r in got:
                r["id"] = f"JC{n:03d}"
                n += 1
            rows.extend(got)
            got_total += len(got)
        print(f"    [{defect}] +{got_total}", flush=True)
    return rows


# ===========================================================================
# 工具函数
# ===========================================================================


def _scope_text(docs: list[Doc]) -> str:
    """知识库范围摘要（用于负例/红队 prompt 的"什么在库内"参照）。"""
    by_domain: dict[str, list[str]] = {}
    for d in docs:
        by_domain.setdefault(d.domain, []).append(f"{d.doc_id}（{d.title}）")
    parts = []
    for domain in ("C", "D", "OWASP", "CHEAT", "E"):
        if domain in by_domain:
            label = {
                "C": "企业风控防守手册",
                "D": "平台制度",
                "OWASP": "OWASP LLM Top10",
                "CHEAT": "账号/Web 安全 Cheat Sheet",
                "E": "平台公告/产品/会议/客服（无方法论的干扰文档）",
            }[domain]
            parts.append(f"【{label}】\n" + "\n".join(by_domain[domain]))
    return "\n\n".join(parts)


def _as_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x)[:100] for x in v][:6]
    if isinstance(v, str) and v.strip():
        return [v.strip()[:100]]
    return []


def _as_contexts(v: Any) -> list[dict[str, str]]:
    if not isinstance(v, list):
        return []
    out: list[dict[str, str]] = []
    for c in v:
        if isinstance(c, dict):
            out.append(
                {
                    "doc_id": str(c.get("doc_id", "unknown")),
                    "text": str(c.get("text", "")),
                }
            )
        elif isinstance(c, str):
            out.append({"doc_id": "unknown", "text": c})
    return out


def _valid_ids(v: Any, group: list[Doc]) -> list[str]:
    """校验 LLM 返回的 docs 是否都在给定文档组内，防止幻觉业务码。"""
    allowed = {d.doc_id for d in group}
    if not isinstance(v, list):
        return []
    ids = [str(x) for x in v if str(x) in allowed]
    return ids[:3]


def _pick(v: Any, allowed: tuple[str, ...], default: str) -> str:
    s = str(v or "").strip().lower()
    return s if s in allowed else default


def _multturn_row(it: dict[str, Any], q: str, scene: str, valid_ids: set[str]) -> dict | None:
    turns = it.get("turns")
    if not isinstance(turns, list) or not turns:
        return None
    turns = [str(t).strip() for t in turns if str(t).strip()]
    if not turns:
        return None
    exp = it.get("final_expectation") if isinstance(it.get("final_expectation"), dict) else {}
    docs = exp.get("expected_doc_ids")
    doc_ids = (
        [str(x) for x in docs if str(x) in valid_ids][:3]
        if isinstance(docs, list)
        else []
    )
    return {
        "id": "MT000",
        "scene": scene,
        "turns": turns,
        "final_expectation": {
            "type": _pick(exp.get("type"), ("answer", "refuse", "clarify"), "answer"),
            "points": _as_list(exp.get("points")),
            "expected_doc_ids": doc_ids,
        },
    }


__all__ = [
    "SeedLLM",
    "Deduper",
    "parse_json_array",
    "load_jsonl",
    "write_jsonl",
    "gen_retrieval",
    "gen_e2e",
    "gen_redteam",
    "gen_intent",
    "gen_case",
    "gen_multturn",
    "gen_judge_calib",
    "load_corpus",
]
