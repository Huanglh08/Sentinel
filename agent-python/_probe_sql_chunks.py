# 一次性诊断：从线上 t_knowledge_vector 捞真实文档的真实 chunk，核对代码块是否被切断
import psycopg

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/sentinel?client_encoding=UTF8"

with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
    cur.execute("SELECT DISTINCT jsonb_object_keys(metadata) FROM t_knowledge_vector")
    print("META KEYS:", [r[0] for r in cur.fetchall()])

    cur.execute("SELECT collection_name, count(*) FROM t_knowledge_vector GROUP BY 1 ORDER BY 2 DESC")
    print("COLLECTIONS:", cur.fetchall())

    for key in ("source_file", "doc_name", "file_name"):
        cur.execute(f"SELECT count(*) FROM t_knowledge_vector WHERE metadata->>'{key}' ILIKE '%SQL_Injection%'")
        print(f"hit[{key}]:", cur.fetchone()[0])

    # 优先用命中非零的键
    key = None
    for k in ("source_file", "doc_name", "file_name"):
        cur.execute(f"SELECT count(*) FROM t_knowledge_vector WHERE metadata->>'{k}' ILIKE '%SQL_Injection%'")
        if cur.fetchone()[0] > 0:
            key = k
            break
    print("USING KEY:", key)
    if key:
        cur.execute(
            f"""SELECT id, collection_name, length(content), metadata->>'chunk_index',
                       (content LIKE '%```java%') AS has_fence,
                       (length(content) - length(replace(content, '```', ''))) / 3 AS fence_cnt
                FROM t_knowledge_vector
                WHERE metadata->>'{key}' ILIKE '%SQL_Injection%'
                ORDER BY (metadata->>'chunk_index')::int"""
        )
        rows = cur.fetchall()
        print(f"CHUNKS: {len(rows)}")
        for r in rows:
            print(f"  idx={r[3]} len={r[2]} fence={r[5]} id={r[0]} coll={r[1]}")

        # 打印含 java 围栏的块，看代码是否完整（首行/末行）
        for r in rows:
            cur.execute("SELECT content FROM t_knowledge_vector WHERE id = %s", (r[0],))
            c = cur.fetchone()[0]
            if "```java" in c:
                lines = c.splitlines()
                print(f"\n--- chunk idx={r[3]} len={r[2]} lines={len(lines)} ---")
                print("FIRST:", lines[0][:120])
                print("LAST :", lines[-1][:120])
                for i, ln in enumerate(lines):
                    if ln.startswith("```"):
                        print(f"  fence line {i}: {ln[:80]}")
