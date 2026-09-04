"""双模型 rerank 相关性门控（P1 负样本治理）——硅基流动 API 版。

背景：负样本（与知识库无关的问题）在向量/全文检索下仍会召回"高分垃圾"chunk，
且百炼 gte-rerank 对其相关性打分虚高，导致下游基于垃圾上下文作答。
本模块通过硅基流动 rerank API（POST /v1/rerank）用两个异构模型打分：

  - BAAI/bge-reranker-v2-m3：判别式序列分类器（语义精确匹配敏感）
  - Qwen/Qwen3-Reranker-0.6B：生成式判官（语义泛化敏感）

当两者对最优 chunk 的相关性判定**同时**低于阈值（AND 门控）时，判定
"库中大概率无相关信息"，建议上层切空检索结果、走"库中无此信息"拒答。
双模型 AND 是刻意的保守设计：单模型低分常见于表述差异误判，双低才切。

设计约束：
  - fail-open：API key 未配置/超时/异常 → triggered=False，绝不因门控故障误拒
  - 分数量纲统一 [0,1]：硅基流动 relevance_score 即 [0,1] 相关性概率
  - 打分只需 top1 判定，默认只对前 top_n 个 chunk 打分以控 token 成本
  - 零本地模型依赖：不加载 torch/transformers，进程内存占用 ~0（勿再走本地权重路线）
"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# 硅基流动 rerank API（与 embedding 同域，复用 SILICONFLOW_API_KEY）
BASE_URL = os.environ.get("RERANK_GATE_BASE_URL") or "https://api.siliconflow.cn/v1/rerank"
BGE_MODEL = os.environ.get("RERANK_GATE_BGE_MODEL") or "BAAI/bge-reranker-v2-m3"
QWEN3_MODEL = os.environ.get("RERANK_GATE_QWEN3_MODEL") or "Qwen/Qwen3-Reranker-0.6B"

# 默认门控阈值（2026-09-01 离线校准实测 rerank-gate-20260901-185423，99正/23负）：
# bge 负样本 16/23 落 [0,0.02)、正样本仅 15/99 落同桶，单独判据不可用，必须 AND；
# qwen3 分离度最好（正样本 76/99 落 [0.8,1]）。(0.10,0.20)=切空 82.6%/误伤 12.1%，
# 同切空率下优于 (0.10,0.40)=误伤 14.1%，取 0.20
DEFAULT_BGE_THRESHOLD = 0.10
DEFAULT_QWEN3_THRESHOLD = 0.20
DEFAULT_TOP_N = 8

# 超时收紧（原 15s）：top_n=8 短文本 reranker API 正常 <1s，4s 已容纳 P99；
# 长尾主要来自 API 抖动 × 15s 超时 × 串行阶段叠加（实测 44-48s 拒答），
# 快速失败走 fail-open 比长时间等待更符合门控定位。
DEFAULT_TIMEOUT_S = 4.0
# 单条 document 超 API 限制会被拒，保守截断（chunk 分块下极少触达）
_MAX_DOC_CHARS = 4000


def _load_api_key() -> str:
    """key 解析顺序：RERANK_GATE_API_KEY → SILICONFLOW_API_KEY → agent-python/.env。"""
    for name in ("RERANK_GATE_API_KEY", "SILICONFLOW_API_KEY"):
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    # 回退 .env（服务以 agent-python 为 CWD 启动，用户级环境变量兜底场景）
    here = Path(__file__).resolve()
    for env_file in (here.parents[2] / ".env", here.parents[3] / "agent-python" / ".env"):
        try:
            if not env_file.is_file():
                continue
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                if key.strip().lower() == "siliconflow_api_key":
                    val = val.strip().strip('"').strip("'")
                    if val:
                        return val
        except OSError:
            continue
    return ""


@dataclass
class GateResult:
    """单次门控判定结果。"""

    triggered: bool
    bge_top1: float = 0.0
    qwen3_top1: float = 0.0
    bge_scores: list[float] = field(default_factory=list)
    qwen3_scores: list[float] = field(default_factory=list)
    scored: int = 0
    error: str | None = None  # 非 None 表示 fail-open（异常原因）

    def to_dict(self) -> dict:
        return {
            "triggered": self.triggered,
            "bge_top1": round(self.bge_top1, 6),
            "qwen3_top1": round(self.qwen3_top1, 6),
            "bge_scores": [round(s, 6) for s in self.bge_scores],
            "qwen3_scores": [round(s, 6) for s in self.qwen3_scores],
            "scored": self.scored,
            "error": self.error,
        }


class DualRerankGate:
    """bge × qwen3 双模型 AND 门控（硅基流动 API）。线程安全（httpx.Client 连接池复用）。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = BASE_URL,
        bge_model: str = BGE_MODEL,
        qwen3_model: str = QWEN3_MODEL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key if api_key is not None else _load_api_key()
        self.base_url = base_url.rstrip("/")
        self.bge_model = bge_model
        self.qwen3_model = qwen3_model
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._client: httpx.Client | None = None

    # ------------------------------------------------------------------ 底层
    def _ensure_client(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                self._client = httpx.Client(timeout=self._timeout_s)
            return self._client

    def _score_one(self, model: str, question: str, texts: list[str]) -> list[float]:
        """单模型打分：返回与 texts 等长、按原始顺序对齐的分数列表；失败抛异常（上层 fail-open）。"""
        client = self._ensure_client()
        resp = client.post(
            self.base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": model,
                "query": question,
                "documents": [t[:_MAX_DOC_CHARS] for t in texts],
                "top_n": len(texts),
                "return_documents": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results") or []
        if not results:
            # 200 但 results 为空 = 异常响应（限流软拒绝/过载/格式变化）。
            # 静默返回全 0 分会让 AND 门控必然切空、架空 fail-open，必须抛异常走 fail-open。
            raise ValueError(
                f"rerank api returned empty results (model={model}, status={resp.status_code})"
            )
        scores = [0.0] * len(texts)
        for item in results:
            idx = int(item["index"])
            if 0 <= idx < len(scores):
                scores[idx] = float(item.get("relevance_score", 0.0))
        return scores

    # ------------------------------------------------------------------ 打分
    def score(self, question: str, texts: list[str], top_n: int = DEFAULT_TOP_N):
        """对前 top_n 个 chunk 双模型并行打分。

        返回 (bge_scores, qwen3_scores, error)；error 非 None 表示 fail-open。
        """
        picked = [t for t in texts if t][:top_n]
        if not picked:
            return [], [], None
        if not self._api_key:
            msg = "gate api key not configured (set SILICONFLOW_API_KEY or RERANK_GATE_API_KEY)"
            logger.warning("DualRerankGate skip (fail-open): %s", msg)
            return [], [], msg
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                bge_future = pool.submit(self._score_one, self.bge_model, question, picked)
                qwen3_future = pool.submit(self._score_one, self.qwen3_model, question, picked)
                bge_scores = bge_future.result()
                qwen3_scores = qwen3_future.result()
            return bge_scores, qwen3_scores, None
        except Exception as exc:  # noqa: BLE001 —— API 异常同样 fail-open
            logger.warning("DualRerankGate score failed (fail-open): %s", exc)
            return [], [], f"score failed: {exc}"

    def score_qwen3(self, question: str, texts: list[str], top_n: int = DEFAULT_TOP_N):
        """仅 qwen3 打分（bge 分数已由 Rerank 阶段复用时的惰性确认路径）。

        AND 门控语义下，bge_top1 达阈值即确定放行，qwen3 只在 bge 低置信
        （可能切空）时才需要调用。返回 (qwen3_scores, error)。
        """
        picked = [t for t in texts if t][:top_n]
        if not picked:
            return [], None
        if not self._api_key:
            msg = "gate api key not configured (set SILICONFLOW_API_KEY or RERANK_GATE_API_KEY)"
            logger.warning("DualRerankGate skip (fail-open): %s", msg)
            return [], msg
        try:
            return self._score_one(self.qwen3_model, question, picked), None
        except Exception as exc:  # noqa: BLE001
            logger.warning("DualRerankGate qwen3 score failed (fail-open): %s", exc)
            return [], f"qwen3 score failed: {exc}"

    # ------------------------------------------------------------------ 门控
    def gate(
        self,
        question: str,
        texts: list[str],
        bge_threshold: float = DEFAULT_BGE_THRESHOLD,
        qwen3_threshold: float = DEFAULT_QWEN3_THRESHOLD,
        top_n: int = DEFAULT_TOP_N,
        bge_scores: list[float] | None = None,
    ) -> GateResult:
        """AND 门控：bge_top1 < bge_threshold 且 qwen3_top1 < qwen3_threshold 才切空。

        bge_scores 非 None 时复用 Rerank 阶段的 bge 分数（同模型同候选，避免重复
        API 调用）；惰性确认：bge_top1 已达阈值时 AND 门控必不触发，跳过 qwen3 调用。
        """
        if not texts:
            return GateResult(triggered=False, scored=0)
        if bge_scores is not None:
            if not bge_scores:
                return GateResult(triggered=False, error="empty bge scores")
            bge_top1 = max(bge_scores)
            scored = min(len(bge_scores), top_n)
            if bge_top1 >= bge_threshold:
                # bge 已高置信：AND 门控不可能触发，无需 qwen3 确认（-1 表示未打分）
                return GateResult(
                    triggered=False, scored=scored,
                    bge_top1=bge_top1, qwen3_top1=-1.0,
                )
            qwen3_scores, err = self.score_qwen3(question, texts, top_n=top_n)
            if err or not qwen3_scores:
                return GateResult(triggered=False, error=err or "no qwen3 scores")
            qwen3_top1 = max(qwen3_scores)
            triggered = qwen3_top1 < qwen3_threshold
            return GateResult(
                triggered=triggered, scored=scored,
                bge_top1=bge_top1, qwen3_top1=qwen3_top1,
            )
        bge_scores, qwen3_scores, err = self.score(question, texts, top_n=top_n)
        if err or not bge_scores or not qwen3_scores:
            return GateResult(triggered=False, error=err or "no scores")
        bge_top1 = max(bge_scores)
        qwen3_top1 = max(qwen3_scores)
        triggered = bge_top1 < bge_threshold and qwen3_top1 < qwen3_threshold
        return GateResult(
            triggered=triggered,
            bge_top1=bge_top1,
            qwen3_top1=qwen3_top1,
            bge_scores=bge_scores,
            qwen3_scores=qwen3_scores,
            scored=len(bge_scores),
        )


# 进程内单例（懒构造）
_default_gate: DualRerankGate | None = None
_gate_lock = threading.Lock()


def get_default_gate() -> DualRerankGate:
    global _default_gate
    if _default_gate is None:
        with _gate_lock:
            if _default_gate is None:
                _default_gate = DualRerankGate()
    return _default_gate
