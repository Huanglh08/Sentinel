# -*- coding: utf-8 -*-
"""Probe: does splitting multi-doc questions into per-topic sub-queries improve retrieval?

Compares original-question retrieval vs manual sub-query split (simulating what the
rewrite LLM would produce under the new rule) on 6 known weak multi-doc questions,
plus 3 single-doc questions to eyeball whether the split rule would misfire.

Run: PYTHONIOENCODING=utf-8 python _probe_subq.py   (agent must be up on :8001)
"""
import json
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

BASE = "http://localhost:8001/debug/retrieval"
SEEDS = Path(__file__).parent.parent / "evaluation" / "seeds" / "ragas-seeds-v2.jsonl"

# ground truth per question: expected_doc_ids from the eval seed file
_GOLD_DOCS = {}
with open(SEEDS, encoding="utf-8") as f:
    for line in f:
        s = json.loads(line)
        _GOLD_DOCS[s["q"]] = s.get("expected_doc_ids") or []

# (question, [sub queries]) -- sub queries hand-written per the proposed rule:
# one topic per sub-query, focused on the fact the question demands.
MULTI = [
    ("OWASP 的认证指南与多因素认证指南，对密码长度与因子强度分别给出了什么要求？两者是什么关系？",
     ["认证指南对用户密码最小长度的要求是什么",
      "多因素认证指南对认证因子强度的要求是什么"]),
    ("会话管理指南与认证指南对「登录成功那一刻」分别要求做什么？",
     ["认证指南对登录成功之后的处理要求",
      "会话管理指南对登录成功后创建会话的要求"]),
    ("凭证填充防御与会话管理，在「登录成功后」的处理上如何配合？",
     ["凭证填充防御对登录成功后的异常检测要求",
      "会话管理指南对登录后防御会话劫持的要求"]),
    ("密码存储指南与认证指南在「盐」和「传输中的口令」上各自负责什么？",
     ["密码存储指南对加盐哈希的要求",
      "认证指南对口令在传输过程中保护的要求"]),
    ("会话管理与 MFA，在「敏感操作二次验证」上如何配合？",
     ["会话管理指南对敏感操作重新验证身份的要求",
      "多因素认证在敏感操作时的再次认证要求"]),
    ("认证指南与授权指南，对「认证失败后的响应」与「授权失败后的响应」分别要求注意什么？",
     ["认证指南对认证失败后的响应处理要求",
      "授权指南对授权失败后的响应处理要求"]),
]

SINGLE = [
    "多因素认证应该使用哪些认证因子？",
    "会话超时时间应该如何设置？",
    "SSRF 防御应该遵循哪些最佳实践？",
]


def retrieve(q):
    url = BASE + "?" + urllib.parse.urlencode({"question": q, "rewrite": "true"})
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.load(r)


def norm_doc(name):
    return name.replace(".md", "").replace("_", " ").lower()


def rrf_merge(list_of_chunks, k=60):
    scores, first_meta = defaultdict(float), {}
    for chunks in list_of_chunks:
        for rank, ch in enumerate(chunks):
            key = ch["doc_id"], ch["chunk_index"]
            scores[key] += 1.0 / (k + rank + 1)
            first_meta.setdefault(key, ch)
    merged = sorted(scores.items(), key=lambda kv: -kv[1])
    return [(first_meta[key], s) for key, s in merged]


def coverage_rank(chunks, exp_docs):
    """(set of expected docs present, best rank of each expected doc)"""
    present, ranks = set(), {}
    for i, ch in enumerate(chunks):
        nd = norm_doc(ch["doc_name"])
        if nd in exp_docs:
            present.add(nd)
            ranks.setdefault(nd, i + 1)
    return present, ranks


def fmt(ranks):
    return ", ".join("%s@%d" % (d.split()[0], r) for d, r in sorted(ranks.items(), key=lambda x: x[1]))


print("=" * 100)
print("PART 1: multi-doc questions -- ORIG(orig only) vs SPLIT(subs only) vs UNION(orig+subs, 3-way RRF)")
print("=" * 100)
for q, subs in MULTI:
    orig = retrieve(q)
    sub_results = [retrieve(s) for s in subs]
    merged_subs = rrf_merge([r["final"] for r in sub_results])[:8]
    merged_union = rrf_merge([orig["final"]] + [r["final"] for r in sub_results])[:8]
    exp = {norm_doc(d) for d in _GOLD_DOCS.get(q, [])}

    o_present, o_ranks = coverage_rank(orig["final"], exp)
    m_present, m_ranks = coverage_rank([ch for ch, _ in merged_subs], exp)
    u_present, u_ranks = coverage_rank([ch for ch, _ in merged_union], exp)
    rw = orig.get("rewritten_query") or ""

    best = max(len(o_present), len(m_present), len(u_present))
    print("\nQ: %s" % q[:64])
    print("  expected docs: %d | rewrite: %s" % (len(exp), rw[:70]))
    print("  ORIG : cover %d/%d | %s" % (len(o_present), len(exp), fmt(o_ranks) or "-"))
    print("  SPLIT: cover %d/%d | %s" % (len(m_present), len(exp), fmt(m_ranks) or "-"))
    print("  UNION: cover %d/%d | %s" % (len(u_present), len(exp), fmt(u_ranks) or "-"))
    print("  VERDICT: UNION is %s" % ("BEST" if len(u_present) == best and len(u_present) >= len(o_present)
                                    else "NO GAIN"))

print()
print("=" * 100)
print("PART 2: single-doc questions -- would the split rule misfire? (expect: no split needed)")
print("=" * 100)
for q in SINGLE:
    r = retrieve(q)
    docs = []
    seen = set()
    for ch in r["final"]:
        if ch["doc_name"] not in seen:
            seen.add(ch["doc_name"])
            docs.append(ch["doc_name"])
    print("\nQ: %s" % q[:64])
    print("  docs in top8: %s" % (docs[:4],))
