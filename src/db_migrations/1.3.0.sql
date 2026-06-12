-- add_column: insight_ns_execution.analysis_parameters_schema_json
ALTER TABLE insight_ns_execution ADD COLUMN analysis_parameters_schema_json TEXT NOT NULL DEFAULT '{}';

-- add_column: insight_ns_execution.analysis_parameters_json
ALTER TABLE insight_ns_execution ADD COLUMN analysis_parameters_json TEXT NOT NULL DEFAULT '{}';

-- add_column: insight_ns_execution.analysis_logic_signature
ALTER TABLE insight_ns_execution ADD COLUMN analysis_logic_signature VARCHAR(128) NOT NULL DEFAULT '';

-- add_column: insight_ns_execution.parent_execution_id
ALTER TABLE insight_ns_execution ADD COLUMN parent_execution_id INTEGER;

-- add_column: insight_ns_execution.execution_source
ALTER TABLE insight_ns_execution ADD COLUMN execution_source VARCHAR(32) NOT NULL DEFAULT 'llm_generated';

CREATE INDEX IF NOT EXISTS idx_execution_parent_execution
ON insight_ns_execution (parent_execution_id);

CREATE INDEX IF NOT EXISTS idx_execution_logic_signature
ON insight_ns_execution (analysis_logic_signature);
