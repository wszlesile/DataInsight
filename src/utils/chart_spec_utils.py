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
