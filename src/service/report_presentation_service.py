import re
from html import escape as escape_html
from typing import Any


_DATASOURCE_REFERENCE = re.compile(r"\u6570\u636e\u6e90\s*#?\s*(\d+)")
_COMPOUND_DATASOURCE_REFERENCE = re.compile(
    r"\u6570\u636e\u6e90\s*#?\s*(\d+)"
    r"((?:\s*(?:\u3001|/|\u4e0e|\u548c|\u53ca)\s*(?:\u6570\u636e\u6e90\s*#?\s*)?\d+)+)"
)
_COMPOUND_TAIL = re.compile(
    r"(\s*(?:\u3001|/|\u4e0e|\u548c|\u53ca)\s*)(?:\u6570\u636e\u6e90\s*#?\s*)?(\d+)"
)
_NUMERIC_RANGE_TILDE = re.compile(r"(?<=\d)\s*~\s*(?=\d)")
_MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\r\n]*)\)")


def _datasource_name_map(
    datasource_bindings: list[dict[str, Any]] | None,
) -> dict[int, str]:
    names: dict[int, str] = {}
    for item in datasource_bindings or []:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("datasource_id", item.get("id"))
        try:
            datasource_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        name = str(
            item.get("datasource_name")
            or item.get("display_name")
            or item.get("name")
            or ""
        ).strip()
        if datasource_id > 0 and name:
            names[datasource_id] = name
    return names


def _replace_datasource_references(report: str, names: dict[int, str]) -> str:
    if not report or not names:
        return report

    def replace_compound(match: re.Match[str]) -> str:
        first_id = int(match.group(1))
        first = names.get(first_id, f"\u6570\u636e\u6e90{first_id}")
        tail = _COMPOUND_TAIL.sub(
            lambda item: item.group(1)
            + names.get(int(item.group(2)), f"\u6570\u636e\u6e90{item.group(2)}"),
            match.group(2),
        )
        return first + tail

    normalized = _COMPOUND_DATASOURCE_REFERENCE.sub(replace_compound, report)
    return _DATASOURCE_REFERENCE.sub(
        lambda match: names.get(int(match.group(1)), match.group(0)),
        normalized,
    )


def _replace_unsupported_markdown_images(report: str) -> str:
    """Keep report text readable without emitting unsupported image artifacts."""
    def replace_image(match: re.Match[str]) -> str:
        alt_text = escape_html(match.group(1).strip(), quote=False)
        destination = match.group(2).strip()
        if destination.lower().startswith("chart:"):
            label = alt_text or destination[len("chart:"):].strip()
            return f"（图表：{label}）" if label else "（图表产物）"
        return alt_text or "（图片产物暂不支持）"

    return _MARKDOWN_IMAGE.sub(replace_image, report)


def normalize_report_presentation(
    analysis_report: Any,
    datasource_bindings: list[dict[str, Any]] | None,
) -> str:
    """Normalize user-visible report notation without changing analytic meaning."""
    report = str(analysis_report or "")
    report = _replace_datasource_references(report, _datasource_name_map(datasource_bindings))
    report = _replace_unsupported_markdown_images(report)
    # Markdown treats two unrelated ASCII tildes as one strikethrough span. A
    # full-width range separator preserves the intended date/number expression.
    return _NUMERIC_RANGE_TILDE.sub("\uff5e", report)
