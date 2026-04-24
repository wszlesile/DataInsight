import json
import re
from collections import defaultdict
from typing import Any

from utils.chart_spec_utils import finalize_chart_spec


SUPPORTED_CHART_KINDS = {"bar", "line", "pie", "scatter"}
DEFAULT_DOCUMENT_VERSION = "1.0"
DEFAULT_TOP_N = 12
BACKEND_LAYOUT_LOCK_KEY = "__layout_managed_by_backend"


def build_chart_document(
    *,
    chart_kind: str,
    data: Any,
    title: str = "",
    description: str = "",
    category_field: str | None = None,
    value_field: str | None = None,
    series_field: str | None = None,
    x_field: str | None = None,
    y_field: str | None = None,
    sort_field: str | None = None,
    sort_order: str = "desc",
    limit: int | None = None,
    orientation: str = "auto",
    label_mode: str = "auto",
    stack: bool = False,
    merge_tail: bool | None = None,
    top_n: int | None = None,
) -> dict[str, Any]:
    chart_kind_value = _normalize_chart_kind(chart_kind)
    dataset = _normalize_dataset(data)

    category_name = _coalesce_field(category_field, x_field)
    value_name = _coalesce_field(value_field, y_field)
    x_name = _coalesce_field(x_field, category_field)
    y_name = _coalesce_field(y_field, value_field)

    if chart_kind_value in {"bar", "line"}:
        if not category_name or not value_name:
            raise ValueError("bar/line chart_document requires category_field and value_field.")
    elif chart_kind_value == "pie":
        if not category_name or not value_name:
            raise ValueError("pie chart_document requires category_field and value_field.")
    elif chart_kind_value == "scatter":
        if not x_name or not y_name:
            raise ValueError("scatter chart_document requires x_field and y_field.")

    document = {
        "version": DEFAULT_DOCUMENT_VERSION,
        "chart_kind": chart_kind_value,
        "title": str(title or "").strip(),
        "description": str(description or "").strip(),
        "dataset": dataset,
        "encoding": {
            "category_field": category_name,
            "value_field": value_name,
            "series_field": str(series_field or "").strip() or None,
            "x_field": x_name,
            "y_field": y_name,
        },
        "transform": {
            "sort_field": str(sort_field or "").strip() or None,
            "sort_order": "asc" if str(sort_order or "").lower() == "asc" else "desc",
            "limit": _to_positive_int(limit),
            "top_n": _to_positive_int(top_n),
        },
        "presentation": {
            "orientation": _normalize_orientation(orientation),
            "label_mode": _normalize_label_mode(label_mode),
            "stack": bool(stack),
            "merge_tail": bool(merge_tail) if merge_tail is not None else chart_kind_value == "pie",
        },
    }
    return document


def build_chart_result(
    *,
    chart_kind: str,
    data: Any,
    title: str,
    description: str = "",
    category_field: str | None = None,
    value_field: str | None = None,
    series_field: str | None = None,
    x_field: str | None = None,
    y_field: str | None = None,
    sort_field: str | None = None,
    sort_order: str = "desc",
    limit: int | None = None,
    orientation: str = "auto",
    label_mode: str = "auto",
    stack: bool = False,
    merge_tail: bool | None = None,
    top_n: int | None = None,
) -> dict[str, Any]:
    document = build_chart_document(
        chart_kind=chart_kind,
        data=data,
        title=title,
        description=description,
        category_field=category_field,
        value_field=value_field,
        series_field=series_field,
        x_field=x_field,
        y_field=y_field,
        sort_field=sort_field,
        sort_order=sort_order,
        limit=limit,
        orientation=orientation,
        label_mode=label_mode,
        stack=stack,
        merge_tail=merge_tail,
        top_n=top_n,
    )
    return {
        "title": str(title or "").strip(),
        "chart_type": "echarts",
        "description": str(description or "").strip(),
        "chart_document": document,
    }


def build_multi_metric_chart_result(
    *,
    data: Any,
    title: str,
    category_field: str,
    value_fields: list[str] | tuple[str, ...],
    description: str = "",
    value_labels: list[str] | tuple[str, ...] | None = None,
    chart_kind: str = "bar",
    sort_field: str | None = None,
    sort_order: str = "desc",
    limit: int | None = None,
    orientation: str = "auto",
    label_mode: str = "hidden",
    stack: bool = False,
) -> dict[str, Any]:
    chart_kind_value = _normalize_chart_kind(chart_kind)
    if chart_kind_value != "bar":
        raise ValueError("build_multi_metric_chart_result currently supports chart_kind='bar' only.")

    dataset = _normalize_dataset(data)
    normalized_value_fields = _normalize_string_list(value_fields)
    if len(normalized_value_fields) < 2:
        raise ValueError("build_multi_metric_chart_result requires at least two value_fields.")

    normalized_value_labels = _normalize_string_list(value_labels or [], dedupe=False)
    if normalized_value_labels and len(normalized_value_labels) != len(normalized_value_fields):
        raise ValueError("value_labels length must match value_fields length.")

    category_name = str(category_field or "").strip()
    if not category_name:
        raise ValueError("build_multi_metric_chart_result requires category_field.")

    document = {
        "version": DEFAULT_DOCUMENT_VERSION,
        "chart_kind": chart_kind_value,
        "title": str(title or "").strip(),
        "description": str(description or "").strip(),
        "dataset": dataset,
        "encoding": {
            "category_field": category_name,
            "value_field": normalized_value_fields[0],
            "value_fields": normalized_value_fields,
            "value_labels": normalized_value_labels,
            "series_field": None,
            "x_field": category_name,
            "y_field": normalized_value_fields[0],
        },
        "transform": {
            "sort_field": str(sort_field or normalized_value_fields[0]).strip() or None,
            "sort_order": "asc" if str(sort_order or "").lower() == "asc" else "desc",
            "limit": _to_positive_int(limit),
            "top_n": None,
        },
        "presentation": {
            "orientation": _normalize_orientation(orientation),
            "label_mode": _normalize_label_mode(label_mode),
            "stack": bool(stack),
            "merge_tail": False,
        },
    }
    _validate_chart_document(document)
    return {
        "title": str(title or "").strip(),
        "chart_type": "echarts",
        "description": str(description or "").strip(),
        "chart_document": document,
    }


def build_chart_suite(
    *,
    data: Any,
    title: str,
    description: str = "",
    category_field: str,
    value_field: str,
    series_field: str | None = None,
    sort_field: str | None = None,
    sort_order: str = "desc",
    limit: int | None = None,
    top_n: int | None = None,
    include_line: bool | None = None,
    include_bar: bool | None = None,
    include_pie: bool | None = None,
    stack: bool = False,
    max_charts: int = 3,
) -> list[dict[str, Any]]:
    dataset = _normalize_dataset(data)
    rows = dataset["rows"]
    categories = _ordered_unique([row.get(category_field) for row in rows if category_field in row])
    category_labels = ["" if value is None else str(value) for value in categories]
    category_count = len(category_labels)
    series_values = _ordered_unique([row.get(series_field) for row in rows if series_field and row.get(series_field) is not None])
    single_series = not series_field or len(series_values) <= 1
    temporal = _looks_temporal_category(category_labels, category_field)

    resolved_limit = _to_positive_int(limit)
    resolved_top_n = _to_positive_int(top_n) or min(DEFAULT_TOP_N, max(category_count, 1))
    suite: list[dict[str, Any]] = []

    want_line = include_line if include_line is not None else temporal
    want_bar = include_bar if include_bar is not None else True
    want_pie = include_pie if include_pie is not None else (single_series and not temporal and category_count <= 8)

    if temporal and want_line:
        suite.append(build_chart_result(
            chart_kind="line",
            data=rows,
            title=_compose_chart_title(title, "趋势"),
            description=_compose_chart_description(description, "展示变化趋势。"),
            category_field=category_field,
            value_field=value_field,
            series_field=series_field,
            sort_field=sort_field or category_field,
            sort_order="asc",
            limit=resolved_limit,
            label_mode="hidden",
            stack=stack,
        ))

    if want_bar:
        bar_sort_field = sort_field or (category_field if temporal else value_field)
        bar_sort_order = "asc" if temporal else sort_order
        suite.append(build_chart_result(
            chart_kind="bar",
            data=rows,
            title=_compose_chart_title(title, "对比"),
            description=_compose_chart_description(description, "突出各项对比关系。"),
            category_field=category_field,
            value_field=value_field,
            series_field=series_field,
            sort_field=bar_sort_field,
            sort_order=bar_sort_order,
            limit=resolved_limit,
            label_mode="hidden",
            stack=stack,
        ))

    if want_pie:
        suite.append(build_chart_result(
            chart_kind="pie",
            data=rows,
            title=_compose_chart_title(title, "占比"),
            description=_compose_chart_description(description, "展示整体构成占比。"),
            category_field=category_field,
            value_field=value_field,
            sort_field=sort_field or value_field,
            sort_order=sort_order,
            top_n=resolved_top_n,
            label_mode="hidden",
            merge_tail=True,
        ))

    if not suite:
        suite.append(build_chart_result(
            chart_kind="bar",
            data=rows,
            title=title,
            description=description,
            category_field=category_field,
            value_field=value_field,
            series_field=series_field,
            sort_field=sort_field or value_field,
            sort_order=sort_order,
            limit=resolved_limit,
            label_mode="hidden",
            stack=stack,
        ))

    deduped_suite: list[dict[str, Any]] = []
    seen_kinds: set[str] = set()
    for item in suite:
        kind = str(((item.get("chart_document") or {}).get("chart_kind")) or "").strip().lower()
        if not kind or kind in seen_kinds:
            continue
        seen_kinds.add(kind)
        deduped_suite.append(item)
        if len(deduped_suite) >= max(1, int(max_charts or 1)):
            break
    return deduped_suite


def normalize_chart_result_item(chart: Any, index: int = 1) -> dict[str, Any]:
    if not isinstance(chart, dict):
        raise ValueError("charts items must be dict objects.")

    chart_document = chart.get("chart_document")
    chart_spec = chart.get("chart_spec")

    if chart_document:
        document = normalize_chart_document(
            chart_document,
            fallback_title=str(chart.get("title") or f"Chart {index}").strip(),
            fallback_description=str(chart.get("description") or "").strip(),
        )
        compiled_spec = mark_chart_spec_backend_managed(compile_chart_document(document))
        return {
            "title": document.get("title") or str(chart.get("title") or f"Chart {index}").strip(),
            "chart_type": "echarts",
            "description": document.get("description") or str(chart.get("description") or "").strip(),
            "chart_spec": finalize_chart_spec(compiled_spec),
        }

    if not isinstance(chart_spec, dict) or not chart_spec:
        raise ValueError("structured chart item must provide non-empty chart_spec or chart_document.")

    title = str(chart.get("title") or f"Chart {index}").strip()
    description = str(chart.get("description") or "").strip()
    chart_type = str(chart.get("chart_type") or "echarts").strip() or "echarts"
    inferred_document = infer_chart_document_from_chart_spec(
        chart_spec,
        fallback_title=title,
        fallback_description=description,
    )
    final_spec = (
        finalize_chart_spec(mark_chart_spec_backend_managed(compile_chart_document(inferred_document)))
        if inferred_document
        else finalize_chart_spec(mark_chart_spec_backend_managed(chart_spec))
    )
    return {
        "title": title,
        "chart_type": chart_type,
        "description": description,
        "chart_spec": final_spec,
    }


def normalize_chart_document(
    chart_document: Any,
    *,
    fallback_title: str = "",
    fallback_description: str = "",
) -> dict[str, Any]:
    if not isinstance(chart_document, dict):
        raise ValueError("chart_document must be a dict.")

    document = json.loads(json.dumps(chart_document, ensure_ascii=False))
    chart_kind = _normalize_chart_kind(document.get("chart_kind") or document.get("chart_type"))
    dataset = _normalize_dataset(document.get("dataset") or document.get("data"))

    encoding = document.get("encoding") if isinstance(document.get("encoding"), dict) else {}
    transform = document.get("transform") if isinstance(document.get("transform"), dict) else {}
    presentation = document.get("presentation") if isinstance(document.get("presentation"), dict) else {}

    category_field = _coalesce_field(
        encoding.get("category_field"),
        encoding.get("category"),
        encoding.get("x"),
        document.get("category_field"),
        document.get("name_field"),
    )
    value_field = _coalesce_field(
        encoding.get("value_field"),
        encoding.get("value"),
        encoding.get("y"),
        document.get("value_field"),
    )
    value_fields = _normalize_string_list(
        encoding.get("value_fields")
        or encoding.get("values")
        or document.get("value_fields")
        or []
    )
    value_labels = _normalize_string_list(
        encoding.get("value_labels")
        or encoding.get("labels")
        or document.get("value_labels")
        or [],
        dedupe=False,
    )
    if value_fields and not value_field:
        value_field = value_fields[0]
    if value_field and not value_fields:
        value_fields = [value_field]
    series_field = _coalesce_field(
        encoding.get("series_field"),
        encoding.get("series"),
        document.get("series_field"),
    )
    x_field = _coalesce_field(
        encoding.get("x_field"),
        encoding.get("x"),
        document.get("x_field"),
        category_field,
    )
    y_field = _coalesce_field(
        encoding.get("y_field"),
        encoding.get("y"),
        document.get("y_field"),
        value_field,
    )
    default_sort_field = value_fields[0] if len(value_fields) > 1 else value_field

    normalized = {
        "version": str(document.get("version") or DEFAULT_DOCUMENT_VERSION),
        "chart_kind": chart_kind,
        "title": str(document.get("title") or fallback_title or "").strip(),
        "description": str(document.get("description") or fallback_description or "").strip(),
        "dataset": dataset,
        "encoding": {
            "category_field": category_field,
            "value_field": value_field,
            "value_fields": value_fields,
            "value_labels": value_labels,
            "series_field": series_field,
            "x_field": x_field,
            "y_field": y_field,
        },
        "transform": {
            "sort_field": _coalesce_field(
                transform.get("sort_field"),
                transform.get("sort_by"),
                document.get("sort_field"),
                default_sort_field,
            ),
            "sort_order": "asc" if str(transform.get("sort_order") or document.get("sort_order") or "").lower() == "asc" else "desc",
            "limit": _to_positive_int(transform.get("limit") or document.get("limit")),
            "top_n": _to_positive_int(transform.get("top_n") or document.get("top_n")),
        },
        "presentation": {
            "orientation": _normalize_orientation(presentation.get("orientation") or document.get("orientation") or "auto"),
            "label_mode": _normalize_label_mode(presentation.get("label_mode") or document.get("label_mode") or "auto"),
            "stack": bool(presentation.get("stack") or document.get("stack")),
            "merge_tail": bool(
                presentation.get("merge_tail")
                if presentation.get("merge_tail") is not None
                else document.get("merge_tail")
                if document.get("merge_tail") is not None
                else chart_kind == "pie"
            ),
        },
    }

    _validate_chart_document(normalized)
    return normalized


def compile_chart_document(chart_document: Any) -> dict[str, Any]:
    document = normalize_chart_document(chart_document)
    chart_kind = document["chart_kind"]

    if chart_kind in {"bar", "line", "scatter"}:
        return _compile_cartesian_chart(document)
    if chart_kind == "pie":
        return _compile_pie_chart(document)
    raise ValueError(f"Unsupported chart_kind: {chart_kind}")


def infer_chart_document_from_chart_spec(
    chart_spec: Any,
    *,
    fallback_title: str = "",
    fallback_description: str = "",
) -> dict[str, Any] | None:
    if not isinstance(chart_spec, dict) or not chart_spec:
        return None

    series_list = [series for series in (chart_spec.get("series") or []) if isinstance(series, dict)]
    if not series_list:
        return None

    series_types = {str(series.get("type") or "").strip().lower() for series in series_list}
    if len(series_types) != 1:
        return None

    chart_kind = next(iter(series_types))
    title_text = _extract_title_text(chart_spec) or fallback_title

    if chart_kind in {"line", "bar"}:
        document = _infer_cartesian_document_from_spec(
            chart_spec=chart_spec,
            chart_kind=chart_kind,
            fallback_title=title_text,
            fallback_description=fallback_description,
        )
        return document
    if chart_kind == "pie":
        return _infer_pie_document_from_spec(
            chart_spec=chart_spec,
            fallback_title=title_text,
            fallback_description=fallback_description,
        )
    return None


def _compile_cartesian_chart(document: dict[str, Any]) -> dict[str, Any]:
    chart_kind = document["chart_kind"]
    encoding = document["encoding"]
    presentation = document["presentation"]
    transform = document["transform"]
    rows = list(document["dataset"]["rows"])

    if chart_kind == "scatter":
        rows = _sort_rows(rows, transform.get("sort_field"), transform.get("sort_order"))
        rows = _limit_rows(rows, transform.get("limit"))
        return _build_scatter_spec(document, rows)

    category_field = encoding["category_field"]
    value_field = encoding["value_field"]
    value_fields = list(encoding.get("value_fields") or [])
    series_field = encoding.get("series_field")

    if chart_kind == "bar" and len(value_fields) > 1 and not series_field:
        return _compile_multi_metric_bar_chart(document, rows)

    rows = _sort_rows(rows, transform.get("sort_field"), transform.get("sort_order"))
    if transform.get("limit"):
        rows = _limit_category_rows(rows, category_field, value_field, transform["limit"], transform.get("sort_order"))

    categories = _ordered_unique([row.get(category_field) for row in rows])
    categories = ["" if value is None else str(value) for value in categories]

    if series_field:
        series_names = _ordered_unique([row.get(series_field) for row in rows])
    else:
        series_names = [document["title"] or value_field]

    series_items = []
    if series_field:
        value_lookup: dict[tuple[str, str], Any] = {}
        for row in rows:
            category_value = "" if row.get(category_field) is None else str(row.get(category_field))
            series_value = "" if row.get(series_field) is None else str(row.get(series_field))
            value_lookup[(category_value, series_value)] = row.get(value_field)

        for series_name in series_names:
            display_name = "" if series_name is None else str(series_name)
            series_data = [value_lookup.get((category, display_name)) for category in categories]
            series_items.append({
                "type": chart_kind,
                "name": display_name,
                "data": series_data,
                "label": {"show": presentation["label_mode"] == "always"},
            })
    else:
        value_lookup = {}
        for row in rows:
            category_value = "" if row.get(category_field) is None else str(row.get(category_field))
            value_lookup[category_value] = row.get(value_field)
        series_items.append({
            "type": chart_kind,
            "name": document["title"] or value_field,
            "data": [value_lookup.get(category) for category in categories],
            "label": {"show": presentation["label_mode"] == "always"},
        })

    if presentation.get("stack") and chart_kind in {"bar", "line"}:
        for item in series_items:
            item["stack"] = "total"

    orientation = _resolve_bar_orientation(categories, chart_kind, presentation.get("orientation"))
    if chart_kind == "bar" and orientation == "horizontal":
        x_axis = {"type": "value"}
        y_axis = {"type": "category", "data": categories}
    else:
        x_axis = {"type": "category", "data": categories}
        y_axis = {"type": "value"}

    legend_data = [item["name"] for item in series_items if item.get("name")]
    spec = {
        "title": {"text": document["title"]},
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 56, "right": 32, "top": 64, "bottom": 56, "containLabel": True},
        "xAxis": x_axis,
        "yAxis": y_axis,
        "series": series_items,
    }
    if len(legend_data) > 1:
        spec["legend"] = {"data": legend_data}
    else:
        spec["legend"] = {"show": False}
    return spec


def _compile_multi_metric_bar_chart(document: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    encoding = document["encoding"]
    presentation = document["presentation"]
    transform = document["transform"]

    category_field = encoding["category_field"]
    value_fields = list(encoding.get("value_fields") or [])
    value_labels = list(encoding.get("value_labels") or [])

    rows = _sort_rows(rows, transform.get("sort_field"), transform.get("sort_order"))
    rows = _limit_rows(rows, transform.get("limit"))

    categories = _ordered_unique([row.get(category_field) for row in rows])
    categories = ["" if value is None else str(value) for value in categories]

    category_rows: dict[str, dict[str, Any]] = {}
    for row in rows:
        category_value = "" if row.get(category_field) is None else str(row.get(category_field))
        category_rows[category_value] = row

    series_items = []
    for index, value_field in enumerate(value_fields):
        display_name = value_labels[index] if index < len(value_labels) and value_labels[index] else value_field
        series_items.append({
            "type": "bar",
            "name": display_name,
            "data": [category_rows.get(category, {}).get(value_field) for category in categories],
            "label": {"show": presentation["label_mode"] == "always"},
        })

    if presentation.get("stack"):
        for item in series_items:
            item["stack"] = "total"

    orientation = _resolve_bar_orientation(categories, "bar", presentation.get("orientation"))
    if orientation == "horizontal":
        x_axis = {"type": "value"}
        y_axis = {"type": "category", "data": categories}
    else:
        x_axis = {"type": "category", "data": categories}
        y_axis = {"type": "value"}

    legend_data = [item["name"] for item in series_items if item.get("name")]
    return {
        "title": {"text": document["title"]},
        "tooltip": {"trigger": "axis"},
        "legend": {"data": legend_data},
        "grid": {"left": 56, "right": 32, "top": 64, "bottom": 56, "containLabel": True},
        "xAxis": x_axis,
        "yAxis": y_axis,
        "series": series_items,
    }


def _build_scatter_spec(document: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    encoding = document["encoding"]
    series_field = encoding.get("series_field")

    if series_field:
        grouped: dict[str, list[list[Any]]] = defaultdict(list)
        for row in rows:
            series_name = "" if row.get(series_field) is None else str(row.get(series_field))
            grouped[series_name].append([row.get(encoding["x_field"]), row.get(encoding["y_field"])])
        series_items = [
            {"type": "scatter", "name": name, "data": data, "label": {"show": False}}
            for name, data in grouped.items()
        ]
    else:
        series_items = [{
            "type": "scatter",
            "name": document["title"] or encoding["y_field"],
            "data": [[row.get(encoding["x_field"]), row.get(encoding["y_field"])] for row in rows],
            "label": {"show": False},
        }]

    return {
        "title": {"text": document["title"]},
        "tooltip": {"trigger": "item"},
        "legend": {"data": [item["name"] for item in series_items if item.get("name")]},
        "grid": {"left": 56, "right": 32, "top": 64, "bottom": 56, "containLabel": True},
        "xAxis": {"type": "value", "name": encoding["x_field"]},
        "yAxis": {"type": "value", "name": encoding["y_field"]},
        "series": series_items,
    }


def _compile_pie_chart(document: dict[str, Any]) -> dict[str, Any]:
    encoding = document["encoding"]
    transform = document["transform"]
    presentation = document["presentation"]
    category_field = encoding["category_field"]
    value_field = encoding["value_field"]

    aggregated: dict[str, float] = defaultdict(float)
    for row in document["dataset"]["rows"]:
        category_value = "" if row.get(category_field) is None else str(row.get(category_field))
        aggregated[category_value] += _coerce_number(row.get(value_field))

    pie_items = [{"name": key, "value": value} for key, value in aggregated.items()]
    pie_items = sorted(
        pie_items,
        key=lambda item: (_coerce_number(item.get("value")), str(item.get("name"))),
        reverse=transform.get("sort_order") != "asc",
    )

    top_n = transform.get("top_n") or transform.get("limit") or DEFAULT_TOP_N
    if presentation.get("merge_tail") and len(pie_items) > top_n:
        head = pie_items[: max(top_n - 1, 1)]
        tail = pie_items[max(top_n - 1, 1):]
        tail_value = sum(_coerce_number(item.get("value")) for item in tail)
        if tail_value > 0:
            head.append({"name": "其他", "value": tail_value})
        pie_items = head
    elif transform.get("limit"):
        pie_items = pie_items[: transform["limit"]]

    spec = {
        "title": {"text": document["title"]},
        "tooltip": {"trigger": "item"},
        "series": [{
            "type": "pie",
            "name": document["title"] or value_field,
            "radius": ["32%", "68%"],
            "center": ["50%", "55%"],
            "data": pie_items,
            "label": {"show": presentation["label_mode"] == "always"},
        }],
    }
    if len(pie_items) > 1:
        spec["legend"] = {"data": [item["name"] for item in pie_items]}
    else:
        spec["legend"] = {"show": False}
    return spec


def _validate_chart_document(document: dict[str, Any]) -> None:
    chart_kind = document["chart_kind"]
    encoding = document["encoding"]
    value_fields = list(encoding.get("value_fields") or [])

    if chart_kind in {"bar", "line", "pie"}:
        if not encoding.get("category_field") or (not encoding.get("value_field") and not value_fields):
            raise ValueError(f"{chart_kind} chart_document requires category_field and value_field.")
    if len(value_fields) > 1:
        if chart_kind != "bar":
            raise ValueError("multi-metric chart_document currently supports bar charts only.")
        if encoding.get("series_field"):
            raise ValueError("multi-metric chart_document cannot be combined with series_field.")
    if chart_kind == "scatter":
        if not encoding.get("x_field") or not encoding.get("y_field"):
            raise ValueError("scatter chart_document requires x_field and y_field.")

    columns = document["dataset"]["columns"]
    field_candidates = [
        encoding.get("category_field"),
        encoding.get("value_field"),
        encoding.get("series_field"),
        encoding.get("x_field"),
        encoding.get("y_field"),
        document["transform"].get("sort_field"),
    ]
    field_candidates.extend(value_fields)
    for field_name in field_candidates:
        if field_name and field_name not in columns:
            raise ValueError(f"chart_document field '{field_name}' is not present in dataset columns.")


def _normalize_dataset(data: Any) -> dict[str, Any]:
    if hasattr(data, "to_dict") and hasattr(data, "columns"):
        rows = data.to_dict(orient="records")
        columns = [str(column) for column in list(data.columns)]
        return {"columns": columns, "rows": [_normalize_row(row, columns) for row in rows]}

    if isinstance(data, dict):
        if isinstance(data.get("rows"), list):
            rows_value = data.get("rows") or []
            columns_value = data.get("columns")
            if columns_value:
                columns = [str(column) for column in columns_value]
            elif rows_value and isinstance(rows_value[0], dict):
                columns = [str(column) for column in rows_value[0].keys()]
            else:
                columns = [str(index) for index in range(len(rows_value[0]))] if rows_value else []

            rows = []
            for row in rows_value:
                if isinstance(row, dict):
                    rows.append(_normalize_row(row, columns))
                elif isinstance(row, (list, tuple)):
                    rows.append({column: row[index] if index < len(row) else None for index, column in enumerate(columns)})
                else:
                    raise ValueError("dataset rows must be dict or list items.")
            return {"columns": columns, "rows": rows}

        rows = [data]
        columns = [str(column) for column in data.keys()]
        return {"columns": columns, "rows": [_normalize_row(data, columns)]}

    if isinstance(data, list):
        if not data:
            raise ValueError("chart_document dataset cannot be empty.")
        first = data[0]
        if isinstance(first, dict):
            columns = [str(column) for column in first.keys()]
            return {"columns": columns, "rows": [_normalize_row(row, columns) for row in data]}
        if isinstance(first, (list, tuple)):
            raise ValueError("list-based dataset requires {'columns': [...], 'rows': [...]} structure.")
        raise ValueError("unsupported dataset row type.")

    raise ValueError("unsupported chart_document dataset type.")


def _normalize_row(row: dict[str, Any], columns: list[str]) -> dict[str, Any]:
    normalized = {}
    for column in columns:
        value = row.get(column)
        normalized[column] = value.item() if hasattr(value, "item") else value
    return normalized


def _sort_rows(rows: list[dict[str, Any]], sort_field: str | None, sort_order: str | None) -> list[dict[str, Any]]:
    if not sort_field:
        return list(rows)
    reverse = str(sort_order or "").lower() != "asc"
    return sorted(
        rows,
        key=lambda row: (_sortable_value(row.get(sort_field)), str(row.get(sort_field))),
        reverse=reverse,
    )


def _limit_rows(rows: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    if not limit:
        return list(rows)
    return list(rows[:limit])


def _limit_category_rows(
    rows: list[dict[str, Any]],
    category_field: str,
    value_field: str,
    limit: int,
    sort_order: str | None,
) -> list[dict[str, Any]]:
    if not limit:
        return list(rows)

    reverse = str(sort_order or "").lower() != "asc"
    category_scores: dict[str, float] = defaultdict(float)
    for row in rows:
        category_value = "" if row.get(category_field) is None else str(row.get(category_field))
        category_scores[category_value] += _coerce_number(row.get(value_field))

    ranked_categories = sorted(
        category_scores.keys(),
        key=lambda key: (category_scores[key], key),
        reverse=reverse,
    )[:limit]
    ranked_set = set(ranked_categories)
    return [row for row in rows if ("" if row.get(category_field) is None else str(row.get(category_field))) in ranked_set]


def _resolve_bar_orientation(categories: list[str], chart_kind: str, preferred: str | None) -> str:
    orientation = _normalize_orientation(preferred)
    if chart_kind != "bar":
        return "vertical"
    if orientation != "auto":
        return orientation
    max_length = max((len(category) for category in categories), default=0)
    if len(categories) > 10 or max_length > 12:
        return "horizontal"
    return "vertical"


def _normalize_chart_kind(value: Any) -> str:
    chart_kind = str(value or "").strip().lower()
    aliases = {
        "column": "bar",
        "area": "line",
        "donut": "pie",
    }
    chart_kind = aliases.get(chart_kind, chart_kind)
    if chart_kind not in SUPPORTED_CHART_KINDS:
        raise ValueError(f"Unsupported chart_kind: {value}")
    return chart_kind


def _normalize_orientation(value: Any) -> str:
    orientation = str(value or "auto").strip().lower()
    if orientation in {"auto", "vertical", "horizontal"}:
        return orientation
    return "auto"


def _normalize_label_mode(value: Any) -> str:
    label_mode = str(value or "auto").strip().lower()
    if label_mode in {"auto", "always", "hidden"}:
        return label_mode
    return "auto"


def _ordered_unique(values: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    ordered: list[Any] = []
    for value in values:
        marker = "" if value is None else str(value)
        if marker in seen:
            continue
        seen.add(marker)
        ordered.append(value)
    return ordered


def _sortable_value(value: Any) -> tuple[int, Any]:
    numeric_value = _coerce_number_or_none(value)
    if numeric_value is not None:
        return (0, numeric_value)
    return (1, "" if value is None else str(value))


def _coerce_number(value: Any) -> float:
    numeric = _coerce_number_or_none(value)
    return numeric if numeric is not None else 0.0


def _coerce_number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_positive_int(value: Any) -> int | None:
    if value in (None, "", 0):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _coalesce_field(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, dict):
            field_name = value.get("field")
            if field_name:
                return str(field_name).strip()
            continue
        text = str(value or "").strip()
        if text:
            return text
    return None


def _normalize_string_list(value: Any, *, dedupe: bool = True) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple, set)):
        candidates = list(value)
    else:
        raise ValueError("expected a string list.")

    normalized: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        text = str(item or "").strip()
        if not text:
            continue
        if dedupe:
            if text in seen:
                continue
            seen.add(text)
        normalized.append(text)
    return normalized


def mark_chart_spec_backend_managed(chart_spec: Any) -> dict[str, Any]:
    if not isinstance(chart_spec, dict):
        return {}
    cloned = json.loads(json.dumps(chart_spec, ensure_ascii=False))
    cloned[BACKEND_LAYOUT_LOCK_KEY] = True
    return cloned


def _compose_chart_title(base_title: str, suffix: str) -> str:
    normalized_title = str(base_title or "").strip()
    normalized_suffix = str(suffix or "").strip()
    if not normalized_title:
        return normalized_suffix
    if not normalized_suffix or normalized_title.endswith(f"（{normalized_suffix}）"):
        return normalized_title
    return f"{normalized_title}（{normalized_suffix}）"


def _compose_chart_description(base_description: str, fallback_sentence: str) -> str:
    normalized_description = str(base_description or "").strip()
    normalized_fallback = str(fallback_sentence or "").strip()
    if normalized_description:
        if normalized_fallback and normalized_fallback not in normalized_description:
            return f"{normalized_description} {normalized_fallback}".strip()
        return normalized_description
    return normalized_fallback


def _looks_temporal_category(categories: list[str], category_field: str | None = None) -> bool:
    field_name = str(category_field or "").strip().lower()
    if any(token in field_name for token in ("date", "time", "day", "week", "month", "year", "日期", "时间", "年月")):
        return True
    if not categories:
        return False

    matched_count = 0
    for category in categories:
        text = str(category or "").strip()
        if not text:
            continue
        if _looks_temporal_value(text):
            matched_count += 1
    return matched_count >= max(2, len([item for item in categories if str(item or "").strip()]) - 1)


def _looks_temporal_value(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    temporal_patterns = (
        r"^\d{4}-\d{2}$",
        r"^\d{4}-\d{2}-\d{2}$",
        r"^\d{4}/\d{2}$",
        r"^\d{4}/\d{2}/\d{2}$",
        r"^\d{4}年\d{1,2}月$",
        r"^\d{1,2}月$",
        r"^Q[1-4]$",
        r"^\d{4}Q[1-4]$",
    )
    return any(re.match(pattern, text) for pattern in temporal_patterns)


def _infer_cartesian_document_from_spec(
    *,
    chart_spec: dict[str, Any],
    chart_kind: str,
    fallback_title: str,
    fallback_description: str,
) -> dict[str, Any] | None:
    x_axis = _extract_first_mapping(chart_spec.get("xAxis"))
    y_axis = _extract_first_mapping(chart_spec.get("yAxis"))
    x_categories = x_axis.get("data") if isinstance(x_axis.get("data"), list) else []
    y_categories = y_axis.get("data") if isinstance(y_axis.get("data"), list) else []
    x_axis_type = str(x_axis.get("type") or ("category" if x_categories else "value")).strip().lower()
    y_axis_type = str(y_axis.get("type") or ("category" if y_categories else "value")).strip().lower()

    if x_axis_type == "category":
        orientation = "vertical"
        categories = ["" if value is None else str(value) for value in x_categories]
        value_axis_name = str(y_axis.get("name") or "").strip()
    elif y_axis_type == "category":
        orientation = "horizontal"
        categories = ["" if value is None else str(value) for value in y_categories]
        value_axis_name = str(x_axis.get("name") or "").strip()
    else:
        return None

    rows: list[dict[str, Any]] = []
    category_field = "category"
    value_field = "value"
    series_field = "series"

    for series in [item for item in (chart_spec.get("series") or []) if isinstance(item, dict)]:
        series_name = str(series.get("name") or "").strip() or value_axis_name or fallback_title or "数值"
        normalized_values = _extract_series_values(series.get("data"), categories)
        if not normalized_values:
            return None
        for category, value in normalized_values:
            row = {category_field: category, value_field: value}
            if series_name:
                row[series_field] = series_name
            rows.append(row)

    if not rows:
        return None

    unique_series = _ordered_unique([row.get(series_field) for row in rows if row.get(series_field)])
    return build_chart_document(
        chart_kind=chart_kind,
        data=rows,
        title=fallback_title,
        description=fallback_description,
        category_field=category_field,
        value_field=value_field,
        series_field=series_field if len(unique_series) > 1 else None,
        sort_field=category_field,
        sort_order="asc",
        orientation=orientation,
        label_mode="hidden",
    )


def _infer_pie_document_from_spec(
    *,
    chart_spec: dict[str, Any],
    fallback_title: str,
    fallback_description: str,
) -> dict[str, Any] | None:
    series = next((item for item in (chart_spec.get("series") or []) if isinstance(item, dict)), None)
    if not series:
        return None

    rows = []
    for item in series.get("data") or []:
        if not isinstance(item, dict):
            return None
        rows.append({
            "category": str(item.get("name") or "").strip(),
            "value": item.get("value"),
        })

    if not rows:
        return None

    return build_chart_document(
        chart_kind="pie",
        data=rows,
        title=fallback_title,
        description=fallback_description,
        category_field="category",
        value_field="value",
        top_n=DEFAULT_TOP_N,
        label_mode="hidden",
        merge_tail=True,
    )


def _extract_series_values(raw_data: Any, categories: list[str]) -> list[tuple[str, Any]]:
    if not isinstance(raw_data, list):
        return []

    pairs: list[tuple[str, Any]] = []
    if raw_data and all(isinstance(item, (list, tuple)) and len(item) >= 2 for item in raw_data):
        for item in raw_data:
            pairs.append(("" if item[0] is None else str(item[0]), item[-1]))
        return pairs

    if categories and len(categories) == len(raw_data):
        for category, value in zip(categories, raw_data):
            if isinstance(value, dict):
                pairs.append((category, value.get("value")))
            else:
                pairs.append((category, value))
        return pairs

    return []


def _extract_title_text(chart_spec: dict[str, Any]) -> str:
    title = _extract_first_mapping(chart_spec.get("title"))
    return str(title.get("text") or "").strip()


def _extract_first_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
        return {}
    if isinstance(value, dict):
        return value
    return {}
