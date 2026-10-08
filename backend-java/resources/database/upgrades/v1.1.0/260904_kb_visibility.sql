-- =============================================================================
-- P1 知识库可见性（参照 Dify dataset permission 思路，收敛为两级角色单字段）
-- visible_to_user: 1=所有登录用户可检索（默认）；0=仅 admin 可检索。
-- 检索侧过滤点在 agent（core/agent.py 按请求角色剔除隐藏库的 collection），
-- 本表只存配置；存量库默认全可见，基线行为不变。
-- 幂等：可重复执行。
-- =============================================================================
ALTER TABLE t_knowledge_base ADD COLUMN IF NOT EXISTS visible_to_user SMALLINT NOT NULL DEFAULT 1;

COMMENT ON COLUMN t_knowledge_base.visible_to_user IS '可见性：1 所有人可检索 / 0 仅 admin 可检索';
