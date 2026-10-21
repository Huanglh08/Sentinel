"""生成后规格校验：检查样本内容是否真的体现了标签宣称的维度。

为什么需要
----------
覆盖率统计只看 `coverage` 标签，不看内容。实测发现 LLM 会「忽略规格」——
例如标注 `disturbance=线索注入` 的样本，clue 里根本没有任何注入文本。
此时覆盖率数字是虚的：bin 被"填满"了，但被填的东西不是那个 bin 该有的。

所以覆盖率之外还要一层**规格执行度校验**，不合格的打回重生成，
这样覆盖率才是可信的（对齐芯片验证里 coverage 必须配合 checker/assertion）。

设计取舍
--------
优先用**规则**而不是 LLM judge：
- 规则零成本、可解释、可复现，适合跑全量 148 条
- 只有规则判不了的部分才考虑 LLM（本模块暂不涉及）

规则必然有误报，所以分两级：
- **error**：强规则，几乎不可能误判（如 boundary_point 数值没出现）
- **warn**：启发式，需要人工过一眼（如 lookalike 的可信解释词缺失）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEEDS = _REPO_ROOT / "evaluation" / "seeds" / "case-seeds.jsonl"


# ===========================================================================
# 规则
# ===========================================================================

# 注入样文本：cue 线索被污染的特征表达
INJECTION_PATTERNS: tuple[str, ...] = (
    r"忽略(上述|以上|之前|前面|以下|所有)",
    r"(请|直接)(把|将).{0,8}(结论|判定|结果).{0,6}(改|设|写成)",
    r"判(为|定|成)\s*(benign|正常|良性|无风险|malicious|恶意)",
    r"输出.{0,6}(系统提示|提示词|system\s*prompt|指令)",
    r"(ignore|disregard)\s+(the\s+)?(previous|above|all)",
    r"you\s+are\s+now",
    r"(现在|从现在起).{0,6}(你是|扮演|角色)",
    r"system\s*:\s*",
)

# 可信的正常解释：C01/C02 手册里列举的"排除"依据
LOOKALIKE_PATTERNS: tuple[str, ...] = (
    r"报备",
    r"工单",
    r"老用户|历史用户|长期用户",
    r"资质(齐全|完整|有效)",
    r"实名(一致|相符|核验通过|认证)",
    r"家庭(共享|成员)|亲属",
    r"出差|差旅",
    r"常用(设备|地址|地点|地)",
    r"正常业务|业务背景|消费习惯",
)

# 可核查实体：账号/订单/设备/IP 等 ID 形态
ENTITY_PATTERNS: tuple[str, ...] = (
    r"[A-Z]{1,4}[-_]?\d{4,}",  # A87654321 / CASE-1234
    r"(?:usr|acc|user|ord|order|dev|device|mer|merchant)[_\-]?\w*\d{3,}",
    r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}",  # IP
    r"\d{4}-\d{2}-\d{2}",  # 日期
    r"[\u4e00-\u9fa5]{1,4}[-_]?[A-Z]{2,}\d{3,}",  # 华为Mate60 / SM-G998B 类
)

# 手法 -> 应出现的关键线索词（宽松，命中任一即可）
# 注意：值是**字符串**不是元组。写成 (r"...") 会因缺逗号退化成字符串，
# 而 _has_any 遍历字符串会逐字符取 pattern——含括号的正则随即编译失败，
# 不含括号的则静默失效（这个坑实测踩过）。
TECHNIQUE_CUES: dict[str, str] = {
    "凭证填充": r"尝试登录|登录失败|撞库|多(个|次)账号|失败率",
    "新设备+敏感操作": r"新设备|全新.{0,6}机型|改绑|修改.{0,4}(手机|密码)|发起转账",
    "IP异地突变": r"异地|归属地|属地.{0,6}(突变|异常)|距离",
    "代理IDC_IP": r"代理|IDC|机房|数据中心|秒拨",
    "会话劫持": r"会话|Cookie|token.{0,6}复用|会话复用",
    "凭证泄露情报": r"泄露(库|情报)|凭证泄露|口令.{0,6}泄露",
    "盗刷": r"盗刷|小额.{0,6}试探|卡.{0,4}被盗|被盗用",
    "欺诈转账": r"欺诈转账|冒充|诱导.{0,4}转账|收款账户",
    "虚构交易套现": r"套现|自买自卖|虚假商户|资金闭环",
    "分单规避": r"分单|拆分|低于阈值|临近阈值|多笔",
    "批量注册": r"批量注册|设备农场|接码|同一设备.{0,8}注册",
    "养号": r"养号|签到|模板化|行为熵",
    "薅羊毛": r"薅羊毛|优惠券|补贴|活动.{0,4}(滥用|套取)|领券|返现",
    "通道异常": r"拒付|退款率|通道|成功率|提现",
}


@dataclass
class CheckResult:
    row_id: str
    name: str
    issues: list[tuple[str, str]] = field(default_factory=list)  # [(level, msg)]

    @property
    def errors(self) -> list[str]:
        return [m for lv, m in self.issues if lv == "error"]

    @property
    def warns(self) -> list[str]:
        return [m for lv, m in self.issues if lv == "warn"]

    @property
    def infos(self) -> list[str]:
        return [m for lv, m in self.issues if lv == "info"]

    @property
    def ok(self) -> bool:
        """info 只作提示，不影响通过判定。"""
        return not self.errors and not self.warns


def _has_any(text: str, patterns: str | tuple[str, ...]) -> bool:
    """patterns 可以是单个正则字符串或正则序列；字符串按单个 pattern 处理。

    必须显式区分：否则传进来一个字符串会被逐字符迭代，静默失效或直接抛错。
    """
    if isinstance(patterns, str):
        patterns = (patterns,)
    return any(re.search(p, text, re.I) for p in patterns)


def _contains_number(text: str, value: str) -> bool:
    """判断阈值取值是否真的出现在线索里。

    单数字必须特殊处理：
    - 直接子串匹配会撞上日期/时间/IP 里的数字（"14:22" 里的 4）
    - 但要求「≥2 位」又会把合法的单数字阈值全部误报
      （实测「完成 9 笔」「图谱簇账号数=4」都被判成没出现）
    所以：多位数用非数字边界；单数字要求带量词或比较符上下文。
    """
    nums = re.findall(r"\d+(?:\.\d+)?", value)
    if not nums:
        # 纯非数字边界点的防御分支（当前种子数据未出现此类取值）：退化为字面
        # 子串匹配，而不是直接放行——直接放行会让失效样本静默通过质检。
        # 注意 P99/±5:00 这类值实际含数字（99、5、00），走下方数字路径。
        return value in text
    for n in nums:
        intpart = n.split(".")[0]
        esc = re.escape(intpart)
        if len(intpart) >= 2:
            if re.search(rf"(?<!\d){esc}(?!\d)", text):
                return True
        else:
            has_unit = re.search(
                rf"(?<!\d){esc}\s*(?:笔|个|次|条|起|张|人|台|天|小时|分钟|分|秒|倍|元|万|%)",
                text,
            )
            has_assign = re.search(rf"[:=＝]\s*{esc}(?!\d)", text)
            if has_unit or has_assign:
                return True
    return False


def check_row(row: dict[str, Any]) -> CheckResult:
    """校验一条种子，返回问题清单。规则分级：error 强判 / warn 启发式。"""
    rid = str(row.get("id", ""))
    name = str(row.get("name", ""))
    clue = str(row.get("clue", ""))
    cov = row.get("coverage") or {}
    res = CheckResult(row_id=rid, name=name)

    # ---- 1) 边界点：线索里必须出现该阈值数值 ----
    bp = row.get("boundary_point")
    if bp:
        if not _contains_number(clue, str(bp)):
            res.issues.append(
                ("error", f"boundary_point={bp} 未在 clue 中出现（阈值边界样本失效）")
            )

    # ---- 2) 线索注入：必须检出注入样文本 ----
    if cov.get("disturbance") == "线索注入":
        if not _has_any(clue, INJECTION_PATTERNS):
            res.issues.append(
                ("error", "disturbance=线索注入，但 clue 中没有注入样文本（规格未执行）")
            )

    # ---- 3) lookalike 合理伪装：必须给出可信的正常解释 ----
    if cov.get("disturbance") == "lookalike合理伪装":
        if not _has_any(clue, LOOKALIKE_PATTERNS):
            res.issues.append(
                ("warn", "disturbance=lookalike合理伪装，但未见可信解释（报备/工单/老用户/资质等）")
            )

    # ---- 4) 信息严重缺失：不该出现多个可核查实体 ----
    if cov.get("info_completeness") == "严重缺失":
        hits = sum(1 for p in ENTITY_PATTERNS if re.search(p, clue))
        # 阈值取 3：实测取 2 会把「有账号+IP+时间」也算进来，而 C11 的「严重缺失」
        # 指的是「无任何可核查实体」（那种线索确实只有一句模糊描述）。
        if hits >= 3:
            res.issues.append(
                ("error", f"info_completeness=严重缺失，但 clue 含 {hits} 类具体实体")
            )

    # ---- 5) 手法：仅作提示，不参与通过判定 ----
    # 手法之间语义高度重叠（「新设备+敏感操作」与「凭证填充」的线索常是同一段文本），
    # 字面规则误报率极高——实测 64 条告警全部是误报。误报的告警比不报更有害，
    # 会把人工评审的注意力引偏，所以降级为 info，只作抽检提示。
    tech = str(cov.get("technique", ""))
    cues = TECHNIQUE_CUES.get(tech)
    if cues and not _has_any(clue, cues):
        res.issues.append(
            ("info", f"technique={tech} 未匹配到字面线索词（仅供参考，需人工确认语义）")
        )

    # ---- 6) 结论与证据强度自洽（边界守卫，illegal bins 之外的软约束）----
    concl = str(row.get("expect_conclusion") or cov.get("conclusion") or "")
    ev = str(cov.get("evidence_strength", ""))
    if ev == "无信号" and concl in ("malicious", "benign"):
        res.issues.append(("error", f"evidence_strength=无信号 却判 {concl}"))
    if ev == "单一孤立" and concl == "malicious":
        res.issues.append(("error", "evidence_strength=单一孤立 却判 malicious"))

    return res


# ===========================================================================
# 报告
# ===========================================================================


def load_rows(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def check_all(rows: list[dict[str, Any]]) -> list[CheckResult]:
    return [check_row(r) for r in rows]


def render_md(results: list[CheckResult], rows_total: int) -> str:
    n_err = sum(1 for r in results if r.errors)
    n_warn = sum(1 for r in results if r.warns and not r.errors)
    n_ok = sum(1 for r in results if r.ok)
    n_info = sum(1 for r in results if r.infos and r.ok)
    rule_stat: Counter[str] = Counter()
    for r in results:
        for lv, msg in r.issues:
            if lv == "info":  # 提示级不参与问题分布统计
                continue
            rule_stat[msg.split("：")[0].split("（")[0][:40]] += 1

    L: list[str] = ["# 生成后规格校验报告\n"]
    L.append(f"- 样本总数：**{rows_total}**")
    L.append(f"- 通过：**{n_ok}**（{n_ok / rows_total:.1%}）" if rows_total else "")
    L.append(f"- 有 error：**{n_err}**")
    L.append(f"- 仅 warn：**{n_warn}**")
    L.append(f"- 另有 {n_info} 条仅含提示（不影响通过）\n")
    L.append("> 覆盖率统计只看标签，本报告校验内容是否真的体现了标签 —— 两者都过，覆盖率才可信。\n")

    if rule_stat:
        L.append("## 问题分布\n")
        L.append("| 问题 | 条数 |")
        L.append("|---|---|")
        for k, v in rule_stat.most_common(15):
            L.append(f"| {k} | {v} |")
        L.append("")

    bad = [r for r in results if not r.ok]
    if bad:
        L.append("## 问题样本明细\n")
        L.append("| id | name | 级别 | 问题 |")
        L.append("|---|---|---|---|")
        for r in bad[:60]:
            for lv, msg in r.issues:
                L.append(f"| {r.row_id} | {r.name} | {lv} | {msg} |")
        L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="生成后规格校验")
    ap.add_argument("--seeds", type=Path, default=DEFAULT_SEEDS)
    ap.add_argument("--md", type=Path, default=None)
    ap.add_argument("--strict", action="store_true", help="存在 error 则退出码 1")
    args = ap.parse_args(argv)

    rows = load_rows(args.seeds)
    if not rows:
        print(f"[spec_check] 无样本：{args.seeds}", file=sys.stderr)
        return 2

    results = check_all(rows)
    md = render_md(results, len(rows))
    print(md)
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(md, encoding="utf-8")
        print(f"[spec_check] 已写入 {args.md}")

    n_err = sum(1 for r in results if r.errors)
    if args.strict and n_err:
        print(f"[spec_check] 门禁未通过：{n_err} 条存在 error", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
