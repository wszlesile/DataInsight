-- add_column: insight_ns_message.content_enabled
ALTER TABLE insight_ns_message ADD COLUMN content_enabled SMALLINT NOT NULL DEFAULT 1;

-- add_column: insight_ns_message.analysis_task_id
ALTER TABLE insight_ns_message ADD COLUMN analysis_task_id BIGINT;

-- add_column: insight_ns_execution.content_enabled
ALTER TABLE insight_ns_execution ADD COLUMN content_enabled SMALLINT NOT NULL DEFAULT 1;

-- add_column: insight_ns_execution.analysis_task_id
ALTER TABLE insight_ns_execution ADD COLUMN analysis_task_id BIGINT;

-- add_column: insight_ns_artifact.content_enabled
ALTER TABLE insight_ns_artifact ADD COLUMN content_enabled SMALLINT NOT NULL DEFAULT 1;

-- add_column: insight_ns_artifact.analysis_task_id
ALTER TABLE insight_ns_artifact ADD COLUMN analysis_task_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_message_turn_task_enabled
ON insight_ns_message (turn_id, analysis_task_id, content_enabled);

CREATE INDEX IF NOT EXISTS idx_execution_turn_task_enabled
ON insight_ns_execution (turn_id, analysis_task_id, content_enabled);

CREATE INDEX IF NOT EXISTS idx_artifact_turn_task_enabled
ON insight_ns_artifact (turn_id, analysis_task_id, content_enabled);
