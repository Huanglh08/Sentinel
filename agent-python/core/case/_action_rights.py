"""动作权限模型 —— Security Copilot 三维自主性模型的落地版（实验模块）。

逆向依据（Security Copilot Agent Responsible AI FAQ，2026-08-09 更新）：
  自主性 = 激活触发器(activation triggers) + 访问权限(access permissions)
         + 动作权限(action rights) 三维，配三条硬约束：
  ① 尽量少做不可逆操作；② 关键决策的控制权保留给人；③ 有界操作需适当批准。

现状缺口（见 _replay_action_rights.py 的量化）：
  闸门只有「结论 + 置信度」两维，动作能否自动执行取决于关键词白名单的
  字符串匹配。两个后果：
  - "观察"（只读）与"限制"（影响用户）在白名单内同等待遇，无不可逆性维度；
  - 不可逆动作靠 denylist 9 词拦截，但真实语料的"加入黑名单/加入拦截名单/
    上报公安平台"等写法不含这些关键词 → 漏检（denylist 只有"拉黑"没有"黑名单"）。

本模块把闸门改为三级串联：
  动作不可逆性分级 → 有界性检查 → 触发器检查
并把"无法归类"也归为人工（fail-safe：未知当危险，不当安全）。

以 _ 前缀标识实验性；达标后再决定是否并入 types.py / agent.py。
"""
from __future__ import annotations

from dataclasses import dataclass

from core.case.types import L2_SEVERITY_SET, MANUAL_ACTION_WORDS, SEVERITY_BENIGN

# --------------------------------------------------------------------------
# 动作档位（按不可逆性与影响面）
# --------------------------------------------------------------------------
TIER_READ_ONLY = "read_only"          # 只读/留痕：不改系统状态，错了无代价
TIER_REVERSIBLE = "reversible"        # 可逆变更：可撤销（放行、加验、切会话）
TIER_IRREVERSIBLE = "irreversible"    # 不可逆/高影响：资金、账户权限、外部上报
TIER_UNKNOWN = "unknown"              # 无法归类 → 一律人工（fail-safe）

# 不可逆/高影响动作词。前 9 个为现有 denylist 全集（保持兼容），
# 其后是按真实语料补齐的漏检模式（观察自三轮 557 条建议动作）。
IRREVERSIBLE_WORDS = (
    "封禁", "冻结", "止付", "清退", "拦截", "拉黑", "关停", "下架", "提现",
    # ↓ 现有 denylist 漏检：语料中的实际写法
    "黑名单", "禁用", "注销", "删除", "扣划", "上报", "移交", "永久",
)

# 可逆变更动作词（可撤销、可回滚）
# "限制" 归可逆档：限制领券/限单/限交易都可以撤销；不可逆的是冻结/封禁/清退（见上表）。
REVERSIBLE_WORDS = (
    "放行", "解除", "通知", "提醒", "工单", "升级", "加验", "验证", "人机协同",
    "滑块", "验证码", "下线", "限单", "限制", "限额", "留痕审视",
)

# 只读/记录类动作词（不改状态）
READ_ONLY_WORDS = (
    "观察", "查询", "调取", "留痕", "记录", "标记", "扫描", "监控", "分析",
    "排查", "复核", "补充", "取证", "研判",
)

# 无界作用域词：命中即视为作用面不可枚举 → 降级人工（有界操作原则）
UNBOUNDED_WORDS = (
    "全部", "所有", "全量", "批量", "全平台", "全网", "及其关联", "关联账号",
    "关联子商户", "关联主体", "该团伙", "通杀",
)

# 档位优先级（取命中的最高档）
_TIER_ORDER = (TIER_IRREVERSIBLE, TIER_REVERSIBLE, TIER_READ_ONLY)

# --------------------------------------------------------------------------
# 动作域（结论 × 动作方向）
# --------------------------------------------------------------------------
# 档位管"能不能自动执行"，动作域管"这个结论有没有资格做这个方向的动作"。
# 缺了它会出现两类语义错配：malicious 案件自动"放行"、suspicious 案件自动"限制"。
# 依据 Security Copilot：唯一自动化动作是"关闭**已确认**的误报"（release 只属于 benign），
# suspicious 只允许继续观察/加验/转人工，restrict 属不可逆动作（另由档位拦住）。
ACTION_DOMAINS = {
    "release": ("放行", "解除", "恢复", "解冻", "解封", "撤销限制"),
    "observe": ("观察", "监控", "留痕", "标记", "记录", "研判", "分析", "排查"),
    "verify": ("加验", "验证", "验证码", "人机协同", "滑块", "二次核验", "补验", "限单"),
    # dispatch：派发处置单（把案子交给执行侧），算真实处置而非单纯"告知"
    "dispatch": ("工单", "派单", "处置单", "下发任务"),
    "notify": ("提醒", "通知", "升级", "复核", "取证", "补充", "移交人工"),
    "restrict": (
        "限制", "限额", "下线", "冻结", "封禁", "止付", "拉黑", "黑名单",
        "禁用", "拦截", "清退", "关停", "下架", "注销", "扣划", "上报",
    ),
}

# 每种结论允许的自动动作域；inconclusive 不登记 → 一律人工（未结案不动作）
# 注意 restrict 的边界靠**档位**兜：可逆限制（限制领券/限单）允许自动，
# 不可逆限制（冻结/封禁/清退）一律人工。域层面只排"方向明显错配"：
# benign 不该限制、suspicious/malicious 不该放行。
ALLOWED_DOMAINS = {
    SEVERITY_BENIGN: ("release", "observe", "notify", "dispatch"),
    "suspicious": ("observe", "verify", "notify", "dispatch", "restrict"),
    "malicious": ("observe", "verify", "notify", "dispatch", "restrict"),
}

# 算作"真实处置"的动作域（只读/告知类不算，用于 L2 处置深度锚点）
DISPOSAL_DOMAINS = {"verify", "restrict", "dispatch"}


def action_domains(action: str) -> tuple[str, ...]:
    """动作命中的全部方向域（一个动作可能同时含"冻结"和"留痕"）。"""
    text = action or ""
    return tuple(d for d, words in ACTION_DOMAINS.items() if _hits(text, words))


@dataclass(frozen=True)
class Decision:
    """单个动作的准入判定结果。"""

    action: str
    auto: bool
    tier: str
    unbounded: bool
    reason: str


def default_config() -> dict:
    """三维权限的出厂配置（对齐 Security Copilot 的三维模型）。

    triggers      —— 激活触发器：什么结论/置信度可以进自动通道
    data_scope    —— 访问权限：自动动作只允许作用于本案主体
    action_rights —— 动作权限：可自动的档位；不可逆与未知一律人工
    """
    return {
        "triggers": {
            # 高置信 benign 才自动（沿用现网 CASE_L1_CONFIDENCE 默认）
            "benign_confidence": 0.85,
            # suspicious/malicious 自动处置默认关闭（对齐现网出厂 l2_autopilot_enabled=0）
            "l2_autopilot_enabled": False,
            "l2_confidence": 0.90,
        },
        "data_scope": {
            # 自动动作的作用域：仅本案主体；超出即人工
            "subject_scope": "case",
        },
        "action_rights": {
            "auto_tiers": (TIER_READ_ONLY, TIER_REVERSIBLE),
            "irreversible": "human_only",
            "unknown": "human_only",  # fail-safe
        },
    }


# 否定前缀：紧跟在这些词后面的动作词是被"取消/解除"的对象，不算命中
NEGATION_PREFIXES = ("解除", "取消", "撤销", "恢复", "移除", "关闭")


def _hits(text: str, words: tuple[str, ...]) -> bool:
    """词表命中检测，跳过被否定前缀修饰的词。

    例："放行并解除本次风控拦截" 中的"拦截"是解除对象，不算不可逆动作。
    取 10 字符窗口而非紧邻两字：语料里否定词与动作词之间常有修饰语
    （"解除本次风控拦截"）。
    """
    for w in words:
        idx = text.find(w)
        while idx != -1:
            prefix = text[max(0, idx - 10):idx]
            if not any(p in prefix for p in NEGATION_PREFIXES):
                return True
            idx = text.find(w, idx + 1)
    return False


def classify_action(action: str) -> tuple[str, bool]:
    """把动作归类到档位，并检出无界作用域。

    返回 (tier, unbounded)。取命中词的最高档：一个动作只要含不可逆词，
    哪怕同时含"观察"也按不可逆处理（如"冻结账户并留痕观察"）。
    """
    text = action or ""
    tier = TIER_UNKNOWN
    for t in _TIER_ORDER:
        words = {
            TIER_IRREVERSIBLE: IRREVERSIBLE_WORDS,
            TIER_REVERSIBLE: REVERSIBLE_WORDS,
            TIER_READ_ONLY: READ_ONLY_WORDS,
        }[t]
        if _hits(text, words):
            tier = t
            break
    unbounded = _hits(text, UNBOUNDED_WORDS)
    return tier, unbounded


def decide(action: str, conclusion: str, confidence: float, config: dict | None = None) -> Decision:
    """判定单个动作能否自动执行。

    顺序（不可逆 → 有界 → 触发器 → 档位），任一步不通过即转人工。
    """
    cfg = config or default_config()
    rights = cfg["action_rights"]
    triggers = cfg["triggers"]
    tier, unbounded = classify_action(action)

    if tier == TIER_IRREVERSIBLE:
        return Decision(action, False, tier, unbounded, "动作不可逆/高影响，按策略必须人工")
    if tier == TIER_UNKNOWN:
        return Decision(action, False, tier, unbounded, "动作无法归类，fail-safe 转人工")
    if unbounded:
        return Decision(action, False, tier, unbounded, "作用域不可枚举（有界操作原则），转人工")

    # 动作域：结论有没有资格做这个方向的动作（benign 才能放行；suspicious 不能限制）
    domains = action_domains(action)
    allowed = ALLOWED_DOMAINS.get(conclusion, ())
    if not domains:
        return Decision(action, False, tier, unbounded, "动作不属于任何已登记动作域，fail-safe 转人工")
    mismatch = [d for d in domains if d not in allowed]
    if mismatch:
        return Decision(
            action, False, tier, unbounded,
            f"动作域 {'/'.join(mismatch)} 与结论 {conclusion} 不匹配，转人工",
        )
    # L2 的"转人工"语义动作（发起人工核查、移交人工）本身就是升级，不能再自动执行
    if conclusion in L2_SEVERITY_SET and any(m in (action or "") for m in MANUAL_ACTION_WORDS):
        return Decision(action, False, tier, unbounded, "动作语义为转人工，不得自动执行")

    # 触发器：结论 + 置信度
    conf = float(confidence or 0.0)
    if conclusion == SEVERITY_BENIGN:
        if conf < float(triggers["benign_confidence"]):
            return Decision(action, False, tier, unbounded, f"benign 置信 {conf:.2f} 未达自动阈值")
    elif conclusion in L2_SEVERITY_SET:
        if not triggers["l2_autopilot_enabled"]:
            return Decision(action, False, tier, unbounded, "L2 自动处置未开启，转人工")
        if conf < float(triggers["l2_confidence"]):
            return Decision(action, False, tier, unbounded, f"L2 置信 {conf:.2f} 未达自动阈值")
    else:
        return Decision(action, False, tier, unbounded, f"结论 {conclusion} 无自动通道，转人工")

    if tier not in tuple(rights["auto_tiers"]):
        return Decision(action, False, tier, unbounded, f"档位 {tier} 不在自动白名单")
    return Decision(action, True, tier, unbounded, f"档位 {tier} 且触发器通过")


def split_recs(
    recs: list[dict], conclusion: str, confidence: float, config: dict | None = None
) -> tuple[list[dict], list[dict]]:
    """把建议拆成（自动子集, 人工子集）。

    L2（suspicious/malicious）额外要求：至少存在一个**真实处置**动作
    （动作域 verify / restrict），否则整批转人工 —— 这是原 _split_auto 的
    "处置深度锚点" 检查的动作域版：通知客户、发提醒工单、标记观察都是伴随动作，
    不构成处置（"通知" 属 reversible 档，单看档位会误判为可自动）。
    """
    cfg = config or default_config()
    auto: list[dict] = []
    pending: list[dict] = []
    for r in recs:
        d = decide(str(r.get("action", "")), conclusion, confidence, cfg)
        (auto if d.auto else pending).append({**r, "_tier": d.tier, "_reason": d.reason})
    # 处置深度锚点必须落在**自动子集**里：若自动的只有"标记观察"这类伴随动作，
    # 而唯一的处置动作是"发起人工核查工单"（本就不可自动），整批转人工。
    if conclusion in L2_SEVERITY_SET and not any(
        DISPOSAL_DOMAINS & set(action_domains(str(r.get("action", "")))) for r in auto
    ):
        return [], list(recs)
    return auto, pending


# --------------------------------------------------------------------------
# 自检：最小的可运行断言（ponytail：非平凡逻辑留一个 check）
# --------------------------------------------------------------------------
if __name__ == "__main__":
    cfg = default_config()
    # 不可逆 → 人工（含 denylist 漏检的写法）
    for act in ("冻结账户", "立即冻结账户A1098723全部资金", "加入内部黑名单", "同步上报公安反诈平台"):
        assert not decide(act, "benign", 0.99, cfg).auto, act
    # 无界作用域 → 人工
    assert not decide("批量限制IP段下所有关联账号", "malicious", 0.99, cfg).auto
    # 否定前缀：解除拦截不算不可逆
    assert classify_action("放行并解除本次风控拦截")[0] == TIER_REVERSIBLE
    # 可逆 + 高置信 benign → 自动
    assert decide("放行", "benign", 0.90, cfg).auto
    assert decide("观察72小时", "benign", 0.90, cfg).auto
    # 未知动作 → fail-safe 人工
    assert not decide("执行某种未登记的操作", "benign", 0.99, cfg).auto
    # L2 未开启 → 人工
    assert not decide("放行", "malicious", 0.99, cfg).auto
    # 档位取最高：含不可逆词即不可逆
    assert classify_action("冻结账户并留痕观察")[0] == TIER_IRREVERSIBLE

    # ---- 动作域：结论 × 动作方向（开启 L2 自动后仍必须匹配）----
    l2_on = default_config()
    l2_on["triggers"]["l2_autopilot_enabled"] = True
    assert not decide("放行", "malicious", 0.99, l2_on).auto, "malicious 不能自动放行"
    assert decide("持续观察并留痕", "malicious", 0.99, l2_on).auto, "malicious 可只读观察"
    assert decide("限制账户交易", "suspicious", 0.99, l2_on).auto, "suspicious 可做可逆限制"
    assert not decide("冻结账户", "suspicious", 0.99, l2_on).auto, "不可逆限制一律人工"
    assert not decide("限制领券功能", "benign", 0.99, l2_on).auto, "benign 不能自动限制"
    assert decide("加验滑块验证", "suspicious", 0.99, l2_on).auto, "suspicious 可加验"
    assert not decide("放行", "inconclusive", 0.99, l2_on).auto, "inconclusive 不动作"
    assert action_domains("放行并解除本次风控拦截") == ("release",), "否定前缀不算 restrict"
    print("action_rights self-check: all assertions passed")
