-- 案件证据链：补结构化工具状态（2026-10-18）
-- 背景：MCP 侧已按 structured=true 返回 case_tool_v1 信封（status=hit|miss|error|unsupported
-- + count），agent 也解析进了内存对象，但落库时被丢掉——证据回放、报告复算、接口展示
-- 都只能再靠「共 0 条」这类文案猜，空结果/命中/报错三态无法区分。
-- 两列纯增量，历史行按空值/0 处理，不回填。
ALTER TABLE t_case_evidence ADD COLUMN IF NOT EXISTS tool_status VARCHAR(16);
ALTER TABLE t_case_evidence ADD COLUMN IF NOT EXISTS record_count INT DEFAULT 0;

COMMENT ON COLUMN t_case_evidence.tool_status IS '结构化工具状态：hit | miss | error | unsupported，空=旧数据未采集';
COMMENT ON COLUMN t_case_evidence.record_count IS '结构化工具返回的记录条数（status=hit 时有效）';
