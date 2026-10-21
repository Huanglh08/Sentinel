"""业务事实与工具信封的 JSON 规范（sentinel.fact.v2 / case_tool_v2）。

范式对齐
--------
与告警夹具（case-alerts-*.jsonl：subject{type,id} + observables[{type,value}]）同构，
并参考 OCSF 的「typed entity + activity class + time」思想：

  事实（fact.v2）
    schema      "sentinel.fact.v2"
    type        login | device | sales | order | ticket | bind
    record_id   业务主键（LG.../WO.../SAL.../ORD...）
    time        ISO-8601 本地时间
    subject     {type, id}         查询主实体（带类型，杜绝「IP 当账号查」）
    related     [{type, value}]    关联实体（带类型）
    attributes  按 type 定型的英文字段（工单为成型对象，非散文拼接）
    summary     一句话人读摘要

信封（case_tool_v2）在 v1 基础上：
    subject     升级为 {type, id}
    items       升级为 fact.v2 对象
    rollup      服务端算好的聚合（distinct 账号/设备/IP 数、时间范围），
                LLM 不再自己「数记录条数」
    error       subject_type_mismatch 等机器可判错误码

本模块是生成端（build_business_data.py）与服务端（server.py）共用的唯一定义源。
"""

from __future__ import annotations

import re

FACT_SCHEMA = "sentinel.fact.v2"
ENVELOPE_SCHEMA = "case_tool_v2"

FACT_TYPES = ("login", "device", "sales", "order", "ticket", "bind")

# 主体/实体类型枚举（与告警夹具 observables 对齐）
SUBJECT_TYPES = ("account", "merchant", "ip", "device", "order", "ticket", "phone", "other")

# 各业务工具允许的查询主体类型：语义是「按主体查它的事实」，
# 设备名/IP 不是合法查询主体（212 误报根源：IP 查账号活动、设备型号查设备画像照样命中）。
TOOL_EXPECTED_SUBJECTS: dict[str, tuple[str, ...]] = {
    "account_activity": ("account", "merchant"),
    "device_query": ("account", "merchant"),
    "ticket_query": ("account", "merchant"),
    "sales_query": ("merchant",),
}

_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_MERCHANT_RE = re.compile(r"^M\d{4,}$")
_ACCOUNT_RE = re.compile(r"^(?:[A-Z]\d{4,}|\d{4,10})$", re.IGNORECASE)
_ORDER_RE = re.compile(r"^(?:ORD|OD)\d*", re.IGNORECASE)
_TICKET_RE = re.compile(r"^(?:WO|TK|TRV|BP|HZ|CD|OA|RC)-?", re.IGNORECASE)
_DEVICE_RE = re.compile(r"^(?:iPhone|iPad|Android|鸿蒙|dev_)", re.IGNORECASE)
_HEXDEV_RE = re.compile(r"^(?=.*[A-F])[0-9A-F]{8}$")  # 设备指纹须含字母；纯数字串按账号处理


def guess_entity_type(value: str) -> str:
    """从实体字符串推断类型（生成端与入参校验共用，口径唯一）。"""
    v = str(value or "").strip()
    if not v:
        return "other"
    if _IP_RE.match(v):
        return "ip"
    if _MERCHANT_RE.match(v):
        return "merchant"
    if _TICKET_RE.match(v):
        return "ticket"
    if _ORDER_RE.match(v):
        return "order"
    if _DEVICE_RE.match(v) or _HEXDEV_RE.match(v):
        return "device"
    if _ACCOUNT_RE.match(v):
        return "account"
    return "other"


def subject_of(value: str, type_hint: str = "") -> dict:
    """构造 typed subject；type_hint 显式给定优先，否则自动推断。"""
    t = type_hint if type_hint in SUBJECT_TYPES else guess_entity_type(value)
    return {"type": t, "id": str(value or "")}


def subject_type_mismatch(tool: str, subject: dict) -> str | None:
    """主体类型与工具语义不符时返回错误码，合法返回 None。"""
    expected = TOOL_EXPECTED_SUBJECTS.get(tool)
    if not expected:
        return None
    if subject.get("type") not in expected:
        return f"subject_type_mismatch: got {subject.get('type')}, expect {'|'.join(expected)}"
    return None


# ---------------------------------------------------------------------------
# attributes 读取（v1 中文键 → v2 英文键兼容层；数据全量重建后 v1 路径退役）
# ---------------------------------------------------------------------------
_ATTR_ALIAS: dict[str, dict[str, tuple[str, ...]]] = {
    # v2 英文键: (中文 v1 键备选...)
    "ticket_id": ("ticket_id", "工单号"),
    "category": ("category", "工单类型"),
    "submitted_by": ("submitted_by", "提交人", "报备人"),
    "submitted_at": ("submitted_at", "提交时间"),
    "reason": ("reason", "事由", "审批事项"),
    "destination": ("destination", "目的地"),
    "approval_status": ("approval_status", "审批状态", "审批结论", "处理结果"),
    "handler": ("handler", "处理人"),
    "priority": ("priority", "优先级"),
    "ref_id": ("ref_id", "凭证号"),
    "source_system": ("source_system", "来源系统"),
    "device": ("device", "设备"),
    "first_seen": ("first_seen", "首次出现"),
    "logins_7d": ("logins_7d", "近7天登录次数"),
    "linked_accounts": ("linked_accounts", "关联账号数"),
    "owner_accounts": ("owner_accounts", "归属账号"),
    "merchant": ("merchant", "商户"),
    "gmv": ("gmv", "交易额"),
    "orders": ("orders", "订单数"),
    "refunds": ("refunds", "退款笔数"),
    "order_id": ("order_id", "订单号"),
    "pay_method": ("pay_method", "支付方式"),
    "amount": ("amount", "金额"),
    "account": ("account", "账号"),
    "ip": ("ip", "IP", "登录IP"),
    "geo": ("geo", "归属地"),
    "login_time": ("login_time", "登录时间"),
    "result": ("result", "结果", "登录结果"),
    "verify_type": ("verify_type", "核验类型"),
    "merchant": ("merchant", "商户"),
    "change_type": ("change_type", "变更类型"),
    "new_value": ("new_value", "新绑定"),
    "channel": ("channel", "渠道"),
    "verify_method": ("verify_method", "验证方式"),
}


def attrs(payload: dict) -> dict:
    """把 payload（v1 中文键或 v2 attributes）归一为 v2 英文键视图。

    v2 键优先；v1 中文键按别名表回填。baseline/pre_opening/day_offset/
    expected_multiple 等控制键两版同名，直接透传。
    """
    p = payload or {}
    out: dict = {}
    for key, aliases in _ATTR_ALIAS.items():
        for a in aliases:
            if a in p and p[a] not in (None, ""):
                out[key] = p[a]
                break
    for k in ("baseline", "is_baseline", "pre_opening", "day_offset", "expected_multiple",
              "linked_account_ids", "subject_account", "status"):
        if k in p and p[k] not in (None, ""):
            out[k] = p[k]
    if "is_baseline" not in out and "baseline" in out:
        out["is_baseline"] = out.pop("baseline")
    return out


def fact_from_row(row: dict) -> dict:
    """DB 行 → fact.v2。payload 已是 v2 时直接用，否则从旧列兜底归一。"""
    payload = row.get("payload") or {}
    if payload.get("schema") == FACT_SCHEMA:
        return payload
    subject_id = str(row.get("record_id") or "")
    ftype = str(row.get("kind") or "other")
    return {
        "schema": FACT_SCHEMA,
        "type": ftype,
        "record_id": subject_id,
        "time": " ".join(str(row.get(k) or "") for k in ("date", "time")).strip(),
        "subject": subject_of(subject_id),
        "related": [],
        "attributes": attrs(payload),
        "summary": str(row.get("detail") or row.get("title") or ""),
    }


def rollup(items: list[dict]) -> dict:
    """服务端聚合：给 LLM 显式的 distinct 计数，杜绝「10 条记录读成 10 个账号」。"""
    if not items:
        return {}
    types = {str(i.get("type") or "") for i in items}

    def _acc(v: str) -> str:
        return str(v or "")

    subjects = {_acc((i.get("subject") or {}).get("id")) for i in items} - {""}
    out: dict = {"records": len(items), "distinct_subjects": len(subjects)}
    if items and items[0].get("type") == "login" or "login" in types:
        devs = {str(attrs(i.get("attributes") or {}).get("device") or "") for i in items} - {""}
        ips = {str(attrs(i.get("attributes") or {}).get("ip") or "") for i in items} - {""}
        accounts = {str(attrs(i.get("attributes") or {}).get("account") or "") for i in items} - {""}
        out["distinct"] = {"devices": len(devs), "ips": len(ips), "accounts": len(accounts)}
        out["hint"] = f"{len(devs)} 台设备 / {len(ips)} 个 IP / {len(accounts)} 个账号"
    if "device" in types:
        devs = {str(attrs(i.get("attributes") or {}).get("device") or "") for i in items} - {""}
        accs = {str(attrs(i.get("attributes") or {}).get("account") or "") for i in items} - {""}
        out["distinct"] = {"devices": len(devs), "accounts": len(accs)}
        out["hint"] = f"{len(devs)} 台设备，关联 {len(accs)} 个账号"
    if "sales" in types:
        days = {str(i.get("time") or "")[:10] for i in items} - {""}
        out["distinct"] = {"days": len(days)}
    if "ticket" in types:
        tids = {str(i.get("record_id") or "") for i in items} - {""}
        out["distinct"] = {"tickets": len(tids)}
    if "order" in types:
        oids = {str(i.get("record_id") or "") for i in items} - {""}
        out["distinct"] = {"orders": len(oids)}
    times = sorted(str(i.get("time") or "") for i in items if i.get("time"))
    if times:
        out["time_range"] = {"from": times[0], "to": times[-1]}
    return out
