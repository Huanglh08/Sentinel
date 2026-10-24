"""文档多样性保底后处理（ragas-v2c 四成分诊断·成分2修复）。

问题机制：多意图综合查询的检索窗口（context_top_k）按全局相关性排座，
相关块多且分数高的强势文档霸占席位，第二篇文档只能分到 0~1 个边缘块
甚至完全缺席，综合题一半的料丢失（诊断：M027 等 7 条 M 域题 ctx_r=0）。

策略：为 top_k 窗口中「完全缺席的文档」做保底补位——逐出窗口内
「同文档已占多席」的最弱一席（按重排序取该文档最后一席）。
- 单文档查询（绝大多数）：无缺席文档，零改动，零回归风险。
- 补位块随全队继续过 RelevanceGate（order=15），相关性由 gate 统一裁决，
  不绕过负样本治理。
- 最多补 2 席，防极端多缺席场景把窗口打散。
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)

# 单次补位上限：M 域诊断显示缺席文档通常 1 个，2 席已覆盖极端情形
MAX_BACKUP_SLOTS = 2


def doc_diversity_backup(
    final: list[Any],
    candidates: list[Any],
    top_k: int,
    *,
    log_tag: str = "DocDiversity",
) -> list[Any]:
    """纯函数：为 final 窗口中完全缺席的文档做保底补位。

    final: 重排截断后的最终窗口（保持原顺序，保底块追加尾部，不按分重排——
           候选池分数是 RRF/向量口径，与 rerank 分数不可比，重排会污染序语义）
    candidates: 重排前候选池（各通道原始块，允许含重复，按 id 去重取最高分）
    """
    if not final or not candidates or len(final) < 2:
        return final
    final_ids = {c.id for c in final}
    seats: dict[str, int] = {}
    for c in final:
        seats[c.doc_id] = seats.get(c.doc_id, 0) + 1
    present = set(seats)
    # 缺席文档的最高分候选（候选池内同口径比较，可按 score 排序）
    best: dict[str, Any] = {}
    for c in candidates:
        if c.doc_id in present or c.id in final_ids:
            continue
        prev = best.get(c.doc_id)
        if prev is None or c.score > prev.score:
            best[c.doc_id] = c
    if not best:
        return final
    out = list(final)
    added = 0
    for doc_id, backup in sorted(best.items(), key=lambda kv: -kv[1].score):
        if added >= MAX_BACKUP_SLOTS:
            break
        if len(out) < top_k:
            _logger.info(
                "[%s] 窗口未满，直接补入缺席文档块: %s#%s(score=%.4f)",
                log_tag, backup.doc_id, backup.id, backup.score,
            )
            out.append(backup)
            seats[doc_id] = seats.get(doc_id, 0) + 1
            added += 1
            continue
        # 逐出对象：席位最多的文档（≥2 席）的最后一席（重排序内最弱席）
        top_doc = ""
        top_seats = 1
        for d, n in seats.items():
            if n >= 2 and n > top_seats:
                top_doc, top_seats = d, n
        victim_idx = -1
        victim_doc = ""
        victim_seats = 0
        if top_doc:
            for i, c in enumerate(out):
                if c.doc_id == top_doc:
                    victim_idx, victim_doc, victim_seats = i, top_doc, top_seats
        if victim_idx < 0:
            break  # 无多席文档可逐，宁缺勿滥（不逐任何文档的唯一席位）
        victim = out[victim_idx]
        _logger.info(
            "[%s] 文档保底补位: 逐出 %s#%s(score=%.4f, 已 %d 席) <- 补入 %s#%s(score=%.4f)",
            log_tag, victim.doc_id, victim.id, victim.score, victim_seats,
            backup.doc_id, backup.id, backup.score,
        )
        out[victim_idx] = backup
        seats[victim_doc] -= 1
        seats[doc_id] = seats.get(doc_id, 0) + 1
        added += 1
    return out


async def doc_diversity_backup_v2(
    final: list[Any],
    candidates: list[Any],
    top_k: int,
    *,
    reranker: Any | None = None,
    query: str = "",
    max_per_doc: int = 2,
    min_score: float = 0.0,
    log_tag: str = "DocDiversityV2",
) -> list[Any]:
    """v2 补位（ragas-v4 归因后的改造，与 v1 可 A/B 消融；默认 env 不启用=v3 行为）。

    v1 的两个 v4 事故暴露的缺陷：
    1. 逐出对象按窗口排位取“多席文档最后一席”——位置敏感：上游任何重排
      （如沉底）都会改变逐出对象，v4 中逐出的变成了主题锚块；
    2. 补位不设相关性门槛——候选池被改写漂移搅浑后，等于主动把无关文档拉进窗口。

    v2 对应修复：
    - 逐出对象与排位解耦：取“超额文档（席位 > max_per_doc）中候选池分数
      最低的一席”，只看席位账和分数，不看块在窗口里的位置；
    - 补位门槛：min_score>0 且提供 reranker 时，对缺席文档候选块补跑一次
      重排打分（rerank 口径与窗口内块同尺度），低于门槛放弃补位，宁缺勿滥。
    """
    if not final or not candidates or len(final) < 2:
        return final
    final_ids = {c.id for c in final}
    seats: dict[str, int] = {}
    for c in final:
        seats[c.doc_id] = seats.get(c.doc_id, 0) + 1
    present = set(seats)
    # 缺席文档的最高分候选（候选池内同口径比较）
    best: dict[str, Any] = {}
    for c in candidates:
        if c.doc_id in present or c.id in final_ids:
            continue
        prev = best.get(c.doc_id)
        if prev is None or c.score > prev.score:
            best[c.doc_id] = c
    if not best:
        return final

    # 门槛：缺席候选过一次重排拿 rerank 分过滤；重排失败按无门槛放行（不让链路崩）
    if min_score > 0 and reranker is not None and query:
        try:
            scored = await reranker.rerank(query, list(best.values()), len(best))
            by_id = {c.id: float(c.score) for c in scored}
            dropped = {d: c for d, c in best.items() if by_id.get(c.id, 0.0) < min_score}
            for d, c in dropped.items():
                _logger.info(
                    "[%s] 补位候选未过重排门槛(rerank=%.4f < %.2f)，放弃补位: %s#%s",
                    log_tag, by_id.get(c.id, 0.0), min_score, d, c.id,
                )
            best = {d: c for d, c in best.items() if d not in dropped}
        except Exception as e:  # noqa: BLE001
            _logger.warning("[%s] 补位门槛重排失败，按无门槛放行: %s", log_tag, e)
    if not best:
        return final

    out = list(final)
    added = 0
    for doc_id, backup in sorted(best.items(), key=lambda kv: -kv[1].score):
        if added >= MAX_BACKUP_SLOTS:
            break
        if len(out) < top_k:
            _logger.info(
                "[%s] 窗口未满，直接补入缺席文档块: %s#%s(score=%.4f)",
                log_tag, backup.doc_id, backup.id, backup.score,
            )
            out.append(backup)
            seats[doc_id] = seats.get(doc_id, 0) + 1
            added += 1
            continue
        # v2 逐出：超额文档（席位 > max_per_doc）中候选池分数最低的一席（与排位无关）
        victim_idx = -1
        victim_doc = ""
        victim_score = 0.0
        for i, c in enumerate(out):
            n = seats.get(c.doc_id, 0)
            if max_per_doc > 0 and n > max_per_doc:
                if victim_idx < 0 or c.score < victim_score:
                    victim_idx, victim_doc, victim_score = i, c.doc_id, c.score
        if victim_idx < 0:
            break  # 无超额文档可逐，宁缺勿滥（不逐任何文档的限额内席位）
        victim = out[victim_idx]
        _logger.info(
            "[%s] 文档保底补位(v2): 逐出超额文档最弱席 %s#%s(score=%.4f, 已 %d 席) <- 补入 %s#%s(score=%.4f)",
            log_tag, victim.doc_id, victim.id, victim.score,
            seats.get(victim_doc, 0), backup.doc_id, backup.id, backup.score,
        )
        out[victim_idx] = backup
        seats[victim_doc] -= 1
        seats[doc_id] = seats.get(doc_id, 0) + 1
        added += 1
    return out
