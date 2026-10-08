import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import invoker
from agent.analysis_preparation import (
    _build_runtime_prompt_snapshot,
    BusinessContextContract,
    BusinessFactCoverageContract,
    BusinessFactRequirementContract,
    BusinessQuestionContract,
    BusinessQuestionCoverageContract,
    DatasourceSelectionContract,
    IntentContract,
    PreparationResult,
)
from agent.context_engineering_runtime import (
    CustomContext,
    build_runtime_datasource_working_message,
)
from agent.runtime_workspace import RuntimeDatasourceContextInput, read_runtime_datasource_context
from langgraph.prebuilt import ToolRuntime


def _working_payload(message):
    content = message.content
    start = content.index("{", content.index("<runtime-context-instructions>"))
    end = content.rindex("}") + 1
    return json.loads(content[start:end])


def test_schema_reader_input_decodes_gateway_serialized_container_arguments():
    payload = RuntimeDatasourceContextInput(
        datasource_ids='[11, 12]',
        field_names_by_datasource='{"11":["amount"],"12":["status"]}',
    )

    assert payload.datasource_ids == [11, 12]
    assert payload.field_names_by_datasource == {"11": ["amount"], "12": ["status"]}


def test_schema_reader_accepts_a_bounded_multi_source_batch():
    """A normal multi-source plan must not fail on an artificial four-source cap."""
    payload = RuntimeDatasourceContextInput(
        datasource_ids=[11, 12, 13, 14, 15, 16],
        field_names_by_datasource={str(source_id): ['business_key'] for source_id in range(11, 17)},
    )

    assert payload.datasource_ids == [11, 12, 13, 14, 15, 16]


def test_schema_reader_accepts_multi_source_full_schema_reads_when_fields_are_omitted():
    payload = RuntimeDatasourceContextInput(
        datasource_ids=[11, 12],
        include_relationships=True,
    )

    assert payload.datasource_ids == [11, 12]
    assert payload.field_names_by_datasource == {}
    assert payload.include_relationships is True


def test_relationship_read_uses_schema_without_parallel_business_context():
    import copy
    relationship = {'relationship_id': 'r1', 'target_datasource_id': 12,
                    'aggregation_safety': {'left': {'status': 'conditional'}}}
    context = CustomContext(username='test', active_datasource_snapshot={},
                            execution_datasource_snapshot={'selected_datasource_snapshot': [
                                {'datasource_id': 11, 'metadata_schema': {
                                    'properties': {'amount': {'type': 'number'}},
                                    'relationships': [relationship],
                                }},
                            ]})
    saved = copy.deepcopy(context.active_datasource_snapshot)
    args = dict(runtime=SimpleNamespace(context=context), datasource_ids=[11],
                field_names_by_datasource={'11': ['amount']})
    assert 'business_context' not in read_runtime_datasource_context.func(**args)
    detail = read_runtime_datasource_context.func(**args, include_relationships=True)
    assert detail['datasources'][0]['metadata_schema']['relationships'] == [relationship]
    assert 'business_context' not in detail
    assert context.active_datasource_snapshot == saved
    invalid = read_runtime_datasource_context.func(
        runtime=SimpleNamespace(context=context), datasource_ids=[99], include_relationships=True,
    )
    assert 'business_context' not in invalid


def _preparation() -> PreparationResult:
    snapshot = {
        "selected_datasource_ids": [11],
        "selected_datasource_snapshot": [{
            "datasource_id": 11,
            "datasource_name": "contract_fact",
            "datasource_identifier": "contract_fact",
            "datasource_type": "table",
            "metadata_schema": {
                "description": "合同事实表。" + "说明" * 2000,
                "properties": {
                    "contract_id": {
                        "type": "string",
                        "description": "合同的唯一业务标识。" + "详情" * 1000,
                    },
                    "amount": {
                        "type": "number",
                        "description": "合同含税金额。" + "详情" * 1000,
                    },
                },
            },
        }],
    }
    return PreparationResult(
        intent=IntentContract(
            business_goal="分析合同金额",
            business_subjects=["合同"],
            business_questions=[BusinessQuestionContract(
                question_id="q1",
                business_question="合同金额是多少",
                subject_scope="question_defined_scope",
                required_business_facts=[BusinessFactRequirementContract(
                    fact_id="f1",
                    business_fact="合同金额",
                    semantic_definition="合同金额的汇总",
                )],
            )],
        ),
        selection=DatasourceSelectionContract(
            selected_datasource_ids=[11],
            question_coverage=[BusinessQuestionCoverageContract(
                question_id="q1",
                datasource_ids=[11],
                fact_coverage=[BusinessFactCoverageContract(
                    fact_id="f1",
                    coverage_status="direct",
                    datasource_ids=[11],
                    coverage_reason="直接字段",
                )],
                coverage_reason="直接字段",
            )],
            selection_reason="合同事实",
        ),
        business_context=BusinessContextContract(),
        selected_snapshot=snapshot,
        query_capabilities=("sql",),
    )


def test_runtime_workspace_keeps_scope_and_reduces_unbound_schema_details():
    preparation = _preparation()
    full = preparation.prompt_snapshot()
    workspace = preparation.runtime_prompt_snapshot()

    assert workspace["selected_datasource_ids"] == full["selected_datasource_ids"] == [11]
    properties = workspace["selected_datasource_snapshot"][0]["metadata_schema"]["properties"]
    assert set(properties) == {"contract_id", "amount"}
    assert "description" not in properties["amount"]
    assert len(str(workspace)) < len(str(full))
    assert workspace["runtime_schema_workspace"]["detail_tool"] == "read_runtime_datasource_context"


def test_semantic_understanding_gets_complete_schema_while_implementation_is_field_scoped():
    preparation = _preparation()
    full = preparation.prompt_snapshot()
    full["analysis_semantic_context"]["insight_lineage"] = [{
        "question_id": "q1",
        "datasource_ids": [11],
        "fact_bindings": [{"fact_id": "f1", "datasource_id": 11, "fields": ["amount"]}],
        "relationship_ids": [],
    }]
    prompt = _build_runtime_prompt_snapshot(full)

    understand = build_runtime_datasource_working_message(
        prompt, preparation.selected_snapshot, work="understand",
    )
    understand_payload = _working_payload(understand)
    assert understand_payload["selected_datasource_ids"] == [11]
    understanding_source = understand_payload["datasource_details"][0]
    expected_schema = preparation.selected_snapshot['selected_datasource_snapshot'][0]['metadata_schema']
    assert all(understanding_source['metadata_schema'][key] == value
               for key, value in expected_schema.items() if key != 'description')
    assert expected_schema['description'].startswith(understanding_source['dataset_context']['description'][:120])
    assert len(understanding_source['dataset_context']['description']) <= 323
    assert understanding_source['dataset_context']['evidence_authority'] == 'context_only'
    assert 'schema_summary' not in understand_payload['datasource_index'][0]

    implement = build_runtime_datasource_working_message(
        prompt,
        preparation.selected_snapshot,
        work="implement",
        datasource_ids=[11],
        field_names_by_datasource={"11": ["amount"]},
    )
    implement_payload = _working_payload(implement)
    details = implement_payload["datasource_details"][0]
    assert set(details["metadata_schema"]["properties"]) == {"amount"}
    assert "合同含税金额" in details["metadata_schema"]["properties"]["amount"]["description"]
    assert "relationships" not in details["metadata_schema"]


def test_understanding_preserves_schema_meanings_and_all_selected_sources_without_reader():
    import copy
    sources = [
        {'datasource_id': 11, 'metadata_schema': {
            'properties': {
                'amount': {'type': 'number', 'description': 'Net value', 'formula': 'gross - rebate'},
                'status': {'type': 'string', 'enum': ['active', 'retired'],
                           'semantic_enrichment': {'business_meaning': 'Lifecycle, not health'},
                           'semantic_interpretation': {'origin': 'preparation_model',
                                                       'confidence': 'inferred',
                                                       'model_confidence': 'confirmed',
                                                       'business_meaning': 'Health'},
                           'observed_value_profile': {'observed_values': ['active']}},
            },
            'relationships': [{'relationship_id': 'r1', 'target_datasource_id': 12}],
        }},
        {'datasource_id': 12, 'metadata_schema': {'properties': {
            'timestamp': {'type': 'string', 'semantic_enrichment': {'time_semantics': 'event_start'}},
        }}},
    ]
    snapshot = {'selected_datasource_ids': [11, 12], 'selected_datasource_snapshot': sources}
    before = copy.deepcopy(snapshot)
    workspace = [{'datasource_id': 11, 'metadata_schema': {'properties': {
        'amount': {**sources[0]['metadata_schema']['properties']['amount'], 'unit': 'USD'},
    }}}]
    payload = _working_payload(build_runtime_datasource_working_message(
        snapshot, snapshot, work='understand', datasource_ids=[11],
        field_names_by_datasource={'11': ['amount']},
        schema_detail_workspace=workspace, omit_replayed_details=True,
    ))
    assert payload['selected_datasource_ids'] == [11, 12]
    assert [source['datasource_id'] for source in payload['datasource_details']] == [11, 12]
    schema = payload['datasource_details'][0]['metadata_schema']
    assert schema['properties']['status'] == sources[0]['metadata_schema']['properties']['status']
    assert schema['properties']['amount']['unit'] == 'USD'
    assert schema['relationships'] == sources[0]['metadata_schema']['relationships']
    assert payload['datasource_details'][1]['metadata_schema']['properties'] == sources[1]['metadata_schema']['properties']
    assert snapshot == before


def test_material_plan_starts_from_compact_index_without_eager_lineage_details():
    preparation = _preparation()
    full = preparation.prompt_snapshot()
    full["analysis_semantic_context"]["insight_lineage"] = [{
        "question_id": "q1",
        "datasource_ids": [11],
        "fact_bindings": [{"fact_id": "f1", "datasource_id": 11, "fields": ["amount"]}],
        "relationship_ids": [],
    }]
    prompt = _build_runtime_prompt_snapshot(full)

    message = build_runtime_datasource_working_message(
        prompt,
        preparation.selected_snapshot,
        work="plan",
    )
    payload = _working_payload(message)

    assert payload["selected_datasource_ids"] == [11]
    assert payload["datasource_index"]
    assert payload["datasource_details"] == []
    assert payload["runtime_schema_workspace"]["detail_datasource_ids"] == []


def test_result_review_keeps_source_index_without_replaying_schema_details():
    preparation = _preparation()
    full = preparation.prompt_snapshot()
    full["analysis_semantic_context"]["insight_lineage"] = [{
        "question_id": "q1",
        "datasource_ids": [11],
        "fact_bindings": [{"fact_id": "f1", "datasource_id": 11, "fields": ["amount"]}],
    }]
    prompt = _build_runtime_prompt_snapshot(full)

    message = build_runtime_datasource_working_message(
        prompt,
        preparation.selected_snapshot,
        work="review",
    )
    payload = _working_payload(message)

    assert payload["selected_datasource_ids"] == [11]
    assert payload["datasource_index"]
    assert payload["datasource_index"][0]["datasource_name"] == "contract_fact"
    assert "schema_summary" not in payload["datasource_index"][0]
    assert payload["datasource_details"] == []
    assert payload["runtime_schema_workspace"]["detail_datasource_ids"] == []


def test_implementation_does_not_replay_all_fields_from_prior_schema_detail_reads():
    preparation = _preparation()
    full = preparation.prompt_snapshot()
    full["analysis_semantic_context"]["insight_lineage"] = [{
        "question_id": "q1",
        "datasource_ids": [11],
        "fact_bindings": [{"fact_id": "f1", "datasource_id": 11, "fields": ["amount"]}],
    }]
    prompt = _build_runtime_prompt_snapshot(full)
    prior_detail = preparation.selected_snapshot["selected_datasource_snapshot"][0]

    message = build_runtime_datasource_working_message(
        prompt,
        preparation.selected_snapshot,
        work="implement",
        datasource_ids=[11],
        field_names_by_datasource={"11": ["amount"]},
        schema_detail_workspace=[prior_detail],
    )
    payload = _working_payload(message)
    details = payload["datasource_details"][0]

    assert set(details["metadata_schema"]["properties"]) == {"amount"}
    assert "runtime_material_plan" not in payload


def test_state_scoped_carrier_does_not_expand_fields_without_material_plan():
    preparation = _preparation()
    full = preparation.prompt_snapshot()
    full["analysis_semantic_context"]["insight_lineage"] = [{
        "question_id": "q1",
        "datasource_ids": [11],
        "fact_bindings": [{"fact_id": "f1", "datasource_id": 11, "fields": ["amount"]}],
    }]
    prompt = _build_runtime_prompt_snapshot(full)
    message = build_runtime_datasource_working_message(
        prompt, preparation.selected_snapshot, work="implement",
    )
    payload = _working_payload(message)
    assert payload["runtime_schema_workspace"]["detail_datasource_ids"] == []
    assert payload["datasource_details"] == []


def test_runtime_workspace_reader_returns_exact_requested_fields_without_expanding_scope():
    preparation = _preparation()
    context = CustomContext(
        username="tester",
        execution_datasource_snapshot=preparation.selected_snapshot,
        active_datasource_snapshot=preparation.runtime_prompt_snapshot(),
    )
    runtime = SimpleNamespace(context=context)

    result = read_runtime_datasource_context.func(
        runtime,
        datasource_ids=[11, 99],
        field_names_by_datasource={"11": ["amount", "missing"]},
    )

    assert result["status"] == "partial"
    assert result["unavailable_datasource_ids"] == [99]
    source = result["datasources"][0]
    assert set(source["metadata_schema"]["properties"]) == {"amount"}
    assert source["unavailable_field_names"] == ["missing"]
    assert "合同含税金额" in source["metadata_schema"]["properties"]["amount"]["description"]


def test_runtime_workspace_tool_exposes_runtime_as_injected_parameter():
    assert read_runtime_datasource_context.func.__annotations__["runtime"] == ToolRuntime[CustomContext]


def test_schema_projection_and_reader_preserve_verified_facts_and_bound_field_semantics():
    preparation = _preparation()
    preparation.selected_snapshot['selected_datasource_snapshot'][0]['metadata_schema']['semantic_enrichment_status'] = 'unavailable'
    full = preparation.prompt_snapshot()
    properties = full["selected_datasource_snapshot"][0]["metadata_schema"]["properties"]
    profile = {"observed_values": ["A", "B"], "availability": "non_empty"}
    properties["contract_id"]["observed_value_profile"] = profile
    properties["amount"]["unit"] = "CNY"
    properties["amount"]["business_formula"] = "gross less tax"
    full["analysis_semantic_context"]["insight_lineage"] = [{
        "question_id": "q1", "fact_bindings": [{"datasource_id": 11, "fields": ["amount"]}],
    }]
    projected = _build_runtime_prompt_snapshot(full)
    view = projected["selected_datasource_snapshot"][0]["metadata_schema"]["properties"]
    assert view["amount"] == properties["amount"]
    assert view["contract_id"]["observed_value_profile"] == profile
    runtime = SimpleNamespace(context=CustomContext(
        username="tester", execution_datasource_snapshot=preparation.selected_snapshot,
        active_datasource_snapshot=projected,
    ))
    detail = read_runtime_datasource_context.func(runtime, datasource_ids=[11],
        field_names_by_datasource={"11": ["contract_id"]})
    assert detail["datasources"][0]["metadata_schema"]["properties"]["contract_id"]["observed_value_profile"] == profile
    assert detail['datasources'][0]['metadata_schema']['semantic_enrichment_status'] == 'unavailable'


def test_schema_reader_result_is_held_in_typed_workspace_not_recovery_prose():
    preparation = _preparation()
    runtime = SimpleNamespace(short_term_memory={})
    context = CustomContext(
        username="tester",
        execution_datasource_snapshot=preparation.selected_snapshot,
    )
    tool_runtime = SimpleNamespace(context=context)
    payload = read_runtime_datasource_context.func(
        tool_runtime,
        datasource_ids=[11],
        field_names_by_datasource={"11": ["amount"]},
    )

    invoker._update_short_term_memory_from_tool_payload(runtime, payload)
    workspace = runtime.short_term_memory[invoker.RUNTIME_SCHEMA_DETAIL_MEMORY_KEY]
    assert len(workspace) == 1
    assert set(workspace[0]["metadata_schema"]["properties"]) == {"amount"}

    runtime.output_contract = {"type": "standard"}
    runtime.clarification_mode = "same_turn"
    with patch.object(invoker, "_load_turn_executions_fresh", return_value=[]), patch.object(
        invoker, "_get_latest_failed_repair_target", return_value={}
    ):
        recovery = invoker._build_analysis_recovery_instruction(None, runtime, 1)
    assert "Runtime" in recovery
    assert "合同含税金额" not in recovery


def test_schema_detail_workspace_replaces_by_datasource_without_fixed_source_slice():
    runtime = SimpleNamespace(short_term_memory={})
    for datasource_id in range(1, 13):
        invoker._update_runtime_schema_detail_workspace(runtime, {
            'datasources': [{
                'datasource_id': datasource_id,
                'metadata_schema': {'properties': {f'field_{datasource_id}': {'type': 'string'}}},
            }],
        })
    workspace = runtime.short_term_memory[invoker.RUNTIME_SCHEMA_DETAIL_MEMORY_KEY]
    assert [item['datasource_id'] for item in workspace] == list(range(1, 13))


def test_schema_detail_workspace_merges_focused_reads_for_the_same_datasource():
    runtime = SimpleNamespace(short_term_memory={})
    invoker._update_runtime_schema_detail_workspace(runtime, {
        'datasources': [{
            'datasource_id': 11,
            'metadata_schema': {'properties': {
                'contract_id': {'type': 'string'},
            }},
        }],
    })
    invoker._update_runtime_schema_detail_workspace(runtime, {
        'datasources': [{
            'datasource_id': 11,
            'metadata_schema': {'properties': {
                'amount': {'type': 'number'},
            }},
        }],
    })

    source = runtime.short_term_memory[invoker.RUNTIME_SCHEMA_DETAIL_MEMORY_KEY][0]
    assert set(source['metadata_schema']['properties']) == {'contract_id', 'amount'}

    invoker._update_runtime_schema_detail_workspace(runtime, {
        'datasources': [{
            'datasource_id': 11,
            'unavailable_field_names': ['future_field'],
            'metadata_schema': {'properties': {}},
        }],
    })
    invoker._update_runtime_schema_detail_workspace(runtime, {
        'datasources': [{
            'datasource_id': 11,
            'metadata_schema': {'properties': {
                'future_field': {'type': 'string'},
            }},
        }],
    })
    source = runtime.short_term_memory[invoker.RUNTIME_SCHEMA_DETAIL_MEMORY_KEY][0]
    assert 'unavailable_field_names' not in source


def test_schema_detail_workspace_is_canonical_source_for_a_compacted_work_view():
    from agent.context_engineering_runtime import (
        runtime_schema_workspace_covers_material_scope,
    )

    assert runtime_schema_workspace_covers_material_scope(
        [{
            'datasource_id': 11,
            'metadata_schema': {'properties': {
                'contract_id': {'type': 'string'},
                'amount': {'type': 'number'},
            }},
        }],
        [11],
        {'11': ['contract_id', 'amount']},
    )
    assert not runtime_schema_workspace_covers_material_scope(
        [{'datasource_id': 11, 'metadata_schema': {'properties': {
            'amount': {'type': 'number'},
        }}}],
        [11],
        {'11': ['contract_id', 'amount']},
    )
def test_local_material_focus_does_not_hide_global_coverage_or_pending_checks():
    import copy
    from agent.context_engineering_runtime import _runtime_semantic_work_view

    semantic = {
        'business_questions': [{'question_id': 'q1'}, {'question_id': 'q2'}],
        'question_fact_coverage': [
            {'question_id': 'q1', 'coverage_status': 'covered'},
            {'question_id': 'q2', 'coverage_status': 'partial'},
        ],
        'runtime_probe_agenda': {'version': 1, 'pending_technical_checks': [
            {'fact_id': 'f1', 'datasource_ids': [11], 'check': 'format'},
            {'fact_id': 'f2', 'datasource_ids': [12], 'check': 'enum'},
        ]},
    }
    original = copy.deepcopy(semantic)
    result = _runtime_semantic_work_view(
        semantic, requested_ids=[11], material_plan={'material_units': [{
            'datasource_id': 11, 'question_ids': ['q1'], 'fact_ids': ['f1'],
        }]},
    )
    assert result['question_fact_coverage'] == semantic['question_fact_coverage']
    assert result['runtime_probe_agenda'] == semantic['runtime_probe_agenda']
    assert semantic == original


def test_semantic_work_view_keeps_selection_fact_sources_and_rule_definitions():
    from agent.context_engineering_runtime import _runtime_semantic_work_view

    semantic = {
        'question_fact_coverage': [{'question_id': 'q1', 'coverage_status': 'direct',
            'fact_coverage': [{'fact_id': 'f1', 'coverage_status': 'direct', 'datasource_ids': [7]}]}],
        'semantic_references': [{'datasource_id': 8, 'metadata_schema': {
            'properties': {'ratio': {'description': 'numerator / denominator'}},
        }}],
    }
    result = _runtime_semantic_work_view(semantic, requested_ids=[])
    assert result['question_fact_coverage'][0]['fact_coverage'][0]['datasource_ids'] == [7]
    assert result['semantic_references'][0]['metadata_schema'] == semantic['semantic_references'][0]['metadata_schema']
