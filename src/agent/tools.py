import time
from typing import Optional, List

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.config import get_stream_writer
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel, Field

from agent.context_engineering import CustomContext
from config.factory import beanFactory
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
    import pandas as pd
    import os

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    if file_path.endswith('.csv'):
        # noinspection PyArgumentList
        df = pd.read_csv(file_path)
    elif file_path.endswith(('.xlsx', '.xls')):
        df = pd.read_excel(file_path, sheet_name=sheet_name if sheet_name else 0)
    else:
        raise ValueError(f"不支持的文件格式: {file_path}")

    return df


def load_minio_file(bucket: str, object_name: str, sheet_name: Optional[str] = None):
    """
    加载 MinIO 远程数据文件，返回 pandas DataFrame。

    参数:
        bucket: MinIO 存储桶名称
        object_name: MinIO 对象名称（文件路径），如 'data/sales.xlsx'
        sheet_name: Excel 文件时指定的工作表名称，不传则默认读取第一个工作表

    返回:
        pandas.DataFrame: 加载的数据，可直接用于数据分析和可视化
    """
    import pandas as pd
    import io

    minio_client = beanFactory.get_bean('minio_client')
    response = minio_client.get_object(bucket, object_name)

    if object_name.endswith('.csv'):
        df = pd.read_csv(io.BytesIO(response.read()))
    elif object_name.endswith(('.xlsx', '.xls')):
        df = pd.read_excel(io.BytesIO(response.read()), sheet_name=sheet_name if sheet_name else 0)
    else:
        raise ValueError(f"不支持的文件格式: {object_name}")

    return df


def load_data_with_sql(sql: str, params: Optional[List] = None):
    """
    通过 SQL 查询数据库，返回 pandas DataFrame。

    参数:
        sql: 要执行的 SQL 查询语句，支持 SELECT 查询
        params: SQL 查询参数列表，用于参数化查询，防止 SQL 注入

    返回:
        pandas.DataFrame: 查询结果数据，可直接用于数据分析和可视化
    """
    import pandas as pd

    if not sql.strip().upper().startswith('SELECT'):
        raise ValueError("仅支持 SELECT 查询语句，不允许执行数据修改操作")

    db_session = beanFactory.get_bean('db_session')
    if params:
        df = pd.read_sql(sql, db_session.bind, params=params)
    else:
        df = pd.read_sql(sql, db_session.bind)

    return df


def load_data_with_api(endpoint: str, method: str = "GET", params: Optional[dict] = None,
                       headers: Optional[dict] = None, timeout: int = 30):
    """
    通过 HTTP API 获取数据，返回 pandas DataFrame。

    参数:
        endpoint: API 端点地址，如 'https://api.example.com/data'
        method: HTTP 请求方法，支持 GET、POST，默认为 GET
        params: 请求参数，GET 请求会拼接为 URL 参数，POST 请求会作为 JSON body 发送
        headers: HTTP 请求头，如 {'Authorization': 'Bearer xxx'}
        timeout: 请求超时时间，单位秒，默认 30 秒

    返回:
        pandas.DataFrame: 获取的数据，可直接用于数据分析和可视化
    """
    import pandas as pd
    import requests
    from io import StringIO

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


"""结构化输出"""


class StructuredResult(BaseModel):
    """洞察结果"""
    file_id: str = Field(description="图表文件ID")
    analysis_report: str = Field(description="分析报告内容")


class AnalysisResult(BaseModel):
    """分析代码执行结果"""
    chart_path: str = Field(description="图表文件完整路径")
    analysis_report: str = Field(description="分析报告内容")


# @tool(description='分析结果保存函数', args_schema=AnalysisResult)
def save_analysis_result(chart_path: str, analysis_report: str) -> StructuredResult:
    # 分析结果保存函数
    result = StructuredResult(file_id=chart_path, analysis_report=analysis_report)
    return result


class ExePythonCodeInput(BaseModel):
    code: str = Field(description="python代码")
    title: str = Field(description="python代码生成的分析报告内容主题摘要")
    description: str = Field(description="python代码生成的分析报告内容")


@tool(description='执行python代码工具', args_schema=ExePythonCodeInput)
def execute_python(runtime: ToolRuntime[CustomContext], code: str, title: str = '', description: str = "") -> Optional[
    ToolMessage]:
    writer = get_stream_writer()
    start_time = time.time()
    logger.info(f"▶ 开始执行：{title or 'Python代码执行'}")
    logger.info(f"▶ 代码：\n```python\n{code}\n```")
    logger.info("⚡ 代码已提交到本地执行器...")
    # 捕获输出
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
        }
        exec(code, namespace)
        # 从 namespace 中取出 result（LLM 生成的代码最后会赋值 result）
        exec_result = namespace.get('result')
    except Exception as e:
        # 执行报错 进行重试
        return ToolMessage(
            content=f"执行python代码工具错误：请检查重新生成。({str(e)})",
            tool_call_id=runtime.tool_call_id
        )

    stdout_result = sys_module.stdout.getvalue()
    stderr_result = sys_module.stderr.getvalue()

    sys_module.stdout = old_stdout
    sys_module.stderr = old_stderr

    execution_time = time.time() - start_time
    logger.info(f"✅ 执行完成，耗时 {execution_time:.2f}秒")

    if error_msg:
        logger.info(f"❌ 执行错误：\n{error_msg}")
        return None
    else:
        if stdout_result:
            logger.info(stdout_result)
        if stderr_result:
            logger.info(f"⚠️ 标准错误：\n{stderr_result}")

        # 如果代码执行后有 result 变量，构建返回结果
        if exec_result and isinstance(exec_result, StructuredResult):
            return ToolMessage(
                content=exec_result.model_dump_json(),
                tool_call_id=runtime.tool_call_id
            )
        return None
