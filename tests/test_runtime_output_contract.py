from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent.tools import CURRENT_OUTPUT_CONTRACT, save_analysis_result


def test_text_output_uses_text_payload_without_artifacts():
    token = CURRENT_OUTPUT_CONTRACT.set({"type": "text"})
    try:
        result = save_analysis_result(output_text="本期风险主要集中在甲产品。")
    finally:
        CURRENT_OUTPUT_CONTRACT.reset(token)

    assert result.output_format == "text"
    assert result.output_payload == "本期风险主要集中在甲产品。"
    assert result.charts == []
    assert result.tables == []


def test_json_output_is_checked_against_current_contract():
    token = CURRENT_OUTPUT_CONTRACT.set({
        "type": "json",
        "schema": {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        },
    })
    try:
        result = save_analysis_result(output_json={"count": 2})
        with pytest.raises(ValueError, match="类型应为"):
            save_analysis_result(output_json={"count": "2"})
    finally:
        CURRENT_OUTPUT_CONTRACT.reset(token)

    assert result.output_format == "json"
    assert result.output_payload == {"count": 2}


def test_output_modes_cannot_be_mixed():
    token = CURRENT_OUTPUT_CONTRACT.set({"type": "text"})
    try:
        with pytest.raises(ValueError, match="只能传入 output_text"):
            save_analysis_result(
                analysis_report="# 报告",
                output_text="文本",
            )
    finally:
        CURRENT_OUTPUT_CONTRACT.reset(token)
