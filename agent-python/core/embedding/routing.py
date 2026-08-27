"""嵌入路由 + 降级执行器：严格对应 ragent ModelRoutingExecutor + RoutingEmbeddingService。

完整照搬 ragent 行为：
  - ModelRoutingExecutor.executeWithFallback(modelId, action)
    - 按 priority 升序构造候选链
    - 从 modelId 对应的候选开始（含）向后降级
    - 跳过熔断器拒绝的候选（累计 errors）
    - 成功后 recordSuccess
    - ModelClientError 后 recordFailure，AUTH_ERROR 直接抛（不降级）
    - 链全失败抛 NoAvailableModelException
  - RoutingEmbeddingService
    - 统一入口；model_id 为空时走 config.embedding_default_model
    - 调用对应 provider 的 client，并做维度校验
"""
from __future__ import annotations

from typing import Callable, TypeVar

from core.embedding.base import BaseEmbeddingClient, EmbeddingResult, ModelTarget
from core.embedding.config import EmbeddingConfig, ModelCandidate
from core.embedding.errors import ModelClientError, ModelClientErrorType
from core.embedding.health import ModelHealthStore

T = TypeVar("T")


class NoAvailableModelException(RuntimeError):
    """严格对应 ragent NoAvailableModelException：所有候选都失败。"""

    def __init__(self, errors: list[Exception]) -> None:
        msg = f"所有 embedding 候选都失败（共 {len(errors)} 个）"
        super().__init__(msg)
        self.errors = errors


class SkippedModelException(RuntimeError):
    """熔断器拒绝的候选——不算"真失败"，不计入熔断统计。"""

    def __init__(self, model_id: str) -> None:
        super().__init__(f"模型被熔断器跳过: {model_id}")
        self.model_id = model_id


class ModelRoutingExecutor:
    """降级执行器——严格对应 ragent infra-ai.model.ModelRoutingExecutor。

    持有：
      - config: 候选链 + provider 路由表
      - health_store: 熔断器
      - clients: provider id -> BaseEmbeddingClient 映射
    """

    DEFAULT_TIMEOUT_MS: int = 30_000

    def __init__(
        self,
        config: EmbeddingConfig,
        health_store: ModelHealthStore,
        clients: dict[str, BaseEmbeddingClient],
    ) -> None:
        self.config = config
        self.health_store = health_store
        self.clients = clients

    # ---------------- public API ----------------

    def execute_with_fallback(
        self,
        model_id: str,
        action: Callable[[ModelTarget], T],
    ) -> T:
        """对应 ragent ModelRoutingExecutor.executeWithFallback()。"""
        chain = self._build_fallback_chain(model_id)
        if not chain:
            raise NoAvailableModelException([])

        errors: list[Exception] = []

        for candidate in chain:
            target = self._build_target(candidate)

            # 严格对齐 ragent：先 isUnavailable 判"应跳过"，再 allowCall 取 permit
            if self.health_store.is_unavailable(target.id):
                errors.append(SkippedModelException(target.id))
                continue
            permit = self.health_store.allow_call(target.id)

            try:
                result = action(target)
                self.health_store.record_success(target.id, permit)
                return result
            except ModelClientError as e:
                self.health_store.record_failure(target.id, permit, e)
                if e.error_type == ModelClientErrorType.UNAUTHORIZED:
                    # 鉴权错：致命，不降级（与 ragent isFatal 行为一致）
                    raise
                errors.append(e)
            except Exception as e:  # noqa: BLE001
                # 业务/代码异常：不计入熔断，但阻止后续降级（让上层看见）
                raise

        raise NoAvailableModelException(errors)

    # ---------------- internal helpers ----------------

    def _build_fallback_chain(self, model_id: str) -> list[ModelCandidate]:
        """对应 ragent ModelRoutingExecutor.buildFallbackChain()。

        语义：按 priority 升序的所有候选 + 从 modelId 对应位置开始。
        modelId 找不到时从 priority=1（首选）开始。
        """
        sorted_cands = self.config.candidates_sorted()
        if not sorted_cands:
            return []

        idx = 0
        for i, c in enumerate(sorted_cands):
            if c.id == model_id:
                idx = i
                break
        return sorted_cands[idx:]

    def _build_target(self, candidate: ModelCandidate) -> ModelTarget:
        """对应 ragent ModelRoutingExecutor.buildTarget()。"""
        provider = self.config.get_provider(candidate.provider)
        return ModelTarget(
            id=candidate.id,
            candidate=candidate,
            provider=provider,
            timeout_ms=self.DEFAULT_TIMEOUT_MS,
        )


class RoutingEmbeddingService:
    """统一嵌入入口——严格对应 ragent RoutingEmbeddingService（@Primary）。

    用法：
        svc = RoutingEmbeddingService(config, health, executor)
        vec = svc.embed("hello")                     # 用默认模型
        vecs = svc.embed_batch(["a", "b"], "qwen-emb-local")  # 指定候选
    """

    def __init__(
        self,
        config: EmbeddingConfig,
        health_store: ModelHealthStore,
        executor: ModelRoutingExecutor,
    ) -> None:
        self._config = config
        self._health = health_store
        self._executor = executor

    def embed(self, text: str, model_id: str | None = None) -> list[float]:
        """单文本嵌入——返回向量，model_id 为空时走默认。"""
        result = self.embed_batch([text], model_id)
        return result.vectors[0]

    def embed_batch(
        self,
        texts: list[str],
        model_id: str | None = None,
    ) -> EmbeddingResult:
        """批量嵌入——走完整降级链 + 维度校验。"""
        if not texts:
            empty_dim = self._config.embedding_dim
            return EmbeddingResult(
                vectors=[],
                model_id=model_id or self._config.embedding_default_model,
                provider="",
                dimension=empty_dim,
            )

        resolved_id = model_id or self._config.embedding_default_model
        return self._executor.execute_with_fallback(resolved_id, lambda t: self._dispatch(t, texts))

    # ---------------- internal ----------------

    def _dispatch(self, target: ModelTarget, texts: list[str]) -> EmbeddingResult:
        """找对应 client → 调嵌入 → 维度校验 → 包装结果。"""
        client = self._executor.clients.get(target.candidate.provider)
        if client is None:
            raise ModelClientError(
                f"未注册 provider client: {target.candidate.provider}",
                error_type=ModelClientErrorType.CLIENT_ERROR,
            )

        vectors = client.embed_batch(texts, target)
        expected_dim = target.candidate.dimension
        for i, vec in enumerate(vectors):
            if len(vec) != expected_dim:
                raise ModelClientError(
                    f"向量维度不匹配: provider={target.candidate.provider} "
                    f"model={target.candidate.model} expected={expected_dim} got={len(vec)} index={i}",
                    error_type=ModelClientErrorType.INVALID_RESPONSE,
                )

        return EmbeddingResult(
            vectors=vectors,
            model_id=target.id,
            provider=target.candidate.provider,
            dimension=expected_dim,
        )

    # ---------------- lifecycle helpers ----------------

    def health(self) -> ModelHealthStore:
        return self._health
