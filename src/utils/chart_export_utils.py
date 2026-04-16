import json
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from utils.logger import logger


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCAL_ECHARTS_PATH = PROJECT_ROOT / 'src' / 'static' / 'vendor' / 'echarts.min.js'


def resolve_local_file_path(file_id: str) -> Path | None:
    """把文件标识解析为本地绝对路径。"""
    if not file_id:
        return None

    path = Path(file_id)
    if path.is_absolute():
        return path if path.exists() else None

    candidate = PROJECT_ROOT / file_id
    return candidate if candidate.exists() else None


def render_chart_file_to_png(file_id: str, timeout_ms: int = 30000) -> bytes | None:
    """
    使用无头浏览器把图表 HTML 渲染成 PNG。

    当前主要用于：
    1. 分析结果 PDF 导出
    2. 后续图表下载接口复用
    """
    chart_path = resolve_local_file_path(file_id)
    if chart_path is None or chart_path.suffix.lower() != '.html':
        return None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport={'width': 1080, 'height': 720},
                    device_scale_factor=2,
                    ignore_https_errors=True,
                )
                page = context.new_page()
                page.goto(chart_path.as_uri(), wait_until='networkidle', timeout=timeout_ms)
                container = page.locator('.chart-container')
                if container.count() == 0:
                    logger.warning("图表文件渲染失败: 未找到 .chart-container, file=%s", chart_path)
                    return None
                return container.screenshot(type='png')
            finally:
                browser.close()
    except Exception as exc:
        logger.warning("图表文件渲染失败: file=%s error=%s", chart_path, exc)
        return None


def render_chart_spec_to_png(chart_spec: dict, timeout_ms: int = 30000) -> bytes | None:
    """
    使用无头浏览器把 ECharts 配置直接渲染为 PNG。
    这条能力用于当前的结构化图表导出链，避免调用方必须先把图表落成 HTML 文件。
    """
    if not isinstance(chart_spec, dict) or not chart_spec:
        return None

    echarts_script = _load_local_echarts_script()
    if not echarts_script:
        logger.warning("图表渲染失败: 本地 echarts.min.js 资源不存在, path=%s", LOCAL_ECHARTS_PATH)
        return None

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>chart-export</title>
  <script>{echarts_script}</script>
  <style>
    html, body {{
      margin: 0;
      padding: 0;
      width: 100%;
      height: 100%;
      background: #ffffff;
    }}
    .chart-container {{
      width: 1080px;
      height: 720px;
      background: #ffffff;
    }}
  </style>
</head>
<body>
  <div id="chart" class="chart-container"></div>
  <script>
    function formatChartNumber(value) {{
      if (value === null || value === undefined || value === '') return '';
      const numericValue = typeof value === 'number' ? value : Number(value);
      if (!Number.isFinite(numericValue)) return value;
      if (Number.isInteger(numericValue)) return numericValue.toLocaleString('zh-CN');
      return numericValue.toLocaleString('zh-CN', {{
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
      }});
    }}

    function enhanceOption(option) {{
      const title = Array.isArray(option.title) ? option.title[0] : option.title;
      const legend = Array.isArray(option.legend) ? option.legend[0] : option.legend;
      const titleItem = title && typeof title === 'object' ? title : {{}};
      const legendItem = legend && typeof legend === 'object' ? legend : {{}};
      const titlePresent = Boolean(titleItem.text);
      const legendPresent = Boolean(Object.keys(legendItem).length);

      if (titlePresent) {{
        titleItem.top = titleItem.top ?? 8;
        titleItem.left = titleItem.left ?? 'center';
        titleItem.textStyle = titleItem.textStyle && typeof titleItem.textStyle === 'object' ? titleItem.textStyle : {{}};
        titleItem.textStyle.fontSize = titleItem.textStyle.fontSize || 16;
        titleItem.textStyle.fontWeight = titleItem.textStyle.fontWeight || 600;
      }}

      if (legendPresent) {{
        legendItem.type = legendItem.type || 'scroll';
        legendItem.left = legendItem.left ?? 'center';
        legendItem.top = legendItem.top ?? (titlePresent ? 36 : 8);
        legendItem.itemWidth = legendItem.itemWidth || 14;
        legendItem.itemHeight = legendItem.itemHeight || 10;
        legendItem.textStyle = legendItem.textStyle && typeof legendItem.textStyle === 'object' ? legendItem.textStyle : {{}};
        legendItem.textStyle.fontSize = legendItem.textStyle.fontSize || 11;
      }}

      if (Array.isArray(option.title)) option.title[0] = titleItem;
      else if (titlePresent) option.title = titleItem;

      if (Array.isArray(option.legend)) option.legend[0] = legendItem;
      else if (legendPresent) option.legend = legendItem;

      const currentGrid = Array.isArray(option.grid) ? option.grid[0] : option.grid;
      const gridItem = currentGrid && typeof currentGrid === 'object' ? currentGrid : {{}};
      let topOffset = 56;
      if (titlePresent && legendPresent) topOffset = 96;
      else if (titlePresent) topOffset = 64;
      else if (legendPresent) topOffset = 72;
      gridItem.top = gridItem.top ?? topOffset;
      gridItem.left = gridItem.left ?? 56;
      gridItem.right = gridItem.right ?? 24;
      gridItem.bottom = gridItem.bottom ?? 56;
      gridItem.containLabel = gridItem.containLabel ?? true;
      if (Array.isArray(option.grid)) option.grid[0] = gridItem;
      else option.grid = gridItem;

      ['xAxis', 'yAxis'].forEach((axisKey) => {{
        const axes = Array.isArray(option[axisKey]) ? option[axisKey] : (option[axisKey] ? [option[axisKey]] : []);
        axes.forEach((axis) => {{
          if (!axis || typeof axis !== 'object') return;
          axis.axisLabel = axis.axisLabel && typeof axis.axisLabel === 'object' ? axis.axisLabel : {{}};
          axis.axisLabel.hideOverlap = true;
          axis.axisLabel.fontSize = axis.axisLabel.fontSize || 11;
          axis.axisLabel.margin = axis.axisLabel.margin || 10;
          if (axis.type === 'value' || axis.type === 'log') {{
            axis.axisLabel.formatter = (value) => formatChartNumber(value);
          }}
          const categoryData = Array.isArray(axis.data) ? axis.data : [];
          const maxLabelLength = categoryData.reduce((max, item) => Math.max(max, String(item ?? '').length), 0);
          if (axis.type === 'category' && (categoryData.length > 8 || maxLabelLength > 8)) {{
            axis.axisLabel.rotate = axis.axisLabel.rotate || (maxLabelLength > 14 ? 45 : 30);
          }}
        }});
      }});

      option.tooltip = option.tooltip && typeof option.tooltip === 'object' ? option.tooltip : {{}};
      if (typeof option.tooltip.formatter !== 'function') {{
        option.tooltip.formatter = (params) => {{
          const rows = Array.isArray(params) ? params : [params];
          return rows
            .filter(Boolean)
            .map((item) => {{
              const marker = item.marker || '';
              const seriesName = item.seriesName || item.name || '';
              const rawValue = Array.isArray(item.value) ? item.value[item.value.length - 1] : item.value;
              return `${{marker}}${{seriesName}}: ${{formatChartNumber(rawValue)}}`;
            }})
            .join('<br/>');
        }};
      }}

      if (Array.isArray(option.series)) {{
        option.series.forEach((series) => {{
          if (!series || typeof series !== 'object') return;
          series.label = series.label && typeof series.label === 'object' ? series.label : {{}};
          series.label.fontSize = series.label.fontSize || 11;
          series.label.overflow = series.label.overflow || 'truncate';
          series.labelLayout = series.labelLayout && typeof series.labelLayout === 'object' ? series.labelLayout : {{}};
          series.labelLayout.hideOverlap = true;
          if (series.type === 'bar') {{
            series.barMinHeight = Math.max(series.barMinHeight || 0, 6);
          }}
          if (series.type === 'pie') {{
            series.avoidLabelOverlap = true;
            series.minAngle = Math.max(series.minAngle || 0, 3);
            series.percentPrecision = series.percentPrecision || 2;
          }}
        }});
      }}

      return option;
    }}

    function estimateChartHeight(option) {{
      let height = 360;
      const title = Array.isArray(option.title) ? option.title[0] : option.title;
      const legend = Array.isArray(option.legend) ? option.legend[0] : option.legend;
      const xAxis = Array.isArray(option.xAxis) ? option.xAxis[0] : option.xAxis;
      const seriesList = Array.isArray(option.series) ? option.series : [];
      if (title && title.text) height += 24;
      const legendData = Array.isArray(legend?.data) ? legend.data : [];
      if (legend) height += legendData.length > 6 ? 56 : 32;
      const categoryData = Array.isArray(xAxis?.data) ? xAxis.data : [];
      const maxLabelLength = categoryData.reduce((max, item) => Math.max(max, String(item ?? '').length), 0);
      if (categoryData.length > 8 || maxLabelLength > 8) height += 48;
      if (seriesList.some((series) => series?.type === 'pie')) {{
        height = Math.max(height, legendData.length > 5 ? 460 : 400);
      }}
      return Math.min(Math.max(height, 320), 560);
    }}

    const option = {json.dumps(chart_spec, ensure_ascii=False)};
    const enhancedOption = enhanceOption(option);
    const chartContainer = document.getElementById('chart');
    chartContainer.style.height = `${{estimateChartHeight(enhancedOption)}}px`;
    const chart = echarts.init(chartContainer, null, {{ renderer: 'canvas' }});
    chart.setOption(enhancedOption, true);
  </script>
</body>
</html>"""

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile('w', suffix='.html', delete=False, encoding='utf-8') as temp_file:
            temp_file.write(html)
            temp_path = Path(temp_file.name)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport={'width': 1080, 'height': 720},
                    device_scale_factor=2,
                    ignore_https_errors=True,
                )
                page = context.new_page()
                page.goto(temp_path.as_uri(), wait_until='networkidle', timeout=timeout_ms)
                container = page.locator('.chart-container')
                if container.count() == 0:
                    logger.warning("图表配置渲染失败: 未找到 .chart-container")
                    return None
                return container.screenshot(type='png')
            finally:
                browser.close()
    except Exception as exc:
        logger.warning("图表配置渲染失败: error=%s", exc)
        return None
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _load_local_echarts_script() -> str:
    if not LOCAL_ECHARTS_PATH.exists():
        return ''
    try:
        return LOCAL_ECHARTS_PATH.read_text(encoding='utf-8')
    except OSError as exc:
        logger.warning("读取本地 echarts 资源失败: path=%s error=%s", LOCAL_ECHARTS_PATH, exc)
        return ''
