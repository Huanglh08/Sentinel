"""One-off probe: is the bailian key in .env alive? Single 'hello' call, tiny budget."""
import json
import urllib.request
import urllib.error

key = None
for name in ("BAILIAN_API_KEY", "API_KEY"):
    with open(".env", "r", encoding="utf-8-sig") as f:
        for line in f:
            if line.startswith(f"{name}="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if key:
        break

if not key:
    print("PROBE: no API_KEY found in .env")
    raise SystemExit(1)

print(f"PROBE: key head={key[:6]}... len={len(key)}")
body = json.dumps({
    "model": "deepseek-v4.1-flash-1017",
    "messages": [{"role": "user", "content": "你好"}],
    "max_tokens": 8,
}).encode()

req = urllib.request.Request(
    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    data=body,
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read().decode())
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        print(f"PROBE OK http=200 reply={content!r}")
except urllib.error.HTTPError as e:
    print(f"PROBE FAIL http={e.code} body={e.read().decode('utf-8', 'replace')[:250]}")
except Exception as e:  # noqa: BLE001
    print(f"PROBE FAIL {e!r}")
