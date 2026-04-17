import json
import tempfile
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from utils.logger import logger


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCAL_ECHARTS_PATH = PROJECT_ROOT / "src" / "static" / "vendor" / "echarts.min.js"


def resolve_local_file_path(file_id: str) -> Path | None:
    """Resolve a chart file id into an absolute local path."""
    if not file_id:
        return None

    path = Path(file_id)
    if path.is_absolute():
        return path if path.exists() else None

    candidate = PROJECT_ROOT / file_id
    return candidate if candidate.exists() else None


def render_chart_file_to_png(file_id: str, timeout_ms: int = 30000) -> bytes | None:
    """
    Render an HTML chart file into a PNG.

    This path is mainly used by PDF export and any future static image downloads.
    """
    chart_path = resolve_local_file_path(file_id)
    if chart_path is None or chart_path.suffix.lower() != ".html":
        return None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport={"width": 1080, "height": 720},
                    device_scale_factor=2,
                    ignore_https_errors=True,
                )
                page = context.new_page()
                page.goto(chart_path.as_uri(), wait_until="networkidle", timeout=timeout_ms)
                container = page.locator(".chart-container")
                if container.count() == 0:
                    logger.warning("Chart render skipped: .chart-container not found, file=%s", chart_path)
                    return None
                return container.screenshot(type="png")
            finally:
                browser.close()
    except Exception as exc:
        logger.warning("Chart file render failed: file=%s error=%s", chart_path, exc)
        return None


def render_chart_spec_to_png(chart_spec: dict[str, Any], timeout_ms: int = 30000) -> bytes | None:
    """
    Render an ECharts option JSON directly into a PNG.

    The backend uses this for exports so callers do not need to materialize an
    intermediate HTML file.
    """
    if not isinstance(chart_spec, dict) or not chart_spec:
        return None

    runtime_page = _open_chart_runtime_page(chart_spec=chart_spec, timeout_ms=timeout_ms)
    if runtime_page is None:
        return None

    browser, container = runtime_page
    try:
        return container.screenshot(type="png")
    except Exception as exc:
        logger.warning("Chart spec render failed while taking screenshot: error=%s", exc)
        return None
    finally:
        browser.close()


def validate_chart_spec_layout(chart_spec: dict[str, Any], timeout_ms: int = 15000) -> dict[str, Any]:
    """
    Validate a chart option by rendering it in a headless browser and inspecting
    the rendered text boxes.
    """
    default_result = {
        "passed": True,
        "issues": [],
        "metrics": {},
        "skipped": False,
    }
    if not isinstance(chart_spec, dict) or not chart_spec:
        return {
            "passed": True,
            "issues": [],
            "metrics": {},
            "skipped": True,
        }

    echarts_script = _load_local_echarts_script()
    if not echarts_script:
        return {
            "passed": True,
            "issues": [],
            "metrics": {},
            "skipped": True,
        }

    html = _build_chart_runtime_html(chart_spec)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as temp_file:
            temp_file.write(html)
            temp_path = Path(temp_file.name)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport={"width": 1080, "height": 900},
                    device_scale_factor=2,
                    ignore_https_errors=True,
                )
                page = context.new_page()
                page.goto(temp_path.as_uri(), wait_until="networkidle", timeout=timeout_ms)
                page.wait_for_function("window.__chartValidationResult !== undefined", timeout=timeout_ms)
                payload = page.evaluate("window.__chartValidationResult")
                if isinstance(payload, dict):
                    payload.setdefault("skipped", False)
                    payload.setdefault("passed", True)
                    payload.setdefault("issues", [])
                    payload.setdefault("metrics", {})
                    return payload
            finally:
                browser.close()
    except Exception as exc:
        logger.warning("Chart layout validation skipped: error=%s", exc)
        return {
            "passed": True,
            "issues": [],
            "metrics": {},
            "skipped": True,
        }
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass

    return default_result


def _open_chart_runtime_page(chart_spec: dict[str, Any], timeout_ms: int) -> tuple[Any, Any] | None:
    echarts_script = _load_local_echarts_script()
    if not echarts_script:
        logger.warning("Chart render failed: local echarts.min.js missing, path=%s", LOCAL_ECHARTS_PATH)
        return None

    html = _build_chart_runtime_html(chart_spec)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as temp_file:
            temp_file.write(html)
            temp_path = Path(temp_file.name)

        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1080, "height": 900},
            device_scale_factor=2,
            ignore_https_errors=True,
        )
        page = context.new_page()
        page.goto(temp_path.as_uri(), wait_until="networkidle", timeout=timeout_ms)
        container = page.locator(".chart-container")
        if container.count() == 0:
            logger.warning("Chart render failed: .chart-container not found")
            browser.close()
            playwright.stop()
            return None

        class ManagedBrowser:
            def __init__(self, browser_ref, context_ref, playwright_ref, file_path):
                self._browser = browser_ref
                self._context = context_ref
                self._playwright = playwright_ref
                self._file_path = file_path

            def close(self):
                try:
                    self._context.close()
                finally:
                    try:
                        self._browser.close()
                    finally:
                        self._playwright.stop()
                        if self._file_path and self._file_path.exists():
                            try:
                                self._file_path.unlink()
                            except OSError:
                                pass

        return ManagedBrowser(browser, context, playwright, temp_path), container
    except Exception as exc:
        logger.warning("Chart runtime page setup failed: error=%s", exc)
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        return None


def _build_chart_runtime_html(chart_spec: dict[str, Any]) -> str:
    echarts_script = _load_local_echarts_script()
    option_json = json.dumps(chart_spec, ensure_ascii=False)

    template = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>chart-runtime</title>
  <script>__ECHARTS_SCRIPT__</script>
  <style>
    html, body {
      margin: 0;
      padding: 0;
      width: 100%;
      height: 100%;
      background: #ffffff;
      overflow: hidden;
    }
    .chart-container {
      width: 1080px;
      min-height: 320px;
      background: #ffffff;
    }
  </style>
</head>
<body>
  <div id="chart" class="chart-container"></div>
  <script>
    function formatChartNumber(value) {
      if (value === null || value === undefined || value === '') return '';
      const numericValue = typeof value === 'number' ? value : Number(value);
      if (!Number.isFinite(numericValue)) return value;
      if (Number.isInteger(numericValue)) return numericValue.toLocaleString('zh-CN');
      return numericValue.toLocaleString('zh-CN', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
      });
    }

    function enhanceOption(option) {
      const cloned = JSON.parse(JSON.stringify(option || {}));

      cloned.animation = false;
      cloned.animationDuration = 0;
      cloned.animationDurationUpdate = 0;
      cloned.animationDelay = 0;
      cloned.animationDelayUpdate = 0;

      const title = Array.isArray(cloned.title) ? cloned.title[0] : cloned.title;
      const legend = Array.isArray(cloned.legend) ? cloned.legend[0] : cloned.legend;
      const titleItem = title && typeof title === 'object' ? title : {};
      const legendItem = legend && typeof legend === 'object' ? legend : {};
      const titlePresent = Boolean(titleItem.text);
      const legendPresent = Boolean(Object.keys(legendItem).length);

      if (titlePresent) {
        titleItem.top = titleItem.top ?? 8;
        titleItem.left = titleItem.left ?? 'center';
        titleItem.padding = titleItem.padding ?? [0, 12, 0, 12];
        titleItem.textStyle = titleItem.textStyle && typeof titleItem.textStyle === 'object' ? titleItem.textStyle : {};
        titleItem.textStyle.fontSize = titleItem.textStyle.fontSize || 16;
        titleItem.textStyle.fontWeight = titleItem.textStyle.fontWeight || 600;
      }

      if (legendPresent) {
        legendItem.type = legendItem.type || 'scroll';
        legendItem.itemWidth = legendItem.itemWidth || 14;
        legendItem.itemHeight = legendItem.itemHeight || 10;
        legendItem.textStyle = legendItem.textStyle && typeof legendItem.textStyle === 'object' ? legendItem.textStyle : {};
        legendItem.textStyle.fontSize = legendItem.textStyle.fontSize || 11;
        if (legendItem.orient === 'vertical') {
          legendItem.right = legendItem.right ?? 8;
          legendItem.top = legendItem.top ?? 'middle';
          legendItem.height = legendItem.height ?? '70%';
        } else {
          legendItem.left = legendItem.left ?? 'center';
          legendItem.top = legendItem.top ?? (titlePresent ? 36 : 8);
        }
      }

      if (Array.isArray(cloned.title)) cloned.title[0] = titleItem;
      else if (titlePresent) cloned.title = titleItem;

      if (Array.isArray(cloned.legend)) cloned.legend[0] = legendItem;
      else if (legendPresent) cloned.legend = legendItem;

      const currentGrid = Array.isArray(cloned.grid) ? cloned.grid[0] : cloned.grid;
      const gridItem = currentGrid && typeof currentGrid === 'object' ? currentGrid : {};
      let topOffset = 56;
      if (titlePresent && legendPresent && legendItem.orient !== 'vertical') topOffset = 96;
      else if (titlePresent) topOffset = 64;
      else if (legendPresent && legendItem.orient !== 'vertical') topOffset = 72;
      gridItem.top = gridItem.top ?? topOffset;
      gridItem.left = gridItem.left ?? 56;
      gridItem.right = gridItem.right ?? (legendItem.orient === 'vertical' ? 148 : 24);
      gridItem.bottom = gridItem.bottom ?? 64;
      gridItem.containLabel = gridItem.containLabel ?? true;
      if (Array.isArray(cloned.grid)) cloned.grid[0] = gridItem;
      else cloned.grid = gridItem;

      ['xAxis', 'yAxis'].forEach((axisKey) => {
        const axes = Array.isArray(cloned[axisKey]) ? cloned[axisKey] : (cloned[axisKey] ? [cloned[axisKey]] : []);
        axes.forEach((axis) => {
          if (!axis || typeof axis !== 'object') return;
          axis.axisLabel = axis.axisLabel && typeof axis.axisLabel === 'object' ? axis.axisLabel : {};
          axis.axisLabel.hideOverlap = true;
          axis.axisLabel.fontSize = axis.axisLabel.fontSize || 11;
          axis.axisLabel.margin = axis.axisLabel.margin || 10;
          axis.axisLabel.overflow = axis.axisLabel.overflow || 'truncate';
          axis.animation = false;
          axis.animationDuration = 0;
          axis.animationDurationUpdate = 0;
          axis.animationDelay = 0;
          axis.animationDelayUpdate = 0;

          if (axis.type === 'value' || axis.type === 'log') {
            axis.axisLabel.formatter = (value) => formatChartNumber(value);
          }
        });
      });

      if (Array.isArray(cloned.series)) {
        cloned.series.forEach((series) => {
          if (!series || typeof series !== 'object') return;
          series.animation = false;
          series.label = series.label && typeof series.label === 'object' ? series.label : {};
          series.label.fontSize = series.label.fontSize || 11;
          series.label.overflow = series.label.overflow || 'truncate';
          series.labelLayout = series.labelLayout && typeof series.labelLayout === 'object' ? series.labelLayout : {};
          series.labelLayout.hideOverlap = true;

          if (series.type === 'line' && series.lineStyle && typeof series.lineStyle === 'object' && series.lineStyle.show === false) {
            delete series.lineStyle.show;
            if (Object.keys(series.lineStyle).length === 0) {
              delete series.lineStyle;
            }
          }

          if (series.label.show && typeof series.label.formatter !== 'function') {
            series.label.formatter = (params) => {
              const rawValue = Array.isArray(params?.value) ? params.value[params.value.length - 1] : params?.value;
              return formatChartNumber(rawValue);
            };
          }

          if (series.type === 'pie' && series.label.show !== false) {
            series.label.formatter = (params) => {
              return `${params?.name || ''}\\n${formatChartNumber(params?.value)} (${(params?.percent ?? 0).toFixed(2)}%)`;
            };
          }
        });
      }

      cloned.tooltip = cloned.tooltip && typeof cloned.tooltip === 'object' ? cloned.tooltip : {};
      if (typeof cloned.tooltip.formatter !== 'function') {
        cloned.tooltip.formatter = (params) => {
          const rows = Array.isArray(params) ? params : [params];
          return rows
            .filter(Boolean)
            .map((item) => {
              const marker = item.marker || '';
              const seriesName = item.seriesName || item.name || '';
              const rawValue = Array.isArray(item.value) ? item.value[item.value.length - 1] : item.value;
              return `${marker}${seriesName}: ${formatChartNumber(rawValue)}`;
            })
            .join('<br/>');
        };
      }

      return cloned;
    }

    function estimateChartHeight(option) {
      let height = 360;
      const title = Array.isArray(option.title) ? option.title[0] : option.title;
      const legend = Array.isArray(option.legend) ? option.legend[0] : option.legend;
      const xAxis = Array.isArray(option.xAxis) ? option.xAxis[0] : option.xAxis;
      const yAxis = Array.isArray(option.yAxis) ? option.yAxis[0] : option.yAxis;
      const dataZoomList = Array.isArray(option.dataZoom) ? option.dataZoom : [];
      const seriesList = Array.isArray(option.series) ? option.series : [];

      if (title?.text) height += 24;

      const legendData = Array.isArray(legend?.data) ? legend.data : [];
      if (legend) {
        if (legend?.orient === 'vertical') height += 24;
        else height += legendData.length > 6 ? 56 : 32;
      }

      const xCategories = Array.isArray(xAxis?.data) ? xAxis.data : [];
      const xMaxLabelLength = xCategories.reduce((max, item) => Math.max(max, String(item ?? '').length), 0);
      if (xCategories.length > 8 || xMaxLabelLength > 8) height += 48;
      if (dataZoomList.some((item) => item && item.xAxisIndex !== undefined)) height += 36;

      const yCategories = Array.isArray(yAxis?.data) ? yAxis.data : [];
      if (yAxis?.type === 'category' && yCategories.length > 6) height = Math.max(height, 420);
      if (dataZoomList.some((item) => item && item.yAxisIndex !== undefined)) height = Math.max(height, 420);

      if (seriesList.some((series) => series?.type === 'pie')) {
        const pieSlices = Math.max(0, ...seriesList.filter((series) => series?.type === 'pie').map((series) => Array.isArray(series.data) ? series.data.length : 0));
        height = Math.max(height, pieSlices > 5 ? 460 : 400);
      }

      return Math.min(Math.max(height, 320), 720);
    }

    function applyMatrix(point, matrix) {
      if (!Array.isArray(matrix) || matrix.length < 6) {
        return { x: point[0], y: point[1] };
      }
      return {
        x: matrix[0] * point[0] + matrix[2] * point[1] + matrix[4],
        y: matrix[1] * point[0] + matrix[3] * point[1] + matrix[5]
      };
    }

    function rectToGlobal(item) {
      if (!item || typeof item.getBoundingRect !== 'function') return null;
      const rect = item.getBoundingRect();
      if (!rect) return null;
      const matrix = typeof item.getComputedTransform === 'function' ? item.getComputedTransform() : item.transform;
      const corners = [
        [rect.x, rect.y],
        [rect.x + rect.width, rect.y],
        [rect.x, rect.y + rect.height],
        [rect.x + rect.width, rect.y + rect.height]
      ].map((point) => applyMatrix(point, matrix));
      const xs = corners.map((point) => point.x);
      const ys = corners.map((point) => point.y);
      return {
        left: Math.min(...xs),
        top: Math.min(...ys),
        right: Math.max(...xs),
        bottom: Math.max(...ys),
        width: Math.max(0, Math.max(...xs) - Math.min(...xs)),
        height: Math.max(0, Math.max(...ys) - Math.min(...ys))
      };
    }

    function collectTextBoxes(chart) {
      const displayList = chart?.getZr?.()?.storage?.getDisplayList?.() || [];
      return displayList
        .filter((item) => item && !item.ignore && !item.invisible && item.style && typeof item.style.text === 'string')
        .map((item) => {
          const rect = rectToGlobal(item);
          if (!rect) return null;
          return {
            text: item.style.text.trim(),
            left: rect.left,
            top: rect.top,
            right: rect.right,
            bottom: rect.bottom,
            width: rect.width,
            height: rect.height
          };
        })
        .filter((item) => item && item.text && item.width > 2 && item.height > 2);
    }

    function boxesOverlap(a, b) {
      const horizontal = Math.min(a.right, b.right) - Math.max(a.left, b.left);
      const vertical = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      return horizontal > 1 && vertical > 1;
    }

    function summarizeValidation(chart) {
      const container = document.getElementById('chart');
      const width = container.clientWidth;
      const height = container.clientHeight;
      const boxes = collectTextBoxes(chart).slice(0, 180);

      let overlapCount = 0;
      for (let i = 0; i < boxes.length; i += 1) {
        for (let j = i + 1; j < boxes.length; j += 1) {
          if (boxesOverlap(boxes[i], boxes[j])) {
            overlapCount += 1;
          }
        }
      }

      const overflowCount = boxes.filter((box) => (
        box.left < -2 ||
        box.top < -2 ||
        box.right > width + 2 ||
        box.bottom > height + 2
      )).length;

      const issues = [];
      if (overlapCount > 3) {
        issues.push({
          code: 'browser_text_overlap',
          message: `rendered text overlaps detected: ${overlapCount}`,
          severity: 'warning'
        });
      }
      if (overflowCount > 0) {
        issues.push({
          code: 'browser_text_overflow',
          message: `rendered text overflow detected: ${overflowCount}`,
          severity: 'warning'
        });
      }

      return {
        passed: issues.length === 0,
        issues,
        metrics: {
          container_width: width,
          container_height: height,
          text_box_count: boxes.length,
          overlap_count: overlapCount,
          overflow_count: overflowCount
        }
      };
    }

    const option = __OPTION_JSON__;
    const enhancedOption = enhanceOption(option);
    const chartContainer = document.getElementById('chart');
    chartContainer.style.height = `${estimateChartHeight(enhancedOption)}px`;
    const chart = echarts.init(chartContainer, null, { renderer: 'canvas' });
    chart.setOption(enhancedOption, true);
    setTimeout(() => {
      window.__chartValidationResult = summarizeValidation(chart);
    }, 80);
  </script>
</body>
</html>
"""

    return (
        template
        .replace("__ECHARTS_SCRIPT__", echarts_script)
        .replace("__OPTION_JSON__", option_json)
    )


def _load_local_echarts_script() -> str:
    if not LOCAL_ECHARTS_PATH.exists():
        return ""
    try:
        return LOCAL_ECHARTS_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to read local echarts asset: path=%s error=%s", LOCAL_ECHARTS_PATH, exc)
        return ""
