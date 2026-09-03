import sys
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent.invoker import (  # noqa: E402
    _merge_agent_round_messages,
    _new_agent_round_messages,
    _runtime_message_stats,
    _runtime_model_response_stats,
)


def test_runtime_message_stats_reports_volume_without_message_content():
    messages = [
        SystemMessage(content="system contract"),
        HumanMessage(content="question"),
        AIMessage(content="analysis", tool_calls=[{
            "name": "execute_python",
            "args": {"code": "result = 1"},
            "id": "call-1",
            "type": "tool_call",
        }]),
        ToolMessage(content="ok", tool_call_id="call-1"),
    ]

    stats = _runtime_message_stats(messages)

    assert stats["message_count"] == 4
    assert stats["tool_call_count"] == 1
    assert stats["content_chars"] == len("system contractquestionanalysisok")
    assert stats["estimated_tokens"] > 0
    assert "system contract" not in str(stats)


def test_runtime_model_response_stats_detects_truncation_and_avoids_double_usage():
    message = AIMessage(
        content="partial",
        response_metadata={
            "finish_reason": "length",
            "token_usage": {
                "prompt_tokens": 101,
                "completion_tokens": 51,
                "total_tokens": 152,
            },
        },
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
        },
    )

    stats = _runtime_model_response_stats([message])

    assert stats["finish_reasons"] == ["length"]
    assert stats["truncated"] is True
    assert stats["usage"] == {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
    }


def test_new_agent_round_messages_excludes_returned_input_transcript():
    system = SystemMessage(content="system", id="system-1")
    human = HumanMessage(content="question", id="human-1")
    answer = AIMessage(content="answer", id="answer-1")

    current = _new_agent_round_messages(
        [system, human],
        [system, human, answer],
    )

    assert current == [answer]


def test_merge_agent_round_messages_preserves_tool_feedback_for_next_repair():
    system = SystemMessage(content="system", id="system-1")
    human = HumanMessage(content="question", id="human-1")
    tool_call = AIMessage(
        content="",
        id="answer-1",
        tool_calls=[{
            "name": "execute_python",
            "args": {"code": "result = 1"},
            "id": "call-1",
            "type": "tool_call",
        }],
    )
    feedback = ToolMessage(
        content='{"error_type":"contract_error","diagnostics":{"missing":["tables"]}}',
        tool_call_id="call-1",
        id="tool-1",
    )

    merged = _merge_agent_round_messages(
        [system, human],
        [system, human, tool_call, feedback],
    )
    repeated = _merge_agent_round_messages(merged, [tool_call, feedback])

    assert merged == [system, human, tool_call, feedback]
    assert repeated == merged
    assert repeated[-1].content == feedback.content
