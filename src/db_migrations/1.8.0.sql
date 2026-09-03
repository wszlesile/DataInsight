-- add_column: insight_namespace.namespace_kind
ALTER TABLE insight_namespace
ADD COLUMN namespace_kind VARCHAR(32) NOT NULL DEFAULT 'interactive';

-- create_table_from_model: InsightWorkflowJob
