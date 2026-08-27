"""从业务知识库文档生成 seed 评估集。

读取 backend-java/resources/docs/knowledge/biz/ 下的 .md 文档,
调用百炼 (DashScope) LLM 抽取 5-10 条用户问题 (含期望 intent_l2),
输出到 agent-python/eval/data/seed-intent.jsonl。

运行:
    python -m eval.build_seed
或:
    python -m eval.build_seed --biz-dir <path> --output <path> --questions-per-doc 8
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import dashscope
from dashscope import Generation

# ---------- Prompt 模板 ----------

SEED_PROMPT = """你是企业知识库评测工程师。下面是来自《{doc_title}》的一段业务规范内容：

```
{doc_excerpt}
```

请基于这段内容,**生成 {n_questions} 个用户可能提出的事实型问题**。
要求:
1. 问题必须是用户视角的、口语化的、5-30 字之间;
2. 每个问题要能在文档里直接找到答案(不要问泛泛的开放问题);
3. 对每个问题,给出:
   - "question": 问题原文
   - "intent_hint": 问题的意图分类提示,英文下划线,3 段式 a.b.c 形式,
     例如 "oa.security.encrypt" / "insurance.security.audit" /
     "oa.permission.role"。前缀 oa / insurance 是业务域,后两段是该域内的子分类;
   - "reference_doc_id": 源文档的文件名(不含后缀)。本文件名是 "{doc_id}"
4. 输出一个 JSON 数组,不要任何解释、不要 Markdown 代码块包裹。
"""

# 业务域前缀映射(根据文档名自动推断)
_DOMAIN_PREFIXES = {
    "OA系统数据安全规范文档": "oa.security",
    "互联网保险系统数据安全规范": "insurance.security",
}


def _detect_domain_prefix(doc_id: str) -> str:
    """根据文档名推断业务域前缀。"""
    for key, prefix in _DOMAIN_PREFIXES.items():
        if key in doc_id:
            return prefix
    return "general.unknown"


def _extract_doc_id(file_path: Path) -> str:
    """从文件路径提取 doc_id(去后缀,保留中文)。"""
    return file_path.stem


def _truncate(text: str, max_chars: int = 6000) -> str:
    """避免 prompt 过长,超出 LLM 上下文限制。"""
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    return head + "\n\n... (内容过长已截断) ...\n\n" + tail


def _parse_llm_json(content: str) -> list[dict[str, Any]]:
    """百炼可能返回带 Markdown 围栏的 JSON,容错解析。"""
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", content, re.S)
        if not m:
            raise ValueError(f"LLM 输出无法解析为 JSON: {content[:200]}")
        data = json.loads(m.group(0))
    if not isinstance(data, list):
        raise ValueError(f"LLM 输出不是数组: {type(data)}")
    return data


def _generate_questions_for_doc(
    file_path: Path,
    n_questions: int,
    model: str,
) -> list[dict[str, Any]]:
    """对单个文档调用百炼生成 seed 问题。"""
    text = file_path.read_text(encoding="utf-8")
    doc_id = _extract_doc_id(file_path)
    domain_prefix = _detect_domain_prefix(doc_id)
    excerpt = _truncate(text)

    prompt = SEED_PROMPT.format(
        doc_title=doc_id,
        doc_excerpt=excerpt,
        n_questions=n_questions,
        doc_id=doc_id,
    )

    response = Generation.call(
        model=model,
        prompt=prompt,
        result_format="message",
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"百炼调用失败 [{response.status_code}]: {response.message}"
        )

    content = response.output.choices[0].message.content
    raw_items = _parse_llm_json(content)

    normalized: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question", "")).strip()
        intent_hint = str(item.get("intent_hint", "")).strip()
        ref_doc = str(item.get("reference_doc_id", doc_id)).strip() or doc_id
        if not question or not intent_hint:
            continue
        if not intent_hint.startswith(domain_prefix):
            intent_hint = f"{domain_prefix}.{intent_hint}" if "." not in intent_hint else intent_hint
        normalized.append({
            "q": question,
            "intent_l2": intent_hint,
            "expected_doc_ids": [ref_doc],
        })
    return normalized


def build_seed(
    biz_dir: Path,
    output: Path,
    n_per_doc: int,
    model: str,
    api_key: str,
) -> int:
    """主入口:遍历 biz_dir 下 .md,逐个生成 seed,写入 jsonl。"""
    dashscope.api_key = api_key

    md_files = sorted(biz_dir.rglob("*.md"))
    if not md_files:
        print(f"[build_seed] 警告:{biz_dir} 下没有 .md 文件", file=sys.stderr)
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    with output.open("w", encoding="utf-8") as fp:
        for md in md_files:
            print(f"[build_seed] 处理 {md.name} ...", file=sys.stderr)
            try:
                items = _generate_questions_for_doc(md, n_per_doc, model)
            except Exception as e:
                print(f"[build_seed] 失败 {md.name}: {e}", file=sys.stderr)
                continue
            for it in items:
                fp.write(json.dumps(it, ensure_ascii=False) + "\n")
                total += 1
            print(
                f"[build_seed]   {md.name} -> {len(items)} 条 (累计 {total})",
                file=sys.stderr,
            )

    print(f"[build_seed] 写入 {total} 条 seed 到 {output}", file=sys.stderr)
    return total


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent.parent
    default_biz = repo_root / "backend-java" / "resources" / "docs" / "knowledge" / "biz"
    default_output = Path(__file__).resolve().parent / "data" / "seed-intent.jsonl"

    parser = argparse.ArgumentParser(description="从业务知识库生成 seed 评估集")
    parser.add_argument("--biz-dir", default=str(default_biz), help="业务文档目录")
    parser.add_argument("--output", default=str(default_output), help="输出 jsonl 路径")
    parser.add_argument("--questions-per-doc", type=int, default=8, help="每个文档生成多少条")
    parser.add_argument(
        "--model",
        default=os.environ.get("BAILIAN_MODEL", "qwen-plus"),
        help="百炼模型名(默认 qwen-plus)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("BAILIAN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", ""),
        help="百炼 API Key(优先读环境变量 BAILIAN_API_KEY,其次 DASHSCOPE_API_KEY,最后 --api-key)",
    )
    args = parser.parse_args()

    if not args.api_key:
        print(
            "[build_seed] 错误:未提供 API Key。请设置环境变量 BAILIAN_API_KEY 或 --api-key",
            file=sys.stderr,
        )
        return 2

    return build_seed(
        biz_dir=Path(args.biz_dir),
        output=Path(args.output),
        n_per_doc=args.questions_per_doc,
        model=args.model,
        api_key=args.api_key,
    )


if __name__ == "__main__":
    raise SystemExit(main())
