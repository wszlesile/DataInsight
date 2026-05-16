import os
from datetime import datetime
from pathlib import Path

from packaging.version import Version
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from config.config import Config
from config.database import engine
from utils import logger


MIGRATION_DIR = Path(__file__).resolve().parents[1] / "db_migrations"
VERSION_TABLE = "insight_schema_version"


def _version_value(value: str | None) -> Version:
    normalized = str(value or "1.0.0").strip() or "1.0.0"
    return Version(normalized)


def _ensure_version_table(connection) -> None:
    connection.execute(text(
        f"""
        CREATE TABLE IF NOT EXISTS {VERSION_TABLE} (
            id INTEGER PRIMARY KEY,
            version VARCHAR(32) NOT NULL,
            updated_at TIMESTAMP
        )
        """
    ))
    row = connection.execute(text(f"SELECT version FROM {VERSION_TABLE} WHERE id = 1")).first()
    if row is None:
        connection.execute(
            text(f"INSERT INTO {VERSION_TABLE} (id, version, updated_at) VALUES (1, :version, :updated_at)"),
            {"version": "1.0.0", "updated_at": datetime.now()},
        )


def _get_current_version(connection) -> str:
    row = connection.execute(text(f"SELECT version FROM {VERSION_TABLE} WHERE id = 1")).first()
    return str(row[0]) if row else "1.0.0"


def _update_current_version(connection, version: str) -> None:
    connection.execute(
        text(f"UPDATE {VERSION_TABLE} SET version = :version, updated_at = :updated_at WHERE id = 1"),
        {"version": version, "updated_at": datetime.now()},
    )


def _iter_migration_files(current_version: str, target_version: str) -> list[Path]:
    current = _version_value(current_version)
    target = _version_value(target_version)
    if target <= current or not MIGRATION_DIR.exists():
        return []
    files: list[Path] = []
    for item in MIGRATION_DIR.glob("*.sql"):
        try:
            version = _version_value(item.stem)
        except Exception:
            continue
        if current < version <= target:
            files.append(item)
    return sorted(files, key=lambda path: _version_value(path.stem))


def _split_sql_statements(sql: str) -> list[tuple[str | None, str]]:
    blocks: list[tuple[str | None, str]] = []
    current_directive: str | None = None
    current_lines: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("-- add_column:"):
            if current_lines:
                blocks.extend((current_directive, statement) for statement in _statements_from_lines(current_lines))
                current_lines = []
            current_directive = stripped.removeprefix("-- add_column:").strip()
            continue
        current_lines.append(line)
    if current_lines:
        blocks.extend((current_directive, statement) for statement in _statements_from_lines(current_lines))
    return blocks


def _statements_from_lines(lines: list[str]) -> list[str]:
    sql = "\n".join(lines).strip()
    if not sql:
        return []
    statements = [item.strip() for item in sql.split(";") if item.strip()]
    return statements


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = inspect(engine)
    try:
        return any(column.get("name") == column_name for column in inspector.get_columns(table_name))
    except SQLAlchemyError:
        return False


def _should_skip_statement(directive: str | None) -> bool:
    if not directive or "." not in directive:
        return False
    table_name, column_name = [part.strip() for part in directive.split(".", 1)]
    return bool(table_name and column_name and _has_column(table_name, column_name))


def _is_duplicate_column_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "duplicate column" in message or "already exists" in message


def _apply_migration_file(connection, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    for directive, statement in _split_sql_statements(sql):
        if _should_skip_statement(directive):
            continue
        try:
            connection.execute(text(statement))
        except SQLAlchemyError as exc:
            if directive and _is_duplicate_column_error(exc):
                continue
            raise


def run_schema_migrations() -> None:
    target_version = str(getattr(Config, "SCHEMA_VERSION", "1.0.0") or "1.0.0")
    if os.environ.get("DATA_INSIGHT_DISABLE_SCHEMA_MIGRATIONS", "").lower() == "true":
        logger.info("数据库结构升级已通过 DATA_INSIGHT_DISABLE_SCHEMA_MIGRATIONS 禁用")
        return

    with engine.begin() as connection:
        _ensure_version_table(connection)
        current_version = _get_current_version(connection)
        migration_files = _iter_migration_files(current_version, target_version)
        for migration_file in migration_files:
            logger.info(f"开始执行数据库结构升级脚本：{migration_file.name}")
            _apply_migration_file(connection, migration_file)
            _update_current_version(connection, migration_file.stem)
            logger.info(f"数据库结构升级完成：{migration_file.name}")
