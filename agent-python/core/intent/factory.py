"""意图树工厂：严格对应 ragent rag.core.intent.IntentTreeFactory.buildIntentTree()。

3 维树形结构：DOMAIN（根）→ CATEGORY（中间层）→ TOPIC（叶子）。
默认树：集团信息化（DOMAIN）→ 人事/IT/财务/法务（4 个 CATEGORY）→ 各 TOPIC。

向后兼容：DEFAULT_INTENT_TREE 扁平 list（KB + MCP + SYSTEM 节点）—— 给旧 IntentResolver 用。
"""
from __future__ import annotations

from core.intent.types import IntentNode, IntentKind, IntentLevel


def build_default_intent_tree() -> list[IntentNode]:
    """严格对齐 ragent IntentTreeFactory.buildIntentTree() 默认 3 维树形。"""
    group = IntentNode(id="group", name="集团信息化", level=IntentLevel.DOMAIN, kind=IntentKind.KB)
    group_hr = IntentNode(
        id="group-hr", name="人事", level=IntentLevel.CATEGORY, parent_id="group", kind=IntentKind.KB,
        description="招聘、入职、转正、离职、绩效、薪资、考勤、请假等人力资源相关问题",
        examples=["请假流程是怎样的？", "试用期多久转正？", "迟到会有什么处罚？"],
    )
    group_it = IntentNode(
        id="group-it", name="IT支持", level=IntentLevel.CATEGORY, parent_id="group", kind=IntentKind.KB,
        description="VPN、邮箱、打印机、网络、电脑账号密码、办公软件等 IT 支持相关问题",
        examples=["电脑打印机怎么连？", "公司 VPN 连不上怎么办？", "邮箱密码忘了怎么重置？"],
    )
    group_finance = IntentNode(
        id="group-finance", name="财务", level=IntentLevel.CATEGORY, parent_id="group", kind=IntentKind.KB,
        description="报销、付款、成本中心、预算等财务相关问题",
        examples=["差旅报销需要哪些资料？"],
    )
    group_finance_invoice = IntentNode(
        id="group-finance-invoice", name="发票相关", level=IntentLevel.TOPIC, parent_id="group-finance", kind=IntentKind.KB,
        description="获取公司发票抬头相关信息",
    )
    group_legal = IntentNode(
        id="group-legal", name="法务", level=IntentLevel.CATEGORY, parent_id="group", kind=IntentKind.KB,
        description="合同、纠纷、合规、知识产权等法务相关问题",
        examples=["合同模板在哪里？"],
    )
    group.children = [group_hr, group_it, group_finance, group_legal]
    group_finance.children = [group_finance_invoice]
    return [group]


# 向后兼容：旧 DEFAULT_INTENT_TREE（扁平 list，给旧 IntentResolver 用）
DEFAULT_INTENT_TREE: list[IntentNode] = [
    IntentNode(
        id="kb.company", name="公司制度",
        description="关于公司规章制度、人事、考勤等", kind=IntentKind.KB,
        examples=["年假怎么请", "考勤制度是什么"], collection_names=["company_policy"],
    ),
    IntentNode(
        id="kb.product", name="产品知识",
        description="关于产品功能、使用说明", kind=IntentKind.KB,
        examples=["这个功能怎么用", "产品介绍"], collection_names=["product_doc"],
    ),
    IntentNode(
        id="mcp.weather", name="天气查询",
        description="查询城市天气", kind=IntentKind.MCP, tool_id="weather_query",
        examples=["北京今天天气", "上海会下雨吗"],
    ),
    IntentNode(
        id="mcp.ticket", name="工单查询",
        description="查询客户技术支持工单", kind=IntentKind.MCP, tool_id="ticket_query",
        examples=["有多少待处理工单", "华东区工单统计"],
    ),
    IntentNode(
        id="mcp.sales", name="销售查询",
        description="查询销售数据", kind=IntentKind.MCP, tool_id="sales_query",
        examples=["本月销售额", "张三卖了多少"],
    ),
    IntentNode(
        id="sys.general", name="通用问答",
        description="不属于以上分类的通用问题", kind=IntentKind.SYSTEM,
        examples=["你好", "你是谁"],
    ),
]
