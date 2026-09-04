-- =============================================================================
-- 案件调查 Phase 3：知识库飞轮候选池
-- 案件归档时把报告 knowledge_distillation（经验手法）回写至此，
-- 形成「调查-报告-沉淀」飞轮；后续 Rule Miner / 知识入库流程消费本表。
-- 幂等：case_id 唯一，重复归档不产生重复沉淀。
-- =============================================================================

CREATE TABLE IF NOT EXISTS t_case_kb_distillation (
    id           VARCHAR(32)   PRIMARY KEY,                         -- 雪花 ID（字符串）
    case_id      VARCHAR(32)   NOT NULL,                            -- 来源 t_case.id
    case_no      VARCHAR(64)   NOT NULL DEFAULT '',                 -- 冗余案件编号
    title        VARCHAR(128)  NOT NULL DEFAULT '',                 -- 案件标题
    conclusion   VARCHAR(16)   NOT NULL DEFAULT '',                 -- 案件结论 benign/suspicious/malicious/inconclusive
    confidence   DECIMAL(6,4),                                      -- 结论置信度 0-1
    distillation VARCHAR(2048) NOT NULL,                            -- 可沉淀的经验手法/规则建议
    operator     VARCHAR(64)   NOT NULL DEFAULT '',                 -- 触发归档的操作人
    create_time  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    update_time  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT uk_case_kb_distillation_case UNIQUE (case_id)
);

CREATE INDEX IF NOT EXISTS idx_case_kb_distillation_conclusion ON t_case_kb_distillation (conclusion, create_time DESC);

COMMENT ON TABLE t_case_kb_distillation IS '知识库飞轮候选池：案件报告经验回写（Phase 3）';
COMMENT ON COLUMN t_case_kb_distillation.distillation IS '报告 knowledge_distillation 字段沉淀的经验手法';
