import time
from typing import Optional, List

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.config import get_stream_writer
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel, Field

from agent.context_engineering import CustomContext
from utils import logger


def load_local_file(file_path: str, sheet_name: Optional[str] = None):
    """
    加载本地数据文件（Excel、CSV），返回 pandas DataFrame。

    参数:
        file_path: 本地文件完整路径，支持 .xlsx、.xls、.csv 格式
        sheet_name: Excel 文件时指定的工作表名称，不传则默认读取第一个工作表

    返回:
        pandas.DataFrame: 加载的数据，可直接用于数据分析和可视化
    """
    import os

    import pandas as pd

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    if file_path.endswith('.csv'):
        df = pd.read_csv(file_path)
    elif file_path.endswith(('.xlsx', '.xls')):
        df = pd.read_excel(file_path, sheet_name=sheet_name if sheet_name else 0)
    else:
        raise ValueError(f"不支持的文件格式: {file_path}")

    return df


def load_minio_file(bucket: str, object_name: str, sheet_name: Optional[str] = None):
    """
    加载 MinIO 远程数据文件，返回 pandas DataFrame。
    """
    pass


def load_data_with_sql(sql: str, params: Optional[List] = None):
    """
    通过 SQL 查询数据库，返回 pandas DataFrame。
    """
    import pandas as pd

    from config.database import engine

    with engine.connect() as connection:
        df = pd.read_sql(sql, connection, params=params if params else None)
    return df


def load_data_with_api(endpoint: str, method: str = "GET", params: Optional[dict] = None,
                       headers: Optional[dict] = None, timeout: int = 30):
    """
    通过 HTTP API 获取数据，返回 pandas DataFrame。
    """
    from io import StringIO

    import pandas as pd
    import requests

    response = requests.request(
        method=method.upper(),
        url=endpoint,
        params=params if method.upper() == "GET" else None,
        json=params if method.upper() == "POST" else None,
        headers=headers,
        timeout=timeout
    )
    response.raise_for_status()

    content_type = response.headers.get('Content-Type', '')
    if 'application/json' in content_type:
        json_data = response.json()
        if isinstance(json_data, list):
            df = pd.DataFrame(json_data)
        elif isinstance(json_data, dict):
            if 'data' in json_data and isinstance(json_data['data'], list):
                df = pd.DataFrame(json_data['data'])
            else:
                df = pd.DataFrame([json_data])
        else:
            raise ValueError("不支持的 JSON 数据格式")
    elif 'text/csv' in content_type or endpoint.endswith('.csv'):
        df = pd.read_csv(StringIO(response.text))
    else:
        raise ValueError(f"不支持的响应格式: {content_type}")

    return df


class StructuredResult(BaseModel):
    """洞察结果"""
    file_id: str = Field(description="图表文件ID")
    analysis_report: str = Field(description="分析报告内容")


class AnalysisResult(BaseModel):
    """分析代码执行结果"""
    chart_path: str = Field(description="图表文件完整路径")
    analysis_report: str = Field(description="分析报告内容")


def save_analysis_result(chart_path: str, analysis_report: str) -> StructuredResult:
    """
    保存分析结果（必须调用此函数来结束分析）
    """
    result = StructuredResult(file_id=chart_path, analysis_report=analysis_report)
    return result


class ExePythonCodeInput(BaseModel):
    code: str = Field(description="python代码")
    title: str = Field(description="python代码生成的分析报告内容主题摘要")
    description: str = Field(description="python代码生成的分析报告内容")


def _sanitize_tool_output(output: str) -> str:
    if not output:
        return ''

    cleaned_lines = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("data: {") and '"type"' in stripped:
            continue
        if stripped.startswith("{'type':") and "'stage':" in stripped:
            continue
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


@tool(description='执行 python 代码工具', args_schema=ExePythonCodeInput)
def execute_python(runtime: ToolRuntime[CustomContext], code: str, title: str = '', description: str = "") -> Optional[
    ToolMessage]:
    try:
        writer = get_stream_writer()
    except Exception:
        writer = None

    def emit(event_type: str, **payload):
        if writer:
            writer({'type': event_type, **payload})

    start_time = time.time()
    emit(
        'status',
        stage='tool_start',
        level='info',
        tool='execute_python',
        message=f"正在执行分析代码：{title or '数据分析任务'}"
    )
    logger.info(f"开始执行：{title or 'Python代码执行'}")
    logger.info(f"代码：\n```python\n{code}\n```")
    logger.info("代码已提交到本地执行器...")
    emit(
        'status',
        stage='tool_running',
        level='info',
        tool='execute_python',
        message='分析代码已提交到本地执行器'
    )

    from io import StringIO
    import sys as sys_module

    old_stdout = sys_module.stdout
    old_stderr = sys_module.stderr
    sys_module.stdout = StringIO()
    sys_module.stderr = StringIO()

    error_msg = None
    try:
        namespace = {
            'load_local_file': load_local_file,
            'load_minio_file': load_minio_file,
            'load_data_with_sql': load_data_with_sql,
            'load_data_with_api': load_data_with_api,
            'save_analysis_result': save_analysis_result
        }
        exec(code, namespace)
        exec_result = namespace.get('result')
    except Exception as exc:
        error_msg = str(exc)
        emit(
            'status',
            stage='tool_error',
            level='error',
            tool='execute_python',
            message=f'代码执行失败：{error_msg}'
        )
        return ToolMessage(
            content=f"执行python代码工具错误：请检查重新生成。({error_msg})",
            tool_call_id=runtime.tool_call_id
        )
    finally:
        stdout_result = sys_module.stdout.getvalue()
        stderr_result = sys_module.stderr.getvalue()
        sys_module.stdout = old_stdout
        sys_module.stderr = old_stderr

    execution_time = time.time() - start_time
    logger.info(f"执行完成，耗时 {execution_time:.2f}秒")
    emit(
        'status',
        stage='tool_finished',
        level='success',
        tool='execute_python',
        message=f'代码执行完成，耗时 {execution_time:.2f} 秒'
    )

    stdout_result = _sanitize_tool_output(stdout_result)
    stderr_result = _sanitize_tool_output(stderr_result)

    if stdout_result:
        logger.info(stdout_result)
        emit(
            'tool_log',
            stage='tool_output',
            level='info',
            tool='execute_python',
            message=stdout_result[:1000]
        )
    if stderr_result:
        logger.info(f"标准错误：\n{stderr_result}")
        emit(
            'tool_log',
            stage='tool_output',
            level='warning',
            tool='execute_python',
            message=stderr_result[:1000]
        )

    if exec_result and isinstance(exec_result, StructuredResult):
        emit(
            'status',
            stage='tool_result',
            level='success',
            tool='execute_python',
            message='分析结果已生成，正在整理最终报告'
        )
        return ToolMessage(
            content=exec_result.model_dump_json(),
            tool_call_id=runtime.tool_call_id
        )

    emit(
        'status',
        stage='tool_retry',
        level='warning',
        tool='execute_python',
        message='生成的代码未按模板返回 result，正在请求模型修正'
    )
    return ToolMessage(
        content='请重新生成，没有按照代码输出模板严格生成执行代码',
        tool_call_id=runtime.tool_call_id
    )
