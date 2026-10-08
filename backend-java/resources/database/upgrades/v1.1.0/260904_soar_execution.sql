-- =============================================================================
-- 案件调查 Phase 3：SOAR 处置执行闭环
-- 1) t_case_approval 加二级复核字段：命中高危动作（L2_ACTION_DENYLIST，如冻结）
--    需两名不同操作人先后裁决，第一次裁决记入 first_*，终审记入 operator/opinion。
-- 2) 新建 t_risk_account_control：审批通过后执行限权/冻结动作的落地表，
--    替代对真实账户系统的调用（项目无账户系统可调，管控状态持久化可查、可演示）。
-- 幂等：可重复执行。
-- =============================================================================

-- 1) 二级复核字段（一级裁决记录；operator 留给终审人，保持既有语义不变）
ALTER TABLE t_case_approval ADD COLUMN IF NOT EXISTS first_operator    VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE t_case_approval ADD COLUMN IF NOT EXISTS first_opinion     VARCHAR(1024) NOT NULL DEFAULT '';
ALTER TABLE t_case_approval ADD COLUMN IF NOT EXISTS first_decide_time TIMESTAMPTZ NULL;

COMMENT ON COLUMN t_case_approval.first_operator    IS '一级裁决人（高危动作双人复核时记录；低危动作为空）';
COMMENT ON COLUMN t_case_approval.first_opinion     IS '一级裁决意见';
COMMENT ON COLUMN t_case_approval.first_decide_time IS '一级裁决时间';

-- 2) 管控落地表
CREATE TABLE IF NOT EXISTS t_risk_account_control (
    id           VARCHAR(32)   PRIMARY KEY,                          -- 雪花 ID（字符串）
    case_id      VARCHAR(32)   NOT NULL DEFAULT '',                  -- 来源 t_case.id
    case_no      VARCHAR(64)   NOT NULL DEFAULT '',                  -- 冗余案件编号
    approval_id  VARCHAR(32)   NOT NULL DEFAULT '',                  -- 来源 t_case_approval.id
    subject_id   VARCHAR(128)  NOT NULL,                             -- 管控对象（账户/手机号/设备号）
    control_type VARCHAR(16)   NOT NULL,                             -- LIMITED 限权 / FROZEN 冻结
    status       VARCHAR(16)   NOT NULL DEFAULT 'ACTIVE',            -- ACTIVE 生效 / RELEASED 已解除
    operator     VARCHAR(64)   NOT NULL DEFAULT '',                  -- 执行操作人（终审人）
    detail       VARCHAR(1024) NOT NULL DEFAULT '',                  -- 执行依据/说明
    create_time  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    update_time  TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- 同一对象同一管控类型同时最多一条生效记录（重复处置幂等去重的兜底约束）
CREATE UNIQUE INDEX IF NOT EXISTS uq_risk_control_active
    ON t_risk_account_control (subject_id, control_type) WHERE status = 'ACTIVE';
CREATE INDEX IF NOT EXISTS idx_risk_control_subject ON t_risk_account_control (subject_id, status);
CREATE INDEX IF NOT EXISTS idx_risk_control_case ON t_risk_account_control (case_id);

COMMENT ON TABLE t_risk_account_control IS 'SOAR 处置执行落地表（限权/冻结管控状态，替代真实账户系统）';
COMMENT ON COLUMN t_risk_account_control.control_type IS 'LIMITED 限权 / FROZEN 冻结（冻结=账号级冻结：登录限制+资金延迟）';
COMMENT ON COLUMN t_risk_account_control.status IS 'ACTIVE 管控生效 / RELEASED 管控解除';
