import re
kb = '<content data-ragent-doc-id="d1">A</content>\n<content data-ragent-doc-id="d2">B</content>\n'

# 1:1 翻译 ragent regex
r = re.compile(r'(?m)^<content([^>]*) data-ragent-doc-id="([^"]*)">$')
print("findall (?m):", r.findall(kb))

# 把 (?m) 换成 flags
r2 = re.compile(r'^<content([^>]*) data-ragent-doc-id="([^"]*)">$', re.MULTILINE)
print("findall flags:", r2.findall(kb))

# 用 [\s\S] 兜底
r3 = re.compile(r'^<content([^>]*) data-ragent-doc-id="([^"]*)">$', re.MULTILINE | re.DOTALL)
print("findall DOTALL|MULTILINE:", r3.findall(kb))

# 简单调试：行级 match
for i, line in enumerate(kb.split("\n")):
    print(f"line{i}: {line!r}")
    if line:
        print(f"  match (?m):", r.match(line))
