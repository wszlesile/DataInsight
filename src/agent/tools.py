import time
from typing import Optional

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.config import get_stream_writer
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel, Field

from agent.context_engineering import CustomContext
from config.factory import beanFactory
from utils import logger

"""结构化输出"""
class StructuredResult(BaseModel):
    """洞察结果"""
    file_id: str = Field(description="报表文件路径或文件服务的文件ID")
    description: str = Field(description="报表内容描述")

@tool(description='获取报表文件临时保存目录')
def get_file_temp_save_path():
    return 'D:\PycharmProjects\DataInsight'

@tool(description='保存报表文件以及描述',args_schema=StructuredResult)
def save_insight_result(runtime: ToolRuntime[CustomContext],file_id: str,description: str):

    pass

class ExePythonCodeInput(BaseModel):
    code:str=Field(description="python代码")
    title:str=Field(description="python代码的内容主题摘要")
    description:str=Field(description="python代码生成报告的详细介绍")

@tool(description='执行python代码工具', args_schema=ExePythonCodeInput)
def execute_python(runtime: ToolRuntime[CustomContext],code: str, title: str = '', description: str = "") -> Optional[ToolMessage]:
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
