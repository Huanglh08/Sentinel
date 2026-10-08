-- =============================================================================
-- P2 风险评级配置化：L2 自动处置策略单行表（整合三处分散配置）
--   ① CASE_L2_AUTOPILOT        环境变量 -> l2_autopilot_enabled
--   ② CASE_L2_CONFIDENCE       环境变量 -> l2_confidence
--   ③ CASE_L2_ACTION_WHITELIST 环境变量 -> l2_action_whitelist
--   ④ L2_ACTION_DENYLIST       硬编码   -> l2_action_denylist
-- 默认值 = 上线前现状（autopilot 关、阈值 0.90、白/黑名单不变），落库即基线。
-- agent 运行时读此表（30s TTL 缓存），DB 不可用时降级出厂默认，行为不劣化。
-- 幂等：可重复执行。
-- =============================================================================
CREATE TABLE IF NOT EXISTS t_risk_policy (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    l2_autopilot_enabled SMALLINT NOT NULL DEFAULT 0,
    l2_confidence NUMERIC(4, 3) NOT NULL DEFAULT 0.900,
    l2_action_whitelist TEXT NOT NULL DEFAULT '观察,提醒,工单,通知,标记,升级,放行,解除,限制',
    l2_action_denylist TEXT NOT NULL DEFAULT '封禁,冻结,止付,清退,拦截,拉黑,关停,下架,提现',
    updated_by VARCHAR(64) NOT NULL DEFAULT 'system',
    update_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_risk_policy_confidence CHECK (l2_confidence >= 0 AND l2_confidence <= 1)
);

COMMENT ON TABLE t_risk_policy IS 'L2 自动处置风险策略（单行配置，admin 管理页维护）';

INSERT INTO t_risk_policy (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
