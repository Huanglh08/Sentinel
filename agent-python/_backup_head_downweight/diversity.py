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
