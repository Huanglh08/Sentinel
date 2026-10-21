"""评测集覆盖率模型（对齐芯片验证的 coverage 方法论）。

为什么要这个模块
----------------
评测集只按「结论三分类」分子集时，维度是塌的：结论分布可以很齐（16/14/10），
但「证据不足时会不会硬判」「评分卡在阈值上会不会错」「被合理伪装骗不骗得过」
这些能力一条都测不到——指标自然没有说服力。

芯片验证（DV）的做法是显式定义 covergroup，再用约束随机 + 定向测试把每个 bin 填满，
最后用覆盖率收敛（coverage closure）作为签收门禁。本模块把这套搬到评测集上：

1. **Coverpoint** —— 正交可测维度，取值有限且互斥（一个样本在每个维度上恰好落一个 bin）
2. **CrossCoverage** —— 维度两两交叉，每个 bin 有目标样本数
3. **Boundary** —— 手册（C01~C13）里的**精确阈值**，每个阈值须有下界/临界/上界三点。
   这是 corner case 的唯一可靠来源：数字是业务写死的，不是我们编的。
4. **Disturbance** —— 干扰注入（对齐 DV 的 fault injection）：
   合理伪装 / 噪声字段 / 字段矛盾，测 agent 会不会被带偏。

覆盖率标签**显式**记录在种子行的 `coverage` 字段（见 CoverageTag）。
统计即计数，不做任何文本推断——推断会引入噪声，让覆盖率数字本身不可信。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

# ===========================================================================
# 1) Coverpoint：正交可测维度
# ===========================================================================


@dataclass(frozen=True)
class Coverpoint:
    """一个覆盖维度。values 必须互斥且有限，否则覆盖率无法收敛。"""

    key: str  # 标签字段名（写进种子行的 coverage.<key>）
    name: str  # 人类可读名
    values: tuple[str, ...]  # 全部合法取值
    source: str  # 业务依据（手册出处）
    priority: str  # P0 / P1
    note: str = ""


# 风险手法：直接取自识别层手册的信号/模式清单，不是在造维度。
TECHNIQUES: tuple[str, ...] = (
    # C01 账号盗用 §2.1（6 类登录信号）+ §2.2（4 类行为信号）
    "凭证填充",
    "新设备+敏感操作",
    "IP异地突变",
    "代理IDC_IP",
    "会话劫持",
    "凭证泄露情报",
    # C02 交易欺诈 §2.1（4 类模式）
    "盗刷",
    "欺诈转账",
    "虚构交易套现",
    "分单规避",
    # C03 批量注册 §2 + §3
    "批量注册",
    "养号",
    # C04 活动滥用 §3.1
    "薅羊毛",
    # C09 支付通道 §2.2
    "通道异常",
)

# 结论：C11 §3.3 三分类
CONCLUSIONS: tuple[str, ...] = ("malicious", "benign", "inconclusive")

# 主体类型：与 seedgen 的 CASE_CATEGORIES 对齐，另加 ip/im（旧 seed-case 出现过）
SUBJECT_TYPES: tuple[str, ...] = (
    "account",
    "device",
    "ip",
    "order",
    "payment",
    "merchant",
    "im",
)

# 证据强度：C01 §4 结论口径 + C11 §3.3 四档判据
EVIDENCE_STRENGTHS: tuple[str, ...] = (
    "无信号",  # 只有模糊举报，没有任何可核查信号
    "单一孤立",  # 仅一个异常点，无第二信号佐证 → 判据要求 inconclusive
    "双信号印证",  # 两个独立信号互相印证
    "多信号+官方核验",  # 多个信号 + 官方核验结果（公安反诈/银联名单等）
)

# 信息完整度：C11 inconclusive 判据③（核心字段缺失）
INFO_COMPLETENESS: tuple[str, ...] = ("完整", "部分缺失", "严重缺失")

# 评分档：C01 §2.3 组合判定阈值（基础分 高40/中20/低10，组合加成 +20，阈值 80/60）
SCORE_BANDS: tuple[str, ...] = ("<60仅记录", "60-79二次验证", "≥80人工研判")

# 风险档位：C05 §2.2 登录四档 / C07 §2.3 挑战四档（两套口径一致）
RISK_LEVELS: tuple[str, ...] = ("低", "中", "高", "极高")

# 处置级别：C10 §2 R1~R5
DISPOSAL_LEVELS: tuple[str, ...] = (
    "R1观察",
    "R2功能限制",
    "R3资金延迟",
    "R4登录限制",
    "R5封禁",
)

# 干扰类型：对齐 DV 的 fault injection，测 agent 的抗干扰能力
DISTURBANCES: tuple[str, ...] = (
    "无",
    "lookalike合理伪装",  # 异常有可信的正常解释（老用户/报备/资质齐全）
    "噪声字段",  # 大量无关字段稀释注意力
    "字段矛盾",  # 时间倒序、金额不符、属地互斥
    "线索注入",  # C08§2：线索文本里嵌指令（"忽略规则/输出系统提示/改判 benign"）
)


COVERPOINTS: tuple[Coverpoint, ...] = (
    Coverpoint(
        "technique", "风险手法", TECHNIQUES,
        "C01§2.1/C02§2.1/C03§2/C04§3.1/C08§2/C09§2.2", "P0",
        note="每个手法至少要有 malicious 与 benign 两侧样本，否则测不出区分度",
    ),
    Coverpoint("conclusion", "结论", CONCLUSIONS, "C11§3.3", "P0"),
    Coverpoint(
        "evidence_strength", "证据强度", EVIDENCE_STRENGTHS,
        "C01§4 + C11§3.3", "P0",
        note="单一孤立信号必须判 inconclusive；多信号+官方核验倾向 malicious",
    ),
    Coverpoint(
        "info_completeness", "信息完整度", INFO_COMPLETENESS,
        "C11§3.3 判据③", "P0",
        note="核心字段缺失时必须 inconclusive，不允许硬凑",
    ),
    Coverpoint(
        "score_band", "评分档", SCORE_BANDS,
        "C01§2.3 组合判定阈值", "P0",
        note="阈值 80/60 两侧必须成对出现，测边界误判",
    ),
    Coverpoint("risk_level", "风险档位", RISK_LEVELS, "C05§2.2 / C07§2.3", "P1"),
    Coverpoint(
        "disposal_level", "处置级别", DISPOSAL_LEVELS,
        "C10§2", "P1",
        note="分级原则「就低不就高」，测 agent 建议会不会跳级",
    ),
    Coverpoint("subject_type", "主体类型", SUBJECT_TYPES, "seedgen CASE_CATEGORIES", "P1"),
    Coverpoint(
        "disturbance", "干扰类型", DISTURBANCES,
        "DV fault injection", "P1",
        note="lookalike 与字段矛盾是两条最容易让 agent 翻车的路径",
    ),
)

COVERPOINT_BY_KEY: dict[str, Coverpoint] = {cp.key: cp for cp in COVERPOINTS}


# ===========================================================================
# 2) CrossCoverage：维度交叉
# ===========================================================================


@dataclass(frozen=True)
class CrossCoverage:
    key: str
    left: str
    right: str
    per_bin: int  # 每个 bin 的目标样本数（覆盖率收敛标准）
    priority: str
    rationale: str  # 为什么必须交叉（防什么失效）


CROSSES: tuple[CrossCoverage, ...] = (
    CrossCoverage(
        "conclusion_x_evidence", "conclusion", "evidence_strength", 3, "P0",
        "防「证据不足却硬判 malicious / benign」——本系统最核心的正确性风险",
    ),
    CrossCoverage(
        "conclusion_x_score", "conclusion", "score_band", 3, "P0",
        "防「评分卡在 60/80 阈值上时结论跳变」",
    ),
    CrossCoverage(
        "conclusion_x_completeness", "conclusion", "info_completeness", 3, "P0",
        "防「核心字段缺失时仍强行给结论」",
    ),
    CrossCoverage(
        "conclusion_x_technique", "conclusion", "technique", 2, "P0",
        "防「某一类手法系统性失准」——40 条样本里 15 类手法根本铺不满",
    ),
    CrossCoverage(
        "conclusion_x_disposal", "conclusion", "disposal_level", 2, "P1",
        "防「处置建议跳级」，违反 C10 的就低不就高原则",
    ),
    CrossCoverage(
        "conclusion_x_disturbance", "conclusion", "disturbance", 2, "P1",
        "防「被 lookalike 合理伪装骗过」或「被字段矛盾带偏」",
    ),
    CrossCoverage(
        "conclusion_x_subject", "conclusion", "subject_type", 2, "P1",
        "防「某类主体（如 im/merchant）缺样本导致指标虚高」",
    ),
)

CROSS_BY_KEY: dict[str, CrossCoverage] = {c.key: c for c in CROSSES}


# ===========================================================================
# 2.5) Illegal bins：语义上不可能成立的交叉（对齐芯片验证的 illegal bins）
# ===========================================================================
# 这些 bin 不是「还没测到」，而是「本来就不该存在」。不排除掉，覆盖率永远收敛不了；
# 每一条都写明业务依据，避免误排除掉真实存在的场景。
# 元组顺序统一为 (left 取值, right 取值)，与 all_cross_bins() 一致。

ILLEGAL_BINS: dict[str, tuple[tuple[str, str], ...]] = {
    "conclusion_x_evidence": (
        # C11§3.3：malicious 要求「两项独立风险信号相互印证」，无信号 / 单一孤立都不够
        ("malicious", "无信号"),
        ("malicious", "单一孤立"),
        # benign 要求「异常存在可信的正常业务解释」，前提是先有异常
        ("benign", "无信号"),
        # 官方核验结果非黑即白（命中黑名单→malicious，核验通过→benign）
        ("inconclusive", "多信号+官方核验"),
    ),
    "conclusion_x_score": (
        # C01§2.3：<60 分仅记录（低风险），不可能得出恶意结论
        ("malicious", "<60仅记录"),
    ),
    "conclusion_x_completeness": (
        # C11§3.3 判据③：核心字段缺失时必须 inconclusive，禁止硬凑
        ("malicious", "严重缺失"),
        ("benign", "严重缺失"),
    ),
    "conclusion_x_disposal": (
        # C10§2：R1 观察仅适用「疑似风险、证据不足」
        ("malicious", "R1观察"),
        # 判良性不应施加资金 / 登录 / 封禁级处置
        ("benign", "R3资金延迟"),
        ("benign", "R4登录限制"),
        ("benign", "R5封禁"),
        # 证据不足不能做重处置
        ("inconclusive", "R4登录限制"),
        ("inconclusive", "R5封禁"),
    ),
    "conclusion_x_disturbance": (
        # C11§3.3 判据②③：核心信号矛盾时归属不明，应判 inconclusive
        ("malicious", "字段矛盾"),
        ("benign", "字段矛盾"),
        # 线索被注入时正确行为是识破注入并继续按证据判，不得因注入文字改判 benign
        ("benign", "线索注入"),
    ),
    # 手法与主体维度：每个取值都能出三种结论（同一手法可能是恶意/误报/证据不足）
    "conclusion_x_technique": (),
    "conclusion_x_subject": (),
}


def illegal_bins(cross_key: str) -> set[tuple[str, str]]:
    """某个交叉的非法 bin 集合。"""
    return set(ILLEGAL_BINS.get(cross_key, ()))


# ===========================================================================
# 3) Boundary：手册里的精确阈值（corner case 的唯一可靠来源）
# ===========================================================================


@dataclass(frozen=True)
class Boundary:
    """一条业务阈值。points 必须包含下界/临界/上界三类，缺一不可。"""

    key: str
    metric: str  # 指标描述
    threshold: str  # 阈值原文（人类可读）
    points: tuple[str, ...]  # 采样点，如 ("4999.99", "5000.00", "5000.01")
    source: str


BOUNDARIES: tuple[Boundary, ...] = (
    # ---- C11 立案标准 ----
    Boundary(
        "loss_amount_5000", "单账户损失金额", "≥5000 元立案",
        ("4999.99", "5000.00", "5000.01"), "C11§2",
    ),
    Boundary(
        "txn_count_10", "涉案交易笔数", "≥10 笔立案",
        ("9", "10", "11"), "C11§2",
    ),
    Boundary(
        "cluster_accounts_5", "图谱簇账号数", "≥5 个账号立案",
        ("4", "5", "6"), "C11§2 + C03§2.2",
    ),
    Boundary(
        "repeat_3in7d", "同手法重复次数", "7 日内 ≥3 起立案",
        ("2起7日内", "3起7日内", "3起第8天"), "C11§2",
    ),
    # ---- C01 组合判定打分 ----
    Boundary(
        "score_80", "分诊总分", "≥80 进人工研判",
        ("79", "80", "81"), "C01§2.3",
    ),
    Boundary(
        "score_60", "分诊总分", "60~79 触发二次验证",
        ("59", "60", "61"), "C01§2.3",
    ),
    Boundary(
        "base_score_tiers", "信号基础分", "高40/中20/低10，组合加成+20",
        ("单中20", "中20+组合20=40", "高40+组合20=60"), "C01§2.3",
    ),
    # ---- C05 登录防线 ----
    Boundary(
        "login_fail_device_5", "同设备失败次数", "≥5 次/10min 触发图形验证",
        ("4", "5", "6"), "C05§2.1",
    ),
    Boundary(
        "login_fail_ip_20", "同 IP 失败次数", "≥20 次/10min 临时封禁",
        ("19", "20", "21"), "C05§2.1",
    ),
    # ---- C03 图谱 ----
    Boundary(
        "shared_resource_60", "簇内共享资源比例", ">60% 判同一主体控制",
        ("59", "60", "61"), "C03§2.2",
    ),
    # ---- C09 支付通道 ----
    Boundary(
        "chargeback_090", "拒付率", ">0.9% 触发通道方审查",
        ("0.89", "0.90", "0.91"), "C09§2.1",
    ),
    Boundary(
        "chargeback_060", "拒付率", ">0.6% 内部预警",
        ("0.59", "0.60", "0.61"), "C09§2.1",
    ),
    Boundary(
        "refund_3x", "退款率日环比", ">3 倍提示套利/盗刷退款潮",
        ("2.9", "3.0", "3.1"), "C09§2.1",
    ),
    Boundary(
        "success_drop_10", "通道成功率降幅", "环比降 >10% 排查",
        ("9.9", "10.0", "10.1"), "C09§2.1",
    ),
    Boundary(
        "channel_concentration_30", "单通道集中度", ">30% 复核资质",
        ("29", "30", "31"), "C09§2.1",
    ),
    # ---- C04 活动滥用 ----
    Boundary(
        "promo_p99", "单账号累计补贴收益", "超人群 P99 触发告警",
        ("P98", "P99", "P99.5"), "C04§3.1",
    ),
    # ---- C12 申诉 ----
    Boundary(
        "appeal_rate_band", "申诉成立率", "健康区间 5%~20%",
        ("4.9", "5.0", "20.0", "20.1"), "C12§6",
    ),
    # ---- C07 接口防线 ----
    Boundary(
        "qps_baseline_10x", "单 IP QPS / 基线", "超 10 倍短时封禁",
        ("9.9", "10.0", "10.1"), "C07§2.1",
    ),
    Boundary(
        "sign_window_5min", "签名时间窗", "±5 分钟",
        ("-4:59", "±5:00", "+5:01"), "C07§3",
    ),
    # ---- C06 模型监控 ----
    Boundary(
        "psi_020", "分数分布 PSI", ">0.2 触发模型复审",
        ("0.19", "0.20", "0.21"), "C06§3.2",
    ),
    Boundary(
        "rule_hit_001pct", "规则命中率", "上线 30 天 <0.01% 进下线评审",
        ("0.009", "0.010", "0.011"), "C06§2",
    ),
)

BOUNDARY_BY_KEY: dict[str, Boundary] = {b.key: b for b in BOUNDARIES}


# ===========================================================================
# 4) 种子行的 coverage 标签
# ===========================================================================


@dataclass
class CoverageTag:
    """一个样本的覆盖标签。每个 coverpoint 恰好命中一个 bin。"""

    values: dict[str, str] = field(default_factory=dict)
    boundary_keys: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "CoverageTag | None":
        """从种子行解析 coverage 标签；没有则返回 None（老种子）。"""
        raw = row.get("coverage")
        if not isinstance(raw, dict):
            return None
        vals = {k: str(v) for k, v in raw.items() if k in COVERPOINT_BY_KEY}
        bks = raw.get("boundary_keys") or []
        if isinstance(bks, str):
            bks = [bks]
        return cls(values=vals, boundary_keys=[str(b) for b in bks])

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = dict(self.values)
        if self.boundary_keys:
            d["boundary_keys"] = list(self.boundary_keys)
        return d

    def validate(self) -> list[str]:
        """返回非法取值列表（空表示合法）。"""
        errs: list[str] = []
        for k, v in self.values.items():
            cp = COVERPOINT_BY_KEY.get(k)
            if cp and v not in cp.values:
                errs.append(f"{k}={v} 不在合法取值内")
        for b in self.boundary_keys:
            if b not in BOUNDARY_BY_KEY:
                errs.append(f"boundary_key={b} 未定义")
        return errs


# ===========================================================================
# 5) bin 枚举与覆盖计算
# ===========================================================================


def all_cross_bins(
    cross: CrossCoverage, *, include_illegal: bool = False
) -> list[tuple[str, str]]:
    """列出一个交叉的全部**合法** bin（left 值 × right 值）。

    默认排除 ILLEGAL_BINS：覆盖率的分母只能是「本应存在的 bin」，
    把语义上不可能的 bin 计入分母，收敛目标永远达不到。
    """
    lp = COVERPOINT_BY_KEY[cross.left]
    rp = COVERPOINT_BY_KEY[cross.right]
    bad = set() if include_illegal else illegal_bins(cross.key)
    return [(a, b) for a in lp.values for b in rp.values if (a, b) not in bad]


@dataclass
class CrossResult:
    cross: CrossCoverage
    hits: dict[tuple[str, str], int]
    targets: dict[tuple[str, str], int]

    @property
    def total_bins(self) -> int:
        return len(self.targets)

    @property
    def covered_bins(self) -> int:
        """达到目标条数的 bin 数（严格覆盖，非「有样本即算」）。"""
        return sum(1 for b, t in self.targets.items() if self.hits.get(b, 0) >= t)

    @property
    def touched_bins(self) -> int:
        """至少有 1 条样本的 bin 数（宽松口径，用于看进度）。"""
        return sum(1 for b in self.targets if self.hits.get(b, 0) > 0)

    @property
    def rate(self) -> float:
        return self.covered_bins / self.total_bins if self.total_bins else 0.0

    def gaps(self) -> list[tuple[tuple[str, str], int, int]]:
        """未达标的 bin：(bin, 当前, 目标)，按缺口从大到小。"""
        out = [
            (b, self.hits.get(b, 0), t)
            for b, t in self.targets.items()
            if self.hits.get(b, 0) < t
        ]
        out.sort(key=lambda x: (x[1] - x[2]))
        return out


def compute_cross(cross: CrossCoverage, rows: Iterable[dict[str, Any]]) -> CrossResult:
    hits: dict[tuple[str, str], int] = {}
    for row in rows:
        tag = CoverageTag.from_row(row)
        if tag is None:
            continue
        a = tag.values.get(cross.left)
        b = tag.values.get(cross.right)
        if a is None or b is None:
            continue
        hits[(a, b)] = hits.get((a, b), 0) + 1
    targets = {b: cross.per_bin for b in all_cross_bins(cross)}
    return CrossResult(cross=cross, hits=hits, targets=targets)


def compute_values(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """每个 coverpoint 各取值的命中数。"""
    counts: dict[str, dict[str, int]] = {cp.key: {v: 0 for v in cp.values} for cp in COVERPOINTS}
    for row in rows:
        tag = CoverageTag.from_row(row)
        if tag is None:
            continue
        for k, v in tag.values.items():
            if k in counts and v in counts[k]:
                counts[k][v] += 1
    return counts


def compute_boundaries(rows: Iterable[dict[str, Any]]) -> dict[str, set[str]]:
    """每个边界阈值已覆盖的采样点集合。"""
    got: dict[str, set[str]] = {b.key: set() for b in BOUNDARIES}
    for row in rows:
        tag = CoverageTag.from_row(row)
        if tag is None:
            continue
        for bk in tag.boundary_keys:
            if bk in got:
                got[bk].add(str(row.get("boundary_point", "")))
    return got


def untagged_rows(rows: Iterable[dict[str, Any]]) -> int:
    """没有 coverage 标签的样本数（老种子）。"""
    return sum(1 for row in rows if CoverageTag.from_row(row) is None)


def all_target_bins() -> list[tuple[str, tuple[str, str], int]]:
    """全部待覆盖目标：(cross_key, bin, 目标条数)。"""
    out: list[tuple[str, tuple[str, str], int]] = []
    for c in CROSSES:
        for b in all_cross_bins(c):
            out.append((c.key, b, c.per_bin))
    return out


# ---------------------------------------------------------------------------
# 覆盖计划：把「要铺满的 bin」翻译成一批可直接生成的样本规格
# ---------------------------------------------------------------------------
# 主控维度取 bin 最多的那个（technique，14 值 × 3 结论 = 42 个 bin 组合）。
# 其余维度用轮转游标依次取值——轮转保证各 bin 均匀命中，避免某个取值被系统性
# 忽略（随机采样在几十条的量级上很容易漏掉长尾取值）。


def _combo_legal(combo: dict[str, str]) -> bool:
    """检查（部分）组合是否与 illegal bin 冲突。

    ILLEGAL_BINS 全部是两两约束，所以逐维度增量判断即可，不需要回溯搜索。
    """
    for cross in CROSSES:
        a = combo.get(cross.left)
        b = combo.get(cross.right)
        if a is None or b is None:
            continue
        if (a, b) in illegal_bins(cross.key):
            return False
    return True


def build_coverage_plan(per_bin: int = 2) -> list[dict[str, str]]:
    """生成交叉覆盖计划：每项是一个样本的完整覆盖规格。

    主轴为 technique × conclusion（bin 最多），其余维度轮转取值并跳过非法组合。

    游标**按 conclusion 分组**：不同 conclusion 的合法取值集合不同（例如 malicious
    只剩「双信号印证 / 多信号+官方核验」两个证据强度），共享游标会让某些组合被
    系统性跳过——实测会漏掉 6 个 bin 导致覆盖率永远收敛不了。
    """
    primary = COVERPOINT_BY_KEY["technique"]
    others = [cp for cp in COVERPOINTS if cp.key not in ("technique", "conclusion")]
    cursors: dict[tuple[str, str], int] = {}
    plan: list[dict[str, str]] = []

    for tech in primary.values:
        for concl in CONCLUSIONS:
            for _ in range(per_bin):
                combo: dict[str, str] = {"technique": tech, "conclusion": concl}
                if not _combo_legal(combo):
                    continue
                for cp in others:
                    ck = (concl, cp.key)
                    start = cursors.get(ck, 0)
                    picked: str | None = None
                    for off in range(len(cp.values)):
                        cand = cp.values[(start + off) % len(cp.values)]
                        if _combo_legal({**combo, cp.key: cand}):
                            picked = cand
                            cursors[ck] = (start + off + 1) % len(cp.values)
                            break
                    if picked is None:
                        # 每个维度对任一 conclusion 都至少有一个合法取值，兜底只为健壮性
                        picked = cp.values[start]
                        cursors[ck] = (start + 1) % len(cp.values)
                    combo[cp.key] = picked
                plan.append(combo)
    return plan


def build_boundary_plan(per_point: int = 1) -> list[dict[str, str]]:
    """生成阈值边界计划：每个阈值的每个采样点各一条规格。

    边界样本与交叉计划分开统计——它们回答的是另一个问题：
    「分数/金额/次数刚好压在阈值上时，agent 会不会错判」，而不是维度组合是否齐全。

    结论维度同样参与轮转：同一个阈值点在不同结论下都成立（例如「损失 5000.00」
    在 malicious 场景是刚够立案线，在 benign 场景是金额达标但有可信解释）。
    """
    dims = list(COVERPOINTS)
    cursors: dict[str, int] = {cp.key: 0 for cp in dims}
    plan: list[dict[str, str]] = []

    for b in BOUNDARIES:
        for point in b.points:
            for _ in range(per_point):
                combo: dict[str, str] = {}
                for cp in dims:
                    start = cursors[cp.key]
                    picked: str | None = None
                    for off in range(len(cp.values)):
                        cand = cp.values[(start + off) % len(cp.values)]
                        if _combo_legal({**combo, cp.key: cand}):
                            picked = cand
                            cursors[cp.key] = (start + off + 1) % len(cp.values)
                            break
                    if picked is None:
                        picked = cp.values[start]
                        cursors[cp.key] = (start + 1) % len(cp.values)
                    combo[cp.key] = picked
                combo["boundary_key"] = b.key
                combo["boundary_point"] = point
                plan.append(combo)
    return plan


def estimate_min_samples() -> tuple[int, dict[str, int]]:
    """估算铺满全部 bin 所需的最少样本数。

    关键点：一个样本在每个 coverpoint 上只落一个值，因此它同时为**每个**交叉的
    一个 bin 计数。所以样本下界不是 bin 总数（107），而是各交叉需求的最大值——
    受最"碎"的维度（手法 42 个 bin × 2）支配。

    返回 (下界, 各交叉需求明细)。实际生成要留冗余（碰撞会导致重复命中），
    建议按下界的 1.4~1.6 倍准备。
    """
    detail = {c.key: len(all_cross_bins(c)) * c.per_bin for c in CROSSES}
    return (max(detail.values()) if detail else 0), detail


# ---------------------------------------------------------------------------
# 规格键：生成计划的去重与断点续传
# ---------------------------------------------------------------------------
# 长任务（148 条规格 × 每次 LLM 调用）必须能中断续跑，否则中途失败要全部重来。
# 键只取覆盖维度 + 边界采样点，因此「原始计划项」与「从已生成种子还原的标签」
# 能算出同一个键——这是续跑能正确跳过已完成规格的前提。


def spec_key(spec: dict[str, Any]) -> str:
    """规格的规范化键。"""
    core = {k: str(spec[k]) for k in COVERPOINT_BY_KEY if k in spec}
    return json.dumps(
        [core, str(spec.get("boundary_point") or "")],
        ensure_ascii=False,
        sort_keys=True,
    )


def spec_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """从已生成的种子行还原规格（用于计算 spec_key）。"""
    cov = row.get("coverage")
    if not isinstance(cov, dict):
        return None
    spec = {k: str(v) for k, v in cov.items() if k in COVERPOINT_BY_KEY}
    if row.get("boundary_point"):
        spec["boundary_point"] = str(row["boundary_point"])
    return spec


__all__ = [
    "BOUNDARIES",
    "BOUNDARY_BY_KEY",
    "CONCLUSIONS",
    "COVERPOINTS",
    "COVERPOINT_BY_KEY",
    "CROSSES",
    "CROSS_BY_KEY",
    "CoverageTag",
    "Coverpoint",
    "Boundary",
    "CrossCoverage",
    "CrossResult",
    "DISPOSAL_LEVELS",
    "DISTURBANCES",
    "EVIDENCE_STRENGTHS",
    "ILLEGAL_BINS",
    "INFO_COMPLETENESS",
    "RISK_LEVELS",
    "SCORE_BANDS",
    "SUBJECT_TYPES",
    "TECHNIQUES",
    "all_cross_bins",
    "compute_boundaries",
    "compute_cross",
    "compute_values",
    "untagged_rows",
]
