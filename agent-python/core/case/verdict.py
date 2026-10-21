"""S4+S6：结构化证据 → 结论（判据规则化）。

决策记录：`_s4-decisions.md`（D1~D6）。

背景
----
2026-10-17 gate20 复测 95%→85%，3 例「存在可信信号但未覆盖全部异常」被模型判 suspicious，
而 prompt 已明文要求 inconclusive——自然语言判据执行不稳。同时这些案子的 `coverage`
是空的，规则拿不到输入，所以 S6（结构化提取）与 S4（规则表）合并落地。

设计
----
- 模型只负责**抽取 signals**（异常/可信，逐条拆开），结论仍由模型先给；
- 本模块以**有前提的收紧**为主（降级到 inconclusive/suspicious）；唯一升档是 R7，
  前提是模型给出的风险评分落在 High 档（对齐 SC InvestigateFailedSignins）；
- 模型没给 signals（旧输出/漏填）时返回 None，保持原结论，避免凭空改写；
- 规则命中原因写入 `verdict_reason` 并带规则号，便于评测归因。

规则表
------
R1 未登记任何异常信号            → inconclusive / EVIDENCE_INSUFFICIENT
R3 可信解释覆盖全部异常            → 降一级（malicious→suspicious、suspicious→inconclusive，SC isUserTravel）
R5 malicious + 评分档位 <High    → Medium→suspicious / Low|Neutral→inconclusive
R6 suspicious + 评分档位 Low/Neutral → inconclusive / EVIDENCE_INSUFFICIENT
R7 suspicious + 评分档位 High    → malicious（唯一升档）
R8 benign + 评分档位 Medium/High  → inconclusive / EVIDENCE_CONFLICT（不得结案）
R9 证据不足 + 凭证覆盖全部风险动作 → benign（线索证据与工具证据同等效力；接入方式类异常不阻断）
R10 证据不足/可疑 + 无任何凭证解释 + 3 条以上异常且含 1 条单看即高危 → malicious（结构下限）

评分档位口径（SC InvestigateFailedSignins 原文）:
0-30 Neutral / 30-50 Low / 50-74 Medium / >75 High。
档位→四档结论的映射是 Sentinel 的适配（SC 只有风险档，没有结论枚举）。

（R4 已移除：凭证来源可以是线索工单/告警原文或工具结果，不再要求工具复核。）

注：「查无记录 / 无异常 / 暂无数据」类 trusted 不计入（空结果不是肯定性证据，对齐 F5）。

对齐 Security Copilot（L4）：LLM 输出先结构化提取再进自动化（Parse_JSON 单值提取），
升降级都有明确前提条件，无数据不硬凑结论。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_RULES_PATH = Path(__file__).with_name("rules.json")
try:
    _RULES = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    # 词表缺失会让所有凭证判 unknown、全部案子降级，必须 fail fast 而不是静默用空表
    raise RuntimeError(f"判据词表加载失败: {_RULES_PATH}: {exc}") from exc


def _words(section: str, key: str = "markers") -> tuple[str, ...]:
    """从 rules.json 取词表；缺失或为空直接报错，避免规则悄悄失效。"""
    node = _RULES.get(section)
    if isinstance(node, dict):
        node = node.get(key)
    if not isinstance(node, list) or not node:
        raise RuntimeError(f"判据词表 {section}.{key} 缺失或为空")
    return tuple(node)

SEVERITY_BENIGN = "benign"
SEVERITY_SUSPICIOUS = "suspicious"
SEVERITY_MALICIOUS = "malicious"
SEVERITY_INCONCLUSIVE = "inconclusive"

ANOMALY = "anomaly"
TRUSTED = "trusted"

_MAX_SIGNALS = 16
_MAX_TEXT = 160

# 「查无记录」不是肯定性证据（与 F5 的 _EMPTY_OBSERVATION_PATTERNS 同口径）。
# 2026-10-17 全量首跑实测：抽取模型会把「未查到异常工单」「近7天无销售明细」标成
# trusted + tool_verified，用来解释异常；若直接采信，会把恶意/可疑案降成 inconclusive。
_ABSENCE_MARKERS = _words("absence", "markers")
_POSITIVE_MARKERS = _words("absence", "positive")
_ABSENCE_NEGATIONS = _words("absence", "negation")

# 字段为空/缺失是「数据缺口」，不是风险行为。2026-10-19 实测：工具返回
# 「手机号绑定变更（新绑定为空）」被登记成 high 异常，直接把单一接入异常升成
# 「接入+敏感动作」恶意链（R10 误升 4 条），或让良性案卡在"无凭证覆盖"。
_EMPTY_FIELD_MARKERS = _words("empty_field")
# 异常抽取时直接丢弃的「查无记录」类伪信号（空结果不是风险，也不是排除依据）
_ANOMALY_DROP_MARKERS = _words("anomaly_drop")


def is_empty_field_artifact(text: str) -> bool:
    """是否为「字段为空/缺失」型数据缺口（不构成异常信号）。"""
    return any(m in str(text or "") for m in _EMPTY_FIELD_MARKERS)


def is_same_order_split(text: str) -> bool:
    """同一订单号拆成多笔支付 = 支付重试/渠道限额，不是拆单规避。

    拆单规避指**多个不同订单**各自把金额卡在阈值下（C06）。2026-10-19 成对集实测：
    抽取器把「订单 OD-771 在 43 秒内分两笔」登记成异常，良性案因此卡在证据不足。
    """
    t = str(text or "")
    cfg = _RULES["same_order_split"]
    if any(k in t for k in cfg["exclude"]):
        return False
    return any(k in t for k in cfg["order"]) and any(k in t for k in cfg["split"])

def is_absence_claim(text: str) -> bool:
    """判断可信信号是否为「查无/无异常」类空结果。

    先看显式否定词（无/未/没有/暂未…），但含「一致/吻合/闭环/已核实」等肯定词的
    不算空结果（"无变更且指纹一致" 是正面核验）。2026-10-17 实测模型会写
    "无历史工单与交易记录"、"无资损记录" 这类长句，原 marker 列表覆盖不到。
    """
    t = str(text or "")
    if any(m in t for m in _ABSENCE_MARKERS):
        return True
    if any(p in t for p in _POSITIVE_MARKERS):
        return False
    return any(n in t for n in _ABSENCE_NEGATIONS)


# ---------------------------------------------------------------------------
# 可信信号的「依据类型」（S7）：trusted 必须是能解释异常为何属正常业务的替代解释，
# 不能是"动作已发生"的确认、当事人的空口自述、查无记录或风险陈述。
# 依据字段来自业务事实表 biz_case_fact 的可用凭证：工单/报备单号、审批记录、
# 官方核验返回（code:0/一致）、备案与资质。分类放在代码里，不交给提示词自觉。
# ---------------------------------------------------------------------------
_SELF_CLAIM_PATTERNS = _words("self_claim")
# 凭证标识词外置在 rules.json：新增「协议/签约/合同/授权」这类凭据不用改代码
_CREDENTIAL_MARKERS = _words("credential", "markers")
# 数据比对证据（工具计算，非模型自述）：只用于量值型异常的证伪
_DATA_CHECK_MARKERS = _words("data_check")
_ACTION_CONFIRM_MARKERS = _words("action_confirmation")
_RISK_MARKERS = _words("risk")


# 异常信号的「环节」（2026-10-18）：接入方式 vs 风险动作。
# 接入异常 = 新设备 / 新 IP / 异地或境外登录，它本身不是风险行为，只是风险动作的载体。
# 抽取器按提示词从不把「新设备」写进可信解释的覆盖清单（报备只解释行程），若 R9 仍要求
# 覆盖全部异常，所有「新设备 + 良性」案永远无法恢复（148 条实测 23 条卡在这里）。
# 只放宽 R9（恢复良性），不放宽 R3（降档）：降档会赦免未解释的高危动作，风险不对称。
_ACTION_MARKERS = _words("action_markers")
_ACCESS_MARKERS = _words("access_markers")

# 需要独立凭证的敏感动作：报备/工单只解释"人在哪"，不解释"为什么做这个动作"。
_SENSITIVE_ACTIONS = _words("sensitive_actions")


def is_access_anomaly(text: str) -> bool:
    """是否为接入方式类异常（新设备 / 新 IP / 异地登录）。

    含任一动作词即按风险动作处理——"新设备登录后 32 分钟改绑手机号" 是行为链，
    不能因为句子提到设备就整条豁免；认不出的异常一律按风险动作处理（宁严勿松）。
    """
    t = str(text or "")
    if any(m in t for m in _ACTION_MARKERS):
        return False
    return any(m in t for m in _ACCESS_MARKERS)


# 量值型异常：只说「多少算正常」（交易额/订单数/退款率/登录次数），不含具体高危动作。
# 业务报备覆盖的是**事件本身**，同一事件的规模、频率、地域参数属于同一事件，
# 不能再拆成独立异常要求逐条解释（2026-10-19：群发 482 人、直播 6 倍增长、
# 夜间 4200 元都被拆成独立异常，凭证只覆盖其中一条，案子停在可疑或证据不足）。
_QUANTITY_MARKERS = _words("quantity")
_ACTION_ANOMALY_MARKERS = _words("action_anomaly")
# 时间与地域是事件的参数，不是独立动作：夜间经营报备、订阅协议可以覆盖它们。
# 但它们不能被交易额基线证伪，所以只进事件覆盖，不进数据比对升档。
_TIME_PARAM_MARKERS = _words("time_param")
_REGION_PARAM_MARKERS = _words("region_param")


def is_quantity_anomaly(text: str) -> bool:
    """量值型异常：能被历史交易/登录基线证伪的那一类。"""
    t = str(text or "")
    if any(k in t for k in _ACTION_ANOMALY_MARKERS) or any(k in t for k in _REGION_PARAM_MARKERS):
        return False
    return any(k in t for k in _QUANTITY_MARKERS)


# 主体不可核查：判据 inconclusive 特征①，不能被「零异常 → 告警误报」的放行规则吃掉
_MISSING_SUBJECT_MARKERS = _words("missing_subject")


def has_missing_subject(text: str) -> bool:
    t = str(text or "")
    return any(m in t for m in _MISSING_SUBJECT_MARKERS)


def is_event_parameter(text: str) -> bool:
    """可由业务报备覆盖的事件参数：量值、时间窗口、地域范围。

    报备覆盖的是事件本身，事件发生的时段、规模、地域都是它的属性；
    动作型异常（改绑、转账、陌生收款方、异地登录）不在此列，仍需独立凭证。
    """
    t = str(text or "")
    if any(k in t for k in _ACTION_ANOMALY_MARKERS):
        return False
    return (
        any(k in t for k in _QUANTITY_MARKERS)
        or any(k in t for k in _TIME_PARAM_MARKERS)
        or any(k in t for k in _REGION_PARAM_MARKERS)
    )


def _has_risk_statement(text: str) -> bool:
    """是否含未被否定的风险词（"未命中"/"无黑名单"不算风险陈述）。"""
    for marker in _RISK_MARKERS:
        idx = text.find(marker)
        while idx != -1:
            if not any(p in text[max(0, idx - 2):idx] for p in ("未", "无", "非")):
                return True
            idx = text.find(marker, idx + 1)
    return False


def trusted_basis(text: str) -> str:
    """把 trusted 信号归类：absence / risk_statement / self_claim / credential / action_confirmation / unknown。

    只有 credential 可以用于降级（R3）或恢复良性（R9）。
    """
    t = str(text or "")
    if is_absence_claim(t):
        return "absence"
    if _has_risk_statement(t):
        return "risk_statement"
    if any(m in t for m in _SELF_CLAIM_PATTERNS) or any(q in t for q in ("称“", "称‘", "称'", '称"')):
        return "self_claim"
    if any(m in t for m in _DATA_CHECK_MARKERS):
        # 工具侧数据比对（本日 vs 历史基线），可证伪量值型异常
        return "data_check"
    if any(m in t for m in _CREDENTIAL_MARKERS):
        return "credential"
    if any(m in t for m in _ACTION_CONFIRM_MARKERS):
        return "action_confirmation"
    return "unknown"


@dataclass(frozen=True)
class Decision:
    """规则改写结果（None 表示规则不介入）。"""

    conclusion: str
    confidence: float
    reason_code: str
    verdict_reason: str
    rule: str


def normalize_signals(raw: list | None) -> list[dict]:
    """把模型输出规整成规则表可用的结构，丢弃不认识的条目与重复异常。

    同一段异常被抽取器重复登记（实测同句出现三次）会把"未覆盖动作"放大，
    但覆盖清单只指向其中一个 id，导致本该全覆盖的案子被误判成有残留。
    去重在代码侧做，不依赖模型自觉。
    """
    out: list[dict] = []
    seen_anomaly: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict) or len(out) >= _MAX_SIGNALS:
            continue
        kind = str(item.get("kind", "")).strip().lower()
        text = str(item.get("text", "")).strip()[:_MAX_TEXT]
        if kind == ANOMALY:
            if not text or text in seen_anomaly:
                continue
            source = str(item.get("source", "")).strip().lower()
            # 工具「查无记录」不是异常信号（抽取器规则 8）。2026-10-18 实测模型会把
            # 「该设备在系统中无任何画像/关联记录」登记成 anomaly，再因无凭证覆盖判
            # inconclusive；空结果既不是风险也不是排除依据，代码侧直接丢弃。
            # 「查无记录」类空结果是数据缺口，不是异常信号，与来源无关。
            # 用窄词表：避免把「新设备登录，无历史记录」这类复合句整条丢掉。
            if any(m in text for m in _ANOMALY_DROP_MARKERS):
                continue
            # 字段为空/缺失是数据缺口，不是风险行为（与来源无关：clue 侧同样会这么写）。
            if is_empty_field_artifact(text):
                continue
            # 同一订单拆多笔支付是支付重试，不是拆单规避（C06：拆单指多个不同订单）。
            if is_same_order_split(text):
                continue
            seen_anomaly.add(text)
            out.append(
                {
                    "id": str(item.get("id") or f"a{len(out) + 1}").strip(),
                    "kind": ANOMALY,
                    "text": text,
                    "severity": "high" if str(item.get("severity", "")).strip().lower() == "high" else "normal",
                    "source": source if source in {"clue", "tool", "authority"} else "clue",
                }
            )
        elif kind == TRUSTED:
            covers = [str(x).strip() for x in (item.get("covers") or []) if str(x).strip()]
            out.append(
                {
                    "kind": TRUSTED,
                    "text": text,
                    "covers": covers[:8],
                    "tool_verified": bool(item.get("tool_verified", False)),
                    # 结构化凭证引用（单号/签约号），由代码从线索补，不依赖模型转述
                    **({"evidence_ref": item["evidence_ref"]} if item.get("evidence_ref") else {}),
                }
            )
    return out


# Security Copilot InvestigateFailedSignins 的评分档位（原文 0-30 Neutral /
# 30-50 Low / 50-74 Medium / >75 High）。档位→四档结论的映射是 Sentinel 的适配。
BAND_NEUTRAL = "neutral"
BAND_LOW = "low"
BAND_MEDIUM = "medium"
BAND_HIGH = "high"


def risk_band(score: int) -> str:
    if score <= 30:
        return BAND_NEUTRAL
    if score <= 50:
        return BAND_LOW
    if score < 75:
        return BAND_MEDIUM
    return BAND_HIGH


def decide(
    conclusion: str,
    confidence: float,
    signals: list[dict],
    risk_score: int = -1,
    clue: str = "",
) -> Decision | None:
    """按规则表校验模型结论；返回 None 表示不介入。"""
    anomalies = [s for s in signals if s.get("kind") == ANOMALY]
    # 只有凭证型可信信号参与降级/恢复：动作确认、空口自述、查无、风险陈述一律不采信。
    trusted = [
        s
        for s in signals
        if s.get("kind") == TRUSTED
        and (trusted_basis(s.get("text", "")) == "credential" or s.get("evidence_ref"))
    ]

    # 模型没给结构化证据 → 不改写（旧输出兼容；不能凭空推翻结论）
    if not signals:
        return None

    # R1：零异常信号。工具查到的东西若真是异常，模型应登记为 anomaly。
    if not anomalies:
        if conclusion == SEVERITY_INCONCLUSIVE:
            return None
        # 良性 + 零异常：判据原文「线索本身没有任何异常行为时是告警误报，直接判 benign，
        # 不要求额外凭证」。2026-10-19 实测 R1 把 7 条商户正常波动降成 inconclusive
        # （模型已判良性、只是审计没列出异常），故此处放行；线索含硬风险词时仍收紧。
        if (
            conclusion == SEVERITY_BENIGN
            and not has_missing_subject(str(clue or ""))
            and (
                any(
                    s.get("kind") == TRUSTED
                    and trusted_basis(s.get("text", "")) in {"credential", "data_check"}
                    for s in signals
                )
                or not _has_risk_statement(str(clue or ""))
            )
        ):
            return None
        return Decision(
            SEVERITY_INCONCLUSIVE,
            min(confidence, 0.45),
            "EVIDENCE_INSUFFICIENT",
            "R1 未登记任何异常信号，无法建立风险结论（零信号不能结案）",
            "R1",
        )

    covered = {aid for t in trusted for aid in (t.get("covers") or [])}
    uncovered = [a for a in anomalies if a.get("id") not in covered]

    # 事件级覆盖：凭证已覆盖某条事件参数时，同案的规模/频率/时段/地域一并视为已解释。
    # 动作型异常（改绑/转账/陌生收款方/异地登录）不受此收敛，仍需独立凭证。
    if any(
        a.get("id") in covered and is_event_parameter(a.get("text", "")) for a in anomalies
    ):
        remaining = [a for a in uncovered if not is_event_parameter(a.get("text", ""))]
        if len(remaining) < len(uncovered):
            covered |= {a.get("id") for a in uncovered if is_event_parameter(a.get("text", ""))}
            uncovered = remaining

    # R3：审批降级（对齐 SC isUserTravel）——可信解释覆盖**全部**异常时，
    # "降低事件严重级别"而不是结案：malicious→suspicious、suspicious→inconclusive。
    # SC 原文是经理审批通过后 reduce the severity，不直接关闭事件。
    # 2026-10-18 全量实测：只要求"覆盖任一异常"会把 14 条恶意案整案降档——
    # 报备只解释了异地登录，改绑/转账等未解释的高危动作仍成立，不得一起降级。
    if trusted and covered and not uncovered and conclusion in {SEVERITY_MALICIOUS, SEVERITY_SUSPICIOUS}:
        if conclusion == SEVERITY_MALICIOUS:
            return Decision(
                SEVERITY_SUSPICIOUS,
                min(confidence, 0.7),
                "",
                "R3 可信解释（线索凭证）→ malicious 降一级 suspicious",
                "R3",
            )
        return Decision(
            SEVERITY_INCONCLUSIVE,
            min(confidence, 0.45),
            "EVIDENCE_INSUFFICIENT",
            "R3 可信解释（线索凭证）→ suspicious 降一级 inconclusive",
            "R3",
        )

    # R9：线索证据与工具证据同等效力（2026-10-18）。
    # reporter 在工具查无时习惯性给 inconclusive，即使异常已被凭证型可信解释完整覆盖。
    # 设计口径是"告警清单与工具查询任一方有证据即可"，因此规则层在此恢复 benign；
    # 置信度压到 0.82（低于 L1 自动放行阈值 0.85，规则推断的良性先走人审）。
    # 2026-10-18 修正：只要求**风险动作**被覆盖，接入方式类异常（新设备/新 IP/异地）
    # 未覆盖也放行——它只是动作的载体，抽取器按提示词不会把它写进 covers，
    # 拿它卡门禁等于让整族"新设备良性案"永远结不了案。R3 仍要求全覆盖，不放松。
    blocking = [a for a in uncovered if not is_access_anomaly(a.get("text", ""))]
    # 凭证强度门槛（2026-10-19）：R9 在 148 条上 8 对 6 错，错的 4 条都是"单条未核验报备"
    # 把 lookalike 翻成 benign。报备属当事人自述，单条不足以推翻 inconclusive；
    # 至少要一条工具核验过的凭证，或两条以上独立凭证。
    anchor = [s for s in trusted if s.get("covers")]
    strong_cred = any(s.get("tool_verified") for s in anchor) or len(anchor) >= 2
    # 报备/工单不是"动作许可"：它解释"人在哪"，不解释"为什么改绑/改密/提现/支付"。
    # 2026-10-19 实测 6 条漏放行里 4 条是这类（报备被当成敏感动作的解释）。
    weak_scope = any(
        any(k in str(s.get("text", "")) for k in ("报备", "工单", "审批"))
        and not any(k in str(s.get("text", "")) for k in ("核验", "一致", "MATCH"))
        and any(
            a.get("id") in (s.get("covers") or [])
            and any(x in str(a.get("text", "")) for x in _SENSITIVE_ACTIONS)
            for a in anomalies
        )
        for s in trusted
    )
    if conclusion == SEVERITY_INCONCLUSIVE and trusted and covered and not blocking and strong_cred and not weak_scope:
        if uncovered:
            reason = (
                "R9 风险动作已被凭证型可信解释完整覆盖 → benign"
                f"（残留接入方式类异常 {len(uncovered)} 条，不构成风险行为）"
            )
        else:
            reason = "R9 异常已被凭证型可信解释完整覆盖 → benign（线索证据与工具证据同等效力）"
        return Decision(SEVERITY_BENIGN, 0.82, "", reason, "R9")

    # 可信解释覆盖全部异常 → 规则不介入（凭证可以来自线索工单，也可以是工具结果，
    # 二者同等有效；是否采信由 reporter 判据决定，规则不再要求工具复核）
    if trusted and not uncovered:
        return None

    # R10：恶意证据下限（结构判定，先于评分档位；2026-10-18）。
    # 判据原文已写明"多条独立风险信号互证即达 malicious 下限"，但 reporter 会把
    # 3~5 条异常、含高危动作的案子判成 inconclusive，并把自评分压在 48
    # （实测恶意案的风险分在 48~86 之间混排，拿它当门禁会系统性漏判）。
    # 2026-10-19 修正：计数只算**未被凭证覆盖**的异常。原来只要有 trusted 就整体跳过，
    # 没有 trusted 时又按全量计数——4 条商户正常波动被「3 条异常中 1 条高危」误升恶意。
    # 判据口径是"凭证已覆盖的异常不再计入高危动作计数"，部分覆盖仍按剩余异常计。
    if conclusion in {SEVERITY_INCONCLUSIVE, SEVERITY_SUSPICIOUS}:
        # 高危只认动作型：量值/时段/地域即使被标 high 也不进下限计数，
        # 否则新店首日交易额这类事件参数会把案子推到恶意（158 实测 3 条高危全来自业务量）
        high = [
            a
            for a in uncovered
            if str(a.get("severity", "")).lower() == "high"
            and not is_event_parameter(a.get("text", ""))
        ]
        if len(uncovered) >= 3 and high:
            return Decision(
                SEVERITY_MALICIOUS,
                min(max(confidence, 0.8), 0.9),
                "",
                f"R10 {len(uncovered)} 条未解释异常中 {len(high)} 条单看即高危，达恶意下限",
                "R10",
            )

    # 以下按 SC InvestigateFailedSignins 的风险评分档位校验结论是否自洽。
    # 模型没给评分（旧输出/截断）→ 不做档位校验，只保留上面的结构校验。
    if risk_score is None or risk_score < 0:
        return None
    band = risk_band(risk_score)
    band_cn = {BAND_NEUTRAL: "Neutral", BAND_LOW: "Low", BAND_MEDIUM: "Medium", BAND_HIGH: "High"}[band]

    # R5：malicious 只允许落在 High 档；Medium 降 suspicious，Low/Neutral 降 inconclusive
    if conclusion == SEVERITY_MALICIOUS:
        if band == BAND_HIGH:
            return None
        if band == BAND_MEDIUM:
            return Decision(
                SEVERITY_SUSPICIOUS,
                min(confidence, 0.7),
                "",
                f"R5 评分档位 Medium（{risk_score}）不足以判 malicious，降 suspicious",
                "R5",
            )
        return Decision(
            SEVERITY_INCONCLUSIVE,
            min(confidence, 0.45),
            "EVIDENCE_INSUFFICIENT",
            f"R5 评分档位 {band_cn}（{risk_score}）不足以判 malicious",
            "R5",
        )

    # R6/R7：suspicious 对应 Medium 档；High 升 malicious，Low/Neutral 降 inconclusive
    if conclusion == SEVERITY_SUSPICIOUS:
        if band == BAND_HIGH:
            return Decision(
                SEVERITY_MALICIOUS,
                min(max(confidence, 0.8), 0.9),
                "",
                f"R7 评分档位 High（{risk_score}）高于 suspicious，按档位升 malicious",
                "R7",
            )
        if band in (BAND_LOW, BAND_NEUTRAL):
            return Decision(
                SEVERITY_INCONCLUSIVE,
                min(confidence, 0.45),
                "EVIDENCE_INSUFFICIENT",
                f"R6 评分档位 {band_cn}（{risk_score}）不足以判 suspicious",
                "R6",
            )
        return None

    # R8：benign 只允许落在 Neutral 档；Medium/High 与"无风险"自相矛盾，不得结案
    if conclusion == SEVERITY_BENIGN and band in (BAND_MEDIUM, BAND_HIGH):
        return Decision(
            SEVERITY_INCONCLUSIVE,
            min(confidence, 0.45),
            "EVIDENCE_CONFLICT",
            f"R8 评分档位 {band_cn}（{risk_score}）与 benign 不符，不得结案",
            "R8",
        )

    return None
