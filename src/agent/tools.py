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


class LocalFileInput(BaseModel):
    """本地文件加载参数"""
    file_path: str = Field(description="本地文件完整路径，支持 .xlsx、.xls、.csv 格式")
    sheet_name: Optional[str] = Field(default=None, description="Excel 文件时指定的工作表名称，不传则默认读取第一个工作表")


@tool(description='加载本地数据文件，返回 pandas DataFrame。用于分析存储在本地磁盘的 Excel 或 CSV 文件。'
                '支持 .xlsx、.xls、.csv 格式。返回的 DataFrame 可直接用于数据分析和可视化。',
      args_schema=LocalFileInput)
def load_local_file(file_path: str, sheet_name: Optional[str] = None) -> dict:
    """加载本地数据文件（Excel、CSV）"""
    import pandas as pd
    import os

    if not os.path.exists(file_path):
        return {"error": f"文件不存在: {file_path}", "data": None}

    try:
        if file_path.endswith('.csv'):
            df = pd.read_csv(file_path)
        elif file_path.endswith(('.xlsx', '.xls')):
            df = pd.read_excel(file_path, sheet_name=sheet_name if sheet_name else 0)
        else:
            return {"error": f"不支持的文件格式: {file_path}", "data": None}

        return {
            "success": True,
            "data": df.to_dict(orient='records'),
            "columns": list(df.columns),
            "row_count": len(df),
            "dtypes": {col: str(dtype) for col, dtype in df.dtypes.items()}
        }
    except Exception as e:
        return {"error": f"读取文件失败: {str(e)}", "data": None}


class MinioFileInput(BaseModel):
    """MinIO 文件加载参数"""
    bucket: str = Field(description="MinIO 存储桶名称")
    object_name: str = Field(description="MinIO 对象名称（文件路径），如 'data/sales.xlsx'")
    sheet_name: Optional[str] = Field(default=None, description="Excel 文件时指定的工作表名称，不传则默认读取第一个工作表")


@tool(description='加载 MinIO 远程存储的数据文件，返回 pandas DataFrame。用于分析存储在 MinIO 对象存储中的 Excel 或 CSV 文件。'
                '需要提供存储桶名称和对象名称。返回的 DataFrame 可直接用于数据分析和可视化。',
      args_schema=MinioFileInput)
def load_minio_file(bucket: str, object_name: str, sheet_name: Optional[str] = None) -> dict:
    """加载 MinIO 远程数据文件"""
    import pandas as pd
    import io

    try:
        minio_client = beanFactory.get_bean('minio_client')
        response = minio_client.get_object(bucket, object_name)

        if object_name.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(response.read()))
        elif object_name.endswith(('.xlsx', '.xls')):
            df = pd.read_excel(io.BytesIO(response.read()), sheet_name=sheet_name if sheet_name else 0)
        else:
            return {"error": f"不支持的文件格式: {object_name}", "data": None}

        return {
            "success": True,
            "data": df.to_dict(orient='records'),
            "columns": list(df.columns),
            "row_count": len(df),
            "dtypes": {col: str(dtype) for col, dtype in df.dtypes.items()}
        }
    except Exception as e:
        return {"error": f"加载 MinIO 文件失败: {str(e)}", "data": None}


class SqlQueryInput(BaseModel):
    """SQL 查询参数"""
    sql: str = Field(description="要执行的 SQL 查询语句，支持 SELECT 查询")
    params: Optional[List] = Field(default=None, description="SQL 查询参数列表，用于参数化查询，防止 SQL 注入")


@tool(description='通过 SQL 查询数据库表，返回 pandas DataFrame。用于分析存储在 MySQL/PostgreSQL/SQLite 等关系型数据库中的数据。'
                '支持任意 SELECT 查询语句，可配合参数化查询确保安全。返回的 DataFrame 可直接用于数据分析和可视化。',
      args_schema=SqlQueryInput)
def load_data_with_sql(sql: str, params: Optional[List] = None) -> dict:
    """通过 SQL 查询数据库"""
    import pandas as pd

    if not sql.strip().upper().startswith('SELECT'):
        return {"error": "仅支持 SELECT 查询语句，不允许执行数据修改操作", "data": None}

    try:
        db_session = beanFactory.get_bean('db_session')
        if params:
            df = pd.read_sql(sql, db_session.bind, params=params)
        else:
            df = pd.read_sql(sql, db_session.bind)

        return {
            "success": True,
            "data": df.to_dict(orient='records'),
            "columns": list(df.columns),
            "row_count": len(df),
            "dtypes": {col: str(dtype) for col, dtype in df.dtypes.items()}
        }
    except Exception as e:
        return {"error": f"SQL 查询失败: {str(e)}", "data": None}


class ApiInput(BaseModel):
    """API 调用参数"""
    endpoint: str = Field(description="API 端点地址，如 'https://api.example.com/data'")
    method: str = Field(default="GET", description="HTTP 请求方法，支持 GET、POST")
    params: Optional[dict] = Field(default=None, description="请求参数，GET 请求会拼接为 URL 参数，POST 请求会作为 JSON body 发送")
    headers: Optional[dict] = Field(default=None, description="HTTP 请求头，如 {'Authorization': 'Bearer xxx'}")
    timeout: int = Field(default=30, description="请求超时时间，单位秒，默认 30 秒")


@tool(description='通过 HTTP API 获取数据，返回 pandas DataFrame。用于分析来自第三方 API 或内部微服务的数据。'
                '支持 GET/POST 请求，可自定义请求头和参数。返回的 DataFrame 可直接用于数据分析和可视化。',
      args_schema=ApiInput)
def load_data_with_api(endpoint: str, method: str = "GET", params: Optional[dict] = None,
                        headers: Optional[dict] = None, timeout: int = 30) -> dict:
    """通过 API 获取数据"""
    import pandas as pd
    import requests

    try:
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
                return {"error": "不支持的 JSON 数据格式", "data": None}
        elif 'text/csv' in content_type or endpoint.endswith('.csv'):
            from io import StringIO
            df = pd.read_csv(StringIO(response.text))
        else:
            return {"error": f"不支持的响应格式: {content_type}", "data": None}

        return {
            "success": True,
            "data": df.to_dict(orient='records'),
            "columns": list(df.columns),
            "row_count": len(df),
            "dtypes": {col: str(dtype) for col, dtype in df.dtypes.items()}
        }
    except requests.exceptions.Timeout:
        return {"error": f"API 请求超时（{timeout}秒）", "data": None}
    except requests.exceptions.RequestException as e:
        return {"error": f"API 请求失败: {str(e)}", "data": None}
    except Exception as e:
        return {"error": f"处理 API 响应失败: {str(e)}", "data": None}

"""结构化输出"""


class StructuredResult(BaseModel):
    """洞察结果"""
    file_id: str = Field(description="图表文件的完整路径")
    description: str = Field(description="分析报告内容")


@tool(description='分析结果保存函数', args_schema=StructuredResult)
def save_analysis_result(runtime: ToolRuntime[CustomContext], file_id: str, description: str):
    pass




@tool(description='获取本地存储的洞察数据文件路径')
def get_sight_datasource_message():
    return 'D:\\PycharmProjects\DataInsight\\xiaoshou.csv'


class ExePythonCodeInput(BaseModel):
    code: str = Field(description="python代码")
    title: str = Field(description="python代码的内容主题摘要")
    description: str = Field(description="python代码生成报告的详细介绍")


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
        exec(code, {})
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
        return None
