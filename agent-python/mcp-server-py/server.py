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

import os
import random
import sys
from datetime import date, datetime, timedelta

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ragent-mcp-server")


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
    seed = d.toordinal() * 31 + hash(city) % (2**31)
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

    today = date.today()
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
    today = date.today()
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


@mcp.tool()
def ticket_query(region: str = None, status: str = None, priority: str = None,
                 product: str = None, customer_name: str = None,
                 query_type: str = "summary", limit: int = 10) -> str:
    """查询客户技术支持工单数据，支持按地区、状态、优先级、产品、客户等维度筛选，支持汇总概览、工单列表、统计分析等多种查询。
    :param region: 地区筛选：华东、华南、华北、西南、西北，不填则查询全国
    :param status: 工单状态：待处理、处理中、已解决、已关闭
    :param priority: 优先级：紧急、高、中、低
    :param product: 产品：企业版、专业版、基础版
    :param customer_name: 客户名称关键字，支持模糊匹配
    :param query_type: 查询类型：summary(汇总概览)、list(工单列表)、stats(统计分析)
    :param limit: 返回记录数限制，默认10
    """
    query_type = query_type or "summary"
    limit = limit if limit and limit > 0 else 10
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
    now = date.today()
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
                sales_person: str = None, query_type: str = "summary", limit: int = 10) -> str:
    """查询软件销售数据，支持按地区、时间、产品、销售人员等维度筛选，支持汇总统计、排名、明细列表等多种查询。
    :param region: 地区筛选：华东、华南、华北、西南、西北，不填则查询全国
    :param period: 时间段：本月、上月、本季度、上季度、本年，默认本月
    :param product: 产品筛选：企业版、专业版、基础版
    :param sales_person: 销售人员姓名，不填则查询全部销售
    :param query_type: 查询类型：summary(汇总)、ranking(排名)、detail(明细)、trend(趋势)
    :param limit: 返回记录数限制，默认10
    """
    period = period or "本月"
    query_type = query_type or "summary"
    limit = limit if limit and limit > 0 else 10
    s, e = _sales_range(period)
    data = _filter_sales(_gen_sales(s, e), region, product, sales_person)

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
# 入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    transport = "stdio"
    if "--transport" in sys.argv:
        transport = sys.argv[sys.argv.index("--transport") + 1]
    if transport == "sse":
        # 显式指定 host/port，便于 Java 端 SSE 远程接入（默认 http://127.0.0.1:8000/sse）
        mcp.run(transport="sse", host="127.0.0.1", port=8000)
    else:
        mcp.run(transport="stdio")
