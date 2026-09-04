"""把意图树定义转成 intent 种子生成所需的清单。

意图树（evaluation/intent-tree/risk-security-intent-tree.json）是导入系统用的完整三层结构，
而种子生成器只需要叶子（TOPIC, level=2）的 {intent_code, name, description, examples}。

用法：
    cd agent-python
    python -m eval.intent_tree_to_seedspec
    python -m eval.intent_tree_to_seedspec --only-kind kb      # 只要 KB 类（排除系统交互）
    python -m eval.intent_tree_to_seedspec --out <path>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TREE = _REPO_ROOT / "evaluation" / "intent-tree" / "risk-security-intent-tree.json"
DEFAULT_OUT = _REPO_ROOT / "evaluation" / "seeds" / "intent-spec.json"


def load_leaves(path: Path, *, only_kind: str | None = None) -> list[dict]:
    tree = json.loads(path.read_text(encoding="utf-8"))
    kind_map = {"kb": 0, "system": 1, "mcp": 2}
    want_kind = kind_map.get(only_kind) if only_kind else None

    leaves: list[dict] = []
    for n in tree["nodes"]:
        if n.get("level") != 2:
            continue
        if want_kind is not None and n.get("kind") != want_kind:
            continue
        leaves.append(
            {
                "intent_code": n["intent_code"],
                "name": n["name"],
                "description": n.get("description", ""),
                "examples": n.get("examples") or [],
                "doc_hint": n.get("doc_hint") or [],
                "parent_code": n.get("parent_code"),
            }
        )
    return leaves


def main() -> int:
    ap = argparse.ArgumentParser(description="意图树 → intent 种子规格清单")
    ap.add_argument("--tree", default=str(DEFAULT_TREE))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--only-kind", choices=["kb", "system", "mcp"], default=None)
    args = ap.parse_args()

    leaves = load_leaves(Path(args.tree), only_kind=args.only_kind)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(leaves, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"叶子意图 {len(leaves)} 个 -> {out}")
    for lv in leaves:
        print(f"  - {lv['intent_code']:<34} examples={len(lv['examples'])} docs={len(lv['doc_hint'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
