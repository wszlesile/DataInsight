from agent.invoker import _iter_stream_update_payloads


def test_stream_update_payloads_ignore_interrupt_control_tuple():
    chunk = {
        "agent": {"messages": ["assistant-message"]},
        "tools": ("__interrupt__", {"messages": ["tool-message"]}),
        "__interrupt__": ("tools",),
    }

    assert list(_iter_stream_update_payloads(chunk)) == [
        {"messages": ["assistant-message"]},
        {"messages": ["tool-message"]},
    ]


def test_stream_update_payloads_skip_non_mapping_control_events():
    assert list(_iter_stream_update_payloads({"__interrupt__": ("tools",)})) == []
