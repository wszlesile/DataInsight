from langchain_core.messages import HumanMessage


def get_history_message():
    return HumanMessage("无历史对话记录")