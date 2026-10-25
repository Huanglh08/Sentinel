-- 261024 案件调查中断续跑：自动处置动作防重放
-- 背景：图引擎断点续跑会重放行动阶段；同案同文本的 L1/L2 自动动作必须唯一，
--       配合写入侧 ON CONFLICT DO NOTHING 保证任何路径不重复落动作/重复执行。
-- 影响面：仅新增部分唯一索引，不改既有表结构与数据。

CREATE UNIQUE INDEX IF NOT EXISTS uq_case_auto_action
    ON t_case_action (case_id, action)
    WHERE operator IN ('L1:auto', 'L2:auto');
