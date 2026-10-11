import sys
import unittest
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from service.conversation_context_service import ConversationContextService
from utils.context_compression import clip_text, summarize_turns_incrementally


def test_history_compression_reuses_only_identical_inputs_and_configuration(monkeypatch):
    from config import Config
    from utils.context_compression import HistoryCompressionCache
    import service.conversation_context_service as context_module
    monkeypatch.setattr(context_module, 'history_compression_cache', HistoryCompressionCache())
    service = ConversationContextService(MagicMock())
    turns = [SimpleNamespace(id=i, turn_no=i) for i in (1, 2, 3)]
    service.session.query.return_value.filter.return_value.order_by.return_value.all.return_value = turns
    payloads = [{'turn_id': 1, 'turn_no': 1, 'question': 'Question',
                 'answer': 'Observed answer', 'used_datasource_ids': [7]}]
    service._build_turn_summary_payloads = MagicMock(side_effect=lambda *args: copy.deepcopy(payloads))
    service._build_summary_text = MagicMock(return_value='Current recent answer')
    service.get_memory = MagicMock(return_value=None)
    calls = []
    def summarize(text):
        calls.append(text)
        return f'Summary {len(calls)}'
    with patch.multiple(Config, CONTEXT_COMPRESSION_ENABLED=True,
                        CONTEXT_COMPRESSION_TRIGGER_TURNS=2, CONTEXT_COMPRESSION_KEEP_RECENT_TURNS=2,
                        CONTEXT_COMPRESSION_MAX_SUMMARY_CHARS=1000, CONTEXT_COMPRESSION_USE_LLM=True), \
            patch('service.conversation_context_service.build_llm_turn_summarizer', return_value=summarize) as factory:
        metadata = service.get_context_compression_metadata(1)
        assert metadata['state'] == 'pending'
        assert not calls
        factory.assert_not_called()
        first = service._build_rolling_summary_payload(1)
        assert len(calls) == 1
        assert service.get_context_compression_metadata(1)['state'] == 'ready'
        assert len(calls) == 1
        service.get_memory.return_value = SimpleNamespace(content_json=json.dumps(first))
        service._build_summary_text.return_value = 'Refreshed recent answer'
        reused = service._build_rolling_summary_payload(1)
        assert len(calls) == 1
        assert factory.call_count == 1
        assert reused['compressed_summary_text'] == first['compressed_summary_text']
        assert 'Refreshed recent answer' in reused['summary_text']
        for field, value in [('answer', 'Changed historical answer'),
                             ('used_datasource_ids', [8]), ('turn_id', 9)]:
            payloads[0][field] = value
            refreshed = service._build_rolling_summary_payload(1)
            assert refreshed['compressed_summary_text'] != first['compressed_summary_text']
        assert len(calls) == 4
        payloads[:] = first['compressed_turn_summaries']
        with patch.object(Config, 'CONTEXT_COMPRESSION_MAX_SUMMARY_CHARS', 800):
            service._build_rolling_summary_payload(1)
        assert len(calls) == 5
        monkeypatch.setattr(context_module, 'history_compression_cache', HistoryCompressionCache())
        service.get_memory.return_value = SimpleNamespace(content_json='{}')
        service._build_rolling_summary_payload(1)
        assert len(calls) == 6
        # A historical rerun has a different prefix. Even an empty prefix
        # must replace the full-history summary rather than reuse it.
        service.get_memory.return_value = SimpleNamespace(content_json=json.dumps(first))
        payloads.clear()
        bounded = service._build_rolling_summary_payload(1)
        assert bounded['compressed_summary_text'] == ''
        assert len(calls) == 6


def test_failed_history_compression_is_not_reused_when_the_model_recovers(monkeypatch):
    from config import Config
    from utils.context_compression import HistoryCompressionCache
    import service.conversation_context_service as context_module
    monkeypatch.setattr(context_module, 'history_compression_cache', HistoryCompressionCache())
    service = ConversationContextService(MagicMock())
    turns = [SimpleNamespace(id=i, turn_no=i) for i in (1, 2, 3)]
    service.session.query.return_value.filter.return_value.order_by.return_value.all.return_value = turns
    service._build_turn_summary_payloads = MagicMock(return_value=[
        {'turn_id': 1, 'turn_no': 1, 'question': 'Question', 'answer': 'Answer'}])
    service._build_summary_text = MagicMock(return_value='Recent')
    service.get_memory = MagicMock(return_value=None)
    model = MagicMock(side_effect=[RuntimeError('unavailable'), 'Recovered model summary'])
    with patch.multiple(Config, CONTEXT_COMPRESSION_ENABLED=True,
                        CONTEXT_COMPRESSION_TRIGGER_TURNS=2, CONTEXT_COMPRESSION_KEEP_RECENT_TURNS=2,
                        CONTEXT_COMPRESSION_MAX_SUMMARY_CHARS=1000, CONTEXT_COMPRESSION_USE_LLM=True), \
            patch('service.conversation_context_service.build_llm_turn_summarizer', return_value=model):
        failed = service._build_rolling_summary_payload(1)
        assert failed['compression_reusable'] is False
        service.get_memory.return_value = SimpleNamespace(content_json=json.dumps(failed))
        recovered = service._build_rolling_summary_payload(1)
        assert recovered['compressed_summary_text'] == 'Recovered model summary'
        assert recovered['compression_reusable'] is True
        assert model.call_count == 2
        # A new reader/session with no durable full-history match still reuses
        # this exact successful historical prefix, without another model call.
        service.get_memory.return_value = None
        assert service._build_rolling_summary_payload(1)['compressed_summary_text'] == 'Recovered model summary'
        assert model.call_count == 2


def test_history_compression_cache_has_a_bounded_recent_input_lifecycle():
    from utils.context_compression import HistoryCompressionCache
    cache = HistoryCompressionCache(max_entries=2)
    cache.put('a', 'A')
    cache.put('b', 'B')
    assert cache.get('a') == 'A'
    cache.put('c', 'C')
    assert cache.get('b') is None
    assert cache.get('a') == 'A'
    assert cache.get('c') == 'C'


class ContextCompressionUtilsTestCase(unittest.TestCase):
    def test_clip_text_preserves_head_and_tail(self) -> None:
        text = "abcdefghijklmnopqrstuvwxyz0123456789"

        clipped = clip_text(text, 28)

        self.assertTrue(clipped.startswith("abcdefgh"))
        self.assertTrue(clipped.endswith("9"))
        self.assertIn("...[omitted]...", clipped)

    def test_summarize_turns_incrementally_uses_custom_summarizer(self) -> None:
        summary = summarize_turns_incrementally(
            turn_payloads=[
                {"turn_no": 1, "question": "看整体趋势", "answer": "产量上涨"},
                {"turn_no": 2, "question": "看异常点", "answer": "晚班波动最大"},
            ],
            summarizer=lambda text: f"压缩后::{len(text)}",
            max_chars=100,
        )

        self.assertTrue(summary.startswith("压缩后::"))


class ConversationContextCompressionTestCase(unittest.TestCase):
    def test_build_execution_summary_item_applies_length_limits(self) -> None:
        service = ConversationContextService.__new__(ConversationContextService)
        execution = SimpleNamespace(
            id=1,
            turn_id=2,
            tool_call_id="tool-1",
            title="执行标题",
            description="执行描述",
            execution_status="success",
            analysis_report="A" * 1600,
            generated_code="print('x')" * 600,
            error_message="",
            execution_seconds=123,
            finished_at=None,
            result_payload_json='{"charts":[{}],"tables":[{}]}',
        )

        payload = service._build_execution_summary_item(execution, include_code=True)

        self.assertLessEqual(len(payload["analysis_report_preview"]), 800)
        self.assertLessEqual(len(payload["generated_code"]), 3000)
        self.assertEqual(payload["chart_count"], 1)
        self.assertEqual(payload["table_count"], 1)

    def test_build_artifact_summary_item_applies_length_limit(self) -> None:
        service = ConversationContextService.__new__(ConversationContextService)
        artifact = SimpleNamespace(
            id=1,
            turn_id=2,
            execution_id=3,
            artifact_type="chart",
            title="图表",
            summary_text="B" * 1200,
            sort_no=1,
            created_at=None,
            content_json='{"chart_type":"line","chart_spec":{"series":[1,2]}}',
        )

        payload = service._build_artifact_summary_item(artifact)

        self.assertLessEqual(len(payload["summary_text"]), 500)
        self.assertEqual(payload["chart_type"], "line")
        self.assertEqual(payload["chart_series_count"], 2)


if __name__ == "__main__":
    unittest.main()
