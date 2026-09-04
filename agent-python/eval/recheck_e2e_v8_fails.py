# -*- coding: utf-8 -*-
"""仍失败的 5 条：完整详情 + 二次重发（三态判定：抖动/稳定/趋势）。"""
import asyncio, io, sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from eval.e2e_eval import _stream_chat, _is_refused, _source_doc_names, _login
import httpx

GATEWAY = "http://localhost:9090/api/sentinel"
FAILS = [
    ("E043", "判决结果都能通过哪些渠道通知到用户？"),
    ("E050", "企业微信和Webhook的告警通知渠道该怎么配置？"),
    ("E052", "部署DeepSeek-R1模型时，对GPU显存有什么要求？"),
    ("E054", "Slack通知渠道的配置方法是什么？"),
    ("E099", "知识库的分块策略对RAG召回率有什么影响？"),
]


async def main():
    async with httpx.AsyncClient() as client:
        token = await _login(client, GATEWAY, "admin", "admin")
        for round_no in (1, 2):
            print(f"########## 第 {round_no} 轮")
            for sid, q in FAILS:
                try:
                    s = await _stream_chat(client, f"{GATEWAY}/rag/v3/chat", token, q, 120.0)
                    refused = _is_refused(s["full_text"], s["sources"])
                    docs = _source_doc_names(s["sources"])
                    print(f"[{sid}] refused={refused} src={len(docs)} ttft={s['ttft_ms']:.0f}ms docs={docs[:3]}")
                    print(f"    ans: {s['full_text'][:100]}".replace(chr(10), " "))
                except Exception as e:
                    print(f"[{sid}] EXC {e}")
            print()

asyncio.run(main())
