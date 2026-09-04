"""临时脚本: 查看 e2e 报告中 Faithfulness 低分样本的 claims 明细。"""
import json
from pathlib import Path

path = sorted(Path(r"C:\Users\30970\Desktop\Folder\sentinel-main\evaluation\reports").glob("e2e_judge_v1-*.json"))[-1]
data = json.loads(path.read_text(encoding="utf-8"))
rows = data.get("cases", data if isinstance(data, list) else [])
scored = [r for r in rows if r.get("faithfulness") is not None]
scored.sort(key=lambda r: r["faithfulness"])
print(f"scored={len(scored)}, avg={sum(r['faithfulness'] for r in scored)/len(scored):.4f}\n")
for r in scored[:3]:
    print("=" * 90)
    print(f"[faith={r['faithfulness']}] {r['question'][:60]}")
    print("preview:", r["answer_preview"][:100])
    reason = r.get("judge_reason", "")
    print("reason:", reason[:400])
    # claims 若在 reason 里被截断则看 raw；JSON 报告只存了 reason 字符串
