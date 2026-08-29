"""审批待办 IM 推送（Phase 3：P3-4 待办推送）。

支持企微 / 钉钉 / 飞书群机器人 webhook（text 消息）：
- CASE_NOTIFY_WEBHOOK_URL   机器人 webhook 地址（默认空 = 关闭推送）
- CASE_NOTIFY_WEBHOOK_TYPE  wecom | dingtalk | feishu（默认 wecom，企微与钉钉 text 格式一致）

设计原则：
- fire-and-forget：asyncio.create_task 后台发送，绝不阻塞调查/裁决主流程
- 静默失败：网络/配置异常仅打 warning，不影响业务
- 未配置 URL 时为零开销直接跳过
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 8.0


def _webhook_config() -> tuple[str, str]:
    url = os.getenv("CASE_NOTIFY_WEBHOOK_URL", "").strip()
    kind = os.getenv("CASE_NOTIFY_WEBHOOK_TYPE", "wecom").strip().lower()
    return url, kind


def _build_payload(kind: str, text: str) -> bytes:
    if kind == "feishu":
        return json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
    # wecom / dingtalk 的 text 格式一致
    return json.dumps({"msgtype": "text", "text": {"content": text}}).encode("utf-8")


def _post_sync(url: str, kind: str, text: str) -> None:
    req = urllib.request.Request(
        url,
        data=_build_payload(kind, text),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:  # noqa: S310
        resp.read()


def _fire_and_send(text: str) -> None:
    url, kind = _webhook_config()
    if not url:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 无运行中的事件循环（如脚本场景）
        logger.warning("[case] IM 推送跳过：无事件循环")
        return

    async def _task() -> None:
        try:
            await asyncio.to_thread(_post_sync, url, kind, text)
        except Exception as e:  # noqa: BLE001
            logger.warning("[case] IM 推送失败（已忽略）: %s", e)

    loop.create_task(_task())


def notify_approval_created(items: list[dict], case_no: str = "") -> None:
    """新审批待办产生时推送群提醒。"""
    if not items:
        return
    lines = [f"【案件审批待办】{case_no}".rstrip(), f"新增 {len(items)} 条待办："]
    for it in items[:5]:
        lines.append(f"- [{it.get('id', '')[:8]}] {it.get('action', '')}｜{it.get('reason', '')[:60]}")
    if len(items) > 5:
        lines.append(f"... 等共 {len(items)} 条")
    _fire_and_send("\n".join(lines))


def notify_approval_decided(item: dict, decision: str, operator: str, opinion: str = "") -> None:
    """审批裁决完成后推送结果。"""
    verdict = "通过" if decision == "APPROVED" else "驳回"
    lines = [
        f"【案件审批裁决】{item.get('case_no', '')}",
        f"- 待办: {item.get('action', '')}",
        f"- 结果: {verdict}（operator={operator or 'anonymous'}）",
    ]
    if opinion:
        lines.append(f"- 意见: {opinion[:80]}")
    _fire_and_send("\n".join(lines))
