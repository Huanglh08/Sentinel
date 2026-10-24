# 归因对照：关沉底后重跑掉幅最大题，看窗口是否恢复 v3 形态
import json
import urllib.parse
import urllib.request

QS = [
    "高风险参与账号的奖励发放策略是什么？延迟到什么时候发放？",
    "通道拒付率进入内部预警线的阈值是多少？",
    "ATO 告警的组合判定里，高、中、低风险信号各折算多少分？总分达到多少进入高危队列人工研判？",
]
for q in QS:
    url = "http://localhost:8001/debug/retrieval?" + urllib.parse.urlencode({"question": q, "rewrite": "true"})
    with urllib.request.urlopen(url, timeout=180) as r:
        d = json.load(r)
    print("Q:", q[:40])
    print("  queries:", len(d.get("queries", [])))
    for i, c in enumerate(d["final"]):
        t = " ".join(str(c.get("text", "")).split())
        print("  %d. %-38s ci=%-4s %s" % (i + 1, (c.get("doc_name") or "")[:38], c.get("chunk_index"), t[:46]))
