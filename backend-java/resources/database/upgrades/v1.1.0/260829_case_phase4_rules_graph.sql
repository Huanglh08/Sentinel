-- =============================================================================
-- 案件调查 Phase 4：规则工厂 + 巡检幂等 + 风控关系图底座
-- ① t_case_rule_candidate : Rule Miner 候选规则（蒸馏池 -> PENDING -> HITL 审核 -> 回注分诊）
-- ② t_case.fingerprint    : 巡检幂等指纹（部分唯一索引防并发重复建案）
-- ③ t_case_graph_edge     : 风控关系图边表（一机多号/一层N层）。当前 mcp-server-py
--                           的 graph_query 走内存 mock（与既有工具一致），本表为
--                           真实化阶段的数据底座，切换时仅替换工具内数据源。
-- =============================================================================

-- ① Rule Miner 候选规则
CREATE TABLE IF NOT EXISTS t_case_rule_candidate (
    id                  VARCHAR(32)   PRIMARY KEY,                         -- 雪花 ID（字符串）
    rule_name           VARCHAR(128)  NOT NULL,
    condition_text      VARCHAR(1024) NOT NULL,                            -- 可判定的线索特征条件
    action_suggestion   VARCHAR(512)  NOT NULL DEFAULT '',                 -- 命中后建议动作（禁止高危动作）
    severity            VARCHAR(16)   NOT NULL DEFAULT '',                 -- 命中该规则的典型结论
    confidence          DECIMAL(6,4),                                      -- 规则可信度 0-1
    condition_hash      VARCHAR(64)   NOT NULL,                            -- 条件文本 sha256 前 32 位（去重护栏）
    status              VARCHAR(16)   NOT NULL DEFAULT 'PENDING',          -- PENDING/APPROVED/REJECTED
    source_case_ids     VARCHAR(512)  NOT NULL DEFAULT '',                 -- 溯源：来源案件 ID 列表
    source_distill_ids  VARCHAR(512)  NOT NULL DEFAULT '',                 -- 溯源：来源蒸馏记录 ID 列表
    validation_hit_rate DECIMAL(6,4),                                      -- 历史案件回放验证命中率
    validation_sample   INT           NOT NULL DEFAULT 0,                  -- 回放样本量
    validation_note     VARCHAR(512)  NOT NULL DEFAULT '',                 -- 回放方法说明
    hit_count           INT           NOT NULL DEFAULT 0,                  -- 分诊注入引用计数（生命周期数据）
    operator            VARCHAR(64)   NOT NULL DEFAULT '',                 -- 挖掘触发人
    review_operator     VARCHAR(64)   NOT NULL DEFAULT '',                 -- 审核人（HITL）
    review_opinion      VARCHAR(512)  NOT NULL DEFAULT '',                 -- 审核意见
    create_time         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    update_time         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT uk_case_rule_condition UNIQUE (condition_hash)
);

CREATE INDEX IF NOT EXISTS idx_case_rule_status
    ON t_case_rule_candidate (status, confidence DESC, hit_count DESC);

COMMENT ON TABLE  t_case_rule_candidate IS 'Rule Miner 候选规则：经验蒸馏 -> HITL 审核 -> 回注分诊（Phase 4）';
COMMENT ON COLUMN t_case_rule_candidate.condition_hash      IS '条件文本 sha256 前 32 位，唯一约束去重（重复经验不重复产规则）';
COMMENT ON COLUMN t_case_rule_candidate.status              IS 'PENDING/APPROVED/REJECTED；仅 APPROVED 回注 CASE_TRIAGE（__RULE_HINTS__）';
COMMENT ON COLUMN t_case_rule_candidate.validation_hit_rate IS '历史案件回放验证命中率（0-1，HITL 量化信号，非审核结论）';
COMMENT ON COLUMN t_case_rule_candidate.hit_count           IS '分诊注入引用计数；关联案件被人工改判时的降级依据（后续迭代）';

-- ② 巡检幂等：t_case 增量指纹列 + 部分唯一索引（空串不参与唯一，存量数据零影响）
ALTER TABLE t_case ADD COLUMN IF NOT EXISTS fingerprint VARCHAR(64) NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS uk_case_fingerprint ON t_case (fingerprint) WHERE fingerprint <> '';
COMMENT ON COLUMN t_case.fingerprint IS '线索指纹 sha256(source|clue)，巡检幂等防重复建案（Phase 4）';

-- ③ 风控关系图边表（有向存储，查询按双向展开；一层N层用递归 CTE）
CREATE TABLE IF NOT EXISTS t_case_graph_edge (
    id          VARCHAR(32)   PRIMARY KEY,                                   -- 雪花 ID（字符串）
    src_type    VARCHAR(16)   NOT NULL,                                      -- account/device/ip/order
    src_id      VARCHAR(128)  NOT NULL,
    rel_type    VARCHAR(32)   NOT NULL,                                      -- same_device/same_ip/transfer/own
    dst_type    VARCHAR(16)   NOT NULL,
    dst_id      VARCHAR(128)  NOT NULL,
    weight      DECIMAL(6,4)  NOT NULL DEFAULT 1,                            -- 关系强度（次数归一化）
    create_time TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_case_graph_src ON t_case_graph_edge (src_type, src_id);
CREATE INDEX IF NOT EXISTS idx_case_graph_dst ON t_case_graph_edge (dst_type, dst_id);

COMMENT ON TABLE  t_case_graph_edge IS '风控关系图边表：一机多号/一层N层关系查询（Phase 4 图查询数据底座）';
COMMENT ON COLUMN t_case_graph_edge.rel_type IS '关系类型: same_device(同设备)/same_ip(同IP)/transfer(资金)/own(持有)';

-- 一层N层扩展示例（真实化阶段 graph_query 的 SQL 形态）:
-- WITH RECURSIVE rel AS (
--     SELECT dst_type, dst_id, rel_type, 1 AS depth
--       FROM t_case_graph_edge
--      WHERE src_type = %s AND src_id = %s AND (%s = '' OR rel_type = %s)
--     UNION
--     SELECT e.dst_type, e.dst_id, e.rel_type, r.depth + 1
--       FROM t_case_graph_edge e
--       JOIN rel r ON e.src_type = r.dst_type AND e.src_id = r.dst_id
--      WHERE r.depth < %s AND (%s = '' OR e.rel_type = %s)
-- )
-- SELECT * FROM rel;
-- 注意: relation 过滤为全跳语义（每一跳都限定 rel_type），与 mcp graph_query 的
-- _graph_bfs 全跳过滤实现保持一致；上例 %s 参数按出现顺序: src_type, src_id, rel_type, depth, rel_type
