# -*- coding: utf-8 -*-
"""Verify: after PROMPT change, does the rewrite model now split multi-doc questions?

Hits the production pipeline via /debug/retrieval?rewrite=true (multi-query union
retrieval is built-in: original question is always query #1). Compares window
coverage against eval-seed ground truth, and checks single-doc questions do not
misfire.

Run: PYTHONIOENCODING=utf-8 python _verify_split.py
"""
import json
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "http://localhost:8001/debug/retrieval"
SEEDS = Path(__file__).parent.parent / "evaluation" / "seeds" / "ragas-seeds-v2.jsonl"

_GOLD_DOCS = {}
with open(SEEDS, encoding="utf-8") as f:
    for line in f:
        s = json.loads(line)
        _GOLD_DOCS[s["q"]] = s.get("expected_doc_ids") or []

# 6 weak multi-doc questions (3 zero-recall + 3 half) from the ragas-v2c report
# TOPICS: topic names explicitly named in the question that each sub-query set must preserve
MULTI = [
    ("OWASP 的认证指南与多因素认证指南，对密码长度与因子强度分别给出了什么要求？两者是什么关系？", ["认证", "多因素认证"]),
    ("会话管理指南与认证指南对「登录成功那一刻」分别要求做什么？", ["会话管理", "认证"]),
    ("凭证填充防御与会话管理，在「登录成功后」的处理上如何配合？", ["凭证填充", "会话管理"]),
    ("密码存储指南与认证指南在「盐」和「传输中的口令」上各自负责什么？", ["密码存储", "认证"]),
    ("会话管理与 MFA，在「敏感操作二次验证」上如何配合？", ["会话管理", "MFA|多因素认证"]),
    ("认证指南与授权指南，对「认证失败后的响应」与「授权失败后的响应」分别要求注意什么？", ["认证", "授权"]),
]
SINGLE = [
    "多因素认证应该使用哪些认证因子？",
    "会话超时时间应该如何设置？",
    "SSRF 防御应该遵循哪些最佳实践？",
]

# baseline coverage from the pre-change probe (ORIG mode, single-query)
BASELINE = {0: 2, 1: 2, 2: 1, 3: 2, 4: 2, 5: 2}


def retrieve(q):
    url = BASE + "?" + urllib.parse.urlencode({"question": q, "rewrite": "true"})
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.load(r)


def norm_doc(name):
    return name.replace(".md", "").replace("_", " ").lower()


print("=" * 100)
print("PART 1: multi-doc questions via production pipeline (model-driven split + union retrieval)")
print("=" * 100)
for i, (q, topics) in enumerate(MULTI):
    r = retrieve(q)
    queries = r.get("queries") or []
    exp = {norm_doc(d) for d in _GOLD_DOCS.get(q, [])}
    present, ranks = set(), {}
    for j, ch in enumerate(r["final"]):
        nd = norm_doc(ch["doc_name"])
        if nd in exp:
            present.add(nd)
            ranks.setdefault(nd, j + 1)
    rk = ", ".join("%s@%d" % (d.split()[0], r2) for d, r2 in sorted(ranks.items(), key=lambda x: x[1]))
    delta = len(present) - BASELINE.get(i, 0)
    # topic fidelity assertion: every topic named in the question must appear in sub-queries
    sub_text = " ".join(queries[1:])  # skip query #1 (the original question itself)
    missing = [t for t in topics if not any(alt.lower() in sub_text.lower() for alt in t.split("|"))]
    ok = "OK" if not missing else "MISSING: %s" % "/".join(missing)
    print("\nQ%d: %s" % (i + 1, q[:64]))
    print("  queries(%d): %s" % (len(queries), " | ".join(x[:38] for x in queries)))
    print("  fidelity [%s] | cover %d/%d (baseline %d, %+d) | %s" % (ok, len(present), len(exp), BASELINE.get(i, 0), delta, rk or "-"))

print()
print("=" * 100)
print("PART 2: single-doc questions -- split misfire check (want: 1 query, no split)")
print("=" * 100)
for q in SINGLE:
    r = retrieve(q)
    queries = r.get("queries") or []
    docs = list(dict.fromkeys(ch["doc_name"] for ch in r["final"]))
    print("\nQ: %s" % q[:60])
    print("  queries(%d): %s" % (len(queries), " | ".join(x[:42] for x in queries)))
    print("  top docs: %s" % (docs[:3],))
