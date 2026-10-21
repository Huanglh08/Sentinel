"""badcase 台账与回归固化。

解决什么问题
------------
评测失败现在只躺在某次报告文件里，下一轮评测不会自动重跑这些用例——修好的问题
会悄悄回归，同一个错误也会在多轮之间反复出现而无人察觉。

芯片验证的铁律是「发现一个 bug 就写一个 test，那个 test 永久进回归集，每次回归必须过」。
本模块把这个机制搬过来：

    评测失败 → 落账（去重、累积出现次数）→ 根因分类 → 修复 → 固化为回归集
                                                  ↓
                                       下次评测必跑，必须全过

根因分类直接复用 C12 手册的「误报归因四分类」（业务侧已定义好，不另造口径）。

数据形态：JSONL 台账，一行一个 badcase，按 (case_name, failure_kind) 去重。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# C12 手册「误报归因四分类」——业务侧既有口径，直接复用
ROOT_CAUSES: tuple[str, ...] = (
    "未归因",
    "策略过严",  # 阈值问题
    "特征失效",  # 信号误判
    "数据错误",  # 输入数据质量问题
    "流程错误",  # 执行错对象
)

# 状态机：open → fixing → fixed → verified；不修的走 wontfix
STATUSES: tuple[str, ...] = ("open", "fixing", "fixed", "verified", "wontfix")

# 失败类型：决定这条 badcase 反映的是什么毛病
FAILURE_KINDS: tuple[str, ...] = (
    "conclusion_mismatch",  # 结论判错（最严重）
    "confidence_low",  # 结论对但置信度不达标
    "phase_broken",  # 三段阶段不完整
    "forced_stop",  # 预算打断
    "run_error",  # 调用失败/异常
)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class BadCase:
    """一条 badcase。按 (case_name, failure_kind, predicted) 去重。"""

    case_name: str
    ground_truth: str
    predicted: str
    failure_kind: str
    category: str = "general"
    coverage: dict[str, Any] = field(default_factory=dict)
    seed_json: dict[str, Any] = field(default_factory=dict)  # 原始种子行，供导出回归集
    first_seen: str = ""
    last_seen: str = ""
    occurrences: int = 0
    root_cause: str = "未归因"
    status: str = "open"
    notes: str = ""

    # ---------------- 去重键 ----------------
    def key(self) -> tuple[str, str, str]:
        return (self.case_name, self.failure_kind, self.predicted)

    def record_hit(self, when: str | None = None) -> None:
        """记一次出现。"""
        ts = when or _now()
        if not self.first_seen:
            self.first_seen = ts
        self.last_seen = ts
        self.occurrences += 1

    def mark(self, status: str, root_cause: str | None = None, notes: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(f"未知状态 {status}，可选 {STATUSES}")
        if root_cause is not None:
            if root_cause not in ROOT_CAUSES:
                raise ValueError(f"未知根因 {root_cause}，可选 {ROOT_CAUSES}")
            self.root_cause = root_cause
        self.status = status
        if notes:
            self.notes = notes

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BadCase":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


# ===========================================================================
# 台账读写
# ===========================================================================


def load_ledger(path: Path) -> dict[tuple[str, str, str], BadCase]:
    """读台账，返回 {去重键: BadCase}。文件不存在则空台账。"""
    path = Path(path)
    out: dict[tuple[str, str, str], BadCase] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        bc = BadCase.from_dict(json.loads(line))
        out[bc.key()] = bc
    return out


def save_ledger(path: Path, ledger: dict[tuple[str, str, str], BadCase]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(
        ledger.values(), key=lambda b: (-b.occurrences, b.case_name)
    )
    with path.open("w", encoding="utf-8") as fp:
        for bc in rows:
            fp.write(json.dumps(bc.to_dict(), ensure_ascii=False) + "\n")
    return len(rows)


# ===========================================================================
# 从评测结果合并
# ===========================================================================


def _failure_kinds(r: Any) -> list[str]:
    """判定一条评测结果属于哪些失败类型（可能同时命中多个）。"""
    kinds: list[str] = []
    if getattr(r, "error", None):
        kinds.append("run_error")
        return kinds
    if not getattr(r, "hit_conclusion", True):
        kinds.append("conclusion_mismatch")
    if not getattr(r, "hit_confidence", True):
        kinds.append("confidence_low")
    if not getattr(r, "phases_ok", True):
        kinds.append("phase_broken")
    if getattr(r, "forced_stop", False):
        kinds.append("forced_stop")
    return kinds


def merge_results(
    ledger: dict[tuple[str, str, str], BadCase],
    results: Iterable[Any],
    seeds_by_name: dict[str, dict[str, Any]] | None = None,
    *,
    when: str | None = None,
) -> dict[str, int]:
    """把一轮评测的失败结果合并进台账（去重累积）。

    results 需要具备 name / ground_truth / predicted_conclusion / category 等属性
    （即 case_eval.CaseEvalResult 的形态）。
    已标记 verified/wontfix 的条目若再次失败，会被自动打回 open——回归失败必须重新处理。
    """
    seeds_by_name = seeds_by_name or {}
    stats = {"new": 0, "repeat": 0, "reopened": 0, "passed_kept": 0}

    for r in results:
        if getattr(r, "passed", False):
            # 本轮通过：把台账里该用例的 fixed 条目推进为 verified。
            # 注意不能用 _failure_kinds(r)——它对本轮通过的结果返回空列表，
            # 那样循环体不执行，状态永远停在 fixed（实测踩过）。
            name = str(getattr(r, "name", ""))
            for k, bc in ledger.items():
                if k[0] == name and bc.status == "fixed":
                    bc.status = "verified"
                    bc.last_seen = when or _now()
                    stats["passed_kept"] += 1
            continue

        for kind in _failure_kinds(r):
            name = str(getattr(r, "name", ""))
            predicted = str(getattr(r, "predicted_conclusion", "") or "")
            k = (name, kind, predicted)
            if k in ledger:
                bc = ledger[k]
                was_closed = bc.status in ("verified", "wontfix", "fixed")
                bc.record_hit(when)
                if was_closed:
                    bc.status = "open"  # 回归失败，打回
                    stats["reopened"] += 1
                else:
                    stats["repeat"] += 1
            else:
                seed = seeds_by_name.get(name, {})
                bc = BadCase(
                    case_name=name,
                    ground_truth=str(getattr(r, "ground_truth", "")),
                    predicted=predicted,
                    failure_kind=kind,
                    category=str(getattr(r, "category", "general")),
                    coverage=dict(seed.get("coverage") or {}),
                    seed_json=dict(seed),
                )
                bc.record_hit(when)
                ledger[k] = bc
                stats["new"] += 1
    return stats


# ===========================================================================
# 回归集导出与检查
# ===========================================================================


def export_regression_seeds(
    ledger: dict[tuple[str, str, str], BadCase], path: Path
) -> int:
    """把台账里未关闭的 badcase 导出成回归种子集（下次评测必跑）。

    排除 wontfix：明确不修的条目留在台账里做记录，但不进回归。
    """
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for bc in ledger.values():
        if bc.status in ("wontfix",):
            continue
        if bc.case_name in seen or not bc.seed_json:
            continue
        seen.add(bc.case_name)
        seed = dict(bc.seed_json)
        seed["regression_of"] = bc.failure_kind
        rows.append(seed)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        for row in rows:
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def check_regression(
    ledger: dict[tuple[str, str, str], BadCase], results: Iterable[Any]
) -> list[str]:
    """回归检查：台账里 open/fixing/fixed 的条目本轮若仍失败，返回失败清单。

    调用方据此决定是否让评测以非零码退出（回归门禁）。
    """
    want = {
        bc.case_name
        for bc in ledger.values()
        if bc.status in ("open", "fixing", "fixed") and bc.failure_kind != "run_error"
    }
    failed: list[str] = []
    for r in results:
        name = str(getattr(r, "name", ""))
        if name in want and not getattr(r, "passed", False):
            failed.append(name)
    return sorted(set(failed))


# ===========================================================================
# 摘要
# ===========================================================================


def coverage_hotspots(ledger: dict[tuple[str, str, str], BadCase]) -> list[tuple[str, str, int]]:
    """按覆盖标签聚合失败数，定位「哪个能力在系统性失效」。

    返回 [(维度, 取值, 失败数)]，降序。这是覆盖率模型与 badcase 台账的接合点：
    单个 case 失败是偶发，同一维度取值下多条失败就是能力问题。
    """
    agg: dict[tuple[str, str], int] = {}
    for bc in ledger.values():
        if bc.status in ("verified", "wontfix"):
            continue
        for k, v in bc.coverage.items():
            if k == "boundary_keys":
                continue
            agg[(k, str(v))] = agg.get((k, str(v)), 0) + 1
    return sorted(
        [(k[0], k[1], n) for k, n in agg.items()], key=lambda x: -x[2]
    )


def render_summary(ledger: dict[tuple[str, str, str], BadCase], top: int = 15) -> str:
    items = sorted(ledger.values(), key=lambda b: (-b.occurrences, b.case_name))
    active = [b for b in items if b.status not in ("verified", "wontfix")]
    L: list[str] = []
    L.append("# badcase 台账摘要\n")
    L.append(f"- 台账条目：**{len(items)}**（未关闭 {len(active)}）")
    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for b in items:
        by_status[b.status] = by_status.get(b.status, 0) + 1
        by_kind[b.failure_kind] = by_kind.get(b.failure_kind, 0) + 1
    L.append(f"- 状态分布：`{by_status}`")
    L.append(f"- 失败类型分布：`{by_kind}`\n")

    if active:
        L.append("## 未关闭条目（按出现次数）\n")
        L.append("| 用例 | 期望 | 实际 | 失败类型 | 出现 | 根因 | 状态 |")
        L.append("|---|---|---|---|---|---|---|")
        for b in active[:top]:
            L.append(
                f"| {b.case_name} | {b.ground_truth} | {b.predicted or '-'} "
                f"| {b.failure_kind} | {b.occurrences} | {b.root_cause} | {b.status} |"
            )
        L.append("")

    spots = coverage_hotspots(ledger)
    if spots:
        L.append("## 能力热点（同一覆盖维度下的失败聚集）\n")
        L.append("| 维度 | 取值 | 失败数 |")
        L.append("|---|---|---|")
        for k, v, n in spots[:top]:
            L.append(f"| {k} | {v} | {n} |")
        L.append("")
    return "\n".join(L)


__all__ = [
    "BadCase",
    "FAILURE_KINDS",
    "ROOT_CAUSES",
    "STATUSES",
    "check_regression",
    "coverage_hotspots",
    "export_regression_seeds",
    "load_ledger",
    "merge_results",
    "render_summary",
    "save_ledger",
]
