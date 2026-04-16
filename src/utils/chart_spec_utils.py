import json
from typing import Any


def normalize_chart_spec(chart_spec: Any) -> dict[str, Any]:
    """
    统一结构化 ECharts 配置，尽量消除不同渲染环境下的展示差异。

    当前主要处理两类稳定性问题：
    1. 关闭动画，避免页面展示、图片下载、PDF 导出截到动画中间帧。
    2. 去掉部分由 pyecharts 带出的脏默认值，例如折线系列里的 lineStyle.show=false。
    """
    if not isinstance(chart_spec, dict) or not chart_spec:
        return {}

    # 深拷贝，避免直接修改调用方传入对象。
    normalized = json.loads(json.dumps(chart_spec, ensure_ascii=False))

    _disable_animation(normalized)
    _normalize_title_legend_grid(normalized)
    _normalize_axes(normalized)
    _normalize_series(normalized.get('series'))
    return normalized


def _disable_animation(option: dict[str, Any]) -> None:
    option['animation'] = False
    option['animationDuration'] = 0
    option['animationDurationUpdate'] = 0
    option['animationDelay'] = 0
    option['animationDelayUpdate'] = 0

    for axis_key in ('xAxis', 'yAxis'):
        axes = option.get(axis_key)
        if isinstance(axes, dict):
            axes = [axes]
        if not isinstance(axes, list):
            continue
        for axis in axes:
            if not isinstance(axis, dict):
                continue
            axis['animation'] = False
            axis['animationDuration'] = 0
            axis['animationDurationUpdate'] = 0
            axis['animationDelay'] = 0
            axis['animationDelayUpdate'] = 0


def _normalize_series(series_list: Any) -> None:
    if not isinstance(series_list, list):
        return

    for series in series_list:
        if not isinstance(series, dict):
            continue

        series['animation'] = False

        # 某些由 pyecharts dump 出来的 lineStyle.show=false
        # 会导致不同渲染路径下折线时而显示、时而消失。
        if series.get('type') == 'line':
            line_style = series.get('lineStyle')
            if isinstance(line_style, dict) and line_style.get('show') is False:
                line_style.pop('show', None)
                if not line_style:
                    series.pop('lineStyle', None)

        label = series.get('label')
        if isinstance(label, dict):
            label.setdefault('fontSize', 11)
            label.setdefault('overflow', 'truncate')

        label_layout = series.get('labelLayout')
        if not isinstance(label_layout, dict):
            label_layout = {}
            series['labelLayout'] = label_layout
        label_layout.setdefault('hideOverlap', True)

        if series.get('type') == 'pie':
            series.setdefault('avoidLabelOverlap', True)
            if isinstance(label, dict):
                label.setdefault('formatter', '{d}%')
            series.setdefault('minAngle', 3)
            series.setdefault('percentPrecision', 2)


def _normalize_title_legend_grid(option: dict[str, Any]) -> None:
    title = option.get('title')
    if isinstance(title, list):
        title_item = title[0] if title else {}
    elif isinstance(title, dict):
        title_item = title
    else:
        title_item = {}

    legend = option.get('legend')
    if isinstance(legend, list):
        legend_item = legend[0] if legend else {}
    elif isinstance(legend, dict):
        legend_item = legend
    else:
        legend_item = {}

    title_present = bool(title_item.get('text'))
    legend_present = bool(legend_item)

    if title_present:
        title_item.setdefault('top', 8)
        title_item.setdefault('left', 'center')
        title_item.setdefault('textStyle', {})
        if isinstance(title_item.get('textStyle'), dict):
            title_item['textStyle'].setdefault('fontSize', 16)
            title_item['textStyle'].setdefault('fontWeight', 600)

    if legend_present:
        legend_item.setdefault('type', 'scroll')
        legend_item.setdefault('left', 'center')
        legend_item.setdefault('top', 36 if title_present else 8)
        legend_item.setdefault('itemWidth', 14)
        legend_item.setdefault('itemHeight', 10)
        legend_item.setdefault('textStyle', {})
        if isinstance(legend_item.get('textStyle'), dict):
            legend_item['textStyle'].setdefault('fontSize', 11)

    if isinstance(option.get('title'), dict):
        option['title'] = title_item
    elif isinstance(option.get('title'), list) and option['title']:
        option['title'][0] = title_item

    if isinstance(option.get('legend'), dict):
        option['legend'] = legend_item
    elif isinstance(option.get('legend'), list) and option['legend']:
        option['legend'][0] = legend_item

    grid = option.get('grid')
    if isinstance(grid, list):
        grid_item = grid[0] if grid else {}
    elif isinstance(grid, dict):
        grid_item = grid
    else:
        grid_item = {}

    top_offset = 56
    if title_present and legend_present:
        top_offset = 96
    elif title_present:
        top_offset = 64
    elif legend_present:
        top_offset = 72

    grid_item.setdefault('top', top_offset)
    grid_item.setdefault('left', 56)
    grid_item.setdefault('right', 24)
    grid_item.setdefault('bottom', 56)
    grid_item.setdefault('containLabel', True)

    if isinstance(option.get('grid'), dict):
        option['grid'] = grid_item
    elif isinstance(option.get('grid'), list) and option['grid']:
        option['grid'][0] = grid_item
    else:
        option['grid'] = grid_item


def _normalize_axes(option: dict[str, Any]) -> None:
    for axis_key in ('xAxis', 'yAxis'):
        axes = option.get(axis_key)
        axis_list = axes if isinstance(axes, list) else ([axes] if isinstance(axes, dict) else [])
        for axis in axis_list:
            if not isinstance(axis, dict):
                continue

            axis.setdefault('nameGap', 18)
            axis.setdefault('axisLabel', {})
            axis_label = axis.get('axisLabel')
            if not isinstance(axis_label, dict):
                continue

            axis_label.setdefault('hideOverlap', True)
            axis_label.setdefault('fontSize', 11)
            axis_label.setdefault('margin', 10)

            data = axis.get('data')
            if axis.get('type') == 'category' and isinstance(data, list):
                max_label_length = max((len(str(item)) for item in data), default=0)
                if len(data) >= 8 or max_label_length >= 8:
                    axis_label.setdefault('rotate', 30 if max_label_length < 14 else 45)
