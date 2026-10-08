from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


@dataclass
class EvaluationCase:
    question: str
    expected_answer: str = ""
    expected_context: list[str] = field(default_factory=list)


@dataclass
class EvaluationReport:
    total: int
    recall_at_k: float
    context_precision: float
    answer_overlap: float
    faithfulness: float
    cases: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)



def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(text) if token.strip()}



def _overlap(a: str, b: str) -> float:
    left = _tokens(a)
    right = _tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)



def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0



def evaluate_cases(cases: list[EvaluationCase], search_results: list[list[dict[str, Any]]], answers: list[str]) -> EvaluationReport:
    details = []
    recalls = []
    precisions = []
    overlaps = []
    faithfulness_scores = []

    for case, results, answer in zip(cases, search_results, answers):
        contexts = [str(item.get("content") or item.get("text") or "") for item in results]
        expected_hits = 0
        for expected in case.expected_context:
            if any(_overlap(expected, context) > 0.25 for context in contexts):
                expected_hits += 1
        recall = expected_hits / len(case.expected_context) if case.expected_context else 0.0
        precision = _mean([max((_overlap(context, expected) for expected in case.expected_context), default=0.0) for context in contexts]) if contexts else 0.0
        answer_overlap = _overlap(answer, case.expected_answer) if case.expected_answer else 0.0
        support = _mean([_overlap(sentence, "\n".join(contexts)) for sentence in re.split(r"[。.!?\n]+", answer) if sentence.strip()])

        recalls.append(recall)
        precisions.append(precision)
        overlaps.append(answer_overlap)
        faithfulness_scores.append(support)
        details.append({
            "question": case.question,
            "recall_at_k": recall,
            "context_precision": precision,
            "answer_overlap": answer_overlap,
            "faithfulness": support,
            "answer": answer,
            "retrieved": contexts,
        })

    return EvaluationReport(
        total=len(cases),
        recall_at_k=round(_mean(recalls), 4),
        context_precision=round(_mean(precisions), 4),
        answer_overlap=round(_mean(overlaps), 4),
        faithfulness=round(_mean(faithfulness_scores), 4),
        cases=details,
    )



def load_cases(path: Path) -> list[EvaluationCase]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("evaluation file must be a JSON array")
    return [EvaluationCase(**item) for item in data]


async def run_remote(base_url: str, cases: list[EvaluationCase], top_k: int, timeout: float) -> EvaluationReport:
    search_results = []
    answers = []
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        for case in cases:
            search_response = await client.post("/search", json={"query": case.question, "top_k": top_k})
            search_response.raise_for_status()
            search_results.append(search_response.json().get("results", []))

            answer_parts = []
            async with client.stream("POST", "/chat", json={"question": case.question}) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                            if event.get("done"):
                                break
                            answer_parts.append(str(event.get("token", "")))
                        except json.JSONDecodeError:
                            answer_parts.append(data.replace("\\n", "\n"))
            answers.append("".join(answer_parts))
    return evaluate_cases(cases, search_results, answers)


async def main_async(args: argparse.Namespace) -> int:
    cases = load_cases(Path(args.cases))
    report = await run_remote(args.base_url, cases, args.top_k, args.timeout)
    output = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        print(output)
    if args.min_recall is not None and report.recall_at_k < args.min_recall:
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate RAG search/chat endpoints.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--cases", required=True)
    parser.add_argument("--top-k", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--output")
    parser.add_argument("--min-recall", type=float)
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
