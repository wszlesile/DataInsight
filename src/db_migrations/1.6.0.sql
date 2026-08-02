-- add_column: insight_ns_execution_evidence.compilation_status
ALTER TABLE insight_ns_execution_evidence
ADD COLUMN compilation_status VARCHAR(32) NOT NULL DEFAULT 'complete';

-- add_column: insight_ns_execution_evidence.diagnostics_json
ALTER TABLE insight_ns_execution_evidence
ADD COLUMN diagnostics_json TEXT NOT NULL DEFAULT '{}';
