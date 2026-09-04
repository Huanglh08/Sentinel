"""CasePatroller —— 定时巡检批处理（Phase 4）。

确定性调度与 Agent 推理分离（业界依据: Tracecat 三概念 —— 「确定性的动作交给
Workflow，Agent 只做推理与调度」; Vigil 预置工作流「分诊->调查->响应->报告」一条龙）:

  调度层（本类, 确定性）: 定时拉线索 -> 指纹幂等去重 -> 建案 -> 逐案走既有调查管线
  Agent 层（既有 CaseInvestigationAgent）: 单案推理, 不感知调度

三个工程护栏（SOAR 通行实践，调研报告未详述，按标准做法落地）:
- 幂等: fingerprint = sha256(source|clue)，t_case.fingerprint 部分唯一索引兜底防并发重复建案;
- 单飞: asyncio.Lock，上一轮未结束直接跳过本轮（防堆积）;
- 背压: CASE_PATROL_BATCH_LIMIT 单批建案上限（防 LLM 预算被巡检挤爆）。

调度使用 APScheduler（BSD-3，Python 事实标准的调度轮子）的 AsyncIOScheduler，
复用 uvicorn 既有事件循环，不新增线程/进程模型。CASE_PATROL_INTERVAL<=0（默认）
时不启动调度器 —— 巡检是纯增量可选能力，不影响既有部署。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# 线索提供者: async callable() -> list[str | dict]
# dict 形如 {"clue": "线索文本", "source": "告警源标识"}；真实告警队列接入时注入实现即可。
ClueProvider = Callable[[], Awaitable[list[Any]]]


def default_clue_provider() -> list[dict]:
    """默认线索源：读 CASE_PATROL_CLUES 环境变量（JSON 数组），未配置返回空。

    真实化阶段把告警队列/MCP 查询封装成 ClueProvider 注入即可，无需改本模块。
    """
    import json

    raw = os.getenv("CASE_PATROL_CLUES", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        logger.warning("[patrol] CASE_PATROL_CLUES 不是合法 JSON 数组，忽略")
        return []


class CasePatroller:
    """巡检器：拉线索 -> 幂等去重 -> 复用调查管线建案。"""

    def __init__(
        self,
        case_agent,
        store,
        clue_provider: ClueProvider | None = None,
        batch_limit: int | None = None,
    ) -> None:
        # case_agent 可以是实例，也可以是零参工厂（lifespan 装配时 Agent 尚未懒加载）
        self._agent_or_factory = case_agent
        self.store = store
        self.clue_provider = clue_provider
        try:
            self.batch_limit = int(
                batch_limit if batch_limit is not None else os.getenv("CASE_PATROL_BATCH_LIMIT", "3")
            )
        except ValueError:
            self.batch_limit = 3
        self.batch_limit = max(1, self.batch_limit)
        self._lock = asyncio.Lock()
        self._scheduler: Any = None

    @property
    def agent(self):
        agent = self._agent_or_factory() if callable(self._agent_or_factory) else self._agent_or_factory
        if agent is None:
            raise RuntimeError("案件调查 Agent 尚未初始化（首个请求后可用）")
        return agent

    @staticmethod
    def fingerprint(source: str, clue: str) -> str:
        """线索指纹：同源同文线索全生命周期只建一次案。"""
        return hashlib.sha256(f"{source}|{clue.strip()}".encode("utf-8")).hexdigest()

    async def patrol_once(self, clues_override: list[Any] | None = None) -> dict:
        """执行一轮巡检。clues_override 供手动触发接口直接传线索（便于测试/演示）。"""
        if self._lock.locked():
            return {"started": False, "message": "上一轮巡检仍在进行（单飞锁跳过）"}
        async with self._lock:
            if clues_override is not None:
                clues = list(clues_override)
            elif self.clue_provider is not None:
                clues = list(await self.clue_provider() or [])
            else:
                clues = list(default_clue_provider())
            if not clues:
                return {
                    "started": True,
                    "clues": 0,
                    "created": [],
                    "skipped_dup": 0,
                    "message": "无线索可巡检（未配置线索源时设置 CASE_PATROL_CLUES 或注入 clue_provider）",
                }
            created: list[dict] = []
            skipped_dup = 0
            for item in clues[: self.batch_limit]:
                if isinstance(item, dict):
                    clue_text = str(item.get("clue", ""))
                    source = str(item.get("source", "PATROL"))
                else:
                    clue_text = str(item)
                    source = "PATROL"
                if not clue_text.strip():
                    continue
                fp = self.fingerprint(source, clue_text)
                # 指纹去重必须与建案同源（agent.store）：DB 降级为内存模式时，
                # 独立 store 实例互相不可见，用 self.store 查会去重失效、每轮重复建案
                if self.agent.store.case_exists_by_fingerprint(fp):
                    skipped_dup += 1
                    logger.info("[patrol] 线索指纹已存在，跳过建案 fp=%s…", fp[:12])
                    continue
                events: list[dict] = []
                try:
                    async for ev in self.agent.investigate(
                        clue_text, user_id="patrol", source=source, fingerprint=fp
                    ):
                        events.append(ev)
                except Exception:  # noqa: BLE001
                    logger.exception("[patrol] 案件调查失败 clue=%s…", clue_text[:40])
                    continue
                end = next((e for e in reversed(events) if e.get("type") == "end"), None) or {}
                created.append(
                    {
                        "fingerprint": fp,
                        "case_id": end.get("case_id", ""),
                        "status": end.get("status", ""),
                        "clue": clue_text[:80],
                    }
                )
            logger.info(
                "[patrol] 巡检完成: 线索 %d, 建案 %d, 幂等跳过 %d（单批上限 %d）",
                len(clues), len(created), skipped_dup, self.batch_limit,
            )
            return {
                "started": True,
                "clues": len(clues),
                "created": created,
                "skipped_dup": skipped_dup,
                "batch_limit": self.batch_limit,
                "message": "",
            }

    # ------------------------------------------------------------------
    # 调度：APScheduler AsyncIOScheduler（复用事件循环；interval<=0 不启动）
    # ------------------------------------------------------------------
    def start(self) -> bool:
        try:
            interval = float(os.getenv("CASE_PATROL_INTERVAL", "0") or "0")
        except ValueError:
            interval = 0.0
        if interval <= 0:
            logger.info("[patrol] CASE_PATROL_INTERVAL 未开启（<=0），巡检调度器不启动")
            return False
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        self._scheduler = AsyncIOScheduler()
        # max(1.0, ...) 防小数配置（如 0.5）被 int() 截断为 0 后被 APScheduler 自动抬成 1s 高频轮询
        self._scheduler.add_job(
            self.patrol_once,
            IntervalTrigger(seconds=int(max(1.0, interval))),
            id="case_patrol",
            max_instances=1,  # 与单飞锁双保险
            coalesce=True,  # 积压多轮合并为一轮（背压）
        )
        self._scheduler.start()
        logger.info(
            "[patrol] 巡检调度器已启动 interval=%ss batch_limit=%d", int(interval), self.batch_limit
        )
        return True

    def shutdown(self) -> None:
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                logger.warning("[patrol] 调度器关闭异常（忽略）")
            self._scheduler = None
