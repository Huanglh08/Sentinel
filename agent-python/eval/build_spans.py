"""为评测题补一层「必须命中的原文片段」标注（检索侧锚点）。

为什么需要：现有参考答案是照文档写的，只能用于生成侧指标；检索侧缺一个
「这几句必须被召回到」的锚点，导致失败率与 token 级精度算不出来。

标注方法：把期望文档切成句子，用参考答案与句子的字符二元组重合度做贪心选择，
二元组按语料文档频率加权（IDF），避免「识别」「异常」这类高频词把无关句子拉进来。
累计权重覆盖到 0.85 或片段达 600 字即停。数值阈值不需要单独特判——「≥80」
在二元组层面与参考答案自然对齐（早期按「数值+单位」抽句会漏掉无单位的阈值）。

每条标注带 ref_coverage（IDF 加权覆盖）、span_chars 与 needs_review，供人工过目。
自检断言：每个片段必须能在期望文档原文里逐字找到。

跨语言题（中文答案 / 英文原文）字面重合不可用，改用 --terms 走关键词锚定：
每道题给一组英文事实关键词，在期望文档里逐句找同时含关键词的句子，
要求每个关键词至少被一个片段覆盖，覆盖不了的记进 missing_terms。

用法：
    python -m eval.build_spans            # 生成并写文件
    python -m eval.build_spans --check    # 只校验已有文件
    python -m eval.build_spans --terms ../evaluation/seeds/_v2_terms.json \
        --seed ../evaluation/seeds/ragas-seeds-v2.jsonl \
        --out ../evaluation/seeds/answer-spans-v2.jsonl
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.chunk_experiment import load_docs
from eval.chunk_gate import _norm_text, _sentences

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEEDS = _REPO_ROOT / "evaluation" / "seeds" / "ragas-seeds.jsonl"
_OUT = _REPO_ROOT / "evaluation" / "seeds" / "answer-spans.jsonl"

_MAX_SPAN = 600
_TARGET_COVER = 0.85
_KEEP_COVER = 0.75  # 裁剪下限：删句后覆盖不得低于此值
_MIN_TERM_SPAN = 40  # 关键词锚点的最短句长：目录项/单词条目「- Least Privilege」不承载事实，不能当锚点
_BLOCK_CHARS = 300  # 列表条目合并上限
_WORD = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")
_LIST = re.compile(r"^(?:[-*+]|\d+\.|\|)")


def _bigrams(text: str) -> set[str]:
    chars = "".join(_WORD.findall(_norm_text(text)))
    return {chars[i : i + 2] for i in range(len(chars) - 1)} or ({chars} if chars else set())


def _idf(docs: dict[str, str]) -> dict[str, float]:
    n = len(docs)
    df: dict[str, int] = {}
    for text in docs.values():
        for gram in _bigrams(text):
            df[gram] = df.get(gram, 0) + 1
    return {gram: math.log((n + 1) / (c + 1)) + 0.1 for gram, c in df.items()}


def _weighted_cover(ref: set[str], span: str, idf: dict[str, float]) -> float:
    total = sum(idf.get(g, 1.0) for g in ref)
    if not total:
        return 0.0
    hit = sum(idf.get(g, 1.0) for g in _bigrams(span) & ref)
    return round(hit / total, 4)


def pick_spans(reference: str, exp_docs: list[str], idf: dict[str, float]) -> list[str]:
    ref = _bigrams(reference)
    cands = [s.strip() for d in exp_docs for s in _sentences(d) if len(s.strip()) >= 8]
    # 文档抬头的业务码/域/版本对答案没有贡献，且因稀有而权重偏高，先剔掉
    cands = [s for s in cands if not re.match(r"[-*]?\s*(业务码|域|适用角色|版本)[：:]", s)]
    picked: list[str] = []
    covered: set[str] = set()
    chars = 0
    while chars < _MAX_SPAN:
        best, best_gain = None, 0.0
        for cand in cands:
            if cand in picked:
                continue
            new = (_bigrams(cand) & ref) - covered
            gain = sum(idf.get(g, 1.0) for g in new)
            if gain > best_gain:
                best, best_gain = cand, gain
        if not best:
            break
        picked.append(best)
        covered |= _bigrams(best) & ref
        chars += len(_norm_text(best))
        if _weighted_cover(ref, "".join(picked), idf) >= _TARGET_COVER:
            break
    return picked


def prune_spans(spans: list[str], reference: str, idf: dict[str, float]) -> list[str]:
    """删掉贡献最小的句子，只要覆盖仍不低于下限——避免把与问题无关的邻句算成必命中证据。"""
    ref = _bigrams(reference)
    spans = list(spans)
    while len(spans) > 1:
        best_rest, best_cover = None, -1.0
        for i in range(len(spans)):
            rest = spans[:i] + spans[i + 1 :]
            cover = _weighted_cover(ref, "".join(rest), idf)
            if cover >= _KEEP_COVER and cover > best_cover:
                best_rest, best_cover = rest, cover
        if best_rest is None:
            break
        spans = best_rest
    return spans


def build_one(case: dict, docs: dict[str, str], idf: dict[str, float]) -> dict | None:
    exp_docs = [docs[d] for d in case["expected_doc_ids"] if d in docs]
    reference = (case.get("reference") or "").strip()
    if not exp_docs or not reference:
        return None  # 负例题不参与检索指标
    spans = prune_spans(pick_spans(reference, exp_docs, idf), reference, idf)
    if not spans:
        return None
    joined = "".join(spans)
    cover = _weighted_cover(_bigrams(reference), joined, idf)
    chars = len(_norm_text(joined))
    return {
        "id": case["id"],
        "expected_doc_ids": case["expected_doc_ids"],
        "spans": spans,
        "span_chars": chars,
        "ref_coverage": cover,
        "needs_review": cover < 0.6 or chars >= _MAX_SPAN,
    }


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _has(low: str, term: str) -> bool:
    """左边界必须是词边界：允许 truncat 命中 truncation，但不许 direct 命中 indirect。"""
    start = low.find(term)
    while start >= 0:
        if start == 0 or not low[start - 1].isalnum():
            return True
        start = low.find(term, start + 1)
    return False


def _blocks(doc: str) -> list[str]:
    """连续列表行合成一块：答案常常是一串条目（日志该记哪些事件），
    只取其中一条会把它变成零散短语。合并上限 _BLOCK_CHARS，超了另起一块，
    否则整节条目会被当成一个锚点。"""
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append("\n".join(buf))
            buf.clear()

    for line in doc.splitlines():
        s = line.strip()
        if _LIST.match(s):
            if sum(len(x) for x in buf) + len(s) > _BLOCK_CHARS:
                flush()
            buf.append(s)
            continue
        flush()
        if s:
            out.append(s)
    flush()
    return out


def pick_spans_by_terms(terms: list[str], exp_docs: list[str]) -> tuple[list[str], list[str], list[str]]:
    """按事实关键词挑句子：每轮选覆盖新增关键词最多的句子，同分选更短的。

    关键词把连续空白折叠成一个空格后按子串匹配：既能跨过原文换行，也保住词边界
    （不能全去空白，否则 direct 会命中 indirect）。
    标题行（# 开头）只说明章节名、不承载事实，先不进候选；只出现在标题里的
    关键词退而取标题，并单独记进 heading_only 供人工复核。
    """
    keys = [(_flat(t), t) for t in terms]
    body: list[tuple[str, set[str]]] = []
    heads: list[tuple[str, set[str]]] = []
    for doc in exp_docs:
        for sent in _blocks(doc):
            low = _flat(sent)
            hit = {t for k, t in keys if k and _has(low, k)}
            if not hit:
                continue
            if sent.lstrip().startswith("#"):
                heads.append((sent.strip(), hit))
            elif len(_norm_text(sent)) >= _MIN_TERM_SPAN:
                body.append((sent.strip(), hit))
    picked: list[str] = []
    covered: set[str] = set()
    chars = 0
    heading_only: list[str] = []
    for pool in (body, heads):
        while len(covered) < len(terms):
            best, best_hit, best_gain = "", set(), 0
            for sent, hit in pool:
                new = hit - covered
                if len(new) > best_gain or (len(new) == best_gain and new and len(sent) < len(best)):
                    best, best_hit, best_gain = sent, hit, len(new)
            if not best:
                break
            if pool is heads:
                heading_only.extend(sorted(best_hit - covered))
            picked.append(best)
            covered |= best_hit
            chars += len(_norm_text(best))
            if chars >= _MAX_SPAN:
                return picked, [t for t in terms if t not in covered], heading_only
    return picked, [t for t in terms if t not in covered], heading_only


def build_one_terms(case: dict, terms: list[str], docs: dict[str, str]) -> dict | None:
    exp_docs = [docs[d] for d in case["expected_doc_ids"] if d in docs]
    if not exp_docs or not terms:
        return None
    spans, missing, heading_only = pick_spans_by_terms(terms, exp_docs)
    if not spans:
        return None
    return {
        "id": case["id"],
        "expected_doc_ids": case["expected_doc_ids"],
        "spans": spans,
        "span_chars": len(_norm_text("".join(spans))),
        "ref_coverage": round((len(terms) - len(missing)) / len(terms), 4),
        "missing_terms": missing,
        "heading_only_terms": heading_only,
        "needs_review": bool(missing or heading_only),
    }


def verify(rows: list[dict], docs: dict[str, str]) -> list[str]:
    """每个片段必须逐字出现在期望文档里，否则标注无效。"""
    bad: list[str] = []
    for row in rows:
        pool = _norm_text("\n".join(docs[d] for d in row["expected_doc_ids"] if d in docs))
        for span in row["spans"]:
            if _norm_text(span) not in pool:
                bad.append(f"{row['id']}: 片段不在原文 {span[:40]}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--seed", default=str(_SEEDS), help="题目文件，默认 ragas-seeds.jsonl")
    ap.add_argument("--out", default=str(_OUT), help="标注输出，默认 answer-spans.jsonl")
    ap.add_argument("--terms", default="", help="关键词锚定文件 {题号: [英文事实关键词]}，给了就走关键词路线")
    args = ap.parse_args()

    docs, _ = load_docs()
    out_path = Path(args.out)
    if args.check:
        rows = [json.loads(l) for l in out_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        bad = verify(rows, docs)
        print(f"校验 {len(rows)} 条，逐字缺失 {len(bad)}")
        for b in bad[:10]:
            print("  ", b)
        return 1 if bad else 0

    seeds = [json.loads(l) for l in Path(args.seed).read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.terms:
        terms = json.loads(Path(args.terms).read_text(encoding="utf-8"))
        rows = [r for r in (build_one_terms(c, terms.get(c["id"], []), docs) for c in seeds) if r]
    else:
        idf = _idf(docs)
        rows = [r for r in (build_one(c, docs, idf) for c in seeds) if r]
    bad = verify(rows, docs)
    if bad:
        print("标注自检失败：")
        for b in bad[:10]:
            print("  ", b)
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    review = [r["id"] for r in rows if r["needs_review"]]
    avg_cover = sum(r["ref_coverage"] for r in rows) / len(rows)
    avg_chars = sum(r["span_chars"] for r in rows) / len(rows)
    negatives = sum(1 for c in seeds if not c.get("expected_doc_ids"))
    print(f"可用题 {len(rows)}/{len(seeds)}（负例 {negatives} 条不参与）")
    if args.terms:
        no_terms = [c["id"] for c in seeds if c.get("expected_doc_ids") and c["id"] not in terms]
        print(f"关键词覆盖题数 {len(rows)}，未给关键词 {no_terms or '无'}")
        for r in rows:
            if r["missing_terms"]:
                print(f"  {r['id']}: 缺失关键词 {r['missing_terms']}")
            if r["heading_only_terms"]:
                print(f"  {r['id']}: 仅标题命中 {r['heading_only_terms']}")
    print(f"平均覆盖 {avg_cover:.3f}，平均片段 {avg_chars:.0f} 字，待人工复核 {len(review)} 条 {review}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
