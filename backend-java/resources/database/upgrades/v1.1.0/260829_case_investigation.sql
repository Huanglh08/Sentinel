-- =============================================================================
-- 案件调查（Case Investigation）Phase 1 DDL
-- 案件一等公民：告警/线索 -> 案件 -> 调查报告 -> 人工决策 -> 处置
-- 对齐 Tracecat Case 模型（5W1H/主体/时间线/TTA-TTR）+ HolmesGPT 证据链
-- 约定：t_ 前缀 / 雪花 VARCHAR(20) 主键 / create_by|create_time|update_by|update_time|deleted 审计列
-- =============================================================================

-- 案件主表 ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS t_case
(
    id              VARCHAR(20) PRIMARY KEY,
    case_no         VARCHAR(32),
    title           VARCHAR(200) NOT NULL,
    status          VARCHAR(32)  NOT NULL DEFAULT 'OPEN',
    -- OPEN -> INVESTIGATING -> REPORTED -> (APPROVED -> RESOLVED | REJECTED) -> CLOSED
    priority        VARCHAR(16)           DEFAULT 'P2',      -- P0/P1/P2/P3
    severity        VARCHAR(16),                             -- benign/suspicious/malicious/inconclusive
    confidence      NUMERIC(5, 4),                           -- 调查结论置信度 0~1
    subject_type    VARCHAR(32),                             -- account/device/ip/order/merchant/other
    subject_id      VARCHAR(64),                             -- 主体标识
    clue_summary    TEXT,                                    -- 线索摘要（5W1H 关键问题）
    raw_input       TEXT,                                    -- 原始告警/线索输入
    triage          TEXT,                                    -- 分诊结果 JSON
    report          TEXT,                                    -- 调查报告 JSON（结论/置信度/证据引用/处置建议）
    user_id         VARCHAR(64),                             -- 提交人
    source          VARCHAR(32)           DEFAULT 'MANUAL',  -- MANUAL/WEBHOOK/SCAN
    tta_ms          BIGINT,                                  -- Time-To-Acknowledge（受理->开始调查）
    ttr_ms          BIGINT,                                  -- Time-To-Report（受理->报告完成）
    create_by       VARCHAR(64)           DEFAULT '',
    create_time     TIMESTAMP             DEFAULT CURRENT_TIMESTAMP,
    update_by       VARCHAR(64)           DEFAULT '',
    update_time     TIMESTAMP             DEFAULT CURRENT_TIMESTAMP,
    deleted         SMALLINT              DEFAULT 0
);
COMMENT ON TABLE t_case IS '风控案件表：调查 Agent 的一等公民，记录线索、分诊、报告与处置指标';
CREATE INDEX IF NOT EXISTS idx_case_user_time ON t_case (user_id, create_time);
CREATE INDEX IF NOT EXISTS idx_case_status ON t_case (status, priority);
CREATE INDEX IF NOT EXISTS idx_case_subject ON t_case (subject_type, subject_id);

-- 案件证据链 ------------------------------------------------------------------
-- 每一步推理/工具调用一行；支撑报告结论下钻回放（evidence_step 引用 step_no）
CREATE TABLE IF NOT EXISTS t_case_evidence
(
    id           VARCHAR(20) PRIMARY KEY,
    case_id      VARCHAR(20) NOT NULL,
    step_no      INT         NOT NULL,
    phase        VARCHAR(16),                          -- TRIAGE/INVESTIGATE/REPORT
    thought      TEXT,                                 -- LLM 本步推理
    tool_id      VARCHAR(64),                         -- 工具名（llm.triage/llm.finish 表示纯推理步）
    tool_args    TEXT,                                 -- 工具参数 JSON
    observation  TEXT,                                 -- 工具观察结果（超长已截断）
    is_error     SMALLINT     DEFAULT 0,
    elapsed_ms   BIGINT,
    create_time  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);
COMMENT ON TABLE t_case_evidence IS '案件证据链：调查过程逐步留痕，报告 key_findings.evidence_step 引用 step_no';
CREATE INDEX IF NOT EXISTS idx_case_evidence_case ON t_case_evidence (case_id, step_no);
