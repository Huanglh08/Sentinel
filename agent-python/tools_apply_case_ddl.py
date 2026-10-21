"""一次性脚本：对 PG 执行案件调查 DDL（建表 + 增量列）并验证结果。"""
import sys
from pathlib import Path

from core.db import get_dsn, get_pool

UPGRADES = Path(__file__).resolve().parent.parent / "backend-java" / "resources" / "database" / "upgrades" / "v1.1.0"
SQL_FILES = (
    UPGRADES / "260829_case_investigation.sql",
    UPGRADES / "261018_case_evidence_status.sql",
)


def main() -> int:
    sql = "\n".join(f.read_text(encoding="utf-8") for f in SQL_FILES)
    print(f"[1/3] DDL 文件读取成功: {len(SQL_FILES)} 个 / {len(sql)} chars")

    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(sql)
    print(f"[2/3] DDL 已执行 - dsn={get_dsn().split('@')[-1]}")

    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name IN ('t_case', 't_case_evidence') "
            "ORDER BY table_name"
        ).fetchall()
        names = [r[0] for r in rows]
        ev_cols = []
        if "t_case_evidence" in names:
            ev_cols = [
                r[0]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='t_case_evidence' ORDER BY ordinal_position"
                ).fetchall()
            ]
    print(f"[3/3] 验证: tables={names}, t_case_evidence columns={ev_cols}")
    if set(names) == {"t_case", "t_case_evidence"} and {"tool_status", "record_count"} <= set(ev_cols):
        print("OK: DDL 执行并验证通过")
        return 0
    print("FAIL: 建表结果不符合预期", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
