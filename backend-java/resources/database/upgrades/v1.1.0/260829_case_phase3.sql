-- 案件调查 Phase 3：处置动作表 + 审计日志表
-- P3-1 审批执行闭环：APPROVED 后按建议落处置动作记录
-- P3-2 审批权限与审计：每次裁决/归档写审计日志

CREATE TABLE IF NOT EXISTS t_case_action (
    id          VARCHAR(32)  PRIMARY KEY,
    case_id     VARCHAR(32)  NOT NULL,
    case_no     VARCHAR(64)  NOT NULL DEFAULT '',
    approval_id VARCHAR(32)  NOT NULL DEFAULT '',
    action      VARCHAR(255) NOT NULL,
    detail      VARCHAR(1024) NOT NULL DEFAULT '',
    status      VARCHAR(16)  NOT NULL DEFAULT 'EXECUTED',
    operator    VARCHAR(64)  NOT NULL DEFAULT '',
    create_time TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_case_action_case_id ON t_case_action (case_id);

CREATE TABLE IF NOT EXISTS t_case_audit (
    id          VARCHAR(32)  PRIMARY KEY,
    case_id     VARCHAR(32)  NOT NULL DEFAULT '',
    approval_id VARCHAR(32)  NOT NULL DEFAULT '',
    operation   VARCHAR(32)  NOT NULL,
    target      VARCHAR(64)  NOT NULL DEFAULT '',
    operator    VARCHAR(64)  NOT NULL DEFAULT '',
    detail      VARCHAR(1024) NOT NULL DEFAULT '',
    create_time TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_case_audit_case_id ON t_case_audit (case_id, create_time DESC);
