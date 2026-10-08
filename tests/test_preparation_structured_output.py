import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import agent.analysis_preparation as prep
from utils.llm_model_factory import LlmRuntimeConfig


def runtime(adapter='openai'):
    return LlmRuntimeConfig('supos_llm_gateway', 'test', 'http://localhost', 'unused', 0, adapter)


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.bound = None
        self.requests = []

    def bind_tools(self, schemas, *, tool_choice):
        self.bound = schemas, tool_choice
        return self

    def invoke(self, messages):
        self.requests.append(list(messages))
        return next(self.responses)


def call(args, identifier='c1'):
    return AIMessage(content='', tool_calls=[{
        'name': 'DatasourceSchemaEnrichmentDraft', 'args': args,
        'id': identifier, 'type': 'tool_call',
    }])


def invoke(monkeypatch, model, stage='business_context', adapter='openai'):
    monkeypatch.setattr(prep, 'create_data_insight_model', lambda _: model)
    return prep._invoke_contract(
        stage=stage, payload={}, output_model=prep.DatasourceSchemaEnrichmentDraft,
        runtime_config=runtime(adapter),
    )


def test_business_context_receives_forced_schema_and_literal_quotes(monkeypatch):
    model = Model([call({'field_semantics': [{
        'datasource_id': 180, 'field': 'status',
        'business_meaning': 'Values "running", "stopped"',
    }]})])
    result = invoke(monkeypatch, model)
    assert model.bound == ([prep.DatasourceSchemaEnrichmentDraft], 'DatasourceSchemaEnrichmentDraft')
    assert result.field_semantics[0].business_meaning == 'Values "running", "stopped"'
    assert len(model.requests) == 1


def test_invalid_arguments_retry_preserves_tool_call_pairing(monkeypatch):
    model = Model([call({'field_semantics': [{'datasource_id': 0, 'field': 'status'}]}), call({}, 'c2')])
    invoke(monkeypatch, model)
    retry = model.requests[1]
    assert isinstance(retry[-2], AIMessage)
    assert isinstance(retry[-1], ToolMessage)
    assert retry[-1].tool_call_id == 'c1'
    assert retry[-1].status == 'error'


def test_gateway_ignoring_tool_choice_keeps_same_validated_json_contract(monkeypatch):
    model = Model([AIMessage(content='{}')])
    result = invoke(monkeypatch, model)
    assert result.field_semantics == []
    assert len(model.requests) == 1


def test_gateway_text_fallback_does_not_accept_invalid_schema(monkeypatch):
    model = Model([AIMessage(content='{"unexpected": 1}'), AIMessage(content='{}')])
    invoke(monkeypatch, model)
    assert len(model.requests) == 2


def test_structured_schema_is_not_repeated_in_payload_or_mutated(monkeypatch):
    import json
    model = Model([call({})])
    monkeypatch.setattr(prep, 'create_data_insight_model', lambda _: model)
    payload = {'selected_datasources': [180], 'output_schema': {'type': 'object'}}
    prep._invoke_contract(
        stage='business_context', payload=payload,
        output_model=prep.DatasourceSchemaEnrichmentDraft, runtime_config=runtime(),
    )
    assert json.loads(model.requests[0][1].content) == {'selected_datasources': [180]}
    assert payload['output_schema'] == {'type': 'object'}


@pytest.mark.parametrize('stage,adapter', [('intent_analysis', 'openai'), ('business_context', 'qwen')])
def test_unrelated_stages_and_adapter_keep_existing_output_path(monkeypatch, stage, adapter):
    model = Model([AIMessage(content='```json\n{}\n```')])
    result = invoke(monkeypatch, model, stage, adapter)
    assert model.bound is None
    assert result.field_semantics == []
