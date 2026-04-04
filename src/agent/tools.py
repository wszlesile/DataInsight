import json
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, time as datetime_time, timedelta
from io import StringIO
from typing import Any, Optional
from zoneinfo import ZoneInfo

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


def get_day_range(days_ago: int = 0, timezone_name: str = 'Asia/Shanghai') -> tuple[datetime, datetime]:
    """
    返回某个自然日的起止时间。

    `days_ago=0` 表示今天，`1` 表示昨天，`2` 表示前天。
    返回值始终带时区信息，便于与带时区的时间列直接比较。
    """
    tz = ZoneInfo(timezone_name)
    target_date = (datetime.now(tz) - timedelta(days=days_ago)).date()
    start_at = datetime.combine(target_date, datetime_time.min, tzinfo=tz)
    end_at = datetime.combine(target_date, datetime_time.max, tzinfo=tz)
    return start_at, end_at


def build_markdown_table(dataframe, columns: Optional[list[str]] = None, max_rows: int = 10) -> str:
    """
    把 DataFrame 转成 Markdown 表格文本。

    这个辅助函数适合在“需要输出 Markdown 表格”的场景中直接复用，
    用来减少手写表格拼接时的引号、换行和格式错误。
    如果当前分析任务并不需要表格展示，则不必使用它。
    """
    import pandas as pd

    if dataframe is None:
        return ''

    if not isinstance(dataframe, pd.DataFrame):
        dataframe = pd.DataFrame(dataframe)

    if columns:
        available_columns = [column for column in columns if column in dataframe.columns]
        table_df = dataframe[available_columns].copy()
    else:
        table_df = dataframe.copy()

    if max_rows > 0:
        table_df = table_df.head(max_rows)

    if table_df.empty:
        return ''

    table_df = table_df.fillna('')

    def _stringify(value: Any) -> str:
        if isinstance(value, float):
            return f"{value:,.2f}".rstrip('0').rstrip('.')
        return str(value)

    headers = [str(column) for column in table_df.columns]
    header_line = "| " + " | ".join(headers) + " |"
    separator_line = "| " + " | ".join(['---'] * len(headers)) + " |"

    body_lines = []
    for _, row in table_df.iterrows():
        values = [_stringify(row[column]).replace('|', '&#124;') for column in table_df.columns]
        body_lines.append("| " + " | ".join(values) + " |")

    return "\n".join([header_line, separator_line, *body_lines])


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
    report_text = (analysis_report or '').strip()
    if not report_text:
        raise ValueError('analysis_report 不能为空，必须传入完整的 Markdown 分析报告。')
    return StructuredResult(file_id=chart_path, analysis_report=report_text)


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
        'get_day_range': get_day_range,
        'build_markdown_table': build_markdown_table,
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


def _classify_execution_error(error: Exception | None, error_message: str) -> str:
    """把执行失败归类成更容易被模型理解的错误类型。"""
    if isinstance(error, SyntaxError):
        return 'syntax_error'
    if isinstance(error, ModuleNotFoundError):
        return 'missing_dependency'
    if isinstance(error, FileNotFoundError):
        return 'data_source_not_found'
    if isinstance(error, KeyError):
        return 'schema_or_column_mismatch'
    if isinstance(error, NameError):
        return 'undefined_name'

    lowered = (error_message or '').lower()
    if 'invalid comparison' in lowered or ('datetime' in lowered and 'timestamp' in lowered):
        return 'data_type_or_time_mismatch'
    if 'not in index' in lowered or 'no such column' in lowered:
        return 'schema_or_column_mismatch'
    if 'save_analysis_result' in lowered or 'analysis_report' in lowered or 'result' in lowered:
        return 'result_contract_error'
    return 'runtime_error'


def _build_repair_instructions(error_type: str) -> list[str]:
    """根据错误类型生成定向修复建议。"""
    common_instructions = [
        '重新生成完整可执行代码，不要只修改局部片段。',
        '保留 save_analysis_result(...) 并把返回值赋给 result。',
        '如果当前修正思路与上一次相同且未解决错误，请更换实现方式后再重试。',
    ]

    specific_instructions = {
        'syntax_error': [
            '先检查引号、括号、缩进和多行字符串是否闭合，再重新生成代码。',
            '构造分析报告时优先使用 report_sections/report_lines 加 "\\n\\n".join(...)，避免手写长字符串拼接。',
        ],
        'missing_dependency': [
            '不要依赖执行环境中未注入或未安装的第三方模块。',
            '优先使用当前工具已提供的内置辅助函数，而不是额外 import 新库。',
        ],
        'data_source_not_found': [
            '检查数据源标识、文件路径、表名或接口地址是否与当前数据源上下文一致。',
            '不要凭空假设新的文件路径或表名。',
        ],
        'schema_or_column_mismatch': [
            '字段选择必须以 metadata_schema.properties 中提供的真实字段名为准。',
            '重新检查 DataFrame 列名后再生成过滤、聚合和展示逻辑。',
        ],
        'undefined_name': [
            '重新检查变量名、函数名和 import 是否一致，避免引用未定义对象。',
            '尽量复用前面已经定义好的中间变量，不要混用多个命名。',
        ],
        'data_type_or_time_mismatch': [
            '先统一时间列或比较字段的数据类型，再进行过滤或比较。',
            '遇到相对日期或时区处理时，优先考虑使用 get_day_range() 并保证两侧都是可比较的带时区时间对象。',
        ],
        'result_contract_error': [
            '最终必须产出完整 analysis_report，并调用 save_analysis_result(chart_path, analysis_report)。',
            '不要遗漏 result 变量，也不要返回空的 analysis_report。',
        ],
        'runtime_error': [
            '根据 error_message 直接修正导致失败的那一步，再重新生成完整代码。',
            '如果错误发生在数据处理阶段，请先验证中间结果再进入图表和报告生成。',
        ],
    }
    return [*specific_instructions.get(error_type, specific_instructions['runtime_error']), *common_instructions]


def _tool_error_message(
    *,
    tool_call_id: str,
    error_type: str,
    error_message: str,
    repair_instructions: list[str],
    stdout_text: str = '',
    stderr_text: str = '',
) -> ToolMessage:
    """构造统一的结构化工具错误反馈，供 Agent 循环继续修正。"""
    payload = {
        'tool': 'execute_python',
        'status': 'failed',
        'error_type': error_type,
        'error_message': error_message,
        'repair_instructions': repair_instructions,
    }
    if stdout_text:
        payload['stdout_text'] = stdout_text[:1000]
    if stderr_text:
        payload['stderr_text'] = stderr_text[:1000]

    return ToolMessage(
        content=json.dumps(payload, ensure_ascii=False),
        tool_call_id=tool_call_id,
    )


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
            error_type = _classify_execution_error(exc, error_message)
            repair_instructions = _build_repair_instructions(error_type)
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
                message=f'代码执行失败（{error_type}）：{error_message}',
            )
            return _tool_error_message(
                tool_call_id=runtime.tool_call_id,
                error_type=error_type,
                error_message=error_message,
                repair_instructions=repair_instructions,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
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
        tool_call_id=runtime.tool_call_id,
        error_type='result_contract_error',
        error_message='生成的代码未按模板产出 result',
        repair_instructions=_build_repair_instructions('result_contract_error'),
        stdout_text=stdout_text,
        stderr_text=stderr_text,
    )
