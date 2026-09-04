# -*- coding: utf-8 -*-
"""v8 误拒 case 重发对照: 判定是 R12/R13 回归还是外部服务抖动。"""
import asyncio, io, sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from eval.e2e_eval import _stream_chat, _is_refused, _source_doc_names, _login
import httpx

GATEWAY = "http://localhost:9090/api/sentinel"
RECHECK = [
    ("E058", "预防SQL注入的四种主要防御方法是什么？"),
    ("E051", "风控分析师研判告警时，能查询涉案账户的哪些级别数据？"),
    ("E059", "ATO告警总分达到多少需要人工研判？判定依据有哪些？"),
    ("E096", "当安全测试发现密码重置功能允许提交超长畸形邮箱（如含script标签），且后端未校验即存入日志并回显，这违反了哪些核心安全原则？"),
    ("E036", "Prompt injection和jailbreaking在LLM安全中有什么关键区别？"),
]

async def main():
    async with httpx.AsyncClient() as client:
        token = await _login(client, GATEWAY, "admin", "admin")
        for sid, q in RECHECK:
            s = await _stream_chat(client, f"{GATEWAY}/rag/v3/chat", token, q, 120.0)
            refused = _is_refused(s["full_text"], s["sources"])
            docs = _source_doc_names(s["sources"])
            print(f"[{sid}] refused={refused} src={len(docs)} ttft={s['ttft_ms']:.0f}ms")
            print(f"    ans: {s['full_text'][:110]}".replace(chr(10), " "))
            print()

asyncio.run(main())
