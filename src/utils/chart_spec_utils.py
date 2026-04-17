import json
from typing import Any


MAX_X_CATEGORIES_WITHOUT_ZOOM = 12
MAX_Y_CATEGORIES_WITHOUT_ZOOM = 8
MAX_LINE_POINTS_WITH_SYMBOL = 24
MAX_LINE_POINTS_WITH_LABEL = 12
MAX_BAR_POINTS_WITH_LABEL = 10
MAX_PIE_SLICES_WITH_LABEL = 6
MAX_PIE_SLICES_AFTER_MERGE = 8
MAX_LEGEND_ITEMS_HORIZONTAL = 8
MAX_VISIBLE_X_POINTS = 10
MAX_VISIBLE_Y_POINTS = 8


def normalize_chart_spec(chart_spec: Any) -> dict[str, Any]:
    """
    Normalize an ECharts option in a deterministic, backend-owned way.

    The goal is not to make the chart "pretty by prompt", but to make the
    returned JSON substantially safer for any frontend that renders it.
    """
    if not isinstance(chart_spec, dict) or not chart_spec:
        return {}

    option = json.loads(json.dumps(chart_spec, ensure_ascii=False))
    _disable_animation(option)
    _normalize_title_legend_grid(option)
    _normalize_axes(option)
    _normalize_tooltip(option)
    _normalize_series(option)
    _normalize_data_zoom(option)
    return option


def finalize_chart_spec(chart_spec: Any, max_rounds: int = 3) -> dict[str, Any]:
    """
    Produce the final chart_spec stored and returned by the backend.

    Flow:
    1. deterministic normalization
    2. heuristic issue detection
    3. optional browser-side validation
    4. deterministic repair
    """
    option = normalize_chart_spec(chart_spec)
    seen_snapshots: set[str] = set()

    for _ in range(max_rounds):
        snapshot = json.dumps(option, ensure_ascii=False, sort_keys=True)
        if snapshot in seen_snapshots:
            break
        seen_snapshots.add(snapshot)

        issues = collect_chart_layout_issues(option)
        browser_report = _run_browser_validation(option, issues)
        if browser_report and not browser_report.get("skipped"):
            issues.extend(browser_report.get("issues") or [])

        if not issues:
            return option

        repaired = repair_chart_spec(option, issues)
        if repaired == option:
            return option
        option = normalize_chart_spec(repaired)

    return option


def collect_chart_layout_issues(chart_spec: Any) -> list[dict[str, Any]]:
    option = normalize_chart_spec(chart_spec)
    issues: list[dict[str, Any]] = []

    x_axes = _coerce_axis_list(option.get("xAxis"))
    y_axes = _coerce_axis_list(option.get("yAxis"))
    legend_items = _extract_legend_items(option)
    legend_max_length = max((len(str(item or "")) for item in legend_items), default=0)

    for axis in x_axes:
        data = axis.get("data") if isinstance(axis.get("data"), list) else []
        max_label_length = max((len(str(item or "")) for item in data), default=0)
        if len(data) > MAX_X_CATEGORIES_WITHOUT_ZOOM:
            issues.append({
                "code": "dense_x_categories",
                "message": f"xAxis category count is {len(data)}.",
                "severity": "warning",
            })
        if max_label_length > 14:
            issues.append({
                "code": "long_x_labels",
                "message": f"xAxis max label length is {max_label_length}.",
                "severity": "warning",
            })

    for axis in y_axes:
        data = axis.get("data") if isinstance(axis.get("data"), list) else []
        max_label_length = max((len(str(item or "")) for item in data), default=0)
        if axis.get("type") == "category" and len(data) > MAX_Y_CATEGORIES_WITHOUT_ZOOM:
            issues.append({
                "code": "dense_y_categories",
                "message": f"yAxis category count is {len(data)}.",
                "severity": "warning",
            })
        if axis.get("type") == "category" and max_label_length > 18:
            issues.append({
                "code": "long_y_labels",
                "message": f"yAxis max label length is {max_label_length}.",
                "severity": "warning",
            })

    if len(legend_items) > MAX_LEGEND_ITEMS_HORIZONTAL or legend_max_length > 14:
        issues.append({
            "code": "crowded_legend",
            "message": f"legend count={len(legend_items)} max_length={legend_max_length}",
            "severity": "warning",
        })

    series_list = option.get("series") if isinstance(option.get("series"), list) else []
    for series in series_list:
        if not isinstance(series, dict):
            continue
        series_type = str(series.get("type") or "").strip().lower()
        data_points = _series_data_length(series)

        if series_type == "line" and data_points > MAX_LINE_POINTS_WITH_LABEL:
            issues.append({
                "code": "dense_line_points",
                "message": f"line series contains {data_points} points.",
                "severity": "warning",
            })
        if series_type == "bar" and data_points > MAX_BAR_POINTS_WITH_LABEL:
            issues.append({
                "code": "dense_bar_points",
                "message": f"bar series contains {data_points} points.",
                "severity": "warning",
            })
        if series_type == "pie" and data_points > MAX_PIE_SLICES_WITH_LABEL:
            issues.append({
                "code": "crowded_pie",
                "message": f"pie series contains {data_points} slices.",
                "severity": "warning",
            })

        label = series.get("label")
        if isinstance(label, dict) and label.get("show") and data_points > MAX_BAR_POINTS_WITH_LABEL:
            issues.append({
                "code": "dense_series_labels",
                "message": f"{series_type} series labels are likely too dense.",
                "severity": "warning",
            })

    return _deduplicate_issues(issues)


def repair_chart_spec(chart_spec: Any, issues: list[dict[str, Any]]) -> dict[str, Any]:
    option = normalize_chart_spec(chart_spec)
    codes = {str(item.get("code") or "") for item in issues if isinstance(item, dict)}

    if {"dense_x_categories", "long_x_labels", "browser_text_overlap", "browser_text_overflow"} & codes:
        _tighten_x_axes(option)
    if {"dense_y_categories", "long_y_labels", "browser_text_overlap", "browser_text_overflow"} & codes:
        _tighten_y_axes(option)
    if {"crowded_legend", "browser_text_overlap", "browser_text_overflow"} & codes:
        _tighten_legend(option)
    if {"dense_series_labels", "dense_line_points", "dense_bar_points", "browser_text_overlap"} & codes:
        _hide_dense_series_labels(option)
    if {"crowded_pie", "browser_text_overlap"} & codes:
        _tighten_pies(option)

    _normalize_title_legend_grid(option)
    _normalize_axes(option)
    _normalize_series(option)
    _normalize_data_zoom(option)
    return option


def estimate_chart_height(chart_spec: Any) -> int:
    option = normalize_chart_spec(chart_spec)
    height = 360

    title = _extract_first_mapping(option.get("title"))
    legend = _extract_first_mapping(option.get("legend"))
    x_axis = _extract_first_mapping(option.get("xAxis"))
    y_axis = _extract_first_mapping(option.get("yAxis"))
    data_zoom = option.get("dataZoom") if isinstance(option.get("dataZoom"), list) else []
    series_list = option.get("series") if isinstance(option.get("series"), list) else []

    if title.get("text"):
        height += 24

    legend_items = legend.get("data") if isinstance(legend.get("data"), list) else []
    if legend:
        if legend.get("orient") == "vertical":
            height += 24
        else:
            height += 56 if len(legend_items) > 6 else 32

    x_categories = x_axis.get("data") if isinstance(x_axis.get("data"), list) else []
    x_label_max_length = max((len(str(item or "")) for item in x_categories), default=0)
    if len(x_categories) > 8 or x_label_max_length > 8:
        height += 48
    if any(isinstance(item, dict) and item.get("xAxisIndex") is not None for item in data_zoom):
        height += 36

    y_categories = y_axis.get("data") if isinstance(y_axis.get("data"), list) else []
    if y_axis.get("type") == "category" and len(y_categories) > 6:
        height = max(height, 420)
    if any(isinstance(item, dict) and item.get("yAxisIndex") is not None for item in data_zoom):
        height = max(height, 420)

    if any(str(series.get("type") or "").lower() == "pie" for series in series_list if isinstance(series, dict)):
        pie_slices = max((_series_data_length(series) for series in series_list if isinstance(series, dict)), default=0)
        height = max(height, 460 if pie_slices > 5 else 400)

    return min(max(height, 320), 720)


def _run_browser_validation(option: dict[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not _should_run_browser_validation(option, issues):
        return None

    try:
        from utils.chart_export_utils import validate_chart_spec_layout
    except Exception:
        return None

    try:
        return validate_chart_spec_layout(option, timeout_ms=12000)
    except Exception:
        return None


def _should_run_browser_validation(option: dict[str, Any], issues: list[dict[str, Any]]) -> bool:
    if issues:
        return True

    legend_items = _extract_legend_items(option)
    if len(legend_items) > 6:
        return True

    for axis in _coerce_axis_list(option.get("xAxis")) + _coerce_axis_list(option.get("yAxis")):
        data = axis.get("data") if isinstance(axis.get("data"), list) else []
        if len(data) > 8:
            return True

    for series in option.get("series") if isinstance(option.get("series"), list) else []:
        if not isinstance(series, dict):
            continue
        if _series_data_length(series) > 10:
            return True

    return False


def _disable_animation(option: dict[str, Any]) -> None:
    option["animation"] = False
    option["animationDuration"] = 0
    option["animationDurationUpdate"] = 0
    option["animationDelay"] = 0
    option["animationDelayUpdate"] = 0

    for axis in _coerce_axis_list(option.get("xAxis")) + _coerce_axis_list(option.get("yAxis")):
        axis["animation"] = False
        axis["animationDuration"] = 0
        axis["animationDurationUpdate"] = 0
        axis["animationDelay"] = 0
        axis["animationDelayUpdate"] = 0


def _normalize_title_legend_grid(option: dict[str, Any]) -> None:
    title = _extract_first_mapping(option.get("title"))
    legend = _extract_first_mapping(option.get("legend"))
    grid = _extract_first_mapping(option.get("grid"))

    title_present = bool(title.get("text"))
    legend_items = _extract_legend_items(option)
    legend_present = bool(legend_items or legend)
    legend_vertical = len(legend_items) > 10 or max((len(str(item or "")) for item in legend_items), default=0) > 14

    if title_present:
        title.setdefault("top", 8)
        title.setdefault("left", "center")
        title.setdefault("padding", [0, 12, 0, 12])
        title.setdefault("textStyle", {})
        if isinstance(title.get("textStyle"), dict):
            title["textStyle"].setdefault("fontSize", 16)
            title["textStyle"].setdefault("fontWeight", 600)
            title["textStyle"].setdefault("overflow", "truncate")
            title["textStyle"].setdefault("width", 640)

    if legend_present:
        legend.setdefault("type", "scroll")
        legend.setdefault("textStyle", {})
        legend_text_style = legend["textStyle"] if isinstance(legend.get("textStyle"), dict) else {}
        legend_text_style.setdefault("fontSize", 11)
        legend_text_style.setdefault("overflow", "truncate")
        legend_text_style.setdefault("width", 96 if legend_vertical else 84)
        legend["textStyle"] = legend_text_style
        legend.setdefault("itemWidth", 14)
        legend.setdefault("itemHeight", 10)
        legend.setdefault("pageIconSize", 10)
        legend.setdefault("pageTextStyle", {"fontSize": 10})

        if legend_vertical:
            legend["orient"] = "vertical"
            legend.setdefault("right", 8)
            legend.setdefault("top", "middle")
            legend.setdefault("height", "70%")
        else:
            legend.setdefault("orient", "horizontal")
            legend.setdefault("left", "center")
            legend.setdefault("top", 36 if title_present else 8)

    top_offset = 56
    if title_present and legend_present and not legend_vertical:
        top_offset = 96
    elif title_present:
        top_offset = 64
    elif legend_present and not legend_vertical:
        top_offset = 72

    bottom_offset = 64
    x_axis = _extract_first_mapping(option.get("xAxis"))
    x_categories = x_axis.get("data") if isinstance(x_axis.get("data"), list) else []
    x_label_max_length = max((len(str(item or "")) for item in x_categories), default=0)
    if len(x_categories) > 8 or x_label_max_length > 8:
        bottom_offset += 28
    if _has_axis_data_zoom(option, "xAxis"):
        bottom_offset += 34

    left_offset = 64
    y_axis = _extract_first_mapping(option.get("yAxis"))
    y_categories = y_axis.get("data") if isinstance(y_axis.get("data"), list) else []
    y_label_max_length = max((len(str(item or "")) for item in y_categories), default=0)
    if y_axis.get("type") == "category" and y_label_max_length > 12:
        left_offset = min(140, 64 + (y_label_max_length - 12) * 4)

    right_offset = 24
    if legend_vertical:
        right_offset = 148
    if _has_axis_data_zoom(option, "yAxis"):
        right_offset = max(right_offset, 54)

    grid.setdefault("top", top_offset)
    grid.setdefault("left", left_offset)
    grid.setdefault("right", right_offset)
    grid.setdefault("bottom", bottom_offset)
    grid.setdefault("containLabel", True)

    _assign_option_mapping(option, "title", title)
    _assign_option_mapping(option, "legend", legend)
    _assign_option_mapping(option, "grid", grid)


def _normalize_axes(option: dict[str, Any]) -> None:
    for axis_key in ("xAxis", "yAxis"):
        axis_list = _coerce_axis_list(option.get(axis_key))
        for axis in axis_list:
            axis.setdefault("nameGap", 18)
            axis.setdefault("axisLabel", {})
            axis_label = axis["axisLabel"] if isinstance(axis.get("axisLabel"), dict) else {}
            axis_label.setdefault("hideOverlap", True)
            axis_label.setdefault("fontSize", 11)
            axis_label.setdefault("margin", 10)
            axis_label.setdefault("overflow", "truncate")

            categories = axis.get("data") if isinstance(axis.get("data"), list) else []
            max_label_length = max((len(str(item or "")) for item in categories), default=0)

            if axis.get("type") == "category":
                if axis_key == "xAxis":
                    if max_label_length > 12:
                        axis_label.setdefault("width", 84)
                    if len(categories) > 8 or max_label_length > 8:
                        axis_label.setdefault("rotate", 30 if max_label_length < 14 else 45)
                    if len(categories) > 16:
                        axis_label.setdefault("interval", max((len(categories) // 8) - 1, 0))
                else:
                    axis_label.setdefault("width", 120 if max_label_length > 20 else 96)
                    if len(categories) > 10:
                        axis_label.setdefault("interval", max((len(categories) // 8) - 1, 0))

            axis["axisLabel"] = axis_label
            axis.setdefault("axisTick", {})
            if isinstance(axis.get("axisTick"), dict):
                axis["axisTick"].setdefault("alignWithLabel", True)
            axis.setdefault("splitLine", {})
            if isinstance(axis.get("splitLine"), dict) and axis.get("type") in {"value", "log"}:
                axis["splitLine"].setdefault("show", True)

        _assign_option_axis_list(option, axis_key, axis_list)


def _normalize_tooltip(option: dict[str, Any]) -> None:
    tooltip = option.get("tooltip")
    if not isinstance(tooltip, dict):
        tooltip = {}
        option["tooltip"] = tooltip
    tooltip.setdefault("trigger", "axis")
    tooltip.setdefault("confine", True)


def _normalize_series(option: dict[str, Any]) -> None:
    series_list = option.get("series")
    if not isinstance(series_list, list):
        return

    for series in series_list:
        if not isinstance(series, dict):
            continue

        series_type = str(series.get("type") or "").strip().lower()
        data_points = _series_data_length(series)
        series["animation"] = False

        label = series.get("label")
        if not isinstance(label, dict):
            label = {}
            series["label"] = label
        label.setdefault("fontSize", 11)
        label.setdefault("overflow", "truncate")

        label_layout = series.get("labelLayout")
        if not isinstance(label_layout, dict):
            label_layout = {}
            series["labelLayout"] = label_layout
        label_layout.setdefault("hideOverlap", True)

        if series_type == "line":
            line_style = series.get("lineStyle")
            if isinstance(line_style, dict) and line_style.get("show") is False:
                line_style.pop("show", None)
                if not line_style:
                    series.pop("lineStyle", None)
            if data_points > MAX_LINE_POINTS_WITH_SYMBOL:
                series["showSymbol"] = False
            if data_points > 80:
                series.setdefault("sampling", "lttb")
            if data_points > MAX_LINE_POINTS_WITH_LABEL:
                label["show"] = False

        if series_type == "bar":
            series.setdefault("barMinHeight", 6)
            if data_points > MAX_BAR_POINTS_WITH_LABEL:
                label["show"] = False
            if data_points > 16:
                series.setdefault("barMaxWidth", 28)

        if series_type in {"scatter", "effectscatter"} and data_points > 20:
            label["show"] = False

        if series_type == "pie":
            _normalize_pie_series(series)


def _normalize_pie_series(series: dict[str, Any]) -> None:
    data = series.get("data")
    if not isinstance(data, list):
        return

    merged_data = _merge_pie_tail(data)
    if merged_data != data:
        series["data"] = merged_data
        data = merged_data

    label = series.get("label") if isinstance(series.get("label"), dict) else {}
    label_line = series.get("labelLine") if isinstance(series.get("labelLine"), dict) else {}

    slice_count = len(data)
    series.setdefault("avoidLabelOverlap", True)
    series.setdefault("minAngle", 3)
    series.setdefault("percentPrecision", 2)
    series.setdefault("radius", ["30%", "68%"])

    label.setdefault("overflow", "truncate")
    label.setdefault("width", 120)
    label.setdefault("bleedMargin", 4)

    if slice_count > MAX_PIE_SLICES_WITH_LABEL:
        label["show"] = False
        label_line["show"] = False
    else:
        label.setdefault("show", True)
        label_line.setdefault("show", True)
        label.setdefault("formatter", "{b}: {d}%")

    series["label"] = label
    series["labelLine"] = label_line


def _normalize_data_zoom(option: dict[str, Any]) -> None:
    x_categories = _first_axis_category_count(option, "xAxis")
    y_categories = _first_axis_category_count(option, "yAxis")

    if x_categories > MAX_X_CATEGORIES_WITHOUT_ZOOM:
        _ensure_axis_data_zoom(option, "xAxis", x_categories, MAX_VISIBLE_X_POINTS)
    if y_categories > MAX_Y_CATEGORIES_WITHOUT_ZOOM:
        _ensure_axis_data_zoom(option, "yAxis", y_categories, MAX_VISIBLE_Y_POINTS)


def _tighten_x_axes(option: dict[str, Any]) -> None:
    axis_list = _coerce_axis_list(option.get("xAxis"))
    for axis in axis_list:
        categories = axis.get("data") if isinstance(axis.get("data"), list) else []
        if axis.get("type") != "category" or not categories:
            continue
        axis_label = axis.get("axisLabel") if isinstance(axis.get("axisLabel"), dict) else {}
        axis_label["rotate"] = 45 if len(categories) <= 18 else 60
        axis_label["overflow"] = "truncate"
        axis_label["width"] = 72
        axis_label["interval"] = max((len(categories) // 6) - 1, 0) if len(categories) > 12 else 0
        axis["axisLabel"] = axis_label
        if len(categories) > MAX_X_CATEGORIES_WITHOUT_ZOOM:
            _ensure_axis_data_zoom(option, "xAxis", len(categories), MAX_VISIBLE_X_POINTS)
    _assign_option_axis_list(option, "xAxis", axis_list)


def _tighten_y_axes(option: dict[str, Any]) -> None:
    axis_list = _coerce_axis_list(option.get("yAxis"))
    for axis in axis_list:
        categories = axis.get("data") if isinstance(axis.get("data"), list) else []
        if axis.get("type") != "category" or not categories:
            continue
        axis_label = axis.get("axisLabel") if isinstance(axis.get("axisLabel"), dict) else {}
        axis_label["overflow"] = "truncate"
        axis_label["width"] = 96
        axis_label["interval"] = max((len(categories) // 6) - 1, 0) if len(categories) > 10 else 0
        axis["axisLabel"] = axis_label
        if len(categories) > MAX_Y_CATEGORIES_WITHOUT_ZOOM:
            _ensure_axis_data_zoom(option, "yAxis", len(categories), MAX_VISIBLE_Y_POINTS)
    _assign_option_axis_list(option, "yAxis", axis_list)


def _tighten_legend(option: dict[str, Any]) -> None:
    legend = _extract_first_mapping(option.get("legend"))
    legend_items = _extract_legend_items(option)

    legend["type"] = "scroll"
    legend.setdefault("textStyle", {})
    if isinstance(legend.get("textStyle"), dict):
        legend["textStyle"]["fontSize"] = 11
        legend["textStyle"]["overflow"] = "truncate"
        legend["textStyle"]["width"] = 88

    if len(legend_items) > MAX_LEGEND_ITEMS_HORIZONTAL:
        legend["orient"] = "vertical"
        legend["right"] = 8
        legend["top"] = "middle"
        legend["height"] = "70%"
    else:
        legend["orient"] = "horizontal"
        legend.setdefault("left", "center")

    _assign_option_mapping(option, "legend", legend)


def _hide_dense_series_labels(option: dict[str, Any]) -> None:
    series_list = option.get("series")
    if not isinstance(series_list, list):
        return

    for series in series_list:
        if not isinstance(series, dict):
            continue
        label = series.get("label")
        if not isinstance(label, dict):
            label = {}
            series["label"] = label
        label["show"] = False

        series_type = str(series.get("type") or "").strip().lower()
        if series_type == "line":
            series["showSymbol"] = False
        if series_type == "pie":
            label_line = series.get("labelLine")
            if not isinstance(label_line, dict):
                label_line = {}
            label_line["show"] = False
            series["labelLine"] = label_line


def _tighten_pies(option: dict[str, Any]) -> None:
    series_list = option.get("series")
    if not isinstance(series_list, list):
        return
    for series in series_list:
        if not isinstance(series, dict) or str(series.get("type") or "").strip().lower() != "pie":
            continue
        _normalize_pie_series(series)
        label = series.get("label") if isinstance(series.get("label"), dict) else {}
        label["show"] = False
        series["label"] = label
        label_line = series.get("labelLine") if isinstance(series.get("labelLine"), dict) else {}
        label_line["show"] = False
        series["labelLine"] = label_line


def _merge_pie_tail(data: list[Any]) -> list[Any]:
    if len(data) <= MAX_PIE_SLICES_AFTER_MERGE:
        return data

    parsed_items: list[tuple[dict[str, Any], float, int]] = []
    for index, item in enumerate(data):
        if isinstance(item, dict):
            payload = dict(item)
        else:
            payload = {"name": str(item), "value": 0}
        value = _to_float(payload.get("value"))
        parsed_items.append((payload, value, index))

    parsed_items.sort(key=lambda item: item[1], reverse=True)
    kept_items = parsed_items[: MAX_PIE_SLICES_AFTER_MERGE - 1]
    merged_items = parsed_items[MAX_PIE_SLICES_AFTER_MERGE - 1 :]
    other_value = sum(item[1] for item in merged_items)

    result = [item[0] for item in kept_items]
    if other_value > 0:
        result.append({
            "name": "其他",
            "value": other_value,
        })
    return result


def _ensure_axis_data_zoom(
    option: dict[str, Any],
    axis_key: str,
    category_count: int,
    visible_count: int,
) -> None:
    if category_count <= 0:
        return

    axis_index_key = "xAxisIndex" if axis_key == "xAxis" else "yAxisIndex"
    data_zoom_list = option.get("dataZoom")
    if not isinstance(data_zoom_list, list):
        data_zoom_list = []
        option["dataZoom"] = data_zoom_list

    end_percentage = round(min(100.0, max(visible_count / max(category_count, 1), 0.1) * 100.0), 2)

    def has_zoom(zoom_type: str) -> bool:
        for item in data_zoom_list:
            if not isinstance(item, dict):
                continue
            if item.get("type") != zoom_type:
                continue
            if item.get(axis_index_key, 0) == 0:
                return True
        return False

    if not has_zoom("inside"):
        data_zoom_list.append({
            "type": "inside",
            axis_index_key: 0,
            "start": 0,
            "end": end_percentage,
            "zoomLock": False,
        })

    if not has_zoom("slider"):
        slider = {
            "type": "slider",
            axis_index_key: 0,
            "start": 0,
            "end": end_percentage,
            "showDetail": False,
            "moveHandleSize": 0,
            "brushSelect": False,
        }
        if axis_key == "xAxis":
            slider.update({
                "bottom": 8,
                "height": 18,
            })
        else:
            slider.update({
                "right": 4,
                "top": 70,
                "bottom": 36,
                "width": 12,
            })
        data_zoom_list.append(slider)


def _has_axis_data_zoom(option: dict[str, Any], axis_key: str) -> bool:
    data_zoom_list = option.get("dataZoom")
    if not isinstance(data_zoom_list, list):
        return False
    axis_index_key = "xAxisIndex" if axis_key == "xAxis" else "yAxisIndex"
    return any(isinstance(item, dict) and item.get(axis_index_key) == 0 for item in data_zoom_list)


def _first_axis_category_count(option: dict[str, Any], axis_key: str) -> int:
    axis = _extract_first_mapping(option.get(axis_key))
    data = axis.get("data") if isinstance(axis.get("data"), list) else []
    if axis.get("type") != "category":
        return 0
    return len(data)


def _series_data_length(series: dict[str, Any]) -> int:
    data = series.get("data")
    if isinstance(data, list):
        return len(data)
    return 0


def _extract_legend_items(option: dict[str, Any]) -> list[str]:
    legend = _extract_first_mapping(option.get("legend"))
    if legend.get("show") is False:
        return []
    data = legend.get("data")
    if isinstance(data, list) and data:
        return [str(item or "") for item in data]

    series_list = option.get("series")
    if not isinstance(series_list, list):
        return []
    return [
        str(series.get("name") or "")
        for series in series_list
        if isinstance(series, dict) and str(series.get("name") or "").strip()
    ]


def _coerce_axis_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _extract_first_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return dict(value[0]) if value and isinstance(value[0], dict) else {}
    if isinstance(value, dict):
        return dict(value)
    return {}


def _assign_option_mapping(option: dict[str, Any], key: str, value: dict[str, Any]) -> None:
    original = option.get(key)
    if isinstance(original, list):
        option[key] = [value]
    else:
        option[key] = value


def _assign_option_axis_list(option: dict[str, Any], key: str, value: list[dict[str, Any]]) -> None:
    original = option.get(key)
    if isinstance(original, list):
        option[key] = value
    else:
        option[key] = value[0] if value else {}


def _deduplicate_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in issues:
        code = str(item.get("code") or "")
        message = str(item.get("message") or "")
        key = (code, message)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(item)
    return deduplicated


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
