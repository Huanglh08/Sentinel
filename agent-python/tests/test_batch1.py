"""第一批修复的自检（纯 assert，无框架）。

运行方式（agent-python 目录下）：
    python tests/test_batch1.py

覆盖：
  1. _context_metrics：RAGAS context_precision/recall 文档级实现的数学正确性
     （E054 断裂层：检索对了但 RRF/精排/门控挤掉期望文档 → recall/precision 必须可见）。
  2. _aggregate_runs：稳定失败判定 = 严格多数（fails*2 > runs），
     单轮失败是时序噪声不计入（v8 E050/E052 误判教训）。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from eval.e2e_eval import E2EEvalSummary, _aggregate_runs, _context_metrics  # noqa: E402


def test_context_metrics():
    TOL = 5e-5  # _context_metrics 输出 round 到 4 位小数
    # 全命中且排名靠前：recall=1，precision 高
    p, r = _context_metrics(["LLM01"], ["LLM01_x", "LLM02_y"])
    assert r == 1.0, r
    assert abs(p - 1.0) < TOL, p  # 唯一命中排第 1 → (1/1)/1

    # 命中但排后：precision 随排名衰减
    p, r = _context_metrics(["LLM01"], ["LLM02_y", "LLM03_z", "LLM01_x"])
    assert r == 1.0
    assert abs(p - 1 / 3) < TOL, p  # 第 3 位命中 → (1/3)/1

    # E054 靶型：期望文档被挤出 sources → recall<1 且 precision 看得见
    p, r = _context_metrics(["LLM01", "XSS02"], ["LLM01_x", "LLM02_y", "LLM03_z"])
    assert abs(r - 0.5) < TOL, r
    assert abs(p - 0.5) < TOL, p  # LLM01 第 1 位命中 → (1/1)/2

    # 多命中 MAP 风格：LLM01@2, XSS02@4 → mean((1/2 + 2/4)) / 2 = 0.5
    p, r = _context_metrics(["LLM01", "XSS02"], ["A", "LLM01_x", "B", "XSS02_y"])
    assert r == 1.0
    assert abs(p - 0.5) < TOL, p

    # 全未命中：recall=0, precision=0（不是 None——明确可见的断裂）
    p, r = _context_metrics(["LLM01"], ["A", "B"])
    assert (p, r) == (0.0, 0.0)

    # 不适用：无期望或无 sources
    assert _context_metrics([], ["A"]) == (None, None)
    assert _context_metrics(["LLM01"], []) == (None, None)
    print("test_context_metrics: OK")


def test_aggregate_runs_stable_failures():
    @dataclass
    class _Case:
        question: str
        passed: bool

    @dataclass
    class _Sum:
        cases: list = field(default_factory=list)
        faithfulness_avg: float | None = 0.9

    # 3 轮：Q1 败 2 轮（2*2=4>3 严格多数 → 稳定失败）；Q2 败 1 轮（噪声）；Q3 全过
    s1 = _Sum([_Case("Q1", False), _Case("Q2", False), _Case("Q3", True)])
    s2 = _Sum([_Case("Q1", False), _Case("Q2", True), _Case("Q3", True)])
    s3 = _Sum([_Case("Q1", True), _Case("Q2", True), _Case("Q3", True)])
    agg, stable = _aggregate_runs([s1, s2, s3])
    qs = [sf["question"] for sf in stable]
    assert qs == ["Q1"], qs
    assert stable[0]["fails"] == 2 and stable[0]["runs"] == 3

    # 2 轮各败 1 次（1*2=2 不大于 2）→ 噪声，非稳定失败
    agg, stable = _aggregate_runs(
        [_Sum([_Case("Q1", False)]), _Sum([_Case("Q1", True)])]
    )
    assert stable == [], stable

    # 均值聚合存在且含 faithfulness_avg
    agg, _ = _aggregate_runs([s1, s2, s3])
    assert "faithfulness_avg" in agg and abs(agg["faithfulness_avg"]["mean"] - 0.9) < 1e-9
    print("test_aggregate_runs_stable_failures: OK")


if __name__ == "__main__":
    test_context_metrics()
    test_aggregate_runs_stable_failures()
    print("all batch1 checks passed")
