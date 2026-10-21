"""
Ragent MCP Server (Python / FastMCP)
=====================================

对应原 Java 版 `mcp-server`（McpServerApplication + 4 个 Executor）：
  - weather_query  天气查询
  - ticket_query   客户工单查询
  - sales_query    销售数据查询
  - youcom_search  联网搜索（需 YDC_API_KEY，缺失则不注册）

工具名 / 入参 / 描述 / 返回文本格式与原 Java 版保持一致，
对调用方（Java MCP 客户端）完全透明，协议字节级兼容。

运行：
  pip install -r requirements.txt
  python server.py                 # 默认 stdio
  python server.py --transport sse # SSE（便于被 Java 端远程接入）
"""
from __future__ import annotations

import json
import os
import random
import sys
import zlib
from datetime import date, datetime, timedelta
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import bizdb
import factschema as fs

# 富化数据源开关：fixture（默认，夹具+合成生成器）| live（只读库直连）
CASE_DATA_SOURCE = os.getenv("CASE_DATA_SOURCE", "fixture").strip().lower()

# ponytail: 依赖版本漂移——pyc 对应的是旧版 mcp（run() 收 host/port）；
# 当前安装的 mcp SDK 中 FastMCP.__init__ 收 host/port，run() 只收 transport。
# 故端口须在构造期确定（装饰器 @mcp.tool() 在导入期注册到该实例）。
_mcp_port = 8000
if "--port" in sys.argv:
    _mcp_port = int(sys.argv[sys.argv.index("--port") + 1])

mcp = FastMCP("ragent-mcp-server", host="127.0.0.1", port=_mcp_port)

# ---------------------------------------------------------------------------
# 案件工具结构化返回（case_tool_v1）
# ---------------------------------------------------------------------------
# 背景：工具过去只返回中文文本，"共 0 条"在调查链里既可能被读成"证据不足"，
# 也可能被读成"可疑/无记录"。结构化后由调用方按 status 判定，不再靠文案猜。
# v2（sentinel.fact.v2 / case_tool_v2）：items 升级为定型事实对象（subject/related
# 带类型、attributes 英文键、工单为成型对象），新增 rollup 聚合——LLM 不再自己
# 「数记录条数」；入参带实体类型校验，IP/设备名当账号查将被拒（修 212 误报根源）。
CASE_TOOL_SCHEMA = fs.ENVELOPE_SCHEMA
_FACT_FIELDS = ("kind", "alert_id", "record_id", "title", "detail", "amount", "date", "time", "status")


def _fact_items(rows: list[dict]) -> list[dict]:
    return [fs.fact_from_row(r) for r in rows]


def _fact_items(rows: list[dict]) -> list[dict]:
    return [fs.fact_from_row(r) for r in rows]


def _reject_mismatch(tool: str, customer_name: str, subject_type: str,
                     alert_id: str, structured: bool):
    """入参实体类型与工具语义不符时拒绝查询（返回拒答信封），否则返回 typed subject。

    这是「IP 查账号活动照样命中 20 条」这类误报的结构性修复：类型不符 → miss，
    而不是拿字符串去碰运气匹配。
    """
    subj = fs.subject_of(customer_name or "", subject_type)
    err = fs.subject_type_mismatch(tool, subj)
    if not err:
        return subj, None
    text = (
        f"【查询被拒】主体 {customer_name} 的实体类型是 {subj['type']}，"
        f"本工具只接受 {'/'.join(fs.TOOL_EXPECTED_SUBJECTS[tool])} 类主体；"
        f"请改用账号/商户号查询，或换用匹配的工具。"
    )
    if not structured:
        return subj, text
    return subj, _envelope(tool, subj, [], text, status="miss",
                           alert_id=alert_id, error="subject_type_mismatch")


# ---------------------------------------------------------------------------
# 数据比对摘要（业务数据接进判据的入口）
# ---------------------------------------------------------------------------
# 只按「数据形态」出摘要，不按业务类型分支：
#   量值（交易额/订单数）→ 本日 vs 历史中位数、偏离倍数、样本天数
#   集合（登录设备/IP）  → 去重个数、是否全部落在常用集合内
# 判据层只读 normal / sample_days 两个字段，算术不交给 LLM。
_MIN_BASELINE_DAYS = 7


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _sales_summary(rows: list[dict]) -> dict:
    """本日交易额/退款率与历史基线（payload.baseline=true）比对。"""
    baseline: dict[str, float] = {}
    rates: list[float] = []
    today_rows = []
    for r in rows:
        a = fs.attrs(r.get("payload") or {})
        day = str(r.get("date") or "")
        if a.get("is_baseline"):
            if day:
                baseline[day] = baseline.get(day, 0.0) + float(r.get("amount") or 0)
            orders = int(a.get("orders") or 0)
            if orders > 0:
                rates.append(int(a.get("refunds") or 0) / orders)
        else:
            today_rows.append(r)
    if not baseline or not today_rows:
        return {}
    amounts = list(baseline.values())
    current_row = max(today_rows, key=lambda r: str(r.get("date") or ""))
    current = float(current_row.get("amount") or 0)
    # 新店：只有「开业前无交易」一条记录，首日没有可比基线，增长不是异常
    if any(fs.attrs(r.get("payload") or {}).get("pre_opening") for r in rows):
        return {
            "kind": "sales",
            "sample_days": 0,
            "pre_opening": True,
            "today": round(current, 2),
            "normal": True,
        }
    a = fs.attrs(current_row.get("payload") or {})
    today_orders = int(a.get("orders") or 0)
    today_rate = int(a.get("refunds") or 0) / today_orders if today_orders else 0.0
    base_rate = _median(rates) if rates else 0.0
    base = _median(amounts)
    ratio = round(current / base, 2) if base > 0 else 0.0
    refund_ratio = round(today_rate / base_rate, 2) if base_rate > 0 else (1.0 if today_rate == 0 else 99.0)
    days = len(baseline)
    # 交易额与退款率都在正常带内才算正常波动；促销类高增长仍要走凭证。
    # 线索明写的增长倍数（较平日增长 N 倍）由事实生成器写进 expected_multiple，
    # 它本身就是业务事实，不能反过来当成异常证据。
    expected = float(a.get("expected_multiple") or 1.0)
    upper = max(2.0, expected * 1.15)
    amount_normal = 0.5 <= ratio <= upper
    refund_normal = refund_ratio <= 2.0
    return {
        "kind": "sales",
        "sample_days": days,
        "today": round(current, 2),
        "baseline_median": round(base, 2),
        "baseline_min": round(min(amounts), 2),
        "baseline_max": round(max(amounts), 2),
        "ratio": ratio,
        "expected_multiple": expected,
        "refund_rate_today": round(today_rate, 4),
        "refund_rate_baseline": round(base_rate, 4),
        "refund_ratio": refund_ratio,
        "normal": days >= _MIN_BASELINE_DAYS and amount_normal and refund_normal,
    }


def _login_summary(rows: list[dict]) -> dict:
    """登录背景：天数、设备/IP 去重个数，判断是否全部为常用集合。"""
    days = {str(r.get("date") or "") for r in rows if r.get("date")}
    devices = {str(fs.attrs(r.get("payload") or {}).get("device") or "") for r in rows} - {""}
    ips = {str(fs.attrs(r.get("payload") or {}).get("ip") or "") for r in rows} - {""}
    n_days = len(days)
    return {
        "kind": "login",
        "sample_days": n_days,
        "events": len(rows),
        "distinct_devices": len(devices),
        "distinct_ips": len(ips),
        "normal": n_days >= _MIN_BASELINE_DAYS and len(devices) <= 1,
    }


def _summary_text(summary: dict) -> str:
    if not summary:
        return ""
    if summary["kind"] == "sales":
        if summary.get("pre_opening"):
            return (
                f"【数据比对】新店开业前无交易记录（本日 ¥{summary['today']:.2f}），"
                f"首日无可比基线，增长不构成异常"
            )
        verdict = "属正常波动" if summary["normal"] else "超出正常波动"
        refund = "与基线一致" if summary.get("refund_ratio", 0) <= 2.0 else "高于基线"
        return (
            f"【数据比对】本日 ¥{summary['today']:.2f}；前 {summary['sample_days']} 天中位数 "
            f"¥{summary['baseline_median']:.2f}（{summary['baseline_min']:.2f}~"
            f"{summary['baseline_max']:.2f}）；偏离 {summary['ratio']:.2f} 倍，{verdict}；"
            f"退款率 {summary.get('refund_rate_today', 0):.2%}（基线 "
            f"{summary.get('refund_rate_baseline', 0):.2%}，{refund}）"
        )
    verdict = "均为常用设备" if summary["normal"] else "存在多设备或样本不足"
    return (
        f"【数据比对】近 {summary['sample_days']} 天 {summary['events']} 次登录；"
        f"设备 {summary['distinct_devices']} 个、IP {summary['distinct_ips']} 个，{verdict}"
    )


def _envelope(
    tool: str,
    subject,
    items: list[dict],
    text: str,
    *,
    status: str | None = None,
    alert_id: str = "",
    error: str = "",
    summary: dict | None = None,
    subject_type: str = "",
) -> str:
    """把工具结果包成 case_tool_v2 信封；text 保持人读格式，rollup 给显式聚合。"""
    st = status or ("hit" if items else "miss")
    subj = subject if isinstance(subject, dict) else fs.subject_of(subject or "", subject_type)
    body = {
        "schema": CASE_TOOL_SCHEMA,
        "tool": tool,
        "status": st,          # hit | miss | error | unsupported
        "subject": subj,
        "alert_id": alert_id,
        "count": len(items),
        "source": "biz_facts",
        "items": items,
        "error": error,
        "text": text,
    }
    r = fs.rollup(items)
    if r:
        # 显式聚合：distinct 账号/设备/IP 数与时间范围，LLM 不再自己数条数
        body["rollup"] = r
    if summary:
        # 数据比对摘要：判据层按 normal/sample_days 判定，不靠 LLM 心算
        body["summary"] = summary
    return json.dumps(body, ensure_ascii=False)


def _mock_today() -> date:
    # ponytail: 合成数据基准日。MOCK_FREEZE_DATE 冻结（评测跨日/跨轮可复现），
    # 缺省跟随系统日期保留"今天工单/今日天气"语义；真实数据源接入后整段退役。
    frozen = os.environ.get("MOCK_FREEZE_DATE", "")
    return date.fromisoformat(frozen) if frozen else date.today()


# ---------------------------------------------------------------------------
# 1) 天气查询  weather_query
# ---------------------------------------------------------------------------
CITY_COORDINATES = {
    "北京": (39.9, 116.4), "上海": (31.2, 121.5), "广州": (23.1, 113.3),
    "深圳": (22.5, 114.1), "杭州": (30.3, 120.2), "成都": (30.6, 104.1),
    "武汉": (30.6, 114.3), "南京": (32.1, 118.8), "西安": (34.3, 108.9),
    "重庆": (29.6, 106.5), "长沙": (28.2, 112.9), "天津": (39.1, 117.2),
    "苏州": (31.3, 120.6), "郑州": (34.7, 113.6), "青岛": (36.1, 120.4),
    "大连": (38.9, 121.6), "厦门": (24.5, 118.1), "昆明": (25.0, 102.7),
    "哈尔滨": (45.8, 126.5), "三亚": (18.3, 109.5),
}
WEATHER_TYPES = {
    0: ["晴", "多云", "阴", "小雨", "阵雨", "多云转晴"],
    1: ["晴", "多云", "雷阵雨", "大雨", "暴雨", "多云转阴"],
    2: ["晴", "多云", "阴", "小雨", "晴转多云", "多云转晴"],
    3: ["晴", "多云", "阴", "小雪", "中雪", "晴转多云", "雾"],
}
DIRECTIONS = ["东风", "南风", "西风", "北风", "东南风", "西北风", "东北风", "西南风"]


def _season(month: int) -> int:
    if 3 <= month <= 5:
        return 0
    if 6 <= month <= 8:
        return 1
    if 9 <= month <= 11:
        return 2
    return 3


def _weather_for(city: str, d: date) -> dict:
    lat = CITY_COORDINATES[city][0]
    # crc32 而非 hash()：str hash 带进程随机盐，重启 MCP 后数据全变，评测不可复现
    seed = d.toordinal() * 31 + zlib.crc32(city.encode("utf-8"))
    rnd = random.Random(seed)
    season = _season(d.month)
    base = {0: 15 - (lat - 25) * 0.5, 1: 30 - (lat - 25) * 0.3,
            2: 18 - (lat - 25) * 0.5, 3: 5 - (lat - 25) * 0.8}[season]
    high = int(base + 3 + rnd.randint(0, 5))
    low = int(base - 3 - rnd.randint(0, 4))
    cur = low + rnd.randint(0, max(1, high - low))
    wtype = WEATHER_TYPES[season][rnd.randint(0, len(WEATHER_TYPES[season]) - 1)]
    humidity = {1: 60 + rnd.randint(0, 29), 3: 20 + rnd.randint(0, 29)}.get(season, 40 + rnd.randint(0, 29))
    if "雨" in wtype or "雪" in wtype:
        humidity = min(95, humidity + 20)
    wind_force = 1 + rnd.randint(0, 4)
    aqi = 30 + rnd.randint(0, 119) + (20 if lat > 35 else 0)
    aq = "优" if aqi <= 50 else "良" if aqi <= 100 else "轻度污染" if aqi <= 150 else "中度污染"
    return dict(weather=wtype, cur=cur, high=high, low=low, humidity=humidity,
                wind_dir=DIRECTIONS[rnd.randint(0, 7)], wind=f"{wind_force}-{wind_force + 1}级", aq=aq)


@mcp.tool()
def weather_query(city: str, query_type: str = "current", days: int = 3) -> str:
    """查询城市天气信息，支持查看当前实时天气和未来多天天气预报，包含温度、湿度、风力、天气状况等信息。
    :param city: 城市名称，如北京、上海、广州等
    :param query_type: 查询类型：current(当前天气)、forecast(未来预报)
    :param days: 预报天数，仅forecast模式有效，默认3天，最多7天
    """
    if not city or not city.strip():
        return "请提供城市名称"
    query_type = query_type or "current"
    days = days if days and days > 0 else 3
    days = min(days, 7)
    if city not in CITY_COORDINATES:
        return "暂不支持查询该城市，当前支持：" + "、".join(CITY_COORDINATES.keys())

    today = _mock_today()
    if query_type == "forecast":
        lines = [f"【{city} 未来{days}天天气预报】\n"]
        for i in range(days):
            d = today + timedelta(days=i)
            w = _weather_for(city, d)
            label = "今天" if i == 0 else "明天" if i == 1 else "后天" if i == 2 else d.strftime("%m月%d日")
            lines.append(f"📅 {label}（{d.strftime('%m-%d')}）")
            lines.append(f"   天气: {w['weather']} | 温度: {w['low']}°C ~ {w['high']}°C")
            lines.append(f"   湿度: {w['humidity']}% | {w['wind_dir']} {w['wind']}\n")
        t0 = _weather_for(city, today)["high"]
        t1 = _weather_for(city, today + timedelta(days=days - 1))["high"]
        trend = t1 - t0
        if abs(trend) >= 5:
            lines.append(f"趋势: 未来{days}天气温{'逐渐升高' if trend > 0 else '逐渐下降'}，"
                         f"注意{'防暑' if trend > 0 else '保暖'}。")
        return "\n".join(lines).strip()

    w = _weather_for(city, today)
    lines = [f"【{city} 今日天气】\n",
             f"日期: {today.strftime('%Y年%m月%d日')}",
             f"天气: {w['weather']}", f"当前温度: {w['cur']}°C",
             f"最高温度: {w['high']}°C", f"最低温度: {w['low']}°C",
             f"相对湿度: {w['humidity']}%", f"风向: {w['wind_dir']}",
             f"风力: {w['wind']}", f"空气质量: {w['aq']}"]
    if "雨" in w["weather"] or "雪" in w["weather"]:
        lines.append("\n提示: 今日有降水，出行请携带雨具。")
    elif w["high"] >= 35:
        lines.append("\n提示: 今日高温，注意防暑降温。")
    elif w["low"] <= 0:
        lines.append("\n提示: 今日气温较低，注意防寒保暖。")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# 2) 工单查询  ticket_query
# ---------------------------------------------------------------------------
REGIONS = ["华东", "华南", "华北", "西南", "西北"]
PRODUCTS = ["企业版", "专业版", "基础版"]
STATUSES = ["待处理", "处理中", "已解决", "已关闭"]
PRIORITIES = ["紧急", "高", "中", "低"]
CATEGORIES = ["功能异常", "性能问题", "安装部署", "使用咨询", "数据问题", "权限问题"]
CUSTOMERS_BY_REGION = {
    "华东": ["腾讯科技", "阿里巴巴", "字节跳动", "网易公司"],
    "华南": ["美团点评", "京东集团", "小米科技", "格力电器"],
    "华北": ["百度在线", "华为技术", "中兴通讯", "用友网络"],
    "西南": ["科大讯飞", "金蝶软件", "三一重工", "中联重科"],
    "西北": ["浪潮集团", "东软集团", "美的集团", "海尔智家"],
}
ENGINEERS_BY_REGION = {
    "华东": ["工程师A1", "工程师A2"], "华南": ["工程师B1", "工程师B2"],
    "华北": ["工程师C1", "工程师C2"], "西南": ["工程师D1", "工程师D2"],
    "西北": ["工程师E1", "工程师E2"],
}
ISSUE_TEMPLATES = [
    "系统登录后页面白屏无法操作", "报表导出功能超时失败", "用户权限配置不生效",
    "数据同步延迟超过预期", "批量导入数据格式校验异常", "API接口调用返回500错误",
    "定时任务未按计划执行", "搜索功能结果不准确", "通知消息无法正常推送",
    "文件上传大小限制配置无效", "仪表盘数据展示不一致", "多租户数据隔离存在问题",
    "审批流程节点卡住无法流转", "移动端页面适配显示异常", "数据备份任务执行失败",
]


def _gen_tickets() -> list[dict]:
    today = _mock_today()
    rnd = random.Random(today.toordinal())
    records = []
    seq = 1
    for i in range(30):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        for _ in range(2 + rnd.randint(0, 4)):
            region = REGIONS[rnd.randint(0, 4)]
            customer = CUSTOMERS_BY_REGION[region][rnd.randint(0, 3)]
            product = PRODUCTS[rnd.randint(0, 2)]
            title = ISSUE_TEMPLATES[rnd.randint(0, len(ISSUE_TEMPLATES) - 1)]
            category = CATEGORIES[rnd.randint(0, len(CATEGORIES) - 1)]
            engineer = ENGINEERS_BY_REGION[region][rnd.randint(0, 1)]
            pw = rnd.randint(0, 99)
            priority = "紧急" if pw < 5 else "高" if pw < 20 else "中" if pw < 60 else "低"
            if i > 7:
                status = "已关闭" if rnd.randint(0, 99) < 80 else "已解决"
            elif i > 3:
                sw = rnd.randint(0, 99)
                status = "已解决" if sw < 30 else "已关闭" if sw < 60 else "处理中" if sw < 85 else "待处理"
            else:
                sw = rnd.randint(0, 99)
                status = "待处理" if sw < 35 else "处理中" if sw < 70 else "已解决" if sw < 90 else "已关闭"
            records.append(dict(ticket_id=f"TK-{today.strftime('%Y%m')}-{seq:04d}", region=region,
                                customer=customer, product=product, title=title, category=category,
                                engineer=engineer, priority=priority, status=status,
                                create_date=d.isoformat()))
            seq += 1
    return records


def _filter_tickets(data, region, status, priority, product, customer_name):
    return [t for t in data
            if (region is None or t["region"] == region)
            and (status is None or t["status"] == status)
            and (priority is None or t["priority"] == priority)
            and (product is None or t["product"] == product)
            and (customer_name is None or customer_name in t["customer"])]


# ---------------------------------------------------------------------------
# 评测夹具：案件可核验事实（MOCK_CASE_FACTS=1 时生效，生产不得开启）
# ---------------------------------------------------------------------------
# 合成工单只认识 20 个固定企业客户，案件主体（A87654321 / zhangw_8823 / M8827364 …）
# 一律查无 → 调查链路永远拿不到主体证据。本表把这些**线索里已写明**的官方记录
# （工单/报备/审批）按主体注册进来，使"线索声称"变成"工具可核验"。
# 数据由 evaluation/seeds/build_case_facts.py 从 clue 生成，不含 key_evidence（判断语）。
# ponytail: 仅评测夹具；真实接入工单系统后整段退役（连同 MOCK_CASE_FACTS 开关）。
_FACTS_INDEX: dict[str, list[dict]] | None = None


def _case_facts() -> dict[str, list[dict]]:
    global _FACTS_INDEX
    if _FACTS_INDEX is not None:
        return _FACTS_INDEX
    _FACTS_INDEX = {}
    if os.environ.get("MOCK_CASE_FACTS", "") not in ("1", "true", "True"):
        return _FACTS_INDEX
    path = Path(
        os.environ.get(
            "MOCK_CASE_FACTS_FILE",
            Path(__file__).resolve().parents[2] / "evaluation" / "seeds" / "case-facts.jsonl",
        )
    )
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError as exc:
        print(f"[case-facts] 未加载（{exc}）", file=sys.stderr)
        return _FACTS_INDEX
    for row in rows:
        for ent in row.get("entities") or []:
            _FACTS_INDEX.setdefault(str(ent).lower(), []).append(row)
    print(f"[case-facts] 已注册 {len(rows)} 条事实 / {len(_FACTS_INDEX)} 个主体", file=sys.stderr)
    return _FACTS_INDEX


def _facts_for(subject: str) -> list[dict]:
    if not subject:
        return []
    key = subject.strip().lower()
    index = _case_facts()
    if not index:
        return []
    hits = index.get(key)
    if hits:
        return hits
    # 线索里主体写法不统一（带/不带前缀、截断），按双向包含兜底
    return [row for k, rows in index.items() if key in k or k in key for row in rows]


def _format_fact_tickets(facts: list[dict], subject: str = "") -> str:
    """把工单事实渲染成文本；v2 起从成型工单对象（attributes）取字段，不再占位。"""
    lines = [f"【工单列表】共 {len(facts)} 条，显示 {len(facts)} 条（按优先级排序）\n"]
    for i, f in enumerate(facts, 1):
        fact = fs.fact_from_row(f)
        a = fact.get("attributes") or {}
        lines += [
            f"{i}. [{a.get('ticket_id') or fact.get('record_id') or '-'}] {a.get('category') or fact.get('title') or '业务工单'}",
            f"   提交人: {a.get('submitted_by') or subject or '-'} | 来源系统: {a.get('source_system') or '-'}",
            f"   优先级: {a.get('priority') or '-'} | 状态: {a.get('status') or f.get('status') or '-'} | 审批状态: {a.get('approval_status') or '-'}",
            f"   提交时间: {a.get('submitted_at') or fact.get('time') or '-'} | 渠道: {a.get('channel') or '-'}",
            f"   事由: {a.get('reason') or fact.get('summary') or '-'}"
            + (f" | 目的地: {a['destination']}" if a.get("destination") else "")
            + f" | 凭证号: {a.get('ref_id') or fact.get('record_id') or '-'}\n",
        ]
    return "\n".join(lines).strip()



@mcp.tool()
def ticket_query(region: str = None, status: str = None, priority: str = None,
                 product: str = None, customer_name: str = None, alert_id: str = "",
                 query_type: str = "summary", limit: int = 10,
                 subject_type: str = "", structured: bool = False) -> str:
    """查询客户技术支持工单数据，支持按地区、状态、优先级、产品、客户等维度筛选，支持汇总概览、工单列表、统计分析等多种查询。
    :param region: 地区筛选：华东、华南、华北、西南、西北，不填则查询全国
    :param status: 工单状态：待处理、处理中、已解决、已关闭
    :param priority: 优先级：紧急、高、中、低
    :param product: 产品：企业版、专业版、基础版
    :param customer_name: 客户名称关键字，支持模糊匹配
    :param query_type: 查询类型：summary(汇总概览)、list(工单列表)、stats(统计分析)
    :param limit: 返回记录数限制，默认10
    :param subject_type: 查询主体类型（account/merchant），留空自动推断
    """
    query_type = query_type or "summary"
    limit = limit if limit and limit > 0 else 10
    if CASE_DATA_SOURCE == "live" and customer_name:
        subj, rejected = _reject_mismatch("ticket_query", customer_name, subject_type, alert_id, structured)
        if rejected:
            return rejected
    if CASE_DATA_SOURCE == "live" and (customer_name or alert_id):
        try:
            live = bizdb.query_facts(customer_name or "", kind="ticket", limit=limit, alert_id=alert_id,
                                     subject_type=subject_type if customer_name else "")
        except bizdb.DataSourceUnavailable as e:
            text = f"【数据源不可用】{e}（本次未取到工单数据，不代表该账号无记录）"
            return _envelope("ticket_query", customer_name or alert_id, [], text, status="error", alert_id=alert_id, error=str(e)) if structured else text
        text = _format_fact_tickets(live, subject=customer_name)
        return _envelope("ticket_query", customer_name or alert_id, _fact_items(live), text, alert_id=alert_id) if structured else text
    facts = [f for f in (_facts_for(customer_name) if customer_name else []) if f.get("kind") != "sales"]
    if facts:
        text = _format_fact_tickets(facts[:limit], subject=customer_name)
        return _envelope("ticket_query", customer_name or "", _fact_items(facts[:limit]), text, alert_id=alert_id) if structured else text
    data = _filter_tickets(_gen_tickets(), region, status, priority, product, customer_name)

    if query_type == "list":
        ordered = sorted(data, key=lambda t: (PRIORITIES.index(t["priority"]), t["create_date"]), reverse=True)[:limit]
        lines = [f"【工单列表】共 {len(data)} 条，显示 {len(ordered)} 条（按优先级排序）\n"]
        for i, t in enumerate(ordered, 1):
            lines += [f"{i}. [{t['ticket_id']}] {t['title']}",
                      f"   客户: {t['customer']} | 产品: {t['product']} | 地区: {t['region']}",
                      f"   优先级: {t['priority']} | 状态: {t['status']} | 分类: {t['category']}",
                      f"   处理人: {t['engineer']} | 创建时间: {t['create_date']}\n"]
        return "\n".join(lines).strip()

    if query_type == "stats":
        if not data:
            return "暂无工单数据"
        by_cat = {}
        for t in data:
            by_cat[t["category"]] = by_cat.get(t["category"], 0) + 1
        lines = ["【工单统计分析】\n", "【问题分类统计】"]
        for k, v in sorted(by_cat.items(), key=lambda x: -x[1]):
            lines.append(f"  {k}: {v} 个 ({v * 100.0 / len(data):.1f}%)")
        lines.append("\n【各产品解决率】")
        by_prod = {}
        for t in data:
            by_prod.setdefault(t["product"], []).append(t)
        for p, ts in by_prod.items():
            resolved = sum(1 for t in ts if t["status"] in ("已解决", "已关闭"))
            lines.append(f"  {p}: {resolved * 100.0 / len(ts):.1f}% ({resolved}/{len(ts)})")
        lines.append("\n【处理人工单量排名】")
        by_eng = {}
        for t in data:
            if t["status"] in ("待处理", "处理中"):
                by_eng[t["engineer"]] = by_eng.get(t["engineer"], 0) + 1
        for k, v in sorted(by_eng.items(), key=lambda x: -x[1])[:5]:
            lines.append(f"  {k}: {v} 个待处理")
        return "\n".join(lines).strip()

    # summary
    total = len(data)
    pending = sum(1 for t in data if t["status"] == "待处理")
    in_prog = sum(1 for t in data if t["status"] == "处理中")
    resolved = sum(1 for t in data if t["status"] == "已解决")
    closed = sum(1 for t in data if t["status"] == "已关闭")
    urgent = sum(1 for t in data if t["priority"] == "紧急")
    high = sum(1 for t in data if t["priority"] == "高")
    lines = ["【客户工单汇总概览】\n"]
    filters = []
    if region:
        filters.append("地区: " + region)
    if status:
        filters.append("状态: " + status)
    if priority:
        filters.append("优先级: " + priority)
    if product:
        filters.append("产品: " + product)
    if filters:
        lines.append("筛选条件: " + "，".join(filters) + "\n")
    lines.append(f"工单总数: {total} 个\n")
    lines.append("【状态分布】")
    lines.append(f"  待处理: {pending} 个")
    lines.append(f"  处理中: {in_prog} 个")
    lines.append(f"  已解决: {resolved} 个")
    lines.append(f"  已关闭: {closed} 个\n")
    if total > 0:
        lines.append(f"解决率: {(resolved + closed) * 100.0 / total:.1f}%\n")
    if urgent + high > 0:
        lines.append(f"\n⚠ 紧急/高优先级工单: {urgent + high} 个（紧急 {urgent}，高 {high}）\n")
    if not product and total:
        bp = {}
        for t in data:
            bp[t["product"]] = bp.get(t["product"], 0) + 1
        lines.append("【按产品分布】")
        for k, v in sorted(bp.items(), key=lambda x: -x[1]):
            lines.append(f"  {k}: {v} 个")
    if not region and total:
        br = {}
        for t in data:
            br[t["region"]] = br.get(t["region"], 0) + 1
        lines.append("\n【按地区分布】")
        for k, v in sorted(br.items(), key=lambda x: -x[1]):
            lines.append(f"  {k}: {v} 个")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# 3) 销售查询  sales_query
# ---------------------------------------------------------------------------
SALES_BY_REGION = {
    "华东": ["张三", "李四", "王五"], "华南": ["赵六", "钱七", "孙八"],
    "华北": ["周九", "吴十", "郑冬"], "西南": ["陈春", "林夏", "黄秋"],
    "西北": ["刘一", "杨二", "马三"],
}
CUSTOMER_POOL = ["腾讯科技", "阿里巴巴", "字节跳动", "美团点评", "京东集团", "百度在线", "网易公司",
                "小米科技", "华为技术", "中兴通讯", "用友网络", "金蝶软件", "浪潮集团", "东软集团",
                "科大讯飞", "三一重工", "中联重科", "格力电器", "美的集团", "海尔智家"]


def _sales_range(period: str):
    now = _mock_today()
    if period == "上月":
        start = now.replace(day=1) - timedelta(days=1)
        start = start.replace(day=1)
        end = now.replace(day=1) - timedelta(days=1)
        return start, end
    if period == "本季度":
        q = (now.month - 1) // 3
        return now.replace(month=q * 3 + 1, day=1), now
    if period == "上季度":
        q = (now.month - 1) // 3
        end = now.replace(month=q * 3 + 1, day=1) - timedelta(days=1)
        start = end.replace(month=((q - 1) % 4) * 3 + 1, day=1)
        return start, end
    if period == "本年":
        return now.replace(month=1, day=1), now
    return now.replace(day=1), now


def _gen_sales(start: date, end: date) -> list[dict]:
    rnd = random.Random(start.toordinal())
    days = (end.toordinal() - start.toordinal()) + 1
    records = []
    for i in range(days):
        d = start + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        for _ in range(3 + rnd.randint(0, 5)):
            region = REGIONS[rnd.randint(0, 4)]
            sales = SALES_BY_REGION[region][rnd.randint(0, 2)]
            product = PRODUCTS[rnd.randint(0, 2)]
            amount = {PRODUCTS[0]: 50 + rnd.random() * 150, PRODUCTS[1]: 10 + rnd.random() * 40}.get(product, 1 + rnd.random() * 9)
            records.append(dict(region=region, sales_person=sales, product=product,
                                customer=CUSTOMER_POOL[rnd.randint(0, 19)] + str(d.day),
                                amount=round(amount, 2), date=d.isoformat()))
    return records


def _filter_sales(data, region, product, sales_person):
    return [r for r in data
            if (region is None or r["region"] == region)
            and (product is None or r["product"] == product)
            and (sales_person is None or r["sales_person"] == sales_person)]


@mcp.tool()
def sales_query(region: str = None, period: str = "本月", product: str = None,
                sales_person: str = None, customer_name: str = None, alert_id: str = "",
                query_type: str = "summary", limit: int = 10,
                subject_type: str = "", structured: bool = False) -> str:
    """查询软件销售数据，支持按地区、时间、产品、销售人员、客户等维度筛选，支持汇总统计、排名、明细列表等多种查询。
    :param region: 地区筛选：华东、华南、华北、西南、西北，不填则查询全国
    :param period: 时间段：本月、上月、本季度、上季度、本年，默认本月
    :param product: 产品筛选：企业版、专业版、基础版
    :param sales_person: 销售人员姓名，不填则查询全部销售
    :param customer_name: 客户名称/账号，填写后只返回该客户的记录（查无则返回 0 条）
    :param query_type: 查询类型：summary(汇总)、ranking(排名)、detail(明细)、trend(趋势)
    :param limit: 返回记录数限制，默认10
    :param subject_type: 查询主体类型（merchant/account），留空自动推断
    """
    period = period or "本月"
    query_type = query_type or "summary"
    limit = limit if limit and limit > 0 else 10
    if CASE_DATA_SOURCE == "live" and customer_name:
        subj, rejected = _reject_mismatch("sales_query", customer_name, subject_type, alert_id, structured)
        if rejected:
            return rejected
    if CASE_DATA_SOURCE == "live" and (customer_name or alert_id):
        try:
            live_sales = bizdb.query_facts(customer_name or "", kind="sales", limit=limit, alert_id=alert_id,
                                           subject_type=subject_type if customer_name else "")
            # 订单/支付类没有商户流水，退回查订单明细：否则孤立一单无法与历史同类订单对比
            if not live_sales:
                live_sales = bizdb.query_facts(customer_name or "", kind="order", limit=limit, alert_id=alert_id,
                                               subject_type=subject_type if customer_name else "")
        except bizdb.DataSourceUnavailable as e:
            text = f"【数据源不可用】{e}（本次未取到交易数据，不代表该账号无交易）"
            return _envelope("sales_query", customer_name or alert_id, [], text, status="error", alert_id=alert_id, error=str(e)) if structured else text
        lines = [f"【{period} 销售明细】\n\n共 {len(live_sales)} 条记录，显示 {len(live_sales)} 条：\n"]
        for i, f in enumerate(live_sales, 1):
            a = fs.attrs(f.get("payload") or {})
            amount = a.get("amount") if a.get("amount") is not None else f.get("amount")
            lines += [
                f"{i}. {a.get('merchant') or customer_name}",
                f"   订单号: {a.get('order_id') or f.get('record_id') or '-'} | 金额: ¥{float(amount or 0):.2f} | 支付方式: {a.get('pay_method') or '-'}",
                f"   日期: {f.get('date') or '-'} | 备注: {a.get('reason') or f.get('detail', '')}",
            ]
        summary = _sales_summary(live_sales)
        if summary:
            lines.append("\n" + _summary_text(summary))
        text = "\n".join(lines).strip()
        return _envelope(
            "sales_query", customer_name or alert_id, _fact_items(live_sales), text,
            alert_id=alert_id, summary=summary,
        ) if structured else text
    s, e = _sales_range(period)
    # 案件交易事实（评测夹具）：线索里写明的支付/下单记录，按主体返回。
    # 否则 sales_query 查无会被调查员读成"该笔支付不存在"，制造假矛盾信号。
    fact_sales = [f for f in (_facts_for(customer_name) if customer_name else []) if f.get("kind") == "sales"]
    if fact_sales:
        shown = fact_sales[:limit]
        lines = [f"【{period} 销售明细】\n\n共 {len(fact_sales)} 条记录，显示 {len(shown)} 条：\n"]
        for i, f in enumerate(shown, 1):
            lines += [
                f"{i}. {customer_name}",
                f"   产品: - | 金额: ¥{f.get('amount', 0):.2f} | 地区: -",
                f"   日期: {f.get('date') or '-'} | 备注: {f.get('detail', '')}",
            ]
        text = "\n".join(lines).strip()
        return _envelope("sales_query", customer_name or "", _fact_items(shown), text, alert_id=alert_id) if structured else text
    data = _filter_sales(_gen_sales(s, e), region, product, sales_person)
    if customer_name:
        # 客户维度此前被静默忽略：调查员按主体查却拿到全国数据，既污染判断
        # 又被当成"主体证据"。这里如实过滤，查无即 0 条。
        data = [r for r in data if customer_name in r["customer"]]

    if query_type == "ranking":
        by_sales = {}
        for r in data:
            by_sales[r["sales_person"]] = by_sales.get(r["sales_person"], 0) + r["amount"]
        lines = [f"【{period}{(' ' + region) if region else ''} 销售排名】\n"]
        if not by_sales:
            lines.append("暂无销售数据")
        else:
            for i, (k, v) in enumerate(sorted(by_sales.items(), key=lambda x: -x[1])[:limit], 1):
                lines.append(f"第{i}名: {k} - ¥{v:.2f} 万")
        return "\n".join(lines).strip()

    if query_type == "detail":
        top = sorted(data, key=lambda r: -r["amount"])[:limit]
        lines = [f"【{period}{(' ' + region) if region else ''} 销售明细】\n",
                 f"共 {len(data)} 条记录，显示金额最高的 {len(top)} 条：\n"]
        for i, r in enumerate(top, 1):
            lines += [f"{i}. {r['customer']}",
                      f"   产品: {r['product']} | 金额: ¥{r['amount']:.2f} 万",
                      f"   销售: {r['sales_person']} | 地区: {r['region']} | 日期: {r['date']}\n"]
        return "\n".join(lines).strip()

    if query_type == "trend":
        by_week = {}
        for r in data:
            wk = "第" + str((date.fromisoformat(r["date"]).day - 1) // 7 + 1) + "周"
            by_week[wk] = by_week.get(wk, 0) + r["amount"]
        lines = [f"【{period}{(' ' + region) if region else ''} 销售趋势】\n"]
        lines += [f"{k}: ¥{v:.2f} 万" for k, v in sorted(by_week.items())] if by_week else ["暂无数据"]
        return "\n".join(lines).strip()

    # summary
    total = sum(r["amount"] for r in data)
    lines = [f"【{period} 销售数据汇总】\n"]
    filters = []
    if region:
        filters.append("地区: " + region)
    if product:
        filters.append("产品: " + product)
    if sales_person:
        filters.append("销售: " + sales_person)
    if filters:
        lines.append("筛选条件: " + "，".join(filters) + "\n")
    lines.append(f"总销售额: ¥{total:.2f} 万")
    lines.append(f"成交订单: {len(data)} 笔")
    lines.append(f"平均单价: ¥{(total / len(data)) if data else 0:.2f} 万")
    if not product and data:
        bp = {}
        for r in data:
            bp[r["product"]] = bp.get(r["product"], 0) + r["amount"]
        lines.append("\n【按产品分布】")
        for k, v in sorted(bp.items(), key=lambda x: -x[1]):
            lines.append(f"  {k}: ¥{v:.2f} 万 ({v / total * 100:.1f}%)")
    if not region and data:
        br = {}
        for r in data:
            br[r["region"]] = br.get(r["region"], 0) + r["amount"]
        lines.append("\n【按地区分布】")
        for k, v in sorted(br.items(), key=lambda x: -x[1]):
            lines.append(f"  {k}: ¥{v:.2f} 万 ({v / total * 100:.1f}%)")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# 4) 联网搜索  youcom_search  （需 YDC_API_KEY，缺失则不注册，对应 @ConditionalOnProperty）
# ---------------------------------------------------------------------------
import httpx


def _maybe_register_youcom():
    if not os.getenv("YDC_API_KEY"):
        print("[youcom_search] 未检测到 YDC_API_KEY，跳过注册该工具", file=sys.stderr)
        return

    @mcp.tool()
    def youcom_search(query: str, count: int = 5, freshness: str = None) -> str:
        """基于 You.com Search API 的联网搜索，返回带来源链接和摘录片段的网页与新闻结果。需要配置 YDC_API_KEY 环境变量。
        :param query: 检索关键词或问题
        :param count: 最多返回的结果条数（网页+新闻合计），默认 5，最大 20
        :param freshness: 结果时效过滤：day(一天内)、week(一周内)、month(一月内)、year(一年内)
        """
        if not query or not query.strip():
            return "请提供检索关键词 query"
        count = count if count and count > 0 else 5
        count = min(count, 20)
        if freshness and freshness not in ("day", "week", "month", "year"):
            return "freshness 参数不合法，可选值：day、week、month、year"

        api_key = os.getenv("YDC_API_KEY")
        if not api_key:
            return ("You.com 联网搜索未配置：请先设置环境变量 YDC_API_KEY"
                    "（可在 https://you.com/platform/api-keys 获取），配置后重启 MCP Server 即可使用")

        params = {"query": query, "count": count}
        if freshness:
            params["freshness"] = freshness
        try:
            resp = httpx.get("https://ydc-index.io/v1/search", params=params,
                              headers={"X-API-Key": api_key}, timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            return "搜索失败: " + str(exc)
        if resp.status_code != 200:
            return f"You.com API 返回异常状态码: {resp.status_code}"

        root = resp.json().get("results", {})
        items = []
        for section in ("web", "news"):
            arr = root.get(section)
            if isinstance(arr, list):
                items.extend(arr)
        if not items:
            return "未检索到相关结果，请尝试更换关键词。"
        items = items[:count]

        out = [f"检索完成，共 {len(items)} 条结果：\n"]
        for i, it in enumerate(items, 1):
            title = it.get("title", "(无标题)")
            url = it.get("url", "")
            excerpt = it.get("description") or (it.get("snippets") or [None])[0] or ""
            out.append(f"{i}. {title}")
            if url:
                out.append(f"   链接: {url}")
            if excerpt:
                out.append(f"   摘录: {excerpt}")
            out.append("")
        return "\n".join(out).strip()


_maybe_register_youcom()


# ---------------------------------------------------------------------------
# 5) 风控关系图查询  graph_query  （Phase 4：一机多号 / 一层N层）
# ---------------------------------------------------------------------------
# 内存 mock 边数据，字段与 PG 边表 t_case_graph_edge 对齐
# （upgrades/v1.1.0/260829_case_phase4_rules_graph.sql，含递归 CTE 示例）。
# 真实化阶段：仅替换本工具内数据源为 PG 递归 CTE 查询，工具签名与返回文本格式
# 保持不变，对调用方（Investigator）透明。
_GRAPH_NODES = {
    "account": ["A1001", "A1002", "A1003", "A1004", "B9001", "A2001"],
    "device": ["D1001", "D1002", "D2001"],
    "ip": ["IP8801"],
    "order": ["O7001"],
}
_GRAPH_EDGES = [
    # 一机多号团伙：设备 D1001 挂 3 个账号
    ("device", "D1001", "same_device", "account", "A1001"),
    ("device", "D1001", "same_device", "account", "A1002"),
    ("device", "D1001", "same_device", "account", "A1003"),
    # 资金归集：两个账号向同一收款卡转账
    ("account", "A1001", "transfer", "account", "B9001"),
    ("account", "A1002", "transfer", "account", "B9001"),
    ("account", "A1003", "transfer", "account", "B9001"),
    # 同 IP 关联：团伙边缘账号
    ("account", "A1002", "same_ip", "ip", "IP8801"),
    ("account", "A1004", "same_ip", "ip", "IP8801"),
    # 一层扩展：A1004 归属另一设备 D1002（团伙第二设备）
    ("device", "D1002", "same_device", "account", "A1004"),
    # 对照组：正常单设备单账号
    ("device", "D2001", "same_device", "account", "A2001"),
]
_GRAPH_REL_LABELS = {
    "same_device": "同设备登录",
    "same_ip": "同IP登录",
    "transfer": "资金转账",
}


def _graph_bfs(entity_type: str, entity_id: str, relation: str | None, depth: int) -> list[dict]:
    """从 (entity_type, entity_id) 出发的有界 BFS（双向边、防环、逐层扩展）。"""
    results: list[dict] = []
    visited: set[tuple[str, str]] = {(entity_type, entity_id)}
    frontier = [(entity_type, entity_id)]
    for level in range(1, depth + 1):
        nxt: list[tuple[str, str]] = []
        for st, si in frontier:
            for row in _GRAPH_EDGES:
                src_t, src_id, rel, dst_t, dst_id = row
                # 双向匹配：正向 src->dst / 反向 dst->src
                if (st, si) == (src_t, src_id):
                    other = (dst_t, dst_id)
                    hop_rel, direction = rel, "out"
                elif (st, si) == (dst_t, dst_id):
                    other = (src_t, src_id)
                    hop_rel, direction = rel, "in"
                else:
                    continue
                if relation and rel != relation:
                    continue
                if other in visited:
                    continue
                visited.add(other)
                results.append(
                    dict(depth=level, rel_type=rel, rel_label=_GRAPH_REL_LABELS.get(rel, rel),
                         direction=direction, dst_type=other[0], dst_id=other[1])
                )
                nxt.append(other)
        frontier = nxt
        if not frontier:
            break
    return results


@mcp.tool()
def graph_query(entity_type: str, entity_id: str, relation: str | None = None,
                depth: int = 1, limit: int = 50, structured: bool = False) -> str:
    """风控关系图查询（只读）：按主体查关系链，识别一机多号、同IP聚集、资金归集等关联风险（一层N层扩展）。
    :param entity_type: 主体类型：account(账号)、device(设备)、ip(IP地址)、order(订单)
    :param entity_id: 主体标识，如 A1001、D1001
    :param relation: 关系类型过滤：same_device(同设备)、same_ip(同IP)、transfer(资金)，不填查全部
    :param depth: 关系扩展层数，默认1层（直接关联），最大2层
    :param limit: 返回关系条数上限，默认50
    """
    entity_type = (entity_type or "").strip().lower()
    entity_id = (entity_id or "").strip()
    if entity_type not in _GRAPH_NODES:
        return "entity_type 不合法，可选值：" + "、".join(_GRAPH_NODES.keys())
    if not entity_id:
        return "请提供 entity_id（主体标识）"
    depth = depth if depth and depth > 0 else 1
    depth = min(depth, 2)
    limit = limit if limit and limit > 0 else 50
    relation = (relation or "").strip() or None

    rels = _graph_bfs(entity_type, entity_id, relation, depth)[:limit]
    if not rels:
        text = f"主体 {entity_type}:{entity_id} 在关系图中无{('「' + relation + '」') if relation else ''}关联记录。"
        return (
            json.dumps(
                {
                    "schema": CASE_TOOL_SCHEMA, "tool": "graph_query", "status": "miss",
                    "subject": f"{entity_type}:{entity_id}", "alert_id": "", "count": 0,
                    "source": "t_case_graph_edge", "items": [], "error": "", "text": text,
                },
                ensure_ascii=False,
            )
            if structured
            else text
        )

    # 风险特征识别（一机多号 / 资金归集）
    out = [f"【关系图查询】主体 {entity_type}:{entity_id}（扩展 {depth} 层）\n",
           f"共 {len(rels)} 条关联关系：\n"]
    for r in rels:
        arrow = "->" if r["direction"] == "out" else "<-"
        out.append(f"  L{r['depth']}  {entity_id} {arrow} [{r['rel_label']}] {r['dst_type']}:{r['dst_id']}")

    # 一机多号检查：同设备挂载的账号数（含主体自身关联）
    by_device: dict[str, list[str]] = {}
    for row in _GRAPH_EDGES:
        if row[2] == "same_device" and row[3] == "account":
            by_device.setdefault(row[1], []).append(row[4])
    multi = {d: a for d, a in by_device.items() if len(a) >= 3 and (entity_id in (d, *a) or depth >= 2)}
    if multi:
        out.append("\n⚠ 一机多号特征:")
        for d, a in multi.items():
            out.append(f"  设备 {d} 挂载 {len(a)} 个账号: {'、'.join(a)}")
    by_target: dict[str, int] = {}
    for row in _GRAPH_EDGES:
        if row[2] == "transfer" and row[3] == "account":
            by_target[row[4]] = by_target.get(row[4], 0) + 1
    funnel = {t: n for t, n in by_target.items() if n >= 3 and any(entity_id in e for e in _GRAPH_EDGES if e[2] == "transfer" and e[4] == t)}
    if funnel:
        out.append("\n⚠ 资金归集特征:")
        for t, n in funnel.items():
            out.append(f"  收款卡 {t} 接收 {n} 个账号转账")
    out.append("\n（数据源: 内存 mock 关系图；PG 边表 t_case_graph_edge 已就绪，真实化阶段切换数据源）")
    text = "\n".join(out).strip()
    if structured:
        items = [
            {
                "src_type": entity_type,
                "src_id": entity_id,
                "relation": r["rel_label"],
                "dst_type": r["dst_type"],
                "dst_id": r["dst_id"],
                "depth": r["depth"],
            }
            for r in rels
        ]
        return json.dumps(
            {
                "schema": CASE_TOOL_SCHEMA, "tool": "graph_query", "status": "hit",
                "subject": f"{entity_type}:{entity_id}", "alert_id": "", "count": len(items),
                "source": "t_case_graph_edge", "items": items, "error": "", "text": text,
            },
            ensure_ascii=False,
        )
    return text


@mcp.tool()
def account_activity(customer_name: str = "", limit: int = 20, alert_id: str = "",
                     subject_type: str = "", structured: bool = False) -> str:
    """查询账号的登录与绑定变更记录（时间、IP、归属地、设备、结果、改绑去向）。

    :param customer_name: 账号/主体标识（不接受 IP/设备名等非账号主体）
    :param limit: 返回记录数上限，默认 20
    :param alert_id: 告警编号；传入后只返回该告警关联的记录（案件隔离）
    :param subject_type: 查询主体类型（account/merchant），留空自动推断
    :param structured: 返回 case_tool_v2 结构化信封（案件调查用）
    """
    limit = limit if limit and limit > 0 else 20
    if CASE_DATA_SOURCE != "live":
        text = f"【账号活动记录】未启用业务数据源，无法查询 {customer_name}"
        return _envelope("account_activity", customer_name, [], text, status="unsupported", alert_id=alert_id) if structured else text
    subj, rejected = _reject_mismatch("account_activity", customer_name, subject_type, alert_id, structured)
    if rejected:
        return rejected
    try:
        rows = bizdb.query_facts(customer_name, kind="login", limit=limit, alert_id=alert_id, subject_type=subj["type"])
        binds = bizdb.query_facts(customer_name, kind="bind", limit=limit, alert_id=alert_id, subject_type=subj["type"])
    except bizdb.DataSourceUnavailable as e:
        text = f"【数据源不可用】{e}（本次未取到账号活动记录，不代表无记录）"
        return _envelope("account_activity", customer_name, [], text, status="error", alert_id=alert_id, error=str(e)) if structured else text
    events = sorted(rows + binds, key=lambda r: str(r.get("time") or ""), reverse=True)
    lines = [f"【账号活动记录】共 {len(events)} 条（登录 {len(rows)} / 绑定变更 {len(binds)}）\n"]
    for i, r in enumerate(events, 1):
        a = fs.attrs(r.get("payload") or {})
        if r["kind"] == "bind":
            lines += [
                f"{i}. [{r.get('time') or r.get('date') or '-'}] 绑定变更",
                f"   类型: 手机号 | 新绑定: {a.get('new_value') or '-'} | 渠道: {a.get('channel') or '-'}",
                f"   验证方式: {a.get('verify_method') or '-'} | 结果: {r.get('status', '-')}",
                f"   详情: {r.get('detail', '')}\n",
            ]
        else:
            lines += [
                f"{i}. [{r.get('time') or r.get('date') or '-'}] 登录",
                f"   IP: {a.get('ip') or '-'} | 归属地: {a.get('geo') or '-'}",
                f"   设备: {a.get('device') or '-'} | 结果: {r.get('status', '-')}",
                f"   详情: {r.get('detail', '')}\n",
            ]
    summary = _login_summary(rows)
    if summary and rows:
        lines.append("\n" + _summary_text(summary))
    text = "\n".join(lines).strip()
    return _envelope(
        "account_activity", customer_name, _fact_items(events), text,
        alert_id=alert_id, summary=summary,
    ) if structured else text


@mcp.tool()
def device_query(customer_name: str = "", limit: int = 10, alert_id: str = "",
                 subject_type: str = "", structured: bool = False) -> str:
    """查询账号关联设备的画像（首次出现时间、近 7 天登录次数、关联账号数）。

    :param customer_name: 账号/主体标识（不接受设备名/设备指纹当查询主体）
    :param limit: 返回记录数上限，默认 10
    :param alert_id: 告警编号；传入后只返回该告警关联的记录（案件隔离）
    :param subject_type: 查询主体类型（account/merchant），留空自动推断
    :param structured: 返回 case_tool_v2 结构化信封（案件调查用）
    """
    limit = limit if limit and limit > 0 else 10
    if CASE_DATA_SOURCE != "live":
        text = f"【设备画像】未启用业务数据源，无法查询 {customer_name}"
        return _envelope("device_query", customer_name, [], text, status="unsupported", alert_id=alert_id) if structured else text
    subj, rejected = _reject_mismatch("device_query", customer_name, subject_type, alert_id, structured)
    if rejected:
        return rejected
    try:
        rows = bizdb.query_facts(customer_name, kind="device", limit=limit, alert_id=alert_id, subject_type=subj["type"])
    except bizdb.DataSourceUnavailable as e:
        text = f"【数据源不可用】{e}（本次未取到设备画像，不代表无记录）"
        return _envelope("device_query", customer_name, [], text, status="error", alert_id=alert_id, error=str(e)) if structured else text
    lines = [f"【设备画像】共 {len(rows)} 条\n"]
    for i, r in enumerate(rows, 1):
        a = fs.attrs(r.get("payload") or {})
        lines += [
            f"{i}. {a.get('device') or '-'}",
            f"   首次出现: {a.get('first_seen') or '-'} | 近7天登录次数: {a.get('logins_7d') or '-'}",
            f"   关联账号数: {a.get('linked_accounts') or '-'} | 归属账号: {a.get('owner_accounts') or '-'}",
            f"   详情: {r.get('detail', '')}\n",
        ]
    text = "\n".join(lines).strip()
    return _envelope("device_query", customer_name, _fact_items(rows), text, alert_id=alert_id) if structured else text


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    transport = "stdio"
    if "--transport" in sys.argv:
        transport = sys.argv[sys.argv.index("--transport") + 1]
    if transport == "sse":
        # host/port 已在构造期写入 mcp 实例，run() 仅指定传输方式
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
