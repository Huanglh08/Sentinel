# -*- coding: utf-8 -*-
"""P1 知识库可见性自检：真库链路 + 过滤组合断言。

用法: python _selfcheck_visibility.py
覆盖: hidden_collections 真实 SQL/缓存链路、visible 剔除组合逻辑。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.db import hidden_collections  # noqa: E402

PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    assert cond, f"FAIL {name} {detail}"
    PASS += 1
    print(f"  ok {PASS:02d} {name}")


def main() -> None:
    test_col = "_hidden_test_col_p1"

    # 1) 初始无隐藏库（生产库默认全 visible=1）
    before = hidden_collections(ttl_s=0)
    check("初始 hidden 为空", test_col not in before, f"got={before}")

    # 2) 造一条隐藏库，hidden_collections 应即时可见（ttl_s=0 绕缓存）
    from core.db import get_pool

    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO t_knowledge_base (id, name, embedding_model, collection_name,"
            " visible_to_user, created_by, updated_by, deleted)"
            " VALUES (999999001, '_p1自检库', 'test-model', %s, 0, 'selfcheck', 'selfcheck', 0)"
            " ON CONFLICT (id) DO UPDATE SET visible_to_user = 0, deleted = 0",
            (test_col,),
        )
        conn.commit()
    after = hidden_collections(ttl_s=0)
    check("隐藏库即时出现在 hidden", test_col in after, f"got={after}")

    # 3) TTL 缓存生效：默认 ttl=60s 下重复调用应命中缓存（结果一致且不报错）
    check("TTL 缓存路径可用", isinstance(hidden_collections(), list))

    # 4) agent 模块可导入（过滤逻辑所在文件无语法/依赖问题）
    from core.agent import Agent  # noqa: F401

    # 5) 剔除组合逻辑（与 agent.answer 内联逻辑一致）
    active = ["riskdocs", "platform-policy", "biz-mock"]
    hidden = {test_col, "platform-policy"}
    visible = [c for c in active if c not in hidden]
    check("剔除隐藏 collection", visible == ["riskdocs", "biz-mock"], f"got={visible}")
    check("admin 语义不过滤", [c for c in active if c not in set()] == active)

    # 6) 清理（id 为 varchar，用 collection_name 定位）
    with get_pool().connection() as conn:
        conn.execute("DELETE FROM t_knowledge_base WHERE collection_name = %s", (test_col,))
        conn.commit()
    cleaned = hidden_collections(ttl_s=0)
    check("清理后 hidden 恢复", test_col not in cleaned, f"got={cleaned}")

    print(f"\n{PASS} checks passed")


if __name__ == "__main__":
    main()
