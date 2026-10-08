# -*- coding: utf-8 -*-
"""Smoke: case proxy chain via gateway 9090 (SSE investigate + JSON endpoints).

Chain: frontend-style request -> Java gateway (login token) -> CaseProxyController
       -> agent 8001 (X-API-Key + X-User-*) -> MCP 8002 ticket_query.
Exit code 0 = all assertions passed.
"""
import json
import sys

import httpx
import os

GW = "http://127.0.0.1:9090/api/sentinel"
AGENT = "http://127.0.0.1:8001"
API_KEY = os.getenv("API_KEY", "")  # 从 .env 读取，切勿硬编码密钥

ok = 0


def check(name, cond, detail=""):
    global ok
    assert cond, "FAIL %s %s" % (name, detail)
    ok += 1
    print("  ok %02d %s" % (ok, name))


def main():
    # 1) login via gateway
    r = httpx.post(GW + "/auth/login", json={"username": "admin", "password": "admin"}, timeout=15)
    check("login", r.status_code == 200, r.text[:100])
    token = r.json()["data"]["token"]

    # 2) SSE investigate through gateway proxy
    events = []
    with httpx.stream(
        "POST", GW + "/case/investigate",
        headers={"Authorization": token},
        json={"clue": "account A1001 shows abnormal refund spikes in east region", "source": "smoke"},
        timeout=180,
    ) as resp:
        check("investigate http 200", resp.status_code == 200, str(resp.status_code))
        ctype = resp.headers.get("content-type", "")
        check("sse content-type", "text/event-stream" in ctype, ctype)
        for line in resp.iter_lines():
            if line.startswith("data:"):
                try:
                    events.append(json.loads(line[5:].strip()))
                except Exception:
                    pass

    types = [e.get("type") for e in events]
    check("has step/report events", "step" in types and "report" in types, str(set(types)))

    # 3) tool observation must come from real MCP ticket_query (not the
    #    "[MCP ... call failed]" degraded placeholder)
    obs = [str(e.get("observation", "")) for e in events if e.get("type") == "step" and e.get("tool_id")]
    check("tool steps executed", len(obs) > 0, str(len(obs)))
    degraded = [o for o in obs if "MCP" in o and "fail" in o.lower()]
    check("no MCP-degraded observation", not degraded, (degraded[0][:80] if degraded else ""))

    # 4) case detail readable via proxy
    r = httpx.get(GW + "/cases?limit=1", headers={"Authorization": token}, timeout=20)
    case_id = r.json()["cases"][0]["id"]
    r = httpx.get(GW + "/case/%s?with=approvals" % case_id, headers={"Authorization": token}, timeout=20)
    check("case detail via proxy", r.status_code == 200 and "case_no" in r.json(), r.text[:80])

    # 5) report export via proxy (streamed md)
    r = httpx.get(GW + "/case/%s/report/export" % case_id, headers={"Authorization": token}, timeout=30)
    check("report export via proxy", r.status_code == 200 and len(r.content) > 50, str(r.status_code))

    # 6) no-key direct access still rejected
    r = httpx.get(AGENT + "/cases", timeout=10)
    check("no-key 401", r.status_code == 401, str(r.status_code))

    # 7) C-side chat main chain (Java -> agent with X-API-Key) unaffected by auth
    #    gateway protocol: GET /rag/v3/chat?question=...  frames: message/complete/finish/done
    import urllib.parse
    got_message = False
    with httpx.stream(
        "GET", GW + "/rag/v3/chat?question=" + urllib.parse.quote("hi, ping"),
        headers={"Authorization": token, "Accept": "text/event-stream"},
        timeout=120,
    ) as resp:
        check("chat http 200", resp.status_code == 200, str(resp.status_code))
        for line in resp.iter_lines():
            if line.startswith("event:") and "message" in line:
                got_message = True
                break
            if line.startswith("event:") and ("error" in line or "done" in line):
                break
    check("chat streams tokens", got_message, "no message frame")

    print("ALL %d CHECKS PASSED" % ok)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(exc)
        sys.exit(1)
