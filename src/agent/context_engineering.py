from langchain_core.messages import HumanMessage
from pydantic import BaseModel


class CustomContext(BaseModel):
    """自定义上下文"""
    username: str
def get_history_message():
    return HumanMessage("无历史对话记录")