import os
from typing import Iterable

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from config.config import Config
from utils.datasource_utils import DEFAULT_CONVERSATION_TITLE, normalize_datasource_type

load_dotenv()

db_type = Config.DB_TYPE.lower()

if db_type == 'sqlite':
    db_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        Config.SQLITE_PATH,
    )
    engine = create_engine(f"sqlite:///{db_path}", echo=Config.DEBUG)
elif db_type == 'mysql':
    engine = create_engine(
        f"mysql://{Config.DB_USER}:{Config.DB_PASSWORD}@{Config.DB_HOST}:{Config.DB_PORT}/{Config.DB_NAME}",
        pool_pre_ping=True,
        echo=Config.DEBUG,
    )
elif db_type == 'postgresql':
    engine = create_engine(
        f"postgresql://{Config.PG_USER}:{Config.PG_PASSWORD}@{Config.PG_HOST}:{Config.PG_PORT}/{Config.PG_NAME}",
        pool_pre_ping=True,
        echo=Config.DEBUG,
    )
else:
    raise ValueError(f"Unsupported database type: {db_type}")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _has_table(table_name: str) -> bool:
    inspector = inspect(engine)
    return inspector.has_table(table_name)


def _existing_columns(table_name: str) -> set[str]:
    inspector = inspect(engine)
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _ensure_columns(table_name: str, column_sql_list: Iterable[str]) -> None:
    existing = _existing_columns(table_name)
    if not existing:
        return

    for column_sql in column_sql_list:
        column_name = column_sql.split()[0]
        if column_name in existing:
            continue
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"))


def _rename_table(old_name: str, new_name: str) -> None:
    if not _has_table(old_name) or _has_table(new_name):
        return
    with engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE {old_name} RENAME TO {new_name}"))


def _rename_column(table_name: str, old_name: str, new_name: str) -> None:
    existing = _existing_columns(table_name)
    if not existing or old_name not in existing or new_name in existing:
        return
    with engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE {table_name} RENAME COLUMN {old_name} TO {new_name}"))


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    existing = _existing_columns(table_name)
    if not existing or column_name not in existing:
        return
    with engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {column_name}"))


def _create_table_if_not_exists(create_sql: str) -> None:
    with engine.begin() as connection:
        connection.execute(text(create_sql))


def _normalize_datasource_type(value) -> str:
    """Compatibility wrapper kept to avoid rewriting existing migration call sites."""
    return normalize_datasource_type(value)


def _backfill_datasource_split() -> None:
    if not _has_table('insight_ns_rel_datasource') or not _has_table('insight_datasource'):
        return

    existing = _existing_columns('insight_ns_rel_datasource')
    if 'datasource_id' not in existing:
        return

    select_fields = [
        'id',
        'insight_namespace_id',
        'datasource_id',
        'datasource_type',
        'datasource_name',
        'knowledge_tag',
        'datasource_schema',
        'datasource_config_json',
        'created_at',
        'updated_at',
    ]
    available_fields = [field for field in select_fields if field in existing]
    if 'id' not in available_fields or 'datasource_id' not in available_fields:
        return

    with engine.begin() as connection:
        rows = connection.execute(text(
            f"SELECT {', '.join(available_fields)} FROM insight_ns_rel_datasource "
            "WHERE datasource_id IS NULL OR datasource_id = 0"
        )).mappings().all()

        for row in rows:
            insert_result = connection.execute(text(
                "INSERT INTO insight_datasource ("
                "insight_namespace_id, datasource_type, datasource_name, knowledge_tag, datasource_schema, datasource_config_json, "
                "created_at, updated_at"
                ") VALUES ("
                ":insight_namespace_id, :datasource_type, :datasource_name, :knowledge_tag, :datasource_schema, :datasource_config_json, "
                ":created_at, :updated_at"
                ")"
            ), {
                'insight_namespace_id': row.get('insight_namespace_id') or 0,
                'datasource_type': _normalize_datasource_type(row.get('datasource_type')),
                'datasource_name': row.get('datasource_name') or '',
                'knowledge_tag': row.get('knowledge_tag') or '',
                'datasource_schema': row.get('datasource_schema') or '',
                'datasource_config_json': row.get('datasource_config_json') or '{}',
                'created_at': row.get('created_at'),
                'updated_at': row.get('updated_at'),
            })
            datasource_id = insert_result.lastrowid
            connection.execute(text(
                "UPDATE insight_ns_rel_datasource "
                "SET datasource_id = :datasource_id, "
                "sort_no = CASE WHEN sort_no IS NULL OR sort_no = 0 THEN id ELSE sort_no END "
                "WHERE id = :rel_id"
            ), {
                'datasource_id': datasource_id,
                'rel_id': row['id'],
            })


def _rebuild_insight_datasource_table() -> None:
    if not _has_table('insight_datasource'):
        return

    columns = {column["name"]: column for column in inspect(engine).get_columns('insight_datasource')}
    datasource_type_column = columns.get('datasource_type')
    if datasource_type_column is None:
        return

    datasource_type_name = str(datasource_type_column.get('type') or '').upper()
    expected = {
        'id',
        'insight_namespace_id',
        'datasource_type',
        'datasource_name',
        'knowledge_tag',
        'uns_node_id',
        'datasource_schema',
        'datasource_config_json',
        'is_deleted',
        'created_at',
        'updated_at',
    }
    if ('CHAR' in datasource_type_name or 'TEXT' in datasource_type_name) and set(columns.keys()) == expected:
        return

    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS insight_datasource_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "insight_namespace_id INTEGER NOT NULL DEFAULT 0, "
            "datasource_type VARCHAR(32) NOT NULL DEFAULT 'unknown', "
            "datasource_name VARCHAR(128) NOT NULL DEFAULT '', "
            "knowledge_tag VARCHAR(128) NOT NULL DEFAULT '', "
            "uns_node_id VARCHAR(128) NOT NULL DEFAULT '', "
            "datasource_schema TEXT NOT NULL DEFAULT '', "
            "datasource_config_json TEXT NOT NULL DEFAULT '{}', "
            "is_deleted INTEGER NOT NULL DEFAULT 0, "
            "created_at DATETIME, "
            "updated_at DATETIME"
            ")"
        ))
        rows = connection.execute(text(
            "SELECT id, datasource_type, datasource_name, knowledge_tag, datasource_schema, datasource_config_json, "
            "insight_namespace_id, is_deleted, created_at, updated_at, "
            "COALESCE(uns_node_id, '') AS uns_node_id "
            "FROM insight_datasource"
        )).mappings().all()
        for row in rows:
            connection.execute(text(
                "INSERT INTO insight_datasource_new ("
                "id, insight_namespace_id, datasource_type, datasource_name, knowledge_tag, uns_node_id, datasource_schema, datasource_config_json, "
                "is_deleted, created_at, updated_at"
                ") VALUES ("
                ":id, :insight_namespace_id, :datasource_type, :datasource_name, :knowledge_tag, :uns_node_id, :datasource_schema, :datasource_config_json, "
                ":is_deleted, :created_at, :updated_at"
                ")"
            ), {
                'id': row['id'],
                'insight_namespace_id': row.get('insight_namespace_id') or 0,
                'datasource_type': _normalize_datasource_type(row.get('datasource_type')),
                'datasource_name': row.get('datasource_name') or '',
                'knowledge_tag': row.get('knowledge_tag') or '',
                'uns_node_id': row.get('uns_node_id') or '',
                'datasource_schema': row.get('datasource_schema') or '',
                'datasource_config_json': row.get('datasource_config_json') or '{}',
                'is_deleted': row.get('is_deleted') or 0,
                'created_at': row.get('created_at'),
                'updated_at': row.get('updated_at'),
            })
        connection.execute(text("DROP TABLE insight_datasource"))
        connection.execute(text("ALTER TABLE insight_datasource_new RENAME TO insight_datasource"))


def _rebuild_ns_rel_datasource_table() -> None:
    if not _has_table('insight_ns_rel_datasource'):
        return

    existing = _existing_columns('insight_ns_rel_datasource')
    expected = {
        'id',
        'insight_namespace_id',
        'insight_conversation_id',
        'datasource_id',
        'is_active',
        'sort_no',
        'bind_source',
        'is_deleted',
        'created_at',
        'updated_at',
    }
    if existing == expected:
        return

    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS insight_ns_rel_datasource_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "insight_namespace_id INTEGER NOT NULL, "
            "insight_conversation_id INTEGER NOT NULL DEFAULT 0, "
            "datasource_id INTEGER NOT NULL DEFAULT 0, "
            "is_active INTEGER NOT NULL DEFAULT 1, "
            "sort_no INTEGER NOT NULL DEFAULT 0, "
            "bind_source VARCHAR(32) NOT NULL DEFAULT 'user_selected', "
            "is_deleted INTEGER NOT NULL DEFAULT 0, "
            "created_at DATETIME, "
            "updated_at DATETIME"
            ")"
        ))
        connection.execute(text(
            "INSERT INTO insight_ns_rel_datasource_new ("
            "id, insight_namespace_id, insight_conversation_id, datasource_id, is_active, sort_no, is_deleted, created_at, updated_at"
            ", bind_source"
            ") "
            "SELECT "
            "id, "
            "insight_namespace_id, "
            "COALESCE(insight_conversation_id, 0), "
            "COALESCE(datasource_id, 0), "
            "COALESCE(is_active, 1), "
            "COALESCE(sort_no, 0), "
            "COALESCE(is_deleted, 0), "
            "created_at, "
            "updated_at, "
            "COALESCE(bind_source, 'user_selected') "
            "FROM insight_ns_rel_datasource"
        ))
        connection.execute(text("DROP TABLE insight_ns_rel_datasource"))
        connection.execute(text("ALTER TABLE insight_ns_rel_datasource_new RENAME TO insight_ns_rel_datasource"))


def _rebuild_ns_rel_knowledge_table() -> None:
    if not _has_table('insight_ns_rel_knowledge'):
        return

    existing = _existing_columns('insight_ns_rel_knowledge')
    expected = {
        'id',
        'insight_namespace_id',
        'insight_conversation_id',
        'knowledge_id',
        'is_deleted',
        'created_at',
    }
    if existing == expected:
        return

    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS insight_ns_rel_knowledge_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "insight_namespace_id INTEGER NOT NULL, "
            "insight_conversation_id INTEGER NOT NULL DEFAULT 0, "
            "knowledge_id INTEGER NOT NULL DEFAULT 0, "
            "is_deleted INTEGER NOT NULL DEFAULT 0, "
            "created_at DATETIME"
            ")"
        ))
        connection.execute(text(
            "INSERT INTO insight_ns_rel_knowledge_new ("
            "id, insight_namespace_id, insight_conversation_id, knowledge_id, is_deleted, created_at"
            ") "
            "SELECT "
            "id, "
            "COALESCE(insight_namespace_id, 0), "
            "COALESCE(insight_conversation_id, 0), "
            "COALESCE(knowledge_id, 0), "
            "COALESCE(is_deleted, 0), "
            "created_at "
            "FROM insight_ns_rel_knowledge"
        ))
        connection.execute(text("DROP TABLE insight_ns_rel_knowledge"))
        connection.execute(text("ALTER TABLE insight_ns_rel_knowledge_new RENAME TO insight_ns_rel_knowledge"))


def _rebuild_ns_execution_table() -> None:
    if not _has_table('insight_ns_execution'):
        return

    existing = _existing_columns('insight_ns_execution')
    expected = {
        'id',
        'conversation_id',
        'turn_id',
        'tool_call_id',
        'title',
        'description',
        'generated_code',
        'execution_status',
        'analysis_report',
        'result_payload_json',
        'stdout_text',
        'stderr_text',
        'execution_seconds',
        'error_message',
        'is_deleted',
        'created_at',
        'updated_at',
        'finished_at',
    }
    if existing == expected:
        return

    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS insight_ns_execution_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "conversation_id INTEGER NOT NULL, "
            "turn_id INTEGER NOT NULL, "
            "tool_call_id VARCHAR(128) NOT NULL DEFAULT '', "
            "title VARCHAR(255) NOT NULL DEFAULT '', "
            "description TEXT NOT NULL DEFAULT '', "
            "generated_code TEXT NOT NULL DEFAULT '', "
            "execution_status VARCHAR(32) NOT NULL DEFAULT 'running', "
            "analysis_report TEXT NOT NULL DEFAULT '', "
            "result_payload_json TEXT NOT NULL DEFAULT '{}', "
            "stdout_text TEXT NOT NULL DEFAULT '', "
            "stderr_text TEXT NOT NULL DEFAULT '', "
            "execution_seconds INTEGER NOT NULL DEFAULT 0, "
            "error_message TEXT NOT NULL DEFAULT '', "
            "is_deleted INTEGER NOT NULL DEFAULT 0, "
            "created_at DATETIME, "
            "updated_at DATETIME, "
            "finished_at DATETIME"
            ")"
        ))
        connection.execute(text(
            "INSERT INTO insight_ns_execution_new ("
            "id, conversation_id, turn_id, tool_call_id, title, description, generated_code, execution_status, "
            "analysis_report, result_payload_json, stdout_text, stderr_text, execution_seconds, error_message, "
            "is_deleted, created_at, updated_at, finished_at"
            ") "
            "SELECT "
            "id, conversation_id, turn_id, COALESCE(tool_call_id, ''), COALESCE(title, ''), COALESCE(description, ''), "
            "COALESCE(generated_code, ''), COALESCE(execution_status, 'running'), COALESCE(analysis_report, ''), "
            "COALESCE(result_payload_json, '{}'), COALESCE(stdout_text, ''), COALESCE(stderr_text, ''), "
            "COALESCE(execution_seconds, 0), COALESCE(error_message, ''), COALESCE(is_deleted, 0), "
            "created_at, updated_at, finished_at "
            "FROM insight_ns_execution"
        ))
        connection.execute(text("DROP TABLE insight_ns_execution"))
        connection.execute(text("ALTER TABLE insight_ns_execution_new RENAME TO insight_ns_execution"))


def _rebuild_ns_artifact_table() -> None:
    if not _has_table('insight_ns_artifact'):
        return

    existing = _existing_columns('insight_ns_artifact')
    expected = {
        'id',
        'conversation_id',
        'turn_id',
        'execution_id',
        'artifact_type',
        'title',
        'summary_text',
        'content_json',
        'metadata_json',
        'sort_no',
        'is_deleted',
        'created_at',
    }
    if existing == expected:
        return

    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS insight_ns_artifact_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "conversation_id INTEGER NOT NULL, "
            "turn_id INTEGER NOT NULL, "
            "execution_id INTEGER NOT NULL DEFAULT 0, "
            "artifact_type VARCHAR(32) NOT NULL, "
            "title VARCHAR(255) NOT NULL DEFAULT '', "
            "summary_text TEXT NOT NULL DEFAULT '', "
            "content_json TEXT NOT NULL DEFAULT '{}', "
            "metadata_json TEXT NOT NULL DEFAULT '{}', "
            "sort_no INTEGER NOT NULL DEFAULT 0, "
            "is_deleted INTEGER NOT NULL DEFAULT 0, "
            "created_at DATETIME"
            ")"
        ))
        connection.execute(text(
            "INSERT INTO insight_ns_artifact_new ("
            "id, conversation_id, turn_id, execution_id, artifact_type, title, summary_text, content_json, "
            "metadata_json, sort_no, is_deleted, created_at"
            ") "
            "SELECT "
            "id, conversation_id, turn_id, COALESCE(execution_id, 0), COALESCE(artifact_type, ''), COALESCE(title, ''), "
            "COALESCE(summary_text, ''), COALESCE(content_json, '{}'), COALESCE(metadata_json, '{}'), "
            "COALESCE(sort_no, 0), COALESCE(is_deleted, 0), created_at "
            "FROM insight_ns_artifact"
        ))
        connection.execute(text("DROP TABLE insight_ns_artifact"))
        connection.execute(text("ALTER TABLE insight_ns_artifact_new RENAME TO insight_ns_artifact"))


def _backfill_datasource_tags() -> None:
    if not _has_table('insight_ns_rel_datasource') or not _has_table('insight_datasource'):
        return

    rel_columns = _existing_columns('insight_ns_rel_datasource')
    datasource_columns = _existing_columns('insight_datasource')
    if 'knowledge_tag' not in rel_columns or 'knowledge_tag' not in datasource_columns:
        return

    with engine.begin() as connection:
        rows = connection.execute(text(
            "SELECT datasource_id, knowledge_tag "
            "FROM insight_ns_rel_datasource "
            "WHERE COALESCE(datasource_id, 0) > 0 AND COALESCE(knowledge_tag, '') != ''"
        )).mappings().all()
        for row in rows:
            connection.execute(text(
                "UPDATE insight_datasource "
                "SET knowledge_tag = :knowledge_tag "
                "WHERE id = :datasource_id AND COALESCE(knowledge_tag, '') = ''"
            ), {
                'knowledge_tag': row['knowledge_tag'],
                'datasource_id': row['datasource_id'],
            })


def _backfill_knowledge_tags() -> None:
    if not _has_table('insight_ns_rel_knowledge') or not _has_table('insight_knowledge'):
        return

    rel_columns = _existing_columns('insight_ns_rel_knowledge')
    knowledge_columns = _existing_columns('insight_knowledge')
    if 'knowledge_tag' not in rel_columns or 'knowledge_tag' not in knowledge_columns:
        return

    with engine.begin() as connection:
        rows = connection.execute(text(
            "SELECT knowledge_id, knowledge_tag "
            "FROM insight_ns_rel_knowledge "
            "WHERE COALESCE(knowledge_id, 0) > 0 AND COALESCE(knowledge_tag, '') != ''"
        )).mappings().all()
        for row in rows:
            connection.execute(text(
                "UPDATE insight_knowledge "
                "SET knowledge_tag = :knowledge_tag "
                "WHERE id = :knowledge_id AND COALESCE(knowledge_tag, '') = ''"
            ), {
                'knowledge_tag': row['knowledge_tag'],
                'knowledge_id': row['knowledge_id'],
            })


def _normalize_existing_datasource_types() -> None:
    if not _has_table('insight_datasource'):
        return

    with engine.begin() as connection:
        rows = connection.execute(text(
            "SELECT id, datasource_type FROM insight_datasource"
        )).mappings().all()
        for row in rows:
            normalized = _normalize_datasource_type(row.get('datasource_type'))
            if normalized == (row.get('datasource_type') or ''):
                continue
            connection.execute(text(
                "UPDATE insight_datasource "
                "SET datasource_type = :datasource_type "
                "WHERE id = :id"
            ), {
                'datasource_type': normalized,
                'id': row['id'],
            })


def _backfill_knowledge_relations() -> None:
    if not _has_table('insight_ns_rel_knowledge') or not _has_table('insight_knowledge'):
        return

    existing = _existing_columns('insight_ns_rel_knowledge')
    if 'knowledge_id' not in existing:
        return

    has_knowledge_name = 'knowledge_name' in existing
    has_file_id = 'file_id' in existing

    with engine.begin() as connection:
        select_fields = ['id', 'knowledge_id']
        if has_knowledge_name:
            select_fields.append('knowledge_name')
        if has_file_id:
            select_fields.append('file_id')
        rows = connection.execute(text(
            f"SELECT {', '.join(select_fields)} "
            "FROM insight_ns_rel_knowledge "
            "WHERE COALESCE(knowledge_id, 0) = 0"
        )).mappings().all()
        for row in rows:
            file_id = row.get('file_id') or ''
            knowledge_name = row.get('knowledge_name') or ''
            if not file_id and not knowledge_name:
                continue
            knowledge = connection.execute(text(
                "SELECT id FROM insight_knowledge "
                "WHERE file_id = :file_id AND COALESCE(is_deleted, 0) = 0 "
                "ORDER BY id ASC LIMIT 1"
            ), {'file_id': file_id}).mappings().first()
            if not knowledge:
                result = connection.execute(text(
                    "INSERT INTO insight_knowledge (knowledge_name, file_id, is_deleted, created_at) "
                    "VALUES (:knowledge_name, :file_id, 0, CURRENT_TIMESTAMP)"
                ), {
                    'knowledge_name': knowledge_name or file_id,
                    'file_id': file_id,
                })
                knowledge_id = result.lastrowid
            else:
                knowledge_id = knowledge['id']
            connection.execute(text(
                "UPDATE insight_ns_rel_knowledge "
                "SET knowledge_id = :knowledge_id "
                "WHERE id = :id"
            ), {
                'knowledge_id': knowledge_id,
                'id': row['id'],
            })


def _backfill_conversation_binding_ids(table_name: str) -> None:
    if not _has_table(table_name) or not _has_table('insight_ns_conversation'):
        return

    existing = _existing_columns(table_name)
    if 'insight_conversation_id' not in existing or 'insight_namespace_id' not in existing:
        return

    with engine.begin() as connection:
        rows = connection.execute(text(
            f"SELECT id, insight_namespace_id FROM {table_name} "
            "WHERE COALESCE(insight_conversation_id, 0) = 0"
        )).mappings().all()
        for row in rows:
            conversation = connection.execute(text(
                "SELECT id FROM insight_ns_conversation "
                "WHERE insight_namespace_id = :namespace_id AND COALESCE(is_deleted, 0) = 0 "
                "ORDER BY id ASC LIMIT 1"
            ), {'namespace_id': row['insight_namespace_id']}).mappings().first()
            if not conversation:
                continue
            connection.execute(text(
                f"UPDATE {table_name} "
                "SET insight_conversation_id = :conversation_id "
                "WHERE id = :id"
            ), {
                'conversation_id': conversation['id'],
                'id': row['id'],
            })


def _run_sqlite_schema_migrations() -> None:
    """
    Apply SQLite-only schema migrations used during active development.

    The migration path is intentionally conservative:
    - add missing columns
    - backfill required data
    - rebuild tables only when structure drift becomes too large
    """
    if db_type != 'sqlite':
        return

    _rename_table('insight_ns_context', 'insight_ns_message')
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX IF EXISTS idx_conversation_user_namespace"))
    _drop_column_if_exists('insight_ns_conversation', 'username')
    _drop_column_if_exists('insight_ns_message', 'username')

    _create_table_if_not_exists(
        "CREATE TABLE IF NOT EXISTS insight_datasource ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "insight_namespace_id INTEGER NOT NULL DEFAULT 0, "
        "datasource_type VARCHAR(32) NOT NULL DEFAULT 'unknown', "
        "datasource_name VARCHAR(128) NOT NULL DEFAULT '', "
        "knowledge_tag VARCHAR(128) NOT NULL DEFAULT '', "
        "uns_node_id VARCHAR(128) NOT NULL DEFAULT '', "
        "datasource_schema TEXT NOT NULL DEFAULT '', "
        "datasource_config_json TEXT NOT NULL DEFAULT '{}', "
        "is_deleted INTEGER NOT NULL DEFAULT 0, "
        "created_at DATETIME, "
        "updated_at DATETIME"
        ")"
    )

    _ensure_columns('insight_ns_rel_datasource', [
        "insight_conversation_id INTEGER NOT NULL DEFAULT 0",
        "datasource_id INTEGER NOT NULL DEFAULT 0",
        "sort_no INTEGER NOT NULL DEFAULT 0",
        "bind_source VARCHAR(32) NOT NULL DEFAULT 'user_selected'",
        "datasource_schema TEXT NOT NULL DEFAULT ''",
        "datasource_config_json TEXT NOT NULL DEFAULT '{}'",
        "is_active INTEGER NOT NULL DEFAULT 1",
        "is_deleted INTEGER NOT NULL DEFAULT 0",
        "updated_at DATETIME",
    ])

    _ensure_columns('insight_datasource', [
        "insight_namespace_id INTEGER NOT NULL DEFAULT 0",
        "knowledge_tag VARCHAR(128) NOT NULL DEFAULT ''",
        "uns_node_id VARCHAR(128) NOT NULL DEFAULT ''",
        "is_deleted INTEGER NOT NULL DEFAULT 0",
    ])

    _create_table_if_not_exists(
        "CREATE TABLE IF NOT EXISTS insight_ns_uns_selection ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "insight_namespace_id INTEGER NOT NULL, "
        "insight_conversation_id INTEGER NOT NULL DEFAULT 0, "
        "uns_node_id VARCHAR(128) NOT NULL DEFAULT '', "
        "uns_node_name VARCHAR(255) NOT NULL DEFAULT '', "
        "uns_node_path VARCHAR(1024) NOT NULL DEFAULT '', "
        "is_folder INTEGER NOT NULL DEFAULT 0, "
        "expanded_uns_node_ids_json TEXT NOT NULL DEFAULT '[]', "
        "is_deleted INTEGER NOT NULL DEFAULT 0, "
        "created_at DATETIME, "
        "updated_at DATETIME"
        ")"
    )
    _ensure_columns('insight_ns_uns_selection', [
        "uns_node_name VARCHAR(255) NOT NULL DEFAULT ''",
        "uns_node_path VARCHAR(1024) NOT NULL DEFAULT ''",
        "is_folder INTEGER NOT NULL DEFAULT 0",
        "expanded_uns_node_ids_json TEXT NOT NULL DEFAULT '[]'",
        "is_deleted INTEGER NOT NULL DEFAULT 0",
        "updated_at DATETIME",
    ])

    _ensure_columns('insight_ns_conversation', [
        f"title VARCHAR(255) NOT NULL DEFAULT '{DEFAULT_CONVERSATION_TITLE}'",
        "status VARCHAR(32) NOT NULL DEFAULT 'active'",
        "summary_text TEXT NOT NULL DEFAULT ''",
        "active_datasource_snapshot TEXT NOT NULL DEFAULT '{}'",
        "last_turn_no INTEGER NOT NULL DEFAULT 0",
        "is_deleted INTEGER NOT NULL DEFAULT 0",
        "last_message_at DATETIME",
        "updated_at DATETIME",
    ])

    _ensure_columns('insight_ns_turn', [
        "selected_datasource_ids_json TEXT NOT NULL DEFAULT '[]'",
        "selected_datasource_snapshot_json TEXT NOT NULL DEFAULT '[]'",
        "is_deleted INTEGER NOT NULL DEFAULT 0",
    ])

    _create_table_if_not_exists(
        "CREATE TABLE IF NOT EXISTS insight_ns_execution ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "conversation_id INTEGER NOT NULL, "
        "turn_id INTEGER NOT NULL, "
        "tool_call_id VARCHAR(128) NOT NULL DEFAULT '', "
        "title VARCHAR(255) NOT NULL DEFAULT '', "
        "description TEXT NOT NULL DEFAULT '', "
        "generated_code TEXT NOT NULL DEFAULT '', "
        "execution_status VARCHAR(32) NOT NULL DEFAULT 'running', "
        "analysis_report TEXT NOT NULL DEFAULT '', "
        "stdout_text TEXT NOT NULL DEFAULT '', "
        "stderr_text TEXT NOT NULL DEFAULT '', "
        "execution_seconds INTEGER NOT NULL DEFAULT 0, "
        "error_message TEXT NOT NULL DEFAULT '', "
        "is_deleted INTEGER NOT NULL DEFAULT 0, "
        "created_at DATETIME, "
        "updated_at DATETIME, "
        "finished_at DATETIME"
        ")"
    )
    _ensure_columns('insight_ns_execution', [
        "tool_call_id VARCHAR(128) NOT NULL DEFAULT ''",
        "title VARCHAR(255) NOT NULL DEFAULT ''",
        "description TEXT NOT NULL DEFAULT ''",
        "generated_code TEXT NOT NULL DEFAULT ''",
        "execution_status VARCHAR(32) NOT NULL DEFAULT 'running'",
        "analysis_report TEXT NOT NULL DEFAULT ''",
        "result_payload_json TEXT NOT NULL DEFAULT '{}'",
        "stdout_text TEXT NOT NULL DEFAULT ''",
        "stderr_text TEXT NOT NULL DEFAULT ''",
        "execution_seconds INTEGER NOT NULL DEFAULT 0",
        "error_message TEXT NOT NULL DEFAULT ''",
        "is_deleted INTEGER NOT NULL DEFAULT 0",
        "updated_at DATETIME",
        "finished_at DATETIME",
    ])

    _ensure_columns('insight_ns_message', [
        "turn_id INTEGER NOT NULL DEFAULT 0",
        "turn_no INTEGER NOT NULL DEFAULT 0",
        "seq_no INTEGER NOT NULL DEFAULT 0",
        "role VARCHAR(32) NOT NULL DEFAULT 'assistant'",
        "message_kind VARCHAR(32) NOT NULL DEFAULT 'final_answer'",
        "content_json TEXT NOT NULL DEFAULT ''",
        "tool_name VARCHAR(128) NOT NULL DEFAULT ''",
        "tool_call_id VARCHAR(128) NOT NULL DEFAULT ''",
        "is_deleted INTEGER NOT NULL DEFAULT 0",
    ])

    _rename_column('insight_user_collect', 'insight_context_id', 'insight_message_id')
    _ensure_columns('insight_user_collect', [
        "collect_type VARCHAR(32) NOT NULL DEFAULT 'message'",
        "target_id INTEGER NOT NULL DEFAULT 0",
        "title VARCHAR(255) NOT NULL DEFAULT ''",
        "summary_text TEXT NOT NULL DEFAULT ''",
        "insight_namespace_id INTEGER NOT NULL DEFAULT 0",
        "insight_conversation_id INTEGER NOT NULL DEFAULT 0",
        "insight_message_id INTEGER NOT NULL DEFAULT 0",
        "insight_artifact_id INTEGER NOT NULL DEFAULT 0",
        "metadata_json TEXT NOT NULL DEFAULT '{}'",
        "is_deleted INTEGER NOT NULL DEFAULT 0",
    ])

    _ensure_columns('insight_namespace', [
        "is_deleted INTEGER NOT NULL DEFAULT 0",
    ])
    _ensure_columns('insight_ns_rel_knowledge', [
        "insight_conversation_id INTEGER NOT NULL DEFAULT 0",
        "knowledge_id INTEGER NOT NULL DEFAULT 0",
        "is_deleted INTEGER NOT NULL DEFAULT 0",
    ])
    _ensure_columns('insight_ns_memory', [
        "is_deleted INTEGER NOT NULL DEFAULT 0",
    ])
    _ensure_columns('insight_ns_artifact', [
        "execution_id INTEGER NOT NULL DEFAULT 0",
        "content_json TEXT NOT NULL DEFAULT '{}'",
        "sort_no INTEGER NOT NULL DEFAULT 0",
        "is_deleted INTEGER NOT NULL DEFAULT 0",
    ])
    _ensure_columns('insight_knowledge', [
        "knowledge_tag VARCHAR(128) NOT NULL DEFAULT ''",
        "is_deleted INTEGER NOT NULL DEFAULT 0",
    ])

    _rebuild_insight_datasource_table()
    _normalize_existing_datasource_types()
    _backfill_datasource_split()
    _backfill_datasource_tags()
    _rebuild_ns_rel_datasource_table()
    _backfill_knowledge_relations()
    _backfill_knowledge_tags()
    _rebuild_ns_rel_knowledge_table()
    _rebuild_ns_execution_table()
    _rebuild_ns_artifact_table()
    # 当前设计中，`insight_conversation_id = 0` 是合法且必须保留的虚拟默认会话资源绑定。
    # 这里不再把 `0` 视为“待补全的缺失值”去回填到真实会话，避免破坏默认资源关系。

    with engine.begin() as connection:
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_conversation_namespace_status "
            "ON insight_ns_conversation (insight_namespace_id, status)"
        ))
        connection.execute(text(
            "DROP INDEX IF EXISTS idx_conversation_user_namespace"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_message_conversation_turn_seq "
            "ON insight_ns_message (insight_conversation_id, turn_no, seq_no)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_message_conversation_created "
            "ON insight_ns_message (insight_conversation_id, created_at)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_insight_datasource_namespace_name "
            "ON insight_datasource (insight_namespace_id, datasource_name)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_insight_datasource_namespace_type "
            "ON insight_datasource (insight_namespace_id, datasource_type)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_insight_datasource_uns_node "
            "ON insight_datasource (uns_node_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_ns_rel_datasource_conversation_datasource "
            "ON insight_ns_rel_datasource (insight_conversation_id, datasource_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_uns_selection_conversation_node "
            "ON insight_ns_uns_selection (insight_conversation_id, uns_node_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_rel_knowledge_conversation_knowledge "
            "ON insight_ns_rel_knowledge (insight_conversation_id, knowledge_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_execution_conversation_turn "
            "ON insight_ns_execution (conversation_id, turn_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_execution_turn_created "
            "ON insight_ns_execution (turn_id, created_at)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_artifact_conversation_turn "
            "ON insight_ns_artifact (conversation_id, turn_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_collect_user_type_target "
            "ON insight_user_collect (username, collect_type, target_id)"
        ))


def init_db():
    _run_sqlite_schema_migrations()
    Base.metadata.create_all(bind=engine)
    _run_sqlite_schema_migrations()
    Base.metadata.create_all(bind=engine)
