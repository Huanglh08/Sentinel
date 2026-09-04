# -*- coding: utf-8 -*-
"""P2b 延迟采样: 8 条代表性问题全链路 TTFT 采样（重启后新版 gate）。"""
import asyncio, io, sys, statistics

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from eval.e2e_eval import _stream_chat, _is_refused, _source_doc_names, _login
import httpx

GATEWAY = "http://localhost:9090/api/sentinel"
CASES = [
    ("E001", "什么是账户接管攻击（ATO）？"),
    ("E013", "发生安全事件后，处置策略是什么？"),
    ("E027", "ATO告警总分达到多少需要人工研判？判定规则中基础分和组合加成分别是怎么计算的？"),
    ("E036", "Prompt injection和jailbreaking在LLM安全中有什么关键区别？"),
    ("E041", "职务侵占行为会触发什么处罚？"),
    ("E058", "预防SQL注入的四种主要防御方法是什么？"),
    ("E076", "MFA失效会导致什么风险？如何防御？"),
    ("E098", "重置密码接口接收邮箱参数后，是否需对邮箱格式做严格校验？若校验过严（如禁用+号子地址），会带来什么风控风险？"),
]


async def main():
    async with httpx.AsyncClient() as client:
        token = await _login(client, GATEWAY, "admin", "admin")
        ttfts = []
        for sid, q in CASES:
            s = await _stream_chat(client, f"{GATEWAY}/rag/v3/chat", token, q, 120.0)
            docs = _source_doc_names(s["sources"])
            ttfts.append(s["ttft_ms"])
            print(f"[{sid}] ttft={s['ttft_ms']:>6.0f}ms src={len(docs)} refused={_is_refused(s['full_text'], s['sources'])}")
        ttfts.sort()
        print(f"\nn={len(ttfts)} P50={statistics.median(ttfts):.0f}ms "
              f"avg={statistics.mean(ttfts):.0f}ms max={ttfts[-1]:.0f}ms")

asyncio.run(main())
