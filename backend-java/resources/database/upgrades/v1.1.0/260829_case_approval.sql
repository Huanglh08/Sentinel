-- =============================================================================
-- 案件调查 Phase 2：人工审批闭环
-- 调查报告中 requires_human_approval=true 的处置建议自动落为审批待办，
-- 管理端 approve/reject 后回写 operator/opinion，形成「发现-建议-审批」闭环。
-- 幂等：可重复执行。
-- =============================================================================

CREATE TABLE IF NOT EXISTS t_case_approval (
    id           VARCHAR(32)  PRIMARY KEY,                          -- 雪花 ID（字符串）
    case_id      VARCHAR(32)  NOT NULL,                             -- 关联 t_case.id
    case_no      VARCHAR(64)  NOT NULL DEFAULT '',                  -- 冗余案件编号，便于检索
    action       VARCHAR(255) NOT NULL,                             -- 建议动作（如 冻结账户）
    reason       VARCHAR(1024) NOT NULL DEFAULT '',                 -- 建议理由
    status       VARCHAR(16)  NOT NULL DEFAULT 'PENDING',           -- PENDING / APPROVED / REJECTED
    operator     VARCHAR(64)  NOT NULL DEFAULT '',                  -- 审批人
    opinion      VARCHAR(1024) NOT NULL DEFAULT '',                 -- 审批意见
    create_time  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    update_time  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_case_approval_case_id ON t_case_approval (case_id);
CREATE INDEX IF NOT EXISTS idx_case_approval_status ON t_case_approval (status, create_time DESC);

COMMENT ON TABLE t_case_approval IS '案件调查人工审批待办（Phase 2 审批闭环）';
COMMENT ON COLUMN t_case_approval.status IS 'PENDING 待审批 / APPROVED 已批准 / REJECTED 已驳回';
