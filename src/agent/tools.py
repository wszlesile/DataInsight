import time

from langchain_core.tools import tool
from pydantic import BaseModel, Field


@tool(description='获取报表文件临时保存目录')
def get_file_temp_save_path():
    return 'D:\PycharmProjects\DataInsight'

@tool(description='保存报告文件的描述')
def save_tile_description(description: str):
    print(description)

class ExecutePythonInput(BaseModel):
    code:str=Field(description="python代码")
    title:str=Field(description="python代码的内容主题摘要")
    description:str=Field(description="python代码生成报告的详细介绍")

@tool(description='执行python代码工具', args_schema=ExecutePythonInput)
def execute_python(code: str, title: str = '', description: str = "") -> None:
    start_time = time.time()
    print(f"▶ 开始执行：{title or 'Python代码执行'}")
    print(f"▶ 代码：\n```python\n{code}\n```")
    print("⚡ 代码已提交到本地执行器...")

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
        error_msg = str(e)

    stdout_result = sys_module.stdout.getvalue()
    stderr_result = sys_module.stderr.getvalue()

    sys_module.stdout = old_stdout
    sys_module.stderr = old_stderr

    execution_time = time.time() - start_time
    print(f"✅ 执行完成，耗时 {execution_time:.2f}秒")

    if error_msg:
        print(f"❌ 执行错误：\n{error_msg}")
    else:
        if stdout_result:
            print(stdout_result)
        if stderr_result:
            print(f"⚠️ 标准错误：\n{stderr_result}")
