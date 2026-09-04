"""重置种子文件：只保留 id 数值 <= keep-max 的记录（通常是手工标注那批）。

存在原因：PowerShell 5.1 里用 `python -c '...'` 传含引号的脚本会被吃掉引号导致语法错误，
因此把重置逻辑做成独立脚本文件，由 run-seedgen.ps1 调用。

用法：
    python -m eval.reset_seeds --file ../evaluation/seeds/retrieval-seeds.jsonl --prefix R --keep-max 20
    python -m eval.reset_seeds --file ../evaluation/seeds/e2e-seeds.jsonl --prefix E --keep-max 16 --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def reset(path: Path, prefix: str, keep_max: int, dry_run: bool = False) -> tuple[int, int]:
    if not path.exists():
        print(f"[reset] 文件不存在，跳过: {path}")
        return 0, 0

    rows = []
    with path.open(encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    def keep(row: dict) -> bool:
        rid = str(row.get("id", ""))
        m = re.match(rf"^{re.escape(prefix)}(\d+)$", rid)
        return bool(m) and int(m.group(1)) <= keep_max

    kept = [r for r in rows if keep(r)]

    if dry_run:
        print(f"[reset] dry-run: {len(rows)} -> {len(kept)}（不写盘）")
        return len(rows), len(kept)

    backup = path.with_suffix(path.suffix + ".bak")
    backup.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    with path.open("w", encoding="utf-8") as fp:
        for r in kept:
            fp.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[reset] {path.name}: {len(rows)} -> {len(kept)}（备份 {backup.name}）")
    return len(rows), len(kept)


def main() -> int:
    ap = argparse.ArgumentParser(description="重置种子文件到手工标注基线")
    ap.add_argument("--file", required=True)
    ap.add_argument("--prefix", required=True, help="id 前缀，如 R / E")
    ap.add_argument("--keep-max", type=int, required=True, help="保留 id 数值 <= 该值的记录")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    reset(Path(args.file), args.prefix, args.keep_max, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
