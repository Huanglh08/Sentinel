"""成分1提示词约束验证：K017×3 稳定性 + 2 条正常题副作用抽查（临时脚本）。"""
import asyncio
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx
from eval.ragas_eval import _build_llm, _load_seed_file, _login, _refused, _stream_chat
from ragas.metrics.collections import Faithfulness

SEEDS, _REFS = _load_seed_file(Path("../evaluation/seeds/ragas-seeds-v2.jsonl"))
GATEWAY = "http://localhost:9090/api/sentinel"


def q_of(kw):
    pat = kw.replace(" ", "")
    return next(s.q for s in SEEDS if pat in s.q.replace(" ", ""))


async def main():
    llm = _build_llm()
    fm = Faithfulness(llm)
    k17 = q_of("设备指纹与 IP 信誉")
    ctxs_ref = None
    async with httpx.AsyncClient() as client:
        token = await _login(client, GATEWAY, "admin", "admin")
        print("== K017 x3（新 grounding 规则）==")
        for i in range(3):
            st = await _stream_chat(client, f"{GATEWAY}/rag/v3/chat", token, k17, 180.0)
            a = st["full_text"]
            print("run%d len=%d sha=%s | %s" % (i + 1, len(a), hashlib.sha1(a.encode()).hexdigest()[:8], a[:70].replace(chr(10), " ")))
        # 统一用一份上下文重判三轮 faith
        import urllib.parse
        resp = await client.get(
            "http://localhost:8001/debug/retrieval?" + urllib.parse.urlencode({"question": k17, "rewrite": "true"}),
            timeout=60,
        )
        ctxs_ref = [str(c.get("text") or "").strip() for c in resp.json().get("final", []) if str(c.get("text") or "").strip()]
        print("\n== 同一稳定上下文重判 ==")
        for i in range(3):
            st = await _stream_chat(client, f"{GATEWAY}/rag/v3/chat", token, k17, 180.0)
            a = st["full_text"]
            r = await fm.ascore(user_input=k17, response=a, retrieved_contexts=ctxs_ref)
            print("run%d len=%d sha=%s faith=%.3f | %s" % (
                i + 1, len(a), hashlib.sha1(a.encode()).hexdigest()[:8], float(r.value), a[:70].replace(chr(10), " ")))
        print("\n== 副作用抽查（不应误拒/答案不塌）==")
        for kw in ["三大类因子", "多租户场景"]:
            q = q_of(kw)
            st = await _stream_chat(client, f"{GATEWAY}/rag/v3/chat", token, q, 180.0)
            a = st["full_text"]
            print("refused=%s len=%d | %s" % (_refused(a), len(a), a[:80].replace(chr(10), " ")))


asyncio.run(main())
