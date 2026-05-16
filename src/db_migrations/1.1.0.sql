-- add_column: insight_ns_execution.used_datasource_ids_json
ALTER TABLE insight_ns_execution ADD COLUMN used_datasource_ids_json VARCHAR(1024) NOT NULL DEFAULT '[]';

-- add_column: insight_ns_turn.used_datasource_ids_json
ALTER TABLE insight_ns_turn ADD COLUMN used_datasource_ids_json VARCHAR(1024) NOT NULL DEFAULT '[]';
