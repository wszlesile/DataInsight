-- create_table_from_model: InsightTaskDefinition
-- create_table_from_model: InsightTaskRevision
-- create_table_from_model: InsightTaskRun

-- add_column: insight_task_run.dispatch_attempts
ALTER TABLE insight_task_run
ADD COLUMN dispatch_attempts INTEGER NOT NULL DEFAULT 0;

-- add_column: insight_task_run.next_attempt_at
ALTER TABLE insight_task_run
ADD COLUMN next_attempt_at TIMESTAMP;
