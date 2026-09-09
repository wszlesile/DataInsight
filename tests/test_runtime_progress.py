import json

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agent.context_engineering_runtime import CustomContext
from agent.runtime_progress import public_summary, tool_progress
from agent.runtime_working_context import RuntimeWorkingContextMiddleware


def test_tool_progress_uses_one_activity_for_start_and_completion():
    call = {
        'id': 'probe-1', 'name': 'probe_analysis_data',
        'args': {'progress_summary': '核实时间字段格式后再计算趋势。'},
    }
    started = tool_progress(call)
    completed = tool_progress(call, json.dumps({
        'status': 'observation', 'diagnostics': {'private': 'not displayed'},
    }))
    assert started['progress']['activity_id'] == completed['progress']['activity_id'] == 'tool:probe-1'
    assert started['progress']['state'] == 'running'
    assert completed['progress']['state'] == 'completed'
    assert '正式统计结果' in completed['message']
    assert 'private' not in completed['message']


def test_public_summary_rejects_code_credentials_and_private_reasoning():
    assert public_summary('核实设备状态与异常时间是否重合。')
    assert not public_summary('```python\nprint(1)\n```')
    assert not public_summary('Authorization: Bearer secret')
    assert not public_summary('<think>private chain</think>')


def test_middleware_emits_model_work_lifecycle_on_custom_stream():
    class OneShotModel(BaseChatModel):
        @property
        def _llm_type(self):
            return 'progress-test'

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content='done'))])

    graph = create_agent(
        model=OneShotModel(), tools=[], context_schema=CustomContext,
        middleware=[RuntimeWorkingContextMiddleware()],
    )
    events = list(graph.stream(
        {'messages': [HumanMessage('Analyze')]}, context=CustomContext(username='test'),
        stream_mode=['custom', 'updates'],
    ))
    custom = [payload for mode, payload in events if mode == 'custom']
    assert [event['progress']['state'] for event in custom] == ['running', 'completed']
    assert custom[0]['progress']['activity_id'] == custom[1]['progress']['activity_id']
    assert all(event['stage'] == 'work_progress' for event in custom)
