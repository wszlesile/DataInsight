import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from io import StringIO
from typing import Any, Optional

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.config import get_stream_writer
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel, Field

from agent.context_engineering import CustomContext
from config.config import Config
from utils import logger

CURRENT_USERNAME = ContextVar('current_username', default='anonymous')
CURRENT_TEMP_DIR = ContextVar('current_temp_dir', default=Config.TEMP_DIR)


def load_local_file(file_path: str, sheet_name: Optional[str] = None):
    """
    加载本地 CSV 或 Excel 文件，并返回 pandas DataFrame。

    这个辅助函数属于 `sys_prompt.md` 中约定的 Python 执行工具契约。
    """
    import pandas as pd

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    if file_path.endswith('.csv'):
        return pd.read_csv(file_path)
    if file_path.endswith(('.xlsx', '.xls')):
        return pd.read_excel(file_path, sheet_name=sheet_name if sheet_name else 0)

    raise ValueError(f"不支持的文件格式: {file_path}")


def load_minio_file(bucket: str, object_name: str, sheet_name: Optional[str] = None):
    """预留给后续 MinIO 文件加载场景的辅助函数。"""
    pass


def load_data_with_sql(sql: str, params: Optional[list[Any]] = None):
    """通过 SQL 从当前配置的数据源中加载表格数据。"""
    import pandas as pd

    from config.database import engine

    with engine.connect() as connection:
        return pd.read_sql(sql, connection, params=params if params else None)


def load_data_with_api(
    endpoint: str,
    method: str = "GET",
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, Any]] = None,
    timeout: int = 30,
):
    """通过 HTTP API 加载表格数据，并返回 pandas DataFrame。"""
    from io import StringIO as CsvBuffer

    import pandas as pd
    import requests

    response = requests.request(
        method=method.upper(),
        url=endpoint,
        params=params if method.upper() == "GET" else None,
        json=params if method.upper() == "POST" else None,
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()

    content_type = response.headers.get('Content-Type', '')
    if 'application/json' in content_type:
        json_data = response.json()
        if isinstance(json_data, list):
            return pd.DataFrame(json_data)
        if isinstance(json_data, dict):
            if 'data' in json_data and isinstance(json_data['data'], list):
                return pd.DataFrame(json_data['data'])
            return pd.DataFrame([json_data])
        raise ValueError("不支持的 JSON 数据格式")

    if 'text/csv' in content_type or endpoint.endswith('.csv'):
        return pd.read_csv(CsvBuffer(response.text))

    raise ValueError(f"不支持的响应格式: {content_type}")


def generate_temp_file_name(prefix: str = 'analysis_chart', extension: str = 'html') -> str:
    """为图表或分析输出文件生成临时文件路径。"""
    safe_prefix = re.sub(r'[^0-9A-Za-z_-]+', '_', prefix).strip('_') or 'analysis_chart'
    safe_extension = extension.lstrip('.') or 'html'
    safe_username = re.sub(r'[^0-9A-Za-z_-]+', '_', CURRENT_USERNAME.get()).strip('_') or 'anonymous'
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    temp_dir = CURRENT_TEMP_DIR.get() or Config.TEMP_DIR
    filename = f"{safe_username}_{timestamp}_{safe_prefix}.{safe_extension}"
    return os.path.join(temp_dir, filename)


class StructuredResult(BaseModel):
    """当前 Agent 运行时消费的标准工具返回结构。"""

    file_id: str = Field(description="结果文件 ID")
    analysis_report: str = Field(description="分析报告内容")


class AnalysisResult(BaseModel):
    """为兼容现有代码结构而保留的中间结果结构。"""

    chart_path: str = Field(description="图表文件完整路径")
    analysis_report: str = Field(description="分析报告内容")


def save_analysis_result(chart_path: str, analysis_report: str) -> StructuredResult:
    """
    结束一次由生成代码驱动的分析任务，并返回标准结果。

    `sys_prompt.md` 当前要求大模型生成的 Python 代码必须调用这个函数，
    并把返回值赋值给 `result`。
    """
    return StructuredResult(file_id=chart_path, analysis_report=analysis_report)


class ExePythonCodeInput(BaseModel):
    code: str = Field(description="待执行的 Python 代码")
    title: str = Field(description="本次代码执行任务的简短标题")
    description: str = Field(description="本次代码执行任务的详细说明")


def _sanitize_tool_output(output: str) -> str:
    """从 stdout/stderr 中去掉流式事件碎片，只保留有意义的执行日志。"""
    if not output:
        return ''

    cleaned_lines: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("data: {") and '"type"' in stripped:
            continue
        if stripped.startswith("{'type':") and "'stage':" in stripped:
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def _create_execution_record(
    conversation_id: int,
    turn_id: int,
    tool_call_id: str,
    code: str,
    title: str,
    description: str,
) -> int:
    """在生成代码真正执行前，先创建一条执行记录。"""
    from config.database import SessionLocal
    from model import InsightNsExecution

    session = SessionLocal()
    try:
        execution = InsightNsExecution(
            conversation_id=conversation_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id or '',
            title=(title or '')[:255],
            description=description or '',
            generated_code=code or '',
            execution_status='running',
        )
        session.add(execution)
        session.commit()
        session.refresh(execution)
        return int(execution.id)
    finally:
        session.close()


def _update_execution_record(
    execution_id: int,
    *,
    execution_status: str,
    result_file_id: str = '',
    analysis_report: str = '',
    stdout_text: str = '',
    stderr_text: str = '',
    execution_seconds: int = 0,
    error_message: str = '',
) -> None:
    """在成功、失败或结果不合法后，回写执行记录。"""
    if execution_id <= 0:
        return

    from config.database import SessionLocal
    from model import InsightNsExecution

    session = SessionLocal()
    try:
        execution = session.query(InsightNsExecution).filter(
            InsightNsExecution.id == execution_id,
            InsightNsExecution.is_deleted == 0,
        ).first()
        if execution is None:
            return

        execution.execution_status = execution_status
        execution.result_file_id = result_file_id or ''
        execution.analysis_report = analysis_report or ''
        execution.stdout_text = stdout_text or ''
        execution.stderr_text = stderr_text or ''
        execution.execution_seconds = int(execution_seconds or 0)
        execution.error_message = error_message or ''
        execution.updated_at = datetime.now()
        execution.finished_at = datetime.now()
        session.commit()
    finally:
        session.close()


def _build_execution_namespace() -> dict[str, Any]:
    """向生成代码暴露允许使用的辅助函数命名空间。"""
    return {
        'load_local_file': load_local_file,
        'load_minio_file': load_minio_file,
        'load_data_with_sql': load_data_with_sql,
        'load_data_with_api': load_data_with_api,
        'generate_temp_file_name': generate_temp_file_name,
        'save_analysis_result': save_analysis_result,
    }


@contextmanager
def _capture_runtime_io(username: str):
    """临时接管 stdout/stderr，并绑定当前运行时上下文变量。"""
    import sys as sys_module

    old_stdout = sys_module.stdout
    old_stderr = sys_module.stderr
    sys_module.stdout = StringIO()
    sys_module.stderr = StringIO()
    username_token = CURRENT_USERNAME.set(username or 'anonymous')
    temp_dir_token = CURRENT_TEMP_DIR.set(Config.TEMP_DIR)

    try:
        yield sys_module.stdout, sys_module.stderr
    finally:
        sys_module.stdout = old_stdout
        sys_module.stderr = old_stderr
        CURRENT_USERNAME.reset(username_token)
        CURRENT_TEMP_DIR.reset(temp_dir_token)


def _get_stream_emitter():
    """返回一个尽力可用的 LangGraph 自定义流事件发送器。"""
    try:
        writer = get_stream_writer()
    except Exception:
        writer = None

    def emit(event_type: str, **payload):
        if writer:
            writer({'type': event_type, **payload})

    return emit


def _tool_error_message(message: str, tool_call_id: str) -> ToolMessage:
    """构造统一的工具错误消息，供 Agent 循环继续处理。"""
    return ToolMessage(content=message, tool_call_id=tool_call_id)


@tool(description='执行 Python 代码工具', args_schema=ExePythonCodeInput)
def execute_python(
    runtime: ToolRuntime[CustomContext],
    code: str,
    title: str = '',
    description: str = "",
) -> Optional[ToolMessage]:
    """
    执行模型生成的 Python 分析代码。

    需要注意：
    - 这里保持当前对外工具契约不变。
    - 执行历史仍会内部持久化到 `insight_ns_execution`。
    """
    emit = _get_stream_emitter()
    start_time = time.time()
    execution_id = _create_execution_record(
        conversation_id=int(getattr(runtime.context, 'conversation_id', 0) or 0),
        turn_id=int(getattr(runtime.context, 'turn_id', 0) or 0),
        tool_call_id=getattr(runtime, 'tool_call_id', '') or '',
        code=code,
        title=title,
        description=description,
    )

    task_title = title or '数据分析任务'
    emit(
        'status',
        stage='tool_start',
        level='info',
        tool='execute_python',
        message=f"正在执行分析代码：{task_title}",
    )
    logger.info(f"开始执行：{task_title}")
    logger.info(f"代码：\n```python\n{code}\n```")
    logger.info("代码已提交到本地执行器。")
    emit(
        'status',
        stage='tool_running',
        level='info',
        tool='execute_python',
        message='分析代码已提交到本地执行器。',
    )

    namespace = _build_execution_namespace()
    exec_result: Any = None
    with _capture_runtime_io(getattr(runtime.context, 'username', 'anonymous')) as (stdout_buffer, stderr_buffer):
        try:
            # 生成代码只在受控的辅助函数命名空间内执行。
            # 模型必须把最终结构化结果赋值给 `result`。
            exec(code, namespace)
            exec_result = namespace.get('result')
        except Exception as exc:
            error_message = str(exc)
            stdout_text = _sanitize_tool_output(stdout_buffer.getvalue())
            stderr_text = _sanitize_tool_output(stderr_buffer.getvalue())
            _update_execution_record(
                execution_id,
                execution_status='failed',
                stdout_text=stdout_text,
                stderr_text=stderr_text,
                execution_seconds=int((time.time() - start_time) * 1000),
                error_message=error_message,
            )
            emit(
                'status',
                stage='tool_error',
                level='error',
                tool='execute_python',
                message=f'代码执行失败：{error_message}',
            )
            return _tool_error_message(
                f"执行 Python 代码工具错误，请检查后重新生成：{error_message}",
                runtime.tool_call_id,
            )
        finally:
            stdout_text = _sanitize_tool_output(stdout_buffer.getvalue())
            stderr_text = _sanitize_tool_output(stderr_buffer.getvalue())

    execution_seconds = int((time.time() - start_time) * 1000)
    logger.info(f"执行完成，耗时 {execution_seconds / 1000:.2f} 秒")
    emit(
        'status',
        stage='tool_finished',
        level='success',
        tool='execute_python',
        message=f'代码执行完成，耗时 {execution_seconds / 1000:.2f} 秒',
    )

    if stdout_text:
        # stdout/stderr 既用于调试，也用于后续回看执行历史。
        logger.info(stdout_text)
        emit(
            'tool_log',
            stage='tool_output',
            level='info',
            tool='execute_python',
            message=stdout_text[:1000],
        )
    if stderr_text:
        logger.info(f"标准错误：\n{stderr_text}")
        emit(
            'tool_log',
            stage='tool_output',
            level='warning',
            tool='execute_python',
            message=stderr_text[:1000],
        )

    if isinstance(exec_result, StructuredResult):
        # 对外继续保持原有工具契约不变，同时在内部持久化更完整的执行记录。
        _update_execution_record(
            execution_id,
            execution_status='success',
            result_file_id=exec_result.file_id,
            analysis_report=exec_result.analysis_report,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            execution_seconds=execution_seconds,
        )
        emit(
            'status',
            stage='tool_result',
            level='success',
            tool='execute_python',
            message='分析结果已生成，正在整理最终报告。',
        )
        return ToolMessage(
            content=exec_result.model_dump_json(),
            tool_call_id=runtime.tool_call_id,
        )

    _update_execution_record(
        execution_id,
        execution_status='invalid',
        stdout_text=stdout_text,
        stderr_text=stderr_text,
        execution_seconds=execution_seconds,
        error_message='生成的代码未按模板产出 result',
    )
    # 如果生成代码没有产出 `result`，这里会要求模型重试，
    # 而不是静默接受一次不完整的执行。
    emit(
        'status',
        stage='tool_retry',
        level='warning',
        tool='execute_python',
        message='生成的代码未按模板返回 result，正在请求模型修正。',
    )
    return _tool_error_message(
        '请重新生成，没有按照代码输出模板严格生成执行代码。',
        runtime.tool_call_id,
    )
