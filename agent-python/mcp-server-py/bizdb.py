"""业务数据只读访问层（MCP 富化的数据源）。

CASE_DATA_SOURCE=live 时，ticket_query / sales_query 等不再走合成生成器，
改为查 PostgreSQL 的分表业务事实（内容来自 evaluation/seeds/case-facts.jsonl）。

分表规范（对齐真实企业业务库形态）：
  - 每类事实一张结构化表：biz_login / biz_bind / biz_device / biz_order / biz_ticket / biz_sales
  - 公共列结构化：alert_id / record_id / title / detail / 时间 / 状态 / 金额 /
    data_tier / subject_type / subject_id / entities / payload
  - 高频专有字段提升为真列（login.ip、order.merchant、ticket.approval_status 等），
    长尾字段保留在 payload jsonb
  - source 列区分数据来源：'eval'=评测夹具 / 'production'=真实业务数据；
    评测重灌只清 eval 行，绝不触碰生产数据

只读约束：应用连接使用 sentinel_ro 角色（仅 SELECT）；建表/灌数走管理员连接。

初始化：python bizdb.py --init [facts.jsonl]
"""

from __future__ import annotations

import json
import os
import sys

import psycopg
from psycopg.rows import dict_row

_DB = os.getenv("BIZ_DB_NAME", "sentinel")
_DEFAULT_ADMIN_DSN = f"postgresql://postgres:postgres@127.0.0.1:5432/{_DB}"
# ponytail: 本地开发默认口令；上线改为密钥注入，并只授予 SELECT。
_DEFAULT_RO_DSN = f"postgresql://sentinel_ro:sentinel_ro@127.0.0.1:5432/{_DB}"
_DEFAULT_FACTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "evaluation",
    "seeds",
    "case-facts.jsonl",
)

# 数据来源标记：评测夹具（可被 --init 安全重灌）与真实业务数据（只追加，评测永不触碰）
SOURCE_EVAL = "eval"
SOURCE_PROD = "production"

# ---------------------------------------------------------------------------
# 分表定义：kind -> (表名, 专有列{列名: 类型}, attributes 提取映射{列名: 键})
# 公共列见 _PUBLIC_COLS；attributes 高频键提升为真列，长尾留在 payload。
# ---------------------------------------------------------------------------
_PUBLIC_COLS: dict[str, str] = {
    "alert_id": "varchar(32) DEFAULT ''",
    "record_id": "varchar(64) DEFAULT ''",
    "title": "varchar(255) DEFAULT ''",
    "detail": "text DEFAULT ''",
    "amount": "numeric(12,2)",
    "fact_date": "varchar(32) DEFAULT ''",
    "fact_time": "varchar(32) DEFAULT ''",
    "status": "varchar(32) DEFAULT ''",
    "data_tier": "varchar(16) DEFAULT ''",
    "source": "varchar(16) DEFAULT 'eval'",
    "subject_type": "varchar(16) DEFAULT ''",
    "subject_id": "varchar(64) DEFAULT ''",
    "entities": "text[] NOT NULL DEFAULT '{}'",
    "seed": "varchar(128) DEFAULT ''",
    "expect": "varchar(16) DEFAULT ''",
    "payload": "jsonb DEFAULT '{}'::jsonb",
}

_KIND_TABLES: dict[str, tuple[str, dict[str, str], dict[str, str]]] = {
    "login": (
        "biz_login",
        {
            "ip": "varchar(64) DEFAULT ''",
            "geo": "varchar(64) DEFAULT ''",
            "device": "varchar(64) DEFAULT ''",
            "result": "varchar(32) DEFAULT ''",
        },
        {"ip": "ip", "geo": "geo", "device": "device", "result": "result"},
    ),
    "bind": (
        "biz_bind",
        {
            "change_type": "varchar(32) DEFAULT ''",
            "channel": "varchar(64) DEFAULT ''",
            "verify_method": "varchar(64) DEFAULT ''",
            "new_value": "varchar(128) DEFAULT ''",
            "result": "varchar(32) DEFAULT ''",
        },
        {
            "change_type": "change_type",
            "channel": "channel",
            "verify_method": "verify_method",
            "new_value": "new_value",
            "result": "result",
        },
    ),
    "device": (
        "biz_device",
        {
            "device": "varchar(64) DEFAULT ''",
            "first_seen": "varchar(32) DEFAULT ''",
            "logins_7d": "integer",
        },
        {"device": "device", "first_seen": "first_seen", "logins_7d": "logins_7d"},
    ),
    "order": (
        "biz_order",
        {
            "order_id": "varchar(64) DEFAULT ''",
            "pay_method": "varchar(32) DEFAULT ''",
            "merchant": "varchar(64) DEFAULT ''",
            "is_baseline": "boolean",
            "day_offset": "integer",
        },
        {
            "order_id": "order_id",
            "pay_method": "pay_method",
            "merchant": "merchant",
            "is_baseline": "is_baseline",
            "day_offset": "day_offset",
        },
    ),
    "ticket": (
        "biz_ticket",
        {
            "category": "varchar(32) DEFAULT ''",
            "approval_status": "varchar(32) DEFAULT ''",
            "submitted_by": "varchar(64) DEFAULT ''",
            "submitted_at": "varchar(32) DEFAULT ''",
            "destination": "varchar(64) DEFAULT ''",
            "ref_id": "varchar(64) DEFAULT ''",
        },
        {
            "category": "category",
            "approval_status": "approval_status",
            "submitted_by": "submitted_by",
            "submitted_at": "submitted_at",
            "destination": "destination",
            "ref_id": "ref_id",
        },
    ),
    "sales": (
        "biz_sales",
        {
            "gmv": "numeric(12,2)",
            "merchant": "varchar(64) DEFAULT ''",
            "orders": "integer",
            "refunds": "integer",
            "is_baseline": "boolean",
            "day_offset": "integer",
        },
        {
            "gmv": "gmv",
            "merchant": "merchant",
            "orders": "orders",
            "refunds": "refunds",
            "is_baseline": "is_baseline",
            "day_offset": "day_offset",
        },
    ),
    "verification": (
        "biz_verification",
        {
            "verify_type": "varchar(32) DEFAULT ''",
            "result": "varchar(32) DEFAULT ''",
        },
        {"verify_type": "verify_type", "result": "result"},
    ),
}


def _kind_tables() -> dict[str, tuple[str, dict[str, str], dict[str, str]]]:
    """允许环境变量收缩/扩展表集合（预留，当前全量六类）。"""
    return _KIND_TABLES


def _ddl() -> str:
    """生成六张分表的 DDL：IF NOT EXISTS，绝不 DROP——重灌只清 source='eval' 行。"""
    stmts: list[str] = []
    for kind, (table, extra, _amap) in _kind_tables().items():
        cols = [f"id bigserial PRIMARY KEY"]
        cols += [f"{name} {ddl}" for name, ddl in _PUBLIC_COLS.items()]
        cols += [f"{name} {ddl}" for name, ddl in extra.items()]
        stmts.append(
            f"CREATE TABLE IF NOT EXISTS {table} (\n  " + ",\n  ".join(cols) + "\n)"
        )
        stmts.append(f"CREATE INDEX IF NOT EXISTS idx_{table}_alert ON {table} (alert_id)")
        stmts.append(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_entities ON {table} USING GIN (entities)"
        )
        stmts.append(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_subject ON {table} (subject_type, subject_id)"
        )
    return ";\n".join(stmts)


class DataSourceUnavailable(RuntimeError):
    """业务库不可用（连接失败/超时/权限）。

    与「查无记录」严格区分：前者表示没取到证据，不能当成本案无异常。
    """


def _admin_dsn() -> str:
    return os.getenv("BIZ_DB_ADMIN_DSN", _DEFAULT_ADMIN_DSN)


def _ro_dsn() -> str:
    return os.getenv("BIZ_DB_DSN", _DEFAULT_RO_DSN)


def _num(v, as_int: bool = False):
    """attributes 值安全转数值；空/非数值返回 None。"""
    if v is None or v == "":
        return None
    try:
        return int(v) if as_int else float(v)
    except (TypeError, ValueError):
        return None


def _row_values(r: dict, kind: str, extra_cols: dict[str, str], amap: dict[str, str]) -> tuple:
    """jsonl 行 -> 分表插入元组（公共列 + 专有列，与 DDL 列序一致）。"""
    p = r.get("payload") or {}
    subj = p.get("subject") or {}
    attrs = p.get("attributes") or {}
    is_int = {"logins_7d", "day_offset", "orders", "refunds"}
    extra = []
    for col in extra_cols:
        raw = attrs.get(amap.get(col, col))
        if col in is_int:
            extra.append(_num(raw, as_int=True))
        elif col in ("amount", "gmv"):
            extra.append(_num(raw))
        elif col in ("is_baseline",):
            extra.append(bool(raw)) if raw is not None else extra.append(None)
        else:
            extra.append(str(raw) if raw is not None else "")
    return (
        r.get("alert_id", "") or p.get("alert_id", ""),
        r.get("record_id", ""),
        r.get("title", ""),
        r.get("detail", ""),
        r.get("amount"),
        r.get("date", ""),
        r.get("time", ""),
        r.get("status", ""),
        r.get("tier", ""),
        SOURCE_EVAL,
        subj.get("type", ""),
        str(subj.get("id", "") or ""),
        r.get("entities") or [],
        r.get("seed", ""),
        r.get("expect", ""),
        json.dumps(p, ensure_ascii=False),
        *extra,
    )


def _insert_sql(table: str, extra_cols: dict[str, str]) -> str:
    cols = [
        "alert_id", "record_id", "title", "detail", "amount", "fact_date", "fact_time",
        "status", "data_tier", "source", "subject_type", "subject_id", "entities",
        "seed", "expect", "payload",
    ] + list(extra_cols)
    marks = ", ".join(["%s"] * len(cols))
    return f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})"


def init_db(facts_path: str = _DEFAULT_FACTS) -> int:
    """建分表 + 建只读角色 + 重灌评测夹具事实（只清 source='eval' 行），返回写入行数。"""
    rows = [json.loads(line) for line in open(facts_path, encoding="utf-8") if line.strip()]
    tables = [spec[0] for spec in _kind_tables().values()]
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(_ddl())
        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentinel_ro') THEN
                    CREATE ROLE sentinel_ro LOGIN PASSWORD 'sentinel_ro';
                END IF;
            END $$;
            """
        )
        cur.execute(f"GRANT CONNECT ON DATABASE {_DB} TO sentinel_ro")
        cur.execute("GRANT USAGE ON SCHEMA public TO sentinel_ro")
        cur.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO sentinel_ro")
        cur.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO sentinel_ro"
        )
        # 评测重灌：只清评测行。真实业务数据（source='production'）永不触碰。
        for t in tables:
            cur.execute(f"DELETE FROM {t} WHERE source = %s", (SOURCE_EVAL,))
        total = 0
        for kind, (table, extra_cols, amap) in _kind_tables().items():
            subset = [r for r in rows if r.get("kind") == kind]
            if not subset:
                continue
            cur.executemany(
                _insert_sql(table, extra_cols),
                [_row_values(r, kind, extra_cols, amap) for r in subset],
            )
            total += len(subset)
    return total


def _table_query(
    kind: str, table: str, where: list[str], params: list, limit: int
) -> str:
    """单表查询分支：返回 (sql, params)。kind 作为字面量补齐输出列。"""
    sql = (
        f"SELECT '{kind}' AS kind, alert_id, record_id AS ticket_id, title, detail, amount, "
        f"fact_date AS date, fact_time AS time, status, data_tier, payload "
        f"FROM {table}"
        + (" WHERE " + " AND ".join(where) if where else "")
        + f" ORDER BY fact_time DESC, id DESC LIMIT {limit}"
    )
    return sql, params


def query_facts(
    subject: str = "",
    kind: str | None = None,
    limit: int = 20,
    alert_id: str = "",
    subject_type: str = "",
) -> list[dict]:
    """查业务事实：优先按 alert_id 做案件隔离，没有告警 ID 时退回按主体查。

    subject_type 非空时校验结构化 subject_type 列（原为 payload 提取，现为真列），
    实体类型不符的行不返回（IP 传给账号工具不再「照样命中」）。
    查无返回空列表；连不上/无权限抛 DataSourceUnavailable。
    输出列与旧宽表逐字兼容：kind/alert_id/ticket_id/title/detail/amount/date/time/status/data_tier/payload。
    """
    if not subject and not alert_id:
        return []
    limit = max(1, min(int(limit or 20), 100))

    def base_where(table: str) -> tuple[list[str], list]:
        where: list[str] = []
        params: list = []
        if alert_id:
            where.append("alert_id = %s")
            params.append(alert_id)
        if subject:
            where.append("%s = ANY(entities)")
            params.append(subject)
        if subject_type:
            where.append("subject_type = %s")
            params.append(subject_type)
        return where, params

    tables = _kind_tables()
    if kind and kind in tables:
        table, _extra, _amap = tables[kind]
        where, params = base_where(table)
        sql, params = _table_query(kind, table, where, params, limit)
    else:
        # kind 未指定：六表 UNION ALL。各表 id 独立自增，同秒行加固定分支序保证排序确定
        branches = []
        params = []
        for seq, (k, (table, _extra, _amap)) in enumerate(tables.items()):
            where, wparams = base_where(table)
            cond = (" WHERE " + " AND ".join(where)) if where else ""
            branches.append(
                f"SELECT '{k}' AS kind, alert_id, record_id AS ticket_id, title, detail, amount, "
                f"fact_date AS date, fact_time AS time, status, data_tier, payload, "
                f"fact_time, id, {seq} AS branch FROM {table}{cond}"
            )
            params.extend(wparams)
        union = " UNION ALL ".join(branches)
        sql = (
            f"SELECT kind, alert_id, ticket_id, title, detail, amount, date, time, status, "
            f"data_tier, payload FROM ({union}) AS u "
            f"ORDER BY time DESC, branch ASC, id DESC LIMIT {limit}"
        )
    try:
        with psycopg.connect(_ro_dsn(), connect_timeout=3, row_factory=dict_row) as conn:
            return conn.execute(sql, params).fetchall()
    except psycopg.Error as e:  # 连接/超时/权限/语法
        raise DataSourceUnavailable(f"{e.__class__.__name__}") from e


if __name__ == "__main__":
    if "--init" not in sys.argv:
        print(__doc__)
        raise SystemExit(1)
    idx = sys.argv.index("--init")
    path = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else _DEFAULT_FACTS
    print(f"loaded {init_db(path)} rows from {path}")
